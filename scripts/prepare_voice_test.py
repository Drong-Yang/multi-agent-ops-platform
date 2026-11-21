"""Prepare isolated synthetic-voice acceptance resources, without starting a tunnel.

Only the new project's .runtime files and its dedicated voice-test database change.
The previously installed ngrok binary/config is read, never edited.
"""
import json
from pathlib import Path
import sys
import wave

ROOT=Path(__file__).resolve().parents[1];BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND))
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
import psycopg
from psycopg import sql
import yaml

config=dotenv_values(BACKEND/'.env')
db=make_url(config['POSTGRES_URI'])
if 'fusion' not in (db.database or ''):raise SystemExit('Non-fusion database refused')
with psycopg.connect(config['POSTGRES_URI'],autocommit=True) as conn:
    if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',('commerce_fusion_voice_test',)).fetchone():
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier('commerce_fusion_voice_test')))
source=ROOT.parents[1]/'.runtime'/'ngrok.yml'
previous=yaml.safe_load(source.read_text(encoding='utf-8'))
token=previous.get('authtoken') or (previous.get('agent') or {}).get('authtoken')
if not token:raise SystemExit('Existing tunnel account configuration unavailable')
target=ROOT/'.runtime'/'ngrok-voice.yml'
target.write_text(yaml.safe_dump({'version':'2','authtoken':token,'web_addr':'127.0.0.1:4041',
    'tunnels':{'fusion_voice_test':{'proto':'http','addr':'127.0.0.1:8011'}}}),encoding='utf-8')
with wave.open(str(ROOT/'.runtime'/'synthetic-question.wav'),'rb') as f:
    params=f.getparams();speech=f.readframes(f.getnframes())
if params.nchannels!=1 or params.sampwidth!=2:raise SystemExit('Expected mono 16-bit synthetic audio')
with wave.open(str(ROOT/'.runtime'/'synthetic-input.wav'),'wb') as f:
    f.setparams(params)
    f.writeframes(bytes(params.framerate*params.sampwidth*4)+speech+bytes(params.framerate*params.sampwidth*30))
(ROOT/'.runtime'/'voice-test-state.json').write_text(json.dumps({
    'database':'commerce_fusion_voice_test','synthetic_question':'耳机拆封后能退吗？',
    'tunnel_port':8011,'tunnel_inspection_port':4041,'temporary':True},ensure_ascii=False,indent=2),encoding='utf-8')
print('Prepared isolated voice-test database, protected callback tunnel config and synthetic input; no credentials printed.')
