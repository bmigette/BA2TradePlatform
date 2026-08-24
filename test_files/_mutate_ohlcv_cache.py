"""Throwaway mutation harness for the OHLCV as_of-cache fix (2026-08-24).

Applies one source mutation at a time, runs the guarding tests, restores the file and
verifies the restore is BYTE-IDENTICAL via ``git hash-object``. Purges __pycache__ and
sets PYTHONDONTWRITEBYTECODE so a same-size mutation written in the same wall second
can never be masked by stale bytecode (a FALSE survivor).

Not a pytest test -- run directly:
    PYTHONDONTWRITEBYTECODE=1 venv/bin/python test_files/_mutate_ohlcv_cache.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MDP = os.path.join(ROOT, "packages/common/ba2_common/core/interfaces/MarketDataProviderInterface.py")
NC = os.path.join(ROOT, "packages/common/ba2_common/core/native_cache.py")

TESTS = ["tests/test_ohlcv_cache_asof_coverage.py", "tests/test_ohlcv_cache_freshness.py"]

# (name, file, old, new)
MUTATIONS = [
    ("M1  top-up reverts to a FULL fetch", MDP,
     "        fetch_start = last_bar_naive + interval_td\n"
     "        fetch_end = now + interval_td\n        try:",
     "        fetch_start = now - timedelta(days=365 * 15)\n"
     "        fetch_end = now + interval_td\n        try:"),

    ("M2  freshness check INVERTED", MDP,
     "                    if cache_path is None or not self._is_cache_valid(\n"
     "                            cache_path, max_cache_age_hours):",
     "                    if cache_path is not None and self._is_cache_valid(\n"
     "                            cache_path, max_cache_age_hours):"),

    ("M3  pinned backtest as_of TRIGGERS a re-fetch", MDP,
     "            if df is not None and not df.empty and is_latest:",
     "            if df is not None and not df.empty:"),

    ("M4  live path NEVER tops up (stale prices)", MDP,
     "            if df is not None and not df.empty and is_latest:",
     "            if df is not None and not df.empty and False:"),

    ("M5  empty/over-limit response CACHED as valid", MDP,
     "            if df is None or df.empty:\n"
     "                raise Exception(f\"Failed to fetch data for {symbol}\")\n\n"
     "            # Clean and harden against malformed data\n"
     "            df = self._clean_dataframe(df)\n\n"
     "            # Save to the parquet as_of store (effective_date == bar Date).\n"
     "            if use_cache:",
     "            if df is None:\n"
     "                raise Exception(f\"Failed to fetch data for {symbol}\")\n\n"
     "            # Clean and harden against malformed data\n"
     "            df = self._clean_dataframe(df)\n\n"
     "            # Save to the parquet as_of store (effective_date == bar Date).\n"
     "            if use_cache:"),

    ("M6  D1 REVERTED (empty as_of slice == cache miss)", MDP,
     "                if not native_cache.timeseries_row_count(provider_name, symbol, interval):\n"
     "                    df = None",
     "                if True:\n"
     "                    df = None"),

    ("M7  D1 OVER-fixed (a 0-row/corrupt cache never refills)", MDP,
     "                if not native_cache.timeseries_row_count(provider_name, symbol, interval):\n"
     "                    df = None",
     "                if False:\n"
     "                    df = None"),

    ("M8  D2 REVERTED (write replaces instead of merging)", MDP,
     "        if existing is not None and len(existing):",
     "        if False:"),

    ("M9  D3 REVERTED (alias write shadows the real cache)", NC,
     "    path = find_timeseries_path(provider, symbol, interval) or \\\n"
     "        timeseries_path(provider, symbol, interval)",
     "    path = timeseries_path(provider, symbol, interval)"),

    ("M10 timeseries_row_count always reports 1 (corrupt reads as populated)", NC,
     "        return int(pq.ParquetFile(path).metadata.num_rows)",
     "        return 1 if pq.ParquetFile(path) is not None else 1"),

    ("M11 timeseries_row_count always reports 0 (everything is a miss)", NC,
     "        return int(pq.ParquetFile(path).metadata.num_rows)",
     "        return 0 * int(pq.ParquetFile(path).metadata.num_rows)"),

    ("M12 _is_latest_request tolerance widened to 10 years", MDP,
     "        return end >= datetime.utcnow() - timedelta(hours=1)",
     "        return end >= datetime.utcnow() - timedelta(days=3650)"),

    ("M13 _is_latest_request always False (nothing is live)", MDP,
     "        return end >= datetime.utcnow() - timedelta(hours=1)",
     "        return False and end >= datetime.utcnow() - timedelta(hours=1)"),

    ("M14 merge keeps the FIRST duplicate (a revised bar never overwrites)", MDP,
     "            merged = (merged.drop_duplicates(subset=['Date'], keep='last')",
     "            merged = (merged.drop_duplicates(subset=['Date'], keep='first')"),

    ("M15 merge drops the sort (unordered cache)", MDP,
     "            merged = (merged.drop_duplicates(subset=['Date'], keep='last')\n"
     "                            .sort_values('Date')\n"
     "                            .reset_index(drop=True))",
     "            merged = (merged.drop_duplicates(subset=['Date'], keep='last')\n"
     "                            .sort_values('Date', ascending=False)\n"
     "                            .reset_index(drop=True))"),

    ("M16 read_timeseries drops the as_of ceiling (LOOKAHEAD)", NC,
     "        df = df[eff <= _as_utc(as_of)]",
     "        df = df[eff <= _as_utc(as_of) + pd.Timedelta(days=3650)]"),

    ("M17 cold-fetch intraday range ignores the requested start", MDP,
     "                fetch_start = normalized_start or (datetime.now() - timedelta(days=365 * 2))",
     "                fetch_start = datetime.now() - timedelta(days=365 * 15)"),

    ("M18 find_timeseries_path only sees the canonical spelling", NC,
     "    for spelling in _INTERVAL_ALIASES.get(canon, [canon]):",
     "    for spelling in [canon]:"),
]


def purge_pyc():
    for dirpath, dirnames, _ in os.walk(ROOT):
        if "/venv" in dirpath:
            dirnames[:] = []
            continue
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                dirnames.remove(d)


def sha(path):
    return subprocess.run(["git", "hash-object", path], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()


def run_tests():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([os.path.join(ROOT, "venv/bin/python"), "-m", "pytest",
                        *TESTS, "-q", "-p", "no:randomly", "--no-header", "-x"],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    tail = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l]
    return r.returncode, (tail[-1] if tail else "<no summary>")


def main():
    baseline = {MDP: sha(MDP), NC: sha(NC)}
    purge_pyc()
    rc, summary = run_tests()
    print(f"BASELINE (unmutated): rc={rc}  {summary}")
    if rc != 0:
        sys.exit("baseline is not green; aborting")

    survivors = []
    for name, path, old, new in MUTATIONS:
        src = open(path).read()
        if src.count(old) != 1:
            print(f"SKIP  {name}: anchor matched {src.count(old)} times")
            survivors.append(name + "  [ANCHOR MISS]")
            continue
        open(path, "w").write(src.replace(old, new, 1))
        purge_pyc()
        rc, summary = run_tests()
        open(path, "w").write(src)
        purge_pyc()
        assert sha(path) == baseline[path], f"RESTORE NOT BYTE-IDENTICAL for {path}"
        status = "KILLED " if rc != 0 else "SURVIVED"
        print(f"{status}  {name}   ->  {summary}")
        if rc == 0:
            survivors.append(name)

    print("\n=== survivors:", survivors or "NONE")
    print("=== restore verified byte-identical:",
          all(sha(p) == h for p, h in baseline.items()))


if __name__ == "__main__":
    main()
