"""Generate NEW disposable configuration outside the checkout; never overwrite."""
from pathlib import Path
import secrets
import sys

target = Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
(Path(__file__).resolve().parents[1] / 'logs').mkdir(exist_ok=True)
with target.open('x', encoding='utf-8') as f:
    for key in ('DEMO_SECRET_KEY', 'DEMO_DB_PASSWORD', 'DEMO_LOGIN_PASSWORD'):
        f.write(f'{key}={secrets.token_hex(24)}\n')
print('Created disposable configuration; keep its values private.')
