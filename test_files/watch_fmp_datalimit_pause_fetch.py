"""Watch the broad 5min fetch for the FMP DATA-LIMIT failure (distinct from 429 rate-limit) and
PAUSE (kill) the fetch when it hits. Signals: real HTTP 403 / 'Limit Reach' / 'upgrade your plan'
/ 'exceeded' in the log, OR a hard stall (cache count flat for ~12 min while the process is alive).
Leaves the optimization grid untouched. Prints a clear verdict + the symbols cached at pause."""
import os
import re
import time
import subprocess

LOG = r"C:\Users\basti\Documents\dev\broad_fetch_5min.log"
CACHE = r"C:\Users\basti\Documents\ba2\common\cache\FMPOHLCVProvider"
# Real data-limit signatures (NOT 429, NOT '403' inside bar counts like '40394').
LIMIT_RE = re.compile(
    r"historical-chart 403 for|\(last: HTTP 403\)|Limit Reach|upgrade your plan|"
    r"exceeded your|bandwidth limit|usage limit|too many requests per (day|month)",
    re.IGNORECASE,
)


def cached():
    try:
        return sum(1 for f in os.listdir(CACHE) if f.endswith("_5min.parquet"))
    except OSError:
        return -1


def fetch_alive():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'fetch-cache --symbols' "
         "-and $_.Name -ne 'pwsh.exe' } | Measure-Object).Count"],
        capture_output=True, text=True,
    )
    try:
        return int(out.stdout.strip()) > 0
    except ValueError:
        return False


def kill_fetch():
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'fetch-cache --symbols' "
         "-and $_.Name -ne 'pwsh.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
         "-ErrorAction SilentlyContinue }"],
        capture_output=True, text=True,
    )


last_growth_count = cached()
last_growth_time = time.time()
for i in range(600):  # up to ~10h
    if not fetch_alive():
        print(f"PAUSE/EXIT: fetch process already gone (cached={cached()}/868+).")
        break
    # 1) data-limit signature in the log?
    hit = None
    try:
        with open(LOG, "r", errors="ignore") as fh:
            tail = fh.readlines()[-300:]
        for ln in tail:
            if LIMIT_RE.search(ln):
                hit = ln.strip()
                break
    except OSError:
        pass
    if hit:
        kill_fetch()
        print(f"DATA_LIMIT_HIT -> PAUSED fetch. cached={cached()}. signal: {hit[:160]}")
        break
    # 2) hard stall (no new files for ~12 min while alive)?
    n = cached()
    if n > last_growth_count:
        last_growth_count, last_growth_time = n, time.time()
    elif time.time() - last_growth_time > 12 * 60:
        kill_fetch()
        print(f"HARD_STALL_12min -> PAUSED fetch (likely data-limit). cached={n}.")
        break
    print(f"{time.strftime('%H:%M:%S')} ok: fetch alive, cached={n}, no data-limit signal")
    time.sleep(90)
else:
    print("WATCH ended (timeout) without a data-limit signal.")
