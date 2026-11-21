"""Explicit paid-service smoke: real Milvus/Embedding and real text generation.

This script is not included in unittest discovery. It never labels HTTP callbacks
as real microphone/ASR/TTS acceptance. Credentials stay in ignored local files.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1];BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND));os.chdir(BACKEND)
from dotenv import load_dotenv
load_dotenv(BACKEND/'.env')
for setting in ('LLM_BASE_URL','ARK_BASE_URL','EMBEDDING_BASE_URL','IMAGE_BASE_URL'):
    destination=os.getenv(setting,'https://ark.cn-beijing.volces.com/api/v3')
    if not (urlsplit(destination).hostname or '').endswith('.volces.com'):
        raise SystemExit('Live verification is restricted to the authorized Volcano cloud destination')
os.environ['MOCK_MODE']='false'
os.environ['FUSION_KB_MODE']='sample'  # start the saved baseline before explicit activation below
os.environ['MILVUS_TIMEOUT']='10';os.environ['EMBEDDING_TIMEOUT']='10'
if sys.platform=='win32':asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fastapi.testclient import TestClient
from app.main import app

report={'mode':'live_milvus_embedding_text','knowledge_collection':'commerce_ops_fusion_v1','steps':[]}
out=ROOT/'evals'/'runs'/'live-text-smoke.json'

def record(name,**data):
    report['steps'].append({'name':name,**data})
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(name+': '+('PASS' if data.get('passed',True) else 'FAIL'),flush=True)

def checked(response,status=200):
    if response.status_code!=status:raise RuntimeError(f'Unexpected API status {response.status_code}: {response.text[:600]}')
    return response.json()

def post(client,url,body,headers,status=200):
    return checked(client.post('/api/v1'+url,headers={**headers,'Idempotency-Key':uuid.uuid4().hex},json=body),status)

def wait(client,task_id,headers):
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        state=checked(client.get('/api/v1/content-tasks/'+task_id,headers=headers))
        if state['status'] in ('waiting_human','text_approved'):return state
        if state['status'] in ('failed','interrupted'):raise RuntimeError(state.get('error','task failed'))
        time.sleep(.15)
    raise TimeoutError('live task timeout')

try:
    with TestClient(app) as client:
        rt=app.state.fusion;original=rt.knowledge.active_snapshot_id
        rt.knowledge.config.KB_MODE='milvus'
        sid=rt.knowledge.register_snapshot(rt.knowledge.snapshot(original)['corpus'])
        client.portal.call(rt.activate_snapshot,sid)
        try:
            credentials={'username':'live_'+uuid.uuid4().hex[:10],'password':uuid.uuid4().hex}
            checked(client.post('/api/v1/auth/register',json=credentials))
            token=checked(client.post('/api/v1/auth/login',json=credentials))['access_token']
            headers={'Authorization':'Bearer '+token}
            session=post(client,'/consultations',{},headers)['session_id']
            for question in ('SC-PB27 多重？','那尺寸呢？','耳机拆封后能退吗？','SC-PB999 多重？','帮我查询我的订单到哪了'):
                started=time.perf_counter()
                response=client.post(f'/api/v1/consultations/{session}/messages',headers=headers,json={'message':question})
                if response.status_code!=200:raise RuntimeError('consultation request failed')
                state=checked(client.get('/api/v1/consultations/'+session,headers=headers))
                turn=state['turns'][-1]
                if turn['question']!=question:raise RuntimeError('consultation did not persist the current turn')
                passed=bool(turn['answer']) and '[模拟' not in turn['answer'] and '服务暂时不可用' not in turn['answer']
                if question=='SC-PB27 多重？':passed=passed and '568' in turn['answer']
                if question.startswith('SC-PB999'):passed=passed and '568' not in turn['answer']
                record('consultation',question=question,answer=turn['answer'],sources=turn['sources'],
                       snapshot_id=turn['knowledge_snapshot_id'],elapsed_seconds=round(time.perf_counter()-started,3),passed=passed)
                if not passed:raise RuntimeError('live consultation did not meet the scenario expectation')
            first=state['turns'][0]
            insight=post(client,'/insights',{'session_id':session,'turn_id':first['turn_id'],
                'summary':'SC-PB27 多重？请制作简短的重量与尺寸FAQ，不涉及价格、登机或额外性能承诺。','confirmed':True},headers)
            task=post(client,'/content-tasks',{'insight_id':insight['insight_id'],'content_type':'faq',
                'product_scope':['SC-PB27'],'content_brief':{'audience':'选购者','min_chars':80,'max_chars':450,
                    'required_sections':['重量','尺寸']}},headers,202)
            task=wait(client,task['task_id'],headers)
            record('evidence_topic',task_id=task['task_id'],snapshot_id=task['knowledge_snapshot_id'],
                   topics=task['values']['generated_topics'])
            post(client,f"/content-tasks/{task['task_id']}/actions",{'action':'select_topic',
                 'data':{'selected_topic':task['values']['generated_topics'][0]},'expected_state_version':task['state_version']},headers,202)
            task=wait(client,task['task_id'],headers)
            facts=task['values']['fact_check_report']
            record('grounded_draft',passed=facts['passed'],content=task['values']['article_content'],
                   fact_check_report=facts,quality_report=task['values']['quality_report'],node_metrics=task['values']['node_metrics'])
            if not facts['passed']:raise RuntimeError('live draft remains blocked by fact checking')
            post(client,f"/content-tasks/{task['task_id']}/actions",{'action':'approve_text','data':{},
                 'expected_state_version':task['state_version']},headers,202)
            task=wait(client,task['task_id'],headers)
            asset=checked(client.get('/api/v1/assets',headers=headers))['assets'][0]
            asset=post(client,f"/assets/{asset['asset_id']}/publish",{'expected_state_version':asset['state_version']},headers)
            client.post(f'/api/v1/consultations/{session}/messages',headers=headers,json={'message':'SC-PB27 重量是多少，保修多久？'})
            turn=checked(client.get('/api/v1/consultations/'+session,headers=headers))['turns'][-1]
            passed=asset['asset_id'] in turn['faq_asset_ids'] and '568' in turn['answer']
            record('faq_live_reuse',passed=passed,asset_id=asset['asset_id'],answer=turn['answer'],faq_asset_ids=turn['faq_asset_ids'])
            if not passed:raise RuntimeError('approved FAQ was not used')
            post(client,f"/assets/{asset['asset_id']}/revoke",{'expected_state_version':asset['state_version']},headers)
            record('faq_revoke',passed=True)
            report['success']=True
        finally:
            client.portal.call(rt.activate_snapshot,original)
except Exception as exc:
    report['success']=False;report['error_type']=type(exc).__name__;report['error']=str(exc)[:1500]
    print('Live verification failed: '+type(exc).__name__,flush=True)
finally:
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
raise SystemExit(0 if report.get('success') else 1)
