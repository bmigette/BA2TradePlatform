#!/usr/bin/env python3
"""Authoritative as_of=None byte-equality probe (Task 11 Step 3).

This is NOT a pytest test (it makes live FMP network calls and imports the
READ-ONLY live reference tree). It is the authoritative "no live-behaviour
change" proof for the two providers touched by the Phase-1 lookahead fixes:

  * insider  (FMPInsiderProvider.get_insider_transactions)
  * statements (FMPCompanyDetailsProvider.get_balance_sheet)

For a fixed (symbol, as_of=None) it fetches via BOTH trees and asserts the
normalized dict outputs are equal:

  * the pre-refactor LIVE tree   ->  BA2TradePlatform/ba2_trade_platform/...
  * the new ba2_providers package -> as_of=None code path

Because the two trees use the same top-level package name for nothing that
overlaps (``ba2_trade_platform`` vs ``ba2_providers``) BUT share transitive
deps and module-global FMP TTL caches, each fetch is run in its OWN subprocess
(per the plan's re-plan checkpoint) and the two JSON outputs are diffed. The
live tree is imported READ-ONLY; this script never writes to it.

USAGE (network + live key required; run once, snapshot the result in the commit):

    export FMP_API_KEY=<your key>
    /Users/bmigette/Documents/dev/BA2/BA2TradePlatform/venv/bin/python \
        BA2TradeProviders/test_files/probe_asof_byte_equality.py AAPL

Exit code 0 + "BYTE-EQUAL" on success; non-zero + a unified diff on mismatch.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))         # BA2TradeProviders
BA2_ROOT = os.path.dirname(REPO_ROOT)                                            # .../BA2
COMMON = os.path.join(BA2_ROOT, "BA2TradeCommon")
PROVIDERS = REPO_ROOT
LIVE_TREE = os.path.join(BA2_ROOT, "BA2TradePlatform")  # contains ba2_trade_platform/


# --- child program: fetch via the NEW ba2_providers package (as_of=None) -------
_NEW_CHILD = r'''
import json, sys, os
from datetime import datetime, timezone
from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider

symbol = sys.argv[1]
end = datetime(2026, 6, 13, tzinfo=timezone.utc)

ins = FMPInsiderProvider.__new__(FMPInsiderProvider)
ins.api_key = os.environ["FMP_API_KEY"]
insider = ins.get_insider_transactions(
    symbol, end_date=end, lookback_days=180, as_of=None, format_type="dict")

det = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
det.api_key = os.environ["FMP_API_KEY"]
balance = det.get_balance_sheet(
    symbol, "annual", end_date=end, lookback_periods=2, as_of=None, format_type="dict")

print(json.dumps({"insider": insider, "balance": balance}, sort_keys=True, default=str))
'''


# --- child program: fetch via the LIVE reference tree (no as_of param) ----------
_LIVE_CHILD = r'''
import json, sys, os
from datetime import datetime, timezone
from ba2_trade_platform.modules.dataproviders.insider.FMPInsiderProvider import FMPInsiderProvider
from ba2_trade_platform.modules.dataproviders.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider

symbol = sys.argv[1]
end = datetime(2026, 6, 13, tzinfo=timezone.utc)

ins = FMPInsiderProvider.__new__(FMPInsiderProvider)
ins.api_key = os.environ["FMP_API_KEY"]
insider = ins.get_insider_transactions(
    symbol, end_date=end, lookback_days=180, format_type="dict")

det = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
det.api_key = os.environ["FMP_API_KEY"]
balance = det.get_balance_sheet(
    symbol, "annual", end_date=end, lookback_periods=2, format_type="dict")

print(json.dumps({"insider": insider, "balance": balance}, sort_keys=True, default=str))
'''


def _run_child(program: str, pythonpath: str, symbol: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(program)
        script = f.name
    try:
        out = subprocess.run(
            [sys.executable, script, symbol],
            env=env, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            sys.stderr.write(out.stderr)
            raise RuntimeError(f"child fetch failed (rc={out.returncode})")
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(script)


def main() -> int:
    if "FMP_API_KEY" not in os.environ:
        sys.stderr.write("FMP_API_KEY env var required (live network probe).\n")
        return 2
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

    new = _run_child(_NEW_CHILD, os.pathsep.join([COMMON, PROVIDERS]), symbol)
    # The live tree's package root is BA2TradePlatform/ (so `import ba2_trade_platform`).
    live = _run_child(_LIVE_CHILD, LIVE_TREE, symbol)

    new_s = json.dumps(new, sort_keys=True, indent=2)
    live_s = json.dumps(live, sort_keys=True, indent=2)
    if new_s == live_s:
        print(f"BYTE-EQUAL: as_of=None == live fetch for {symbol} (insider + balance_sheet)")
        return 0

    import difflib
    diff = difflib.unified_diff(
        live_s.splitlines(), new_s.splitlines(),
        fromfile="live_tree", tofile="ba2_providers(as_of=None)", lineterm="")
    sys.stderr.write("\n".join(diff) + "\n")
    sys.stderr.write(f"MISMATCH for {symbol}: as_of=None diverged from live fetch.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
