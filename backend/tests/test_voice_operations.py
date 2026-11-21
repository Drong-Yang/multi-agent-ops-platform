"""Manual voice launch/shutdown contracts; no real cloud services are called."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from pathlib import Path
from app.fusion.startup import activate_requested_retrieval, stop_active_voices, validate_voice_settings, validate_tunnel_settings
from app.fusion.legacy_rag.config import Config


class VoiceOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def startup_switch(self, ready):
        from app.fusion.knowledge import KnowledgeService
        from app.fusion.repository import Repository
        from app.fusion.runtime import FusionRuntime
        directory = Path(__file__).parent / '.repository-test-data'
        directory.mkdir(exist_ok=True)
        file = directory / ('startup-'+uuid.uuid4().hex+'.db')
        repo = Repository('sqlite:///'+str(file))
        repo.initialize()
        knowledge = KnowledgeService(mode='sample')
        old = knowledge.active_snapshot_id
        repo.create('knowledge_snapshot', 'system', knowledge.snapshot(old), record_id=old)
        repo.activate_snapshot(old)
        modes = []
        async def verify(identity):
            mode = knowledge.snapshot(identity)['retrieval_profile']['mode']
            modes.append(mode)
            if mode != 'milvus':
                raise AssertionError('startup tried the obsolete retrieval dependency')
            knowledge._snapshots[identity]['status'] = 'ready' if ready else 'building'
            return {'ready': ready}
        knowledge.ensure_ready = verify
        knowledge.persist_snapshot = lambda identity: None
        rt = FusionRuntime(repo, knowledge)
        try:
            with patch('app.fusion.runtime.get_checkpointer', new=AsyncMock(return_value=None)), \
                 patch('app.fusion.runtime.build_fusion_graph', return_value=object()):
                if ready:
                    await rt.start('milvus')
                    self.assertNotEqual(repo.get_active_snapshot()['id'], old)
                else:
                    with self.assertRaises(ValueError):
                        await rt.start('milvus')
                    self.assertEqual(repo.get_active_snapshot()['id'], old)
            self.assertTrue(modes)
        finally:
            await rt.close()
            file.unlink(missing_ok=True)

    async def test_explicit_start_switch_does_not_probe_obsolete_retrieval_dependency(self):
        await self.startup_switch(True)

    async def test_explicit_start_failure_keeps_saved_knowledge_pointer(self):
        await self.startup_switch(False)

    def test_tunnel_preflight_allows_only_private_callback_gateway(self):
        config = {'version': '2', 'authtoken': 'test-private', 'web_addr': '127.0.0.1:4041',
                  'tunnels': {'fusion_voice_test': {'proto': 'http', 'addr': '127.0.0.1:8011'}}}
        self.assertTrue(validate_tunnel_settings(config)['ready'])
        self.assertNotIn('test-private', repr(validate_tunnel_settings(config)))
        for addr in ('127.0.0.1:8010', '0.0.0.0:8011', '8011'):
            config['tunnels']['fusion_voice_test']['addr'] = addr
            with self.subTest(addr=addr), self.assertRaises(ValueError):
                validate_tunnel_settings(config)

    def test_tunnel_preflight_rejects_public_inspection_or_overriding_parameters(self):
        config = {'version': '2', 'authtoken': 'test-private', 'web_addr': '0.0.0.0:4041',
                  'tunnels': {'fusion_voice_test': {'proto': 'http', 'addr': '127.0.0.1:8011'}}}
        with self.assertRaises(ValueError):
            validate_tunnel_settings(config)
        config['web_addr'] = '127.0.0.1:4041'
        config['tunnels']['fusion_voice_test']['schemes'] = ['http']
        with self.assertRaises(ValueError):
            validate_tunnel_settings(config)

    def config(self):
        config = Config()
        for key in ('RTC_APP_ID', 'RTC_APP_KEY', 'VOLC_AK', 'VOLC_SK', 'ARK_ENDPOINT_ID', 'ARK_API_KEY'):
            setattr(config, key, 'fictional-private-value')
        config.VOICE_CONFIG_FILE = Path(__file__).parents[1] / 'voice_config.example.json'
        return config

    def test_preflight_validates_config_without_creating_cloud_tasks(self):
        report = validate_voice_settings(self.config(), 'postgresql+asyncpg://u:pw@localhost/commerce_fusion',
                                         'postgresql://u:pw@localhost/commerce_fusion', 's'*32)
        self.assertTrue(report['ready'])
        self.assertNotIn('fictional-private-value', repr(report))
        self.assertNotIn('pw', repr(report))

    def test_preflight_rejects_original_or_mismatched_databases_and_missing_keys(self):
        for db, checkpoint in [('aicontent', 'aicontent'), ('commerce_fusion', 'original')]:
            with self.subTest(db=db), self.assertRaises(ValueError):
                validate_voice_settings(self.config(), 'postgresql://u:pw@localhost/'+db,
                                         'postgresql://u:pw@localhost/'+checkpoint, 's'*32)
        config = self.config()
        config.RTC_APP_KEY = ''
        with self.assertRaisesRegex(ValueError, 'RTC_APP_KEY'):
            validate_voice_settings(config, 'postgresql://u:pw@localhost/commerce_fusion',
                                    'postgresql://u:pw@localhost/commerce_fusion', 's'*32)

    def runtime(self):
        knowledge = SimpleNamespace(active_snapshot_id='saved', config=SimpleNamespace(KB_MODE='sample'),
            snapshot=lambda sid: {'corpus': {'chunks': ['same-evidence']}, 'retrieval_profile': {'mode': 'sample'}},
            register_snapshot=lambda corpus: 'verified-milvus')
        return SimpleNamespace(knowledge=knowledge, activate_snapshot=AsyncMock())

    async def test_default_start_preserves_saved_snapshot(self):
        rt = self.runtime()
        await activate_requested_retrieval(rt, '')
        rt.activate_snapshot.assert_not_awaited()

    async def test_explicit_milvus_activates_same_corpus_via_existing_ready_gate(self):
        rt = self.runtime()
        await activate_requested_retrieval(rt, 'milvus')
        rt.activate_snapshot.assert_awaited_once_with('verified-milvus')
        self.assertEqual(rt.knowledge.config.KB_MODE, 'sample')

    async def test_failed_readiness_does_not_bypass_gate_or_mutate_config(self):
        rt = self.runtime()
        rt.activate_snapshot.side_effect = RuntimeError('index unavailable')
        with self.assertRaises(RuntimeError):
            await activate_requested_retrieval(rt, 'milvus')
        self.assertEqual(rt.knowledge.config.KB_MODE, 'sample')

    async def test_invalid_start_mode_is_rejected_before_activation(self):
        rt = self.runtime()
        with self.assertRaises(ValueError):
            await activate_requested_retrieval(rt, 'cloud')
        rt.activate_snapshot.assert_not_awaited()

    async def test_shutdown_stops_only_active_bound_tasks_and_marks_success(self):
        records = [self.record('active', 'started'), self.record('uncertain', 'start_failed'),
                   self.record('never', 'created'), self.record('ended', 'stopped')]
        repo = SimpleNamespace(scan=lambda kind: records, update_cas=lambda *args: None)
        controller = SimpleNamespace(stop=AsyncMock())
        result = await stop_active_voices(repo, controller)
        self.assertEqual(result, {'stopped': 2, 'failed': 0})
        self.assertEqual(controller.stop.await_count, 2)

    async def test_shutdown_continues_after_one_provider_failure_without_leaking_error(self):
        records = [self.record('bad', 'stop_failed'), self.record('good', 'started')]
        updates = []
        repo = SimpleNamespace(scan=lambda kind: records, update_cas=lambda *args: updates.append(args))
        controller = SimpleNamespace(stop=AsyncMock(side_effect=[RuntimeError('secret-provider-value'), None]))
        result = await stop_active_voices(repo, controller)
        self.assertEqual(result, {'stopped': 1, 'failed': 1})
        self.assertEqual(updates[0][-1]['status'], 'stop_failed')
        self.assertNotIn('secret-provider-value', repr(updates))
        self.assertEqual(updates[1][-1]['status'], 'stopped')

    def record(self, identity, status):
        return {'id': identity, 'owner_id': 'test', 'state_version': 1,
                'payload': {'status': status, 'RoomId': 'room_'+identity, 'TaskId': 'task_'+identity, 'UserId': 'user'}}

class MilvusStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_transient_milvus_503_is_retried(self):
        from app.fusion.startup import retry_milvus_startup
        from app.fusion.legacy_rag.milvus_service import MilvusResponseError

        probe = AsyncMock(side_effect=[MilvusResponseError('entities/query', 503), {'ready': True}])
        with patch('app.fusion.startup.asyncio.sleep', new=AsyncMock()) as pause:
            self.assertEqual(await retry_milvus_startup(probe), {'ready': True})
        self.assertEqual(probe.await_count, 2)
        pause.assert_awaited_once()

    async def test_invalid_schema_is_not_retried(self):
        from app.fusion.startup import retry_milvus_startup

        probe = AsyncMock(side_effect=ValueError('incompatible schema'))
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            await retry_milvus_startup(probe)
        probe.assert_awaited_once()

    async def test_persistent_503_stops_after_the_bounded_attempts(self):
        from app.fusion.startup import retry_milvus_startup
        from app.fusion.legacy_rag.milvus_service import MilvusResponseError

        probe = AsyncMock(side_effect=MilvusResponseError('entities/query', 503))
        with patch('app.fusion.startup.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(MilvusResponseError):
                await retry_milvus_startup(probe)
        self.assertEqual(probe.await_count, 4)
