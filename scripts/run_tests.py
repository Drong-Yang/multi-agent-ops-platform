"""Reproducible offline/DB test entrypoint; never uses the demo/original DB.

Run with backend/.venv/Scripts/python.exe scripts/run_tests.py [--repository-postgres]
"""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND))
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
import psycopg
from psycopg import sql

parser=argparse.ArgumentParser()
parser.add_argument('--repository-postgres',action='store_true')
parser.add_argument('--isolated-postgres', action='store_true',
                    help='Use OPS_TEST_POSTGRES_URI targeting an existing local commerce_fusion_test database')
parser.add_argument('--output-dir', type=Path, default=ROOT/'evals'/'runs')
args=parser.parse_args()
config=dotenv_values(BACKEND/'.env')
test_database='commerce_fusion_test'
if args.isolated_postgres:
    source_url=make_url(os.environ['OPS_TEST_POSTGRES_URI'])
    if source_url.host not in ('localhost', '127.0.0.1', '::1') or source_url.database != test_database:
        raise SystemExit('Expected a local commerce_fusion_test database')
else:
    source_url=make_url(config['POSTGRES_URI'])
    if 'fusion' not in (source_url.database or ''):
        raise SystemExit('Refusing a non-fusion database')
    with psycopg.connect(config['POSTGRES_URI'],autocommit=True) as conn:
        if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(test_database,)).fetchone():
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(test_database)))
plain=source_url.set(database=test_database).render_as_string(hide_password=False)
env={**os.environ,**{k:v for k,v in config.items() if v is not None}}
env.update(POSTGRES_URI=plain,DATABASE_URL=make_url(plain).set(drivername='postgresql+asyncpg').render_as_string(hide_password=False),
           MOCK_MODE='true',KB_MODE='sample',FUSION_KB_MODE='sample',FUSION_ACTIVATE_RETRIEVAL='sample',
           FUSION_MCP_INDEX_OPERATORS='',DEEPSEEK_API_KEY='',PYTHONIOENCODING='utf-8')
out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
if args.isolated_postgres:
    snapshots = out / '.fixture-snapshots'
    snapshots.mkdir(exist_ok=True)
    shutil.copyfile(ROOT/'knowledge'/'snapshots'/'ecommerce-v2.json', snapshots/'ecommerce-v2.json')
    env['FUSION_KNOWLEDGE_DIR']=str(snapshots)
if args.repository_postgres:
    env['FUSION_TEST_DATABASE_URL']=plain
    command=[sys.executable,'-m','unittest','tests.test_repository','-v']
    name='repository-postgres'
else:
    # discovery covers new modules, imported customer regressions and the graph/API tests.
    command=[sys.executable,'-m','unittest','discover','-s','tests','-t','.','-v']
    name='backend-all'
result=subprocess.run(command,cwd=BACKEND,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
output=result.stdout.decode('utf-8',errors='replace')
(out/(name+'.log')).write_text(output,encoding='utf-8')
results=[{'suite':name,'exit_code':result.returncode,'log':name+'.log'}]
if not args.repository_postgres:
    legacy=subprocess.run([sys.executable,'-m','unittest','test_reproduction','test_quality_upgrade','-v'],
                          cwd=BACKEND,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    legacy_output=legacy.stdout.decode('utf-8',errors='replace')
    (out/'creator-original-regression.log').write_text(legacy_output,encoding='utf-8')
    results.append({'suite':'creator-original','exit_code':legacy.returncode,'log':'creator-original-regression.log'})
    output+='\n'+legacy_output
manifest={'at':datetime.now(timezone.utc).isoformat(),'database':test_database,
          'mode':'mock_models_real_postgresql','results':results}
(out/(name+'-manifest.json')).write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
for line in output.splitlines():
    if line.startswith(('Ran ','OK','FAILED','FAIL:','ERROR:')):print(line)
raise SystemExit(0 if all(r['exit_code']==0 for r in results) else 1)
