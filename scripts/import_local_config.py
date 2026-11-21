"""Reuse only cloud settings from the user's development copies, never old DBs.

No value is printed. This writes ignored local files inside the fusion project.
"""
from pathlib import Path
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND))
from dotenv import dotenv_values,set_key

WORKSPACE=ROOT.parent
creator=WORKSPACE/'项目2自媒体运营Ai智能体'/'graph_xiaohongshu-master-9b8f479bd21512201eb7d74ed034cd05e4af28c2'
customer=WORKSPACE/'项目1Ai语音对话智能客服项目'/'rtc_basic-main-8decc9076f4683cfd741b4275e7c1e7ce029a4cd'/'rag_llm_server'
allowed={
    'LLM_API_KEY','LLM_BASE_URL','LLM_MODEL','LLM_MODEL_FAST','LLM_TEMPERATURE','LLM_TEMPERATURE_FAST',
    'LLM_THINKING_MODE','IMAGE_API_KEY','IMAGE_BASE_URL','IMAGE_MODEL',
    'VOLC_ACCESS_KEY','VOLC_SECRET_KEY','VOLC_ACCOUNT_ID','ARK_API_KEY','ARK_ENDPOINT_ID','ARK_BASE_URL',
    'RTC_APP_ID','RTC_APP_KEY','RTC_API_VERSION','EMBEDDING_API_KEY','EMBEDDING_MODEL',
    'EMBEDDING_BASE_URL','EMBEDDING_API_PATH','EMBEDDING_DIMENSIONS',
}
target=BACKEND/'.env'
existing=dotenv_values(target)
count=0
for source in (creator/'.env',customer/'.env'):
    for name,value in dotenv_values(source).items():
        if name in allowed and value and not existing.get(name):
            set_key(target,name,value)
            existing[name]=value;count+=1
voice=customer/'voice_config.json'
if voice.exists() and not (BACKEND/'voice_config.json').exists():
    config=json.loads(voice.read_text(encoding='utf-8-sig'))
    # The fusion controller injects its own callback URL/secret at run time.
    config.pop('LLMConfig',None)
    (BACKEND/'voice_config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Imported {count} cloud settings; original database/JWT/callback/tunnel values were excluded.')
