"""Explicit local operations for the manual voice channel, without auto-calling RTC."""
import asyncio


def validate_tunnel_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError('隧道配置必须为对象')
    tunnel = settings.get('tunnels', {}).get('fusion_voice_test', {})
    if (str(settings.get('version')) != '2' or not settings.get('authtoken')
            or settings.get('web_addr') != '127.0.0.1:4041'
            or tunnel != {'proto': 'http', 'addr': '127.0.0.1:8011'}):
        raise ValueError('隧道仅允许固定本机回调网关8011，检查端口仅允许本机4041')
    return {'ready': True, 'validation': 'private_callback_gateway_only'}


def validate_voice_settings(config, database_url, checkpoint_url, callback_secret):
    """Offline preflight. Returned data contains no key, token or connection URL."""
    from sqlalchemy.engine import make_url
    from app.fusion.rtc_control import RtcController
    database, checkpoint = make_url(database_url), make_url(checkpoint_url)
    if not database.database or 'fusion' not in database.database.lower():
        raise ValueError('语音渠道必须使用名称包含 fusion 的独立数据库')
    if database.set(drivername='postgresql') != checkpoint.set(drivername='postgresql'):
        raise ValueError('业务数据库与检查点数据库必须相同')
    config.require('RTC_APP_ID', 'RTC_APP_KEY', 'VOLC_AK', 'VOLC_SK', 'ARK_ENDPOINT_ID', 'ARK_API_KEY')
    voice = RtcController(config).voice_config('https://preflight.invalid/callbacks/rtc/llm/'+'x'*32, callback_secret)
    if not voice.get('ASRConfig') or not voice.get('TTSConfig'):
        raise ValueError('语音配置缺少 ASRConfig 或 TTSConfig')
    return {'ready': True, 'validation': 'offline_configuration_only', 'cloud_tasks_started': 0}


async def retry_milvus_startup(operation, *args):
    from app.fusion.legacy_rag.milvus_service import MilvusResponseError

    # The Milvus health endpoint can precede collection query readiness after a cold start.
    async with asyncio.timeout(25):
        for attempt in range(4):
            try:
                return await operation(*args)
            except MilvusResponseError as error:
                if error.code != 503 or attempt == 3:
                    raise
                await asyncio.sleep(1 + attempt)


async def activate_requested_retrieval(runtime, requested, snapshot_id=None):
    # The persisted pointer remains authoritative unless launch explicitly opts in.
    if not requested:
        return
    if requested not in ('sample', 'milvus'):
        raise ValueError('启动检索模式只支持 sample 或 milvus')
    knowledge = runtime.knowledge
    snapshot = knowledge.snapshot(snapshot_id or knowledge.active_snapshot_id)
    if snapshot['retrieval_profile']['mode'] == requested:
        return
    previous = knowledge.config.KB_MODE
    try:
        knowledge.config.KB_MODE = requested
        identity = knowledge.register_snapshot(snapshot['corpus'])
        # Existing readiness and pointer transaction gates apply; never rebuild/index.
        await runtime.activate_snapshot(identity)
    finally:
        knowledge.config.KB_MODE = previous


async def stop_active_voices(repository, controller):
    """Stop this project's known tasks, including uncertain starts; keep retry state."""
    result = {'stopped': 0, 'failed': 0}
    for record in await asyncio.to_thread(repository.scan, 'voice'):
        payload = record['payload']
        if payload.get('status') not in ('started', 'starting', 'stopping', 'start_failed', 'stop_failed'):
            continue
        if not all(payload.get(key) for key in ('RoomId', 'TaskId', 'UserId')):
            result['failed'] += 1
            continue
        try:
            await controller.stop(payload)
            await asyncio.to_thread(repository.update_cas, record['id'], record['owner_id'],
                                    record['state_version'], {**payload, 'status': 'stopped', 'last_error': ''})
            result['stopped'] += 1
        except Exception:
            result['failed'] += 1
            try:
                await asyncio.to_thread(repository.update_cas, record['id'], record['owner_id'],
                    record['state_version'], {**payload, 'status': 'stop_failed',
                    'last_error': '关闭语音任务未完成，请重试关闭脚本或在RTC控制台核对任务'})
            except Exception:
                pass
    return result
