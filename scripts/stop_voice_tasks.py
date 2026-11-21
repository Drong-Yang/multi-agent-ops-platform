"""Stop only this fusion database's bound active RTC tasks before local shutdown."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
load_dotenv(ROOT / 'backend' / '.env')
from app.fusion.legacy_rag.config import Config
from app.fusion.repository import Repository
from app.fusion.rtc_control import RtcController
from app.fusion.startup import stop_active_voices

async def main():
    url = make_url(os.environ['POSTGRES_URI'])
    if 'fusion' not in (url.database or '').lower():
        raise ValueError('Refusing a non-fusion database')
    config = Config()
    config.MOCK_MODE = False
    repo = Repository(url.set(drivername='postgresql+psycopg').render_as_string(hide_password=False))
    try:
        result = await stop_active_voices(repo, RtcController(config))
        print(json.dumps(result))
        return 1 if result['failed'] else 0
    finally:
        repo.close()

try:
    raise SystemExit(asyncio.run(main()))
except Exception as exc:
    print('云端任务清理未完成，请重试或在 RTC 控制台核对；错误类型：'+type(exc).__name__)
    raise SystemExit(1)
