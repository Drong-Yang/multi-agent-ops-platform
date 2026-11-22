"""Authenticated fusion API and a separately authenticated RTC protocol edge."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.dependencies.auth import get_current_user
from app.graph.quality import ContentBrief
from app.fusion.repository import RepositoryError, Conflict, Invalid, NotFound
from app.fusion.runtime import public_task, public_asset, mode
from app.fusion.privacy import sanitize
from app.fusion.faq import select_faq_assets, describe_asset, hydrate_asset_topic
from app.fusion.legacy_rag.commerce import direct_answer,retrieval_query
from app.fusion.legacy_rag.llm_service import LLMService
from app.fusion.catalog_answers import catalog_quote
from app.fusion.knowledge_access import KnowledgeUnavailable, active_snapshot_id
from app.fusion.support_wiki import WikiDraftInput, WikiReview, candidate_question, review_wiki, select_page
from app.fusion.wiki_drafts import WikiDraftUnavailable, create_wiki_draft

router=APIRouter(prefix='/api/v1',tags=['Fusion'])
callbacks=APIRouter(prefix='/callbacks/rtc',tags=['RTC callbacks'])


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')


class ConsultationInput(Input):
    channel: Literal['text','voice']='text'


class MessageInput(Input):
    message: str=Field(min_length=1,max_length=3000)


class InsightInput(Input):
    session_id: str
    turn_id: str
    summary: str=Field(min_length=1,max_length=2000)
    confirmed: bool


class TaskInput(Input):
    insight_id: str|None=None
    topic_direction: str=Field(default='',max_length=200)
    content_type: Literal['faq','article','wiki']='faq'
    product_scope: list[str]=Field(default_factory=list,max_length=5)
    content_brief: ContentBrief=Field(default_factory=lambda:ContentBrief(min_chars=50,max_chars=1500))


class ActionInput(Input):
    action: Literal['select_topic','reject','approve_text','retry','generate_images']
    data: dict=Field(default_factory=dict)
    expected_state_version: int=Field(ge=1)


class VersionInput(Input):
    expected_state_version: int=Field(ge=1)


def runtime(request):return request.app.state.fusion


def key_required(key):
    if not key or not key.strip() or len(key)>200:
        raise HTTPException(422,'需要1～200字符的 Idempotency-Key')
    return key


def sse(kind,payload,**meta):
    return 'data: '+json.dumps({'schema_version':'1.0','event_id':uuid.uuid4().hex,
                              'type':kind,'payload':payload,**meta},ensure_ascii=False)+'\n\n'


async def active_id(rt):
    try:
        return await active_snapshot_id(rt)
    except KnowledgeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


async def prepare_answer(rt,session,message):
    snapshot_id = await active_id(rt)
    owner = session['owner_id']
    previous = session['payload'].get('turns', [])[-2:]
    available = await rt.db('valid_faq', owner)
    valid_ids = {a['id'] for a in available if a['payload'].get('knowledge_snapshot_id') == snapshot_id}
    pages = await rt.db('valid_wiki', owner) if candidate_question(message) or any(t.get('wiki_asset_id') for t in previous) else []
    wiki_ids = {page['id'] for page in pages if page['payload']['knowledge_snapshot_id'] == snapshot_id}
    history = []
    for turn in previous:
        history.append({'role':'user', 'content':turn['question']})
        if (turn.get('knowledge_snapshot_id') == snapshot_id and set(turn.get('faq_asset_ids', [])).issubset(valid_ids)
                and (not turn.get('wiki_asset_id') or turn['wiki_asset_id'] in wiki_ids)):
            history.append({'role':'assistant', 'content':turn['answer']})
    history.append({'role':'user', 'content':sanitize(message)})
    query = retrieval_query(history)
    voice = session['payload'].get('channel') == 'voice'
    fixed = direct_answer(message)
    wiki = select_page(pages, message, snapshot_id)
    if fixed:
        result = {'sources':[], 'context':'', 'knowledge_snapshot_id':snapshot_id}
        route = 'business_boundary'
    elif wiki:
        payload = wiki['payload']
        fixed = payload['wiki']['answer']
        result = {'sources':payload['evidence_refs'], 'context':'', 'knowledge_snapshot_id':snapshot_id,
                  'wiki_asset_id':wiki['id']}
        route = 'support_wiki'
    else:
        result = await asyncio.wait_for(rt.knowledge.retrieve(query, snapshot_id=snapshot_id), timeout=5)
        quote = catalog_quote(message, result, voice=voice)
        if quote is not None:
            fixed, result['sources'] = quote
            result['context'] = ''
            route = 'catalog_quote' if result['sources'] else 'no_evidence'
        elif not result['sources']:
            fixed = '目前没有这项信息，我无法确认。请补充准确的商品型号，或通过购买平台联系人工客服核实。'
            route = 'no_evidence'
        else:
            route = 'rag'
    selected = []
    if route == 'rag':
        selected, trace = await select_faq_assets(rt, owner, message, query, result, candidates=available)
    else:
        labels = {'business_boundary':'本轮使用固定业务答复。', 'support_wiki':'本轮直接复用已审核的售后 Wiki。',
                  'catalog_quote':'本轮引用商品原文。', 'no_evidence':'原始资料不足或没有匹配的商品参数。'}
        trace = {'status':route, 'message':labels[route], 'selected_assets':[], 'candidates':[]}
    result.update(faq_reuse=trace, answer_route=route, answer_model_skipped=bool(fixed))
    if fixed:
        result['direct_text'] = fixed
    context = result['context']
    if selected:
        context += '\n以下为已审FAQ表达素材，不能新增或覆盖上方基础事实：\n' + '\n'.join(
            a['payload']['content'] for a in selected)
    return history,result,selected,context


async def answer_chunks(rt,session,message):
    history,result,selected,context=await prepare_answer(rt,session,message)
    fixed = result.pop('direct_text', None)
    yield {'kind':'evidence','result':result,'faq_asset_ids':[a['id'] for a in selected]}
    answer = fixed or ''
    if fixed:
        yield {'kind':'text', 'text':fixed}
    else:
        llm = LLMService(rt.knowledge.config, rt.knowledge.snapshot(result['knowledge_snapshot_id'])['corpus'])
        try:
            async for chunk in llm.chat_stream(history, context):
                for choice in chunk.get('choices', []):
                    text = choice.get('delta', {}).get('content') or ''
                    if text:
                        answer += text
                        yield {'kind':'text', 'text':text}
        except Exception:
            failure = '服务暂时不可用，请稍后重试；本次未完成回答。'
            yield {'kind':'text', 'text':failure}
            answer += failure
    turn={'turn_id':uuid.uuid4().hex,'question':sanitize(message),'answer':sanitize(answer),
          'sources':result['sources'],'knowledge_snapshot_id':result['knowledge_snapshot_id'],
          'faq_asset_ids':[a['id'] for a in selected],'faq_reuse':result['faq_reuse'],'mode':mode(),
          'answer_route':result['answer_route'],'answer_model_skipped':result['answer_model_skipped'],
          'wiki_asset_id':result.get('wiki_asset_id'), 'wiki_candidate':candidate_question(message)}
    # A concurrent turn can't silently overwrite this one.
    for attempt in range(3):
        current=await rt.db('get',session['id'],session['owner_id'],'consultation')
        try:
            await rt.db('update_cas',current['id'],current['owner_id'],current['state_version'],
                        {**current['payload'],'turns':current['payload'].get('turns',[])+[turn]})
            break
        except Conflict:
            if attempt==2:raise
    yield {'kind':'done','turn':turn}


@router.get('/knowledge/active')
async def knowledge_active(request:Request,user=Depends(get_current_user)):
    rt=runtime(request);sid=await active_id(rt)
    return {'knowledge_snapshot_id':sid,'mode':mode(),'retrieval_mode':rt.knowledge.snapshot(sid)['retrieval_profile']['mode'],
            'snapshot':rt.knowledge.snapshot(sid)}


@router.post('/consultations')
async def create_consultation(body:ConsultationInput,request:Request,user=Depends(get_current_user)):
    record=await runtime(request).db('create','consultation',str(user.id),
                                    {'channel':body.channel,'turns':[],'mode':mode()})
    return {**record['payload'],'session_id':record['id'],'state_version':record['state_version']}


@router.get('/consultations/{session_id}')
async def get_consultation(session_id:str,request:Request,user=Depends(get_current_user)):
    record=await runtime(request).db('get',session_id,str(user.id),'consultation')
    return {**record['payload'],'session_id':record['id'],'state_version':record['state_version']}


@router.get('/consultations')
async def list_consultations(request:Request,user=Depends(get_current_user)):
    records=await runtime(request).db('list','consultation',str(user.id))
    return {'sessions':[{'session_id':r['id'],'channel':r['payload']['channel'],
                         'turn_count':len(r['payload'].get('turns',[])),
                         'last_question':r['payload'].get('turns',[{}])[-1].get('question','') if r['payload'].get('turns') else '',
                         'updated_at':r['updated_at']} for r in reversed(records)]}


@router.post('/consultations/{session_id}/messages')
async def consultation_message(session_id:str,body:MessageInput,request:Request,user=Depends(get_current_user)):
    rt=runtime(request);session=await rt.db('get',session_id,str(user.id),'consultation')
    async def stream():
        try:
            async for item in answer_chunks(rt,session,body.message):
                if item['kind']=='text':yield sse('answer_delta',{'content':item['text']},session_id=session_id)
                elif item['kind']=='evidence':yield sse('evidence',item['result'],session_id=session_id)
                else:yield sse('answer_completed',item['turn'],session_id=session_id)
        except Exception:
            yield sse('run_failed',{'message':'知识检索不可用，本次未切换模拟模式'},session_id=session_id)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@router.post('/insights')
async def create_insight(body:InsightInput,request:Request,user=Depends(get_current_user),
                         idem:str|None=Header(None,alias='Idempotency-Key')):
    if not body.confirmed:raise HTTPException(422,'请先确认要保存的脱敏摘要')
    rt=runtime(request);owner=str(user.id);key_required(idem)
    def operation(tx):
        session=tx.get(body.session_id,owner,'consultation')
        turn=next((t for t in session['payload']['turns'] if t['turn_id']==body.turn_id),None)
        if turn is None:raise NotFound('Consultation turn not found')
        record=tx.create('insight',owner,{'summary':sanitize(body.summary),'confirmed':True,
            'session_id':body.session_id,'turn_id':body.turn_id,'evidence_refs':turn['sources'],
            'knowledge_snapshot_id':turn['knowledge_snapshot_id'],'mode':turn['mode']},source_id=body.session_id)
        return {**record['payload'],'insight_id':record['id']}
    return await rt.db('idempotent',owner,'create_insight',idem,body.model_dump(),operation)


@router.post('/content-tasks',status_code=202)
async def create_task(body:TaskInput,request:Request,user=Depends(get_current_user),
                      idem:str|None=Header(None,alias='Idempotency-Key')):
    rt=runtime(request);owner=str(user.id);key_required(idem)
    replay=await rt.db('lookup_operation',owner,'create_task',idem,body.model_dump())
    if replay is not None:
        rt.wake()
        return replay
    insight=await rt.db('get',body.insight_id,owner,'insight') if body.insight_id else None
    topic=sanitize(body.topic_direction or (insight['payload']['summary'] if insight else ''))[:200]
    if not topic:raise HTTPException(422,'需要已确认的咨询线索或内容方向')
    sid=await active_id(rt)
    if body.content_type == 'wiki':
        if not insight:
            raise Invalid('售后 Wiki 须来自已确认的咨询线索')
        return await rt.db('idempotent', owner, 'create_task', idem, body.model_dump(),
                           lambda tx: create_wiki_task(tx, rt.knowledge, owner, insight, topic, sid))
    try:
        result=await rt.knowledge.retrieve(topic,snapshot_id=sid,product_scope=body.product_scope or None,purpose='writing')
    except Exception as exc:
        raise HTTPException(503,'知识检索不可用，暂未创建内容任务') from exc
    if not result['sources']:raise HTTPException(422,'没有可用于创作的商品证据，请补充准确型号或资料')
    bundle=rt.knowledge.fact_bundle(sid,result['sources'])
    scope=body.product_scope or bundle.get('supported_skus',[])
    def operation(tx):
        task_id=uuid.uuid4().hex;run_id=uuid.uuid4().hex
        source={'insight_id':body.insight_id or '',
                'session_id':insight['payload']['session_id'] if insight else '',
                'turn_id':insight['payload']['turn_id'] if insight else ''}
        task=tx.create('task',owner,{'run_id':run_id,'thread_id':'fusion_'+owner+'_'+task_id,
            'status':'queued','content_type':body.content_type,'topic_direction':topic,
            'content_brief':body.content_brief.model_dump(),'product_scope':scope,
            'knowledge_snapshot_id':sid,'evidence_refs':result['sources'],'fact_bundle':bundle,
            'source':source,'values':{},'interrupt_info':None,'mode':mode(),'image_status':'not_requested'},
            record_id=task_id,source_id=body.insight_id)
        tx.create('run',owner,{'task_id':task_id,'action':'start','status':'queued'},record_id=run_id,source_id=task_id)
        return public_task(task)
    value=await rt.db('idempotent',owner,'create_task',idem,body.model_dump(),operation)
    rt.wake();return value


def create_wiki_task(tx, knowledge, owner, insight, topic, sid):
    origin = insight['payload']
    session = tx.get(origin['session_id'], owner, 'consultation')
    turn = next((item for item in session['payload']['turns'] if item['turn_id'] == origin['turn_id']), None)
    if not turn or not candidate_question(turn['question']):
        raise Invalid('请选择问题明确、可独立说明的售后咨询轮次')
    evidence = []
    if origin['knowledge_snapshot_id'] == sid:
        refs = [item for item in origin['evidence_refs'] if item.get('kind') != 'wiki_evidence']
        evidence = knowledge.fact_bundle(sid, refs)['evidence']
    task = tx.create('task', owner, {
        'content_type':'wiki', 'status':'waiting_human', 'topic_direction':topic, 'knowledge_snapshot_id':sid,
        'source':{'insight_id':insight['id'], 'session_id':origin['session_id'], 'turn_id':origin['turn_id']},
        'source_question':turn['question'], 'source_answer':turn['answer'][:800], 'source_answer_route':turn.get('answer_route'),
        'evidence_refs':evidence, 'values':{},
    }, source_id=insight['id'])
    return public_task(task)


@router.post('/content-tasks/{task_id}/wiki-draft')
async def draft_wiki(task_id: str, body: WikiDraftInput, request: Request, user=Depends(get_current_user)):
    try:
        return await create_wiki_draft(runtime(request), str(user.id), task_id, body)
    except WikiDraftUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post('/content-tasks/{task_id}/wiki-review')
async def approve_wiki(task_id: str, body: WikiReview, request: Request, user=Depends(get_current_user),
                       idem: str | None = Header(None, alias='Idempotency-Key')):
    rt = runtime(request)
    owner = str(user.id)
    key_required(idem)
    def operation(tx):
        task, asset = review_wiki(tx, rt.knowledge, task_id, owner, body)
        return {'task':public_task(task), 'asset':public_asset(asset)}
    return await rt.db('idempotent', owner, 'wiki_review:' + task_id, idem, body.model_dump(mode='json'), operation)


@router.get('/content-tasks')
async def list_tasks(request:Request,user=Depends(get_current_user)):
    return {'tasks':[public_task(r) for r in reversed(await runtime(request).db('list','task',str(user.id)))]}


@router.get('/content-tasks/{task_id}')
async def get_task(task_id:str,request:Request,user=Depends(get_current_user)):
    return public_task(await runtime(request).db('get',task_id,str(user.id),'task'))


@router.post('/content-tasks/{task_id}/actions',status_code=202)
async def task_action(task_id:str,body:ActionInput,request:Request,user=Depends(get_current_user),
                      idem:str|None=Header(None,alias='Idempotency-Key')):
    rt=runtime(request);owner=str(user.id);key_required(idem)
    def operation(tx):
        task=tx.get(task_id,owner,'task');payload=task['payload'];values=payload.get('values',{})
        if payload.get('content_type') == 'wiki':raise Invalid('请在售后 Wiki 核对表单中审核或修订')
        if task['state_version']!=body.expected_state_version:raise Conflict('任务已变化，请刷新后重试')
        if payload['status'] in ('queued','running'):raise Conflict('任务正在执行，请勿重复推进')
        info=payload.get('interrupt_info') or {};resume={}
        if body.action=='select_topic':
            topic=body.data.get('selected_topic')
            if info.get('action_required')!='select_topic':raise Conflict('当前不是选题阶段')
            if topic not in values.get('generated_topics',[]):raise Invalid('选题不在候选列表中')
            resume={'selected_topic':topic}
        elif body.action in ('reject','approve_text'):
            if info.get('action_required')!='review':raise Conflict('当前不是人工审稿阶段')
            if body.action=='approve_text':
                active=tx.get_active_snapshot()
                if not active or active['id']!=payload['knowledge_snapshot_id']:raise Conflict('知识已更新，请以新版本重新创建任务并审核')
                if values.get('fact_check_report',{}).get('passed') is not True:raise Conflict('关键事实检查未通过，暂不能批准')
                resume={'action':'approve'}
            else:
                feedback=sanitize(body.data.get('feedback',''))
                if not feedback or len(feedback)>2000:raise Invalid('修改意见须为1～2000字符')
                resume={'action':'reject','feedback':feedback}
        elif body.action=='retry':
            if payload['status'] not in ('failed','interrupted'):raise Conflict('任务不处于可重试状态')
        elif body.action=='generate_images':
            if payload['status']!='text_approved':raise Conflict('请先批准文本再生成配图')
        run_id=uuid.uuid4().hex
        tx.create('run',owner,{'task_id':task_id,'action':body.action,'resume':resume,'status':'queued',
                              'previous_run_id':payload.get('run_id')},record_id=run_id,source_id=task_id)
        updated=tx.update_cas(task_id,owner,body.expected_state_version,
                              {**payload,'run_id':run_id,'status':'queued','error':''})
        return public_task(updated)
    value=await rt.db('idempotent',owner,'task_action:'+task_id,idem,body.model_dump(),operation)
    rt.wake();return value


@router.get('/content-tasks/{task_id}/events')
async def task_events(task_id:str,request:Request,user=Depends(get_current_user)):
    rt=runtime(request);owner=str(user.id)
    await rt.db('get',task_id,owner,'task')
    async def stream():
        async with rt.subscribe(task_id) as queue:
            task=public_task(await rt.db('get',task_id,owner,'task'))
            yield sse('state_snapshot',task,task_id=task_id,run_id=task['run_id'],state_version=task['state_version'])
            while task['status'] in ('queued','running'):
                if await request.is_disconnected():return
                try:
                    event=await asyncio.wait_for(queue.get(),timeout=1)
                    yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                except asyncio.TimeoutError:
                    yield ': heartbeat\n\n'
                task=public_task(await rt.db('get',task_id,owner,'task'))
            yield sse('state_snapshot',task,task_id=task_id,run_id=task['run_id'],state_version=task['state_version'])
            yield sse('run_completed',{'status':task['status']},task_id=task_id,run_id=task['run_id'])
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache'})


@router.get('/assets')
async def list_assets(request:Request,user=Depends(get_current_user)):
    rt=runtime(request);owner=str(user.id);sid=await active_id(rt)
    eligible={a['id'] for a in await rt.db('valid_faq',owner)}
    wiki_ids = {a['id'] for a in await rt.db('valid_wiki', owner)}
    assets=[]
    for record in reversed(await rt.db('list','asset',owner)):
        record=await hydrate_asset_topic(rt,record,owner)
        if record['payload'].get('content_type') == 'wiki':
            assets.append({**public_asset(record), 'wiki_eligible':record['id'] in wiki_ids})
        else:
            assets.append({**public_asset(record),'reuse':describe_asset(record,sid,eligible=record['id'] in eligible)})
    return {'assets':assets}


async def asset_operation(request,user,asset_id,body,idem,action):
    key_required(idem);rt=runtime(request);owner=str(user.id)
    def operation(tx):
        return public_asset(getattr(tx,action)(asset_id,owner,body.expected_state_version))
    return await rt.db('idempotent',owner,action+':'+asset_id,idem,body.model_dump(),operation)


@router.post('/assets/{asset_id}/publish')
async def publish_asset(asset_id:str,body:VersionInput,request:Request,user=Depends(get_current_user),
                        idem:str|None=Header(None,alias='Idempotency-Key')):
    return await asset_operation(request,user,asset_id,body,idem,'publish')


@router.post('/assets/{asset_id}/revoke')
async def revoke_asset(asset_id:str,body:VersionInput,request:Request,user=Depends(get_current_user),
                       idem:str|None=Header(None,alias='Idempotency-Key')):
    return await asset_operation(request,user,asset_id,body,idem,'revoke')


def callback_secret():
    value=os.getenv('FUSION_CALLBACK_SECRET','')
    if len(value)<24:raise HTTPException(503,'回调凭据未配置')
    return value


async def bound_voice(request,handle,authorization):
    expected='Bearer '+callback_secret()
    if not authorization or not hmac.compare_digest(authorization,expected):raise HTTPException(401,'回调鉴权失败')
    digest=hashlib.sha256(handle.encode()).hexdigest()
    # Internal-only lookup; callbacks never accept an operator/user ID from the body.
    for record in await runtime(request).db('scan','voice'):
        if hmac.compare_digest(record['payload']['handle_sha256'],digest):
            if record['payload']['expires_at']<time.time():raise HTTPException(403,'语音会话已过期')
            return record
    raise HTTPException(404,'语音会话不存在')


@router.post('/voice/sessions')
async def voice_session(body:ConsultationInput,request:Request,user=Depends(get_current_user)):
    rt=runtime(request);owner=str(user.id);handle=secrets.token_urlsafe(32)
    session=await rt.db('create','consultation',owner,{'channel':'voice','turns':[],'mode':mode()})
    voice=await rt.db('create','voice',owner,{'handle_sha256':hashlib.sha256(handle.encode()).hexdigest(),
        'session_id':session['id'],'expires_at':time.time()+3600,'status':'created'},source_id=session['id'])
    return {'voice_session_id':voice['id'],'session_id':session['id'],'callback_handle':handle,
            'callback_path':'/callbacks/rtc/llm/'+handle,'mode':mode(),'rtc_ready':False}


@callbacks.post('/llm')
async def unbound_callback(request:Request,authorization:str|None=Header(None)):
    if not authorization or not hmac.compare_digest(authorization,'Bearer '+callback_secret()):raise HTTPException(401,'回调鉴权失败')
    raise HTTPException(403,'需要已绑定的专用回调地址')


@callbacks.post('/llm/{handle}')
async def voice_callback(handle:str,request:Request,authorization:str|None=Header(None)):
    voice=await bound_voice(request,handle,authorization)
    body=await request.json();messages=body.get('messages',[])
    user_messages=[m for m in messages if isinstance(m,dict) and m.get('role')=='user' and isinstance(m.get('content'),str)]
    if not user_messages or len(user_messages[-1]['content'])>3000:raise HTTPException(422,'无有效用户消息')
    rt=runtime(request);session=await rt.db('get',voice['payload']['session_id'],voice['owner_id'],'consultation')
    async def stream():
        yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'role':'assistant'}}]})+'\n\n'
        try:
            async for item in answer_chunks(rt,session,user_messages[-1]['content']):
                if item['kind']=='text':
                    yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'content':item['text']}}]},ensure_ascii=False)+'\n\n'
        except Exception:
            yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'content':'资料暂时不可用，请稍后再试。'}}]},ensure_ascii=False)+'\n\n'
        yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})+'\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache'})
