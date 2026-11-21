"""FAQ selection must be observable and never turn drafts into new facts."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.fusion.faq import select_faq_assets, describe_asset


class FaqReuseTests(unittest.IsolatedAsyncioTestCase):
    def product(self, sku='SC-PB27'):
        return {'chunk_id': sku, 'sku': sku, 'kind': 'product', 'content': sku+' 重量568g，尺寸153×80×33mm。'}

    def rule(self, identity='returns'):
        return {'chunk_id': identity, 'kind': 'rule', 'content': '退货需核对完好、拆封状态，以对应订单和平台公示为准。'}

    def asset(self, identity='faq1', *, scope=None, content='SC-PB27 重量568g。', refs=None):
        return {'id': identity, 'owner_id': 'alice', 'payload': {'content': content, 'content_type': 'faq',
                'publication_status': 'published', 'knowledge_snapshot_id': 'ks1', 'version_id': 'v1',
                'product_scope': ['SC-PB27'] if scope is None else scope, 'intent_tags': [],
                'evidence_refs': [self.product()] if refs is None else refs}}

    def runtime(self, assets):
        async def db(method, *args, **kwargs):
            if method == 'valid_faq':
                self.assertEqual(args[0], 'alice')
                return assets
            if method == 'get':
                return {'payload': {'topic_direction': 'SC-PB27 商品说明'}}
            raise AssertionError(method)
        def bundle(snapshot, refs):
            if any(ref.get('chunk_id') == 'forged' for ref in refs):
                raise ValueError('unverified evidence')
            return {'evidence': refs}
        return SimpleNamespace(db=db, knowledge=SimpleNamespace(fact_bundle=bundle))

    async def select(self, assets, message='SC-PB27 多重？', sources=None, query=None):
        result = {'sources': [self.product()] if sources is None else sources, 'knowledge_snapshot_id': 'ks1'}
        return await select_faq_assets(self.runtime(assets), 'alice', message, query or message, result)

    async def test_existing_published_asset_with_empty_tags_is_selected_from_approved_content(self):
        selected, trace = await self.select([self.asset()])
        self.assertEqual([a['id'] for a in selected], ['faq1'])
        self.assertEqual(trace['status'], 'selected')
        self.assertEqual(trace['selected_assets'][0]['asset_id'], 'faq1')
        self.assertTrue(trace['selected_assets'][0]['match_reason'])

    async def test_general_policy_faq_does_not_require_a_product_sku(self):
        asset = self.asset(scope=[], refs=[self.rule()], content='耳机拆封后能退吗？退货需核对商品完好，以对应订单和平台公示为准。')
        selected, _ = await self.select([asset], message='耳机拆封后能退吗？', sources=[self.rule()])
        self.assertEqual(len(selected), 1)

    async def test_product_scoped_policy_cannot_be_selected_without_matching_product(self):
        asset = self.asset(refs=[self.product(), self.rule()], content='SC-PB27退货需核对完好。')
        selected, trace = await self.select([asset], message='耳机拆封后能退吗？', sources=[self.rule()])
        self.assertEqual(selected, [])
        self.assertEqual(trace['candidates'][0]['code'], 'product_scope_mismatch')

    async def test_followup_dimensions_does_not_match_only_previous_weight_intent(self):
        selected, trace = await self.select([self.asset()], message='那尺寸呢？', query='SC-PB27多重；追问：那尺寸呢？')
        self.assertEqual(selected, [])
        self.assertEqual(trace['candidates'][0]['code'], 'topic_mismatch')

    async def test_no_basis_never_uses_faq_as_new_product_knowledge(self):
        selected, trace = await self.select([self.asset()], sources=[])
        self.assertEqual(selected, [])
        self.assertEqual(trace['status'], 'no_evidence')

    async def test_wrong_product_and_invalid_evidence_cannot_enter_context(self):
        wrong = self.asset('other', scope=['SC-PB20'])
        forged = self.asset('forged', refs=[{'chunk_id': 'forged'}])
        selected, trace = await self.select([wrong, forged])
        self.assertEqual(selected, [])
        self.assertEqual([c['code'] for c in trace['candidates']], ['product_scope_mismatch', 'evidence_invalid'])

    async def test_current_repository_gate_is_used_and_revoked_assets_disappear(self):
        # valid_faq is the authoritative gate; a revoked candidate is never revived.
        selected, trace = await self.select([])
        self.assertEqual(selected, [])
        self.assertEqual(trace['status'], 'no_available')

    async def test_semantically_different_topic_is_not_matched_only_by_shared_sku(self):
        selected, trace = await self.select([self.asset()], message='SC-PB27 保修多久？')
        self.assertEqual(selected, [])

    async def test_powerbank_noun_is_not_a_charging_intent_shared_by_every_question(self):
        asset=self.asset(content='SC-PB27充电宝的重量为568g。')
        selected, _ = await self.select([asset], message='SC-PB27充电宝保修多久？')
        self.assertEqual(selected, [])

    async def test_untagged_source_question_has_conservative_lexical_fallback(self):
        asset = self.asset(content='适合摄影棚固定机位使用，按已有商品资料核对。')
        asset['payload']['source_question'] = 'SC-PB27 适合摄影棚固定机位吗？'
        selected, _ = await self.select([asset], message='SC-PB27 适合摄影棚固定机位吗？')
        self.assertEqual(len(selected), 1)

    def test_asset_description_explains_revoke_and_provides_a_safe_test_question(self):
        asset = self.asset()
        description = describe_asset(asset, 'ks1', eligible=True)
        self.assertTrue(description['eligible'])
        self.assertIn('SC-PB27', description['sample_question'])
        self.assertIn('weight', description['intent_tags'])
        asset['payload']['publication_status'] = 'revoked'
        self.assertFalse(describe_asset(asset, 'ks1', eligible=False)['eligible'])
        self.assertEqual(describe_asset(asset, 'ks1', eligible=False)['status'], 'revoked')

    def test_article_is_never_advertised_as_customer_faq(self):
        asset = self.asset()
        asset['payload']['content_type'] = 'article'
        self.assertEqual(describe_asset(asset, 'ks1', eligible=False)['status'], 'not_faq')

    async def prepare_history(self, *, saved_snapshot='ks1', faq_ids=None, available=None):
        from app.fusion.api import prepare_answer
        async def db(method, *args, **kwargs):
            if method == 'get_active_snapshot':
                return {'id': 'ks1'}
            if method == 'valid_faq':
                return available or []
            raise AssertionError(method)
        rt = self.runtime([])
        rt.db = db
        rt.knowledge.retrieve = AsyncMock(return_value={'sources': [self.product()], 'context': 'current verified facts', 'knowledge_snapshot_id': 'ks1'})
        session = {'owner_id': 'alice', 'payload': {'turns': [{'question': 'SC-PB27多重？',
            'answer': 'OLD_FAQ_PHRASE', 'knowledge_snapshot_id': saved_snapshot, 'faq_asset_ids': faq_ids or []}]}}
        history, _, _, _ = await prepare_answer(rt, session, '那尺寸呢？')
        return history, session

    async def test_revoked_or_changed_faq_answer_cannot_reenter_model_through_history(self):
        history, session = await self.prepare_history(faq_ids=['no-longer-eligible'])
        self.assertFalse(any(m['role'] == 'assistant' for m in history))
        self.assertIn('SC-PB27', history[0]['content'])
        self.assertEqual(session['payload']['turns'][0]['answer'], 'OLD_FAQ_PHRASE')

    async def test_old_knowledge_answer_is_excluded_even_without_faq_ids(self):
        history, _ = await self.prepare_history(saved_snapshot='old-knowledge')
        self.assertFalse(any(m['role'] == 'assistant' for m in history))

    async def test_valid_same_version_history_is_retained(self):
        history, _ = await self.prepare_history(faq_ids=['faq1'], available=[self.asset()])
        self.assertTrue(any(m.get('content') == 'OLD_FAQ_PHRASE' for m in history))
