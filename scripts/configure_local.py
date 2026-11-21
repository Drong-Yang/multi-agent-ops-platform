"""Create ignored local settings, preserving any already configured values."""
from pathlib import Path
import secrets

ROOT=Path(__file__).resolve().parents[1]
pw=secrets.token_urlsafe(24)
root_env=ROOT/'.env'
if not root_env.exists():
    root_env.write_text(f'POSTGRES_USER=fusion\nPOSTGRES_PASSWORD={pw}\nPOSTGRES_DB=commerce_fusion\n'
        f'MINIO_ACCESS_KEY=fusionminio\nMINIO_SECRET_KEY={secrets.token_urlsafe(32)}\n',encoding='utf-8')
else:
    # This parser handles the generated local file only; don't print its values.
    values=dict(line.split('=',1) for line in root_env.read_text(encoding='utf-8').splitlines() if '=' in line)
    pw=values['POSTGRES_PASSWORD'].strip("'\"")
backend_env=ROOT/'backend'/'.env'
if not backend_env.exists():
    backend_env.write_text(f'APP_NAME=电商运营Agent\nDEBUG=false\nMOCK_MODE=true\nKB_MODE=sample\n'
        f'DATABASE_URL=postgresql+asyncpg://fusion:{pw}@127.0.0.1:55432/commerce_fusion\n'
        f'POSTGRES_URI=postgresql://fusion:{pw}@127.0.0.1:55432/commerce_fusion\n'
        f'JWT_SECRET_KEY={secrets.token_urlsafe(48)}\nFUSION_CALLBACK_SECRET={secrets.token_urlsafe(32)}\n'
        'LOG_CONSOLE=false\nLOG_LEVEL=WARNING\nMILVUS_URI=http://127.0.0.1:19531\n',encoding='utf-8')
(ROOT/'.runtime').mkdir(exist_ok=True)
print('Local settings ready; existing values were preserved.')
