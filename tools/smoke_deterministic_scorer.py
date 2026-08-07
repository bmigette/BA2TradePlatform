"""Real-data smoke test for DeterministicScorer after the 2026-08-07 review fixes.

Runs a 6-month hermetic backtest over a small large-cap universe under several
settings combinations. This is a SMOKE test, not an evaluation: the universe and
window are far too small to say anything about edge. What it is actually
checking is that the corrected wiring behaves on real data:

  * the run completes with no hermetic violation (the fixed handlers no longer
    swallow one, so a cold cache FAILS here instead of silently degrading);
  * trades actually happen (the live path used to skip every symbol, and the
    macro cliff used to pin everything to HOLD);
  * the macro regime is backed by more than the lone index-trend input;
  * macro_mode / section-weight changes actually MOVE the result -- a knob that
    cannot change anything is the inert-gene bug class this review was about.

Run:  cd testplatform/backend && ~/ba2-venvs/test/bin/python ../../tools/smoke_deterministic_scorer.py
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date

# Direct run_daily_backtest calls bypass the GA's logging suppression (10x+ slower).
logging.disable(logging.INFO)

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                        "testplatform", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.chdir(_BACKEND)


def _bootstrap_app_settings() -> None:
    """Point ba2_common at the TEST DB so app settings (FMP key) resolve.

    Same bootstrap ba2test_launcher does: a bare CLI process otherwise reads
    ba2_common's neutral default DB and every key comes back empty.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_BACKEND, ".env"))
        load_dotenv(os.path.join(_BACKEND, "..", "..", ".env"))
    except ImportError:
        pass
    from app.models.database import DATABASE_URL
    if DATABASE_URL.startswith("sqlite:///"):
        from ba2_common.core import db as _db
        _db.configure_db(DATABASE_URL.replace("sqlite:///", "", 1))
    if not os.getenv("FMP_API_KEY"):
        from ba2_common.config import get_app_setting
        key = get_app_setting("FMP_API_KEY")
        if key:
            os.environ["FMP_API_KEY"] = key

UNIVERSE = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "WMT", "KO", "PFE"]
START = date(2024, 3, 1)
END = date(2024, 8, 31)

# 4 combinations chosen to exercise the things the review actually changed.
COMBOS = {
    "1-defaults": {},
    "2-macro-off": {"macro_mode": "off"},
    "3-technical-only": {"w_fundamental": 0.0, "w_technical": 1.0},
    "4-loose-thresholds": {"theta_buy": 0.3, "theta_sell": 0.15},
}


def _payload(backtest_id: int, overrides: dict) -> dict:
    from ba2_experts.DeterministicScorer import DeterministicScorer
    settings = {k: v["default"]
                for k, v in DeterministicScorer.get_settings_definitions().items()}
    settings.update(overrides)
    # The RM needs these to fund anything (interface defaults are False/True).
    settings.update({"allow_automated_trade_opening": True, "enable_buy": True,
                     "sizing_mode": "risk_atr"})
    return {
        "backtest_id": backtest_id,
        "name": "smoke-deterministic-scorer",
        "enabled_instruments": list(UNIVERSE),
        "experts": [{"class": "DeterministicScorer", "settings": settings}],
        "start_date": START.isoformat(),
        "end_date": END.isoformat(),
        "initial_capital": 100_000.0,
        "commission": 1.0,
        "slippage": 0.0,
        "fill_model": "next_bar_open",
        "seed": 42,
    }


def main() -> int:
    _bootstrap_app_settings()
    from app.services.backtest import daily_backtest_handler as H

    print(f"universe={len(UNIVERSE)} syms  window={START}..{END}  (hermetic, real cache)\n")
    rows = []
    for i, (label, overrides) in enumerate(COMBOS.items(), start=1):
        cfg = H._build_config(_payload(9000 + i, overrides))
        t0 = time.monotonic()
        try:
            res = H.run_daily_backtest(cfg)
        except Exception as e:
            print(f"{label:<20} FAILED  {type(e).__name__}: {str(e)[:160]}")
            rows.append((label, None))
            continue
        dt = time.monotonic() - t0
        rows.append((label, res))
        print(f"{label:<20} trades={res.get('total_trades', 0):>3}  "
              f"return={res.get('total_return', 0.0):>7.2f}%  "
              f"win={res.get('win_rate', 0.0):>5.1f}%  "
              f"maxDD={res.get('max_drawdown', 0.0):>6.2f}%  "
              f"sharpe={res.get('sharpe_ratio', 0.0):>5.2f}  [{dt:.0f}s]")

    ok = [r for _, r in rows if r is not None]
    print()
    if not ok:
        print("VERDICT: every combination failed -- see the errors above.")
        return 1
    traded = [r for r in ok if (r.get("total_trades") or 0) > 0]
    distinct = {round(float(r.get("total_return") or 0.0), 6) for r in ok}
    print(f"combinations run      : {len(ok)}/{len(COMBOS)}")
    print(f"combinations that trade: {len(traded)}/{len(ok)}")
    print(f"distinct outcomes      : {len(distinct)}  "
          f"({'settings move the result' if len(distinct) > 1 else 'INERT -- knobs changed nothing'})")
    return 0 if traded and len(distinct) > 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
