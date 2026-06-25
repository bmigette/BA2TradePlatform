"""Ad-hoc probe: is FMP's 5min historical-chart endpoint 429-ing (quota) or serving?"""
import os
import time
import requests


def _load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env(r"C:\Users\basti\Documents\dev\BA2TestPlatform\.env")
_load_env(r"C:\Users\basti\Documents\dev\BA2TradePlatform\.env")

k = os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY") or os.getenv("FMP_KEY")
print("key present:", bool(k), "| len", len(k) if k else 0)

for i in range(4):
    url = (
        "https://financialmodelingprep.com/api/v3/historical-chart/5min/AAPL"
        f"?from=2025-12-01&to=2025-12-05&apikey={k}"
    )
    r = requests.get(url, timeout=20)
    head = r.text[:140].replace("\n", " ")
    print(f"try{i}: status {r.status_code} | bytes {len(r.text)} | head {head}")
    time.sleep(4)
