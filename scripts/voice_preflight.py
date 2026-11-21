"""Validate this project's local voice settings; never calls a cloud service."""
import json
import os
from pathlib import Path
import sys
import argparse
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from dotenv import load_dotenv
load_dotenv(ROOT / 'backend' / '.env')
from app.fusion.legacy_rag.config import Config
from app.fusion.startup import validate_voice_settings, validate_tunnel_settings

parser = argparse.ArgumentParser()
parser.add_argument('--ngrok-config', type=Path)
args = parser.parse_args()

try:
    report = validate_voice_settings(Config(), os.environ.get('DATABASE_URL', ''),
        os.environ.get('POSTGRES_URI', ''), os.environ.get('FUSION_CALLBACK_SECRET', ''))
    if args.ngrok_config:
        validate_tunnel_settings(yaml.safe_load(args.ngrok_config.read_text(encoding='utf-8-sig')))
    print(json.dumps(report))
except Exception as exc:
    # Validation errors may contain URLs from parsers; never echo raw exceptions.
    print('语音离线预检失败：请核对新工程数据库、RTC/ARK/AK/SK、回调密钥及 voice_config.json。类型：'+type(exc).__name__)
    raise SystemExit(1)
