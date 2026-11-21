"""Run the app on the isolated voice-test DB; demo settings/files stay unchanged."""
import os
from pathlib import Path
import sys
import asyncio
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1];BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND));os.chdir(BACKEND)
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
load_dotenv(BACKEND/'.env')
callback=os.getenv('SERVER_URL','')
if urlsplit(callback).scheme!='https':raise SystemExit('A protected HTTPS callback is required')
base=make_url(os.environ['POSTGRES_URI']).set(database='commerce_fusion_voice_test')
os.environ['POSTGRES_URI']=base.render_as_string(hide_password=False)
os.environ['DATABASE_URL']=base.set(drivername='postgresql+asyncpg').render_as_string(hide_password=False)
os.environ['MOCK_MODE']='false';os.environ['FUSION_KB_MODE']='milvus'
os.environ['FUSION_ACTIVATE_RETRIEVAL']='milvus'
os.environ['MILVUS_TIMEOUT']='10';os.environ['EMBEDDING_TIMEOUT']='10'
if sys.platform=='win32':asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import uvicorn
uvicorn.run('app.main:app',host='127.0.0.1',port=8010,loop='asyncio:SelectorEventLoop')
