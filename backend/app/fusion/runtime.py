"""Persistent single-process worker; clients observe rather than own graph runs."""
import asyncio
import contextlib
import os
import uuid
from copy import deepcopy

from langgraph.types import Command
from app.fusion.repository import Repository, NotFound, Conflict, content_hash
from app.fusion.workflow import build_fusion_graph, initial_fusion_state
from app.graph.utils import get_checkpointer
from app.api.v1.workflow import extract_interrupt_info
from app.fusion.intents import intent_tags


def mode():
    return 'mock' if os.getenv('MOCK_MODE','false').lower() == 'true' else 'live'


def public_task(record):
    return {**record['payload'],'task_id':record['id'],'state_version':record['state_version'],
            'created_at':record['created_at'],'updated_at':record['updated_at']}


def public_asset(record):
    payload = record['payload']
    page = payload.get('wiki')
    if payload.get('content_type') == 'wiki' and isinstance(page, dict) and isinstance(page.get('answer'), str):
        # Present the approved reply for both current and legacy stored assets.
        answer = page['answer']
        payload = {**payload, 'content':answer, 'content_sha256':content_hash(answer)}
    return {**payload,'asset_id':record['id'],'state_version':record['state_version'],
            'created_at':record['created_at']}


class FusionRuntime:
    def __init__(self, repository, knowledge):
        self.repo=repository
        self.knowledge=knowledge
        self.graph=None
        self._worker=None
        self._wake=asyncio.Event()
        self._subscribers={}
        self._orphaned_runs={}
        self.last_worker_error=None

    async def db(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.repo,method),*args,**kwargs)

    async def start(self, requested_retrieval=''):
        from app.fusion.startup import activate_requested_retrieval, retry_milvus_startup

        await self.db('initialize')
        active=await self.db('get_active_snapshot')
        if active:
            self.knowledge.restore_snapshot(active['payload'])
        if requested_retrieval:
            await retry_milvus_startup(activate_requested_retrieval, self, requested_retrieval, active['id'] if active else None)
            active=await self.db('get_active_snapshot')
        snapshot_id=active['id'] if active else self.knowledge.active_snapshot_id
        readiness=await retry_milvus_startup(self.knowledge.ensure_ready, snapshot_id)
        if not readiness['ready']:
            raise RuntimeError('知识快照未验证就绪，拒绝启动为可用知识服务')
        self.knowledge.persist_snapshot(snapshot_id)
        try:
            await self.db('get',snapshot_id,'system')
        except NotFound:
            snap=self.knowledge.snapshot(snapshot_id)
            await self.db('create','knowledge_snapshot','system',
                          {**snap,'status':'ready'},record_id=snapshot_id)
        if active is None:
            await self.db('activate_snapshot',snapshot_id)
        else:
            self.knowledge.activate(active['id'])
        self.graph=build_fusion_graph(await get_checkpointer())
        # A crash never automatically repeats an uncertain external generation.
        for run in await self.db('scan','run'):
            if run['payload'].get('status') == 'running':
                payload={**run['payload'],'status':'interrupted','error':'服务已重启，请核对后显式重试'}
                await self.db('update_cas',run['id'],run['owner_id'],run['state_version'],payload)
                task=await self.db('get',payload['task_id'],run['owner_id'],'task')
                if task['payload'].get('run_id')==run['id']:
                    await self.db('update_cas',task['id'],task['owner_id'],task['state_version'],
                                  {**task['payload'],'status':'interrupted','error':payload['error']})
        self._worker=asyncio.create_task(self._loop(),name='fusion-persistent-worker')
        self._wake.set()

    async def close(self):
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker=None
        await self.db('close')

    def wake(self):
        self._wake.set()

    def emit(self, task_id, event_type, payload, **meta):
        event={'event_id':uuid.uuid4().hex,'schema_version':'1.0','task_id':task_id,
               'type':event_type,'payload':payload,**meta}
        for queue in self._subscribers.get(task_id,set()):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):queue.get_nowait()
            queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self,task_id):
        queue=asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(task_id,set()).add(queue)
        try:yield queue
        finally:
            self._subscribers[task_id].discard(queue)
            if not self._subscribers[task_id]:self._subscribers.pop(task_id,None)

    async def _loop(self):
        while True:
            self._wake.clear()
            try:
                for identity,orphan in list(self._orphaned_runs.items()):
                    if await self._interrupt_orphan(orphan):self._orphaned_runs.pop(identity,None)
                queued=[r for r in await self.db('scan','run') if r['payload'].get('status')=='queued']
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_worker_error=type(exc).__name__
                await asyncio.sleep(.25)
                continue
            for run in queued:
                try:
                    claimed=await self.db('update_cas',run['id'],run['owner_id'],run['state_version'],
                                          {**run['payload'],'status':'running'})
                except Conflict:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_worker_error=type(exc).__name__
                    break
                try:
                    await self._execute(claimed)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # One broken record/short DB outage must not kill every later job.
                    # The claimed run may have made an external call: require review,
                    # never silently enqueue it for automatic replay.
                    self.last_worker_error=type(exc).__name__
                    self._orphaned_runs[claimed['id']]=claimed
                    if await self._interrupt_orphan(claimed):self._orphaned_runs.pop(claimed['id'],None)
            try:
                await asyncio.wait_for(self._wake.wait(),timeout=.25)
            except asyncio.TimeoutError:
                pass

    async def _interrupt_orphan(self,run):
        try:
            current=await self.db('get',run['id'],run['owner_id'],'run')
            try:task=await self.db('get',run['payload']['task_id'],run['owner_id'],'task')
            except NotFound:task=None
            message='执行状态待核对，请显式重试；未自动重放外部调用'
            terminal=bool(task and task['payload'].get('status') in ('text_approved','completed','waiting_human'))
            await self.db('update_cas',current['id'],current['owner_id'],current['state_version'],
                          {**current['payload'],'status':'completed' if terminal else 'interrupted','error':message})
            if task and task['payload'].get('run_id')==run['id'] and not terminal:
                await self.db('update_cas',task['id'],task['owner_id'],task['state_version'],
                              {**task['payload'],'status':'interrupted','error':message})
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _execute(self,run):
        owner=run['owner_id'];task_id=run['payload']['task_id']
        task=await self.db('get',task_id,owner,'task')
        if task['payload'].get('run_id') != run['id']:
            await self.db('update_cas',run['id'],owner,run['state_version'],{**run['payload'],'status':'canceled'})
            return
        task=await self.db('update_cas',task_id,owner,task['state_version'],
                           {**task['payload'],'status':'running','error':''})
        payload=task['payload'];cfg={'configurable':{'thread_id':payload['thread_id']}}
        try:
            if run['payload']['action']=='generate_images':
                from app.graph.nodes.visualizer import extract_visuals_node,generate_images_node
                values=deepcopy(payload['values'])
                values.update(await extract_visuals_node(values))
                values.update(await generate_images_node(values))
                image_status='completed' if values.get('status')=='completed' else 'partial_failed' if values.get('image_urls') else 'failed'
                payload.update(values=values,status='text_approved',image_status=image_status)
                for asset in await self.db('list','asset',owner):
                    if asset['payload'].get('task_id')==task_id and asset['payload'].get('version_id')==payload.get('current_version_id'):
                        await self.db('update_cas',asset['id'],owner,asset['state_version'],
                            {**asset['payload'],'image_status':image_status,'image_urls':values.get('image_urls',[])})
            else:
                if run['payload']['action']=='start':
                    graph_input=initial_fusion_state(task_id=task_id,owner_id=owner,
                        topic=payload['topic_direction'],content_type=payload['content_type'],
                        snapshot_id=payload['knowledge_snapshot_id'],evidence=payload['evidence_refs'],
                        fact_bundle=payload['fact_bundle'],content_brief=payload['content_brief'],
                        insight_id=payload.get('source',{}).get('insight_id',''))
                elif run['payload']['action']=='retry':graph_input=None
                else:graph_input=Command(resume=run['payload']['resume'])
                async for event in self.graph.astream_events(graph_input,cfg,version='v2'):
                    metadata=event.get('metadata',{})
                    if metadata.get('langgraph_node')=='write_draft' and event.get('event')=='on_chat_model_start':
                        self.emit(task_id,'node_status',{'node':'write_draft','status':'running'},run_id=run['id'],
                                  article_version_id=metadata.get('article_version_id',''),state_version=task['state_version'])
                    elif metadata.get('langgraph_node')=='write_draft' and event.get('event')=='on_chat_model_stream':
                        chunk=event.get('data',{}).get('chunk')
                        if getattr(chunk,'content',''):
                            self.emit(task_id,'article_delta',{'content':chunk.content},run_id=run['id'],
                                      article_version_id=metadata.get('article_version_id',''))
                    elif event.get('event') in ('on_chain_start','on_chain_end') and metadata.get('langgraph_node'):
                        self.emit(task_id,'node_status',{'node':metadata['langgraph_node'],
                                                        'phase':event['event']},run_id=run['id'])
                snapshot=await self.graph.aget_state(cfg,subgraphs=True)
                values=dict(snapshot.values)
                info=extract_interrupt_info(snapshot)
                status='waiting_human' if info else 'text_approved' if values.get('status')=='text_approved' else 'completed'
                payload.update(values=values,interrupt_info=info,status=status)
                versions=values.get('article_versions',[])
                if versions:
                    latest=versions[-1]
                    payload.update(current_version_id=latest['version_id'],content_sha256=content_hash(latest['content']))
                    for version in versions:
                        try:await self.db('get',version['version_id'],owner,'version')
                        except NotFound:
                            await self.db('create','version',owner,{**version,'task_id':task_id,
                                'knowledge_snapshot_id':payload['knowledge_snapshot_id']},
                                record_id=version['version_id'],source_id=task_id)
                if status=='text_approved':
                    await self._save_asset(task_id,owner,payload)
            task=await self.db('update_cas',task_id,owner,task['state_version'],payload)
            current_run=await self.db('get',run['id'],owner,'run')
            await self.db('update_cas',run['id'],owner,current_run['state_version'],
                          {**current_run['payload'],'status':'waiting_human' if payload['status']=='waiting_human' else 'completed'})
            self.emit(task_id,'state_snapshot',public_task(task),run_id=run['id'],state_version=task['state_version'])
            self.emit(task_id,'run_completed',{'status':payload['status']},run_id=run['id'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never put provider payloads, connection strings or raw prompts in responses.
            payload.update(status='failed',error=f'执行失败（{type(exc).__name__}），可核对配置后重试')
            try:
                snapshot=await self.graph.aget_state(cfg,subgraphs=True)
                if snapshot.values:payload['values']=dict(snapshot.values)
            except Exception:pass
            current=await self.db('get',task_id,owner,'task')
            task=await self.db('update_cas',task_id,owner,current['state_version'],payload)
            current_run=await self.db('get',run['id'],owner,'run')
            await self.db('update_cas',run['id'],owner,current_run['state_version'],
                          {**current_run['payload'],'status':'failed','error':payload['error']})
            self.emit(task_id,'run_failed',public_task(task),run_id=run['id'])

    async def _save_asset(self,task_id,owner,payload):
        version=payload['values']['article_versions'][-1]
        asset_id=content_hash(task_id+':'+version['version_id'])[:32]
        try:
            await self.db('get',asset_id,owner,'asset')
            return
        except NotFound:pass
        checksum=content_hash(version['content'])
        await self.db('create','asset',owner,{'task_id':task_id,'version_id':version['version_id'],
            'content':version['content'],'content_sha256':checksum,'content_type':payload['content_type'],
            'product_scope':payload['product_scope'],'knowledge_snapshot_id':payload['knowledge_snapshot_id'],
            'topic_direction':payload['topic_direction'],
            'intent_tags':intent_tags(version['content']),
            'evidence_refs':payload['evidence_refs'],'fact_check_report':payload['values']['fact_check_report'],
            'text_review_status':'approved','publication_status':'draft','image_status':'not_requested',
            'approval':{'version_id':version['version_id'],'content_sha256':checksum,
                        'knowledge_snapshot_id':payload['knowledge_snapshot_id'],'owner_id':owner},
            'mode':payload['mode']},record_id=asset_id,source_id=task_id)

    async def activate_snapshot(self,snapshot_id):
        readiness=await self.knowledge.ensure_ready(snapshot_id)
        if not readiness['ready']:raise ValueError('知识未就绪，不能激活')
        snapshot=self.knowledge.snapshot(snapshot_id)
        try:await self.db('get',snapshot_id,'system','knowledge_snapshot')
        except NotFound:await self.db('create','knowledge_snapshot','system',{**snapshot,'status':'ready'},record_id=snapshot_id)
        await self.db('activate_snapshot',snapshot_id)
        self.knowledge.activate(snapshot_id)
