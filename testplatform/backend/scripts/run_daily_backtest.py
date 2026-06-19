#!/usr/bin/env python3
"""Daily expert-aware backtest CLI (Phase: CLI).

Runs ONE daily multi-asset expert backtest through the SAME synchronous core the task
queue + joint optimizer call -- ``app.services.backtest.daily_backtest_handler.run_daily_backtest``
-- so the CLI exercises the real ba2trade order path (BacktestAccount + DailyBacktestEngine
over an as-of FMP OHLCV price source), not a parallel implementation.

It is "expert-aware": pick the expert by class name (``--expert``), the universe
(``--universe``), the date range (``--start``/``--end``) and the engine type
(``--engine-type daily_expert``). For a BYPASS expert (FactorRanker, which declares
``bypasses_classic_rm``) the engine already routes its weight targets straight through its
FactorPortfolioManager -- the CLI needs no special-casing; it just hands the config to the
core and the engine/handler branch on the marker.

DATA SOURCE (auto-detected, never fabricated):

  * REAL daily data -- if an ``FMP_API_KEY`` is configured (env, or ``backend/.env`` /
    project-root ``.env`` which this script loads on startup), the run resolves the live FMP
    OHLCV provider and pulls real daily bars for the universe/date-range.

  * HERMETIC fixture smoke -- if NO FMP key is found, the run is driven against the
    deterministic fixture cache (``tests/backtest/fixtures/hermetic_providers.py``): zero
    network, no key, byte-reproducible. This proves the CLI + engine work end-to-end. A
    real-data run then only needs ``FMP_API_KEY`` set (or an imported parquet cache) -- the
    CLI command line is identical.

Usage (real data, key configured)::

    cd backend
    ./venv/bin/python scripts/run_daily_backtest.py \
        --expert FMPEarningsDrift \
        --universe AAPL,MSFT,NVDA \
        --start 2024-01-02 --end 2024-06-28 \
        --engine-type daily_expert --seed 42 --initial-capital 100000

Usage (no key -> hermetic smoke; same flags, universe/window are coerced to the fixture
cache so a signal actually fires)::

    ./venv/bin/python scripts/run_daily_backtest.py --expert FMPEarningsDrift

Output: prints the metric blob + the equity curve (head/tail) to stdout; with ``--out PATH``
also writes the full results JSON (metrics + full equity/drawdown curve + trades).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# --- import roots (mirror the other backend scripts) -----------------------
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

# Load .env so a configured FMP_API_KEY (project root or backend/.env) is picked up before
# any provider resolution. Mirrors scripts/test_amgn_gaps.py. Absent files are a no-op.
try:
    from dotenv import load_dotenv

    load_dotenv(BACKEND_ROOT / ".env")
    load_dotenv(BACKEND_ROOT.parent / ".env")
except Exception:  # noqa: BLE001 — dotenv is optional; absence just means env-only resolution
    pass


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _run_schedule_override(spec, day: str = "monday"):
    """Map the ``--run-schedule`` shorthand to the engine's ``run_schedule_override`` dict.

    ``daily`` / ``None`` -> ``None`` (analyse every bar, legacy). ``weekly`` -> analyse for
    NEW entries only on ``day`` (default Monday): ``{"days": {<day>: True, rest: False}}``.
    Fills + open-position management still run every bar (the engine gates only entry
    analysis), so weekly cadence is ~5x fewer expensive expert evaluations.
    """
    if not spec or spec == "daily":
        return None
    if spec == "weekly":
        return {"days": {d: (d == day) for d in _WEEKDAYS}}
    raise SystemExit(f"--run-schedule must be 'daily' or 'weekly' (got {spec!r})")


def _has_fmp_key() -> bool:
    """True iff an FMP API key is configured.

    Two sources are checked (matching where the real providers actually read the key):
      * ``os.getenv('FMP_API_KEY')`` -- env / ``.env`` (loaded above);
      * ``get_app_setting('FMP_API_KEY')`` -- the live BA2Trade app-settings DB, which is what
        ``FMPOHLCVProvider`` itself resolves the key from. The per-run backtest DB carries this
        key forward (see backtest_db.backtest_trading_db) so the provider sees it inside the run.
    """
    if os.getenv("FMP_API_KEY"):
        return True
    try:
        from ba2_common.config import get_app_setting

        return bool(get_app_setting("FMP_API_KEY"))
    except Exception:  # noqa: BLE001 — DB not ready -> treat as no key (hermetic smoke)
        return False


def _parse_args(argv: list) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_daily_backtest",
        description="Run a daily expert-aware backtest (real FMP data if a key is configured, "
        "else a hermetic fixture smoke).",
    )
    p.add_argument(
        "--expert",
        default="FMPEarningsDrift",
        help="Expert class name (FMPEarningsDrift / FMPInsiderClusterBuy / FactorRanker).",
    )
    p.add_argument(
        "--universe",
        default="AAPL,MSFT",
        help="Comma-separated symbols (the trading universe). Ignored in hermetic mode, where "
        "the fixture universe is used so a planted signal fires.",
    )
    p.add_argument("--start", default=None, help="ISO start date (YYYY-MM-DD).")
    p.add_argument("--end", default=None, help="ISO end date (YYYY-MM-DD).")
    p.add_argument(
        "--engine-type",
        default="daily_expert",
        choices=["daily_expert"],
        help="Engine type. Only 'daily_expert' is supported by this CLI.",
    )
    p.add_argument("--seed", type=int, default=42, help="RNG seed (run determinism).")
    p.add_argument(
        "--initial-capital", type=float, default=100_000.0, help="Starting cash."
    )
    p.add_argument("--commission", type=float, default=1.0, help="Commission per trade.")
    p.add_argument("--slippage", type=float, default=0.0, help="Slippage in bps.")
    p.add_argument(
        "--fill-model",
        default="next_bar_open",
        help="Fill model (e.g. next_bar_open).",
    )
    p.add_argument(
        "--warmup-days",
        type=int,
        default=60,
        help="History preloaded before start_date (fetch-window sizing, not a trade param).",
    )
    p.add_argument(
        "--interval",
        default="1d",
        help="Execution bar interval for the fill clock (1d default; e.g. 1h, 15m for finer "
             "open/close fill detection). Experts still fetch any interval they need separately.",
    )
    p.add_argument(
        "--run-schedule",
        default="daily",
        choices=["daily", "weekly"],
        help="Entry cadence: 'daily' analyses for new positions every bar (legacy); 'weekly' "
             "analyses once a week (on --run-schedule-day). Fills + open-position management "
             "still run every bar — weekly is ~5x fewer (expensive) expert evaluations/fetches.",
    )
    p.add_argument(
        "--run-schedule-day",
        default="monday",
        choices=list(_WEEKDAYS),
        help="Weekday for weekly cadence (default monday).",
    )
    p.add_argument(
        "--name", default=None, help="Optional run name (defaults to expert+date stamp)."
    )
    p.add_argument(
        "--out",
        default=None,
        help="Optional path to write the full results JSON (metrics + curves + trades).",
    )
    p.add_argument(
        "--track",
        action="store_true",
        help="Persist this run as a tracked Backtest row (shows up in `ba2-test runs list`, "
             "deletable via clear-unsaved). Same results table the API/UI uses.",
    )
    p.add_argument(
        "--save",
        action="store_true",
        help="Persist AND mark the run saved (is_saved=True) so it survives `runs clear-unsaved`. "
             "Implies --track.",
    )
    return p.parse_args(argv)


def _build_real_config(args: argparse.Namespace) -> dict:
    """Assemble the engine run config for a REAL-data run from the CLI args.

    Produces the same shape ``daily_backtest_handler._build_config`` emits (account_settings,
    enabled_instruments, experts, dates, warmup_days, seed). A backtest_id is required only to
    name the per-run trading DB; we stamp a unique one off the clock.
    """
    if not args.start or not args.end:
        raise SystemExit(
            "real-data run requires --start and --end (ISO dates). "
            "Re-run with them, or run without an FMP key for the hermetic smoke."
        )
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    if end < start:
        raise SystemExit("--end must be on or after --start")

    universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    if not universe:
        raise SystemExit("--universe must list at least one symbol")

    backtest_id = int(datetime.now().timestamp())
    config = {
        "backtest_id": backtest_id,
        "name": args.name or f"cli-{args.expert}-{args.start}_{args.end}",
        "start_date": start,
        "end_date": end,
        "enabled_instruments": universe,
        "experts": [args.expert],
        "initial_capital": float(args.initial_capital),
        "account_settings": {
            "starting_cash": float(args.initial_capital),
            "commission_per_trade": float(args.commission),
            "slippage_bps": float(args.slippage),
            "fill_model": str(args.fill_model),
        },
        "warmup_days": int(args.warmup_days),
        "execution_interval": str(args.interval),
        "seed": int(args.seed),
        "subtype": "daily_expert",
    }
    override = _run_schedule_override(args.run_schedule, args.run_schedule_day)
    if override:
        config["run_schedule_override"] = override
    return config


def _run_real(args: argparse.Namespace) -> dict:
    """Run against the LIVE FMP OHLCV provider (real daily bars)."""
    from app.services.backtest.daily_backtest_handler import (
        _SUPPORTED_EXPERTS,
        run_daily_backtest,
    )

    if args.expert not in _SUPPORTED_EXPERTS:
        raise SystemExit(
            f"unsupported --expert {args.expert!r}; supported: {sorted(_SUPPORTED_EXPERTS)}"
        )
    config = _build_real_config(args)

    def progress(pct: float, msg: str) -> None:
        sys.stderr.write(f"\r[{pct:5.1f}%] {msg[:70]:<70}")
        sys.stderr.flush()

    results = run_daily_backtest(config, progress_cb=progress)
    sys.stderr.write("\n")
    return results, config


def _run_hermetic(args: argparse.Namespace) -> dict:
    """Run against the deterministic fixture cache (no network / no FMP key).

    Reuses the e2e harness: ``hermetic_providers()`` re-points BOTH provider seams at the
    fixture cache, and the fixture UNIVERSE / TRADE_START..END / expert settings keep a
    planted signal live so the run actually trades. We call the SAME synchronous core
    (``run_daily_backtest``) inside that context so the CLI path is exercised end-to-end.
    """
    from tests.backtest.fixtures.e2e_support import hermetic_providers
    from tests.backtest.fixtures.hermetic_providers import (
        EARNINGS_DRIFT_SETTINGS,
        INSIDER_CLUSTER_SETTINGS,
        TRADE_END,
        TRADE_START,
        UNIVERSE,
    )

    from app.services.backtest.daily_backtest_handler import (
        _SUPPORTED_EXPERTS,
        run_daily_backtest,
    )

    if args.expert not in _SUPPORTED_EXPERTS:
        raise SystemExit(
            f"unsupported --expert {args.expert!r}; supported: {sorted(_SUPPORTED_EXPERTS)}"
        )

    # Pick the fixture settings that match the chosen clean expert so a signal fires. The
    # fixture cache only plants AAPL signals for the two FMP* clean experts; FactorRanker
    # would need its own factor inputs, so this hermetic smoke targets the FMP* experts.
    settings_by_expert = {
        "FMPEarningsDrift": EARNINGS_DRIFT_SETTINGS,
        "FMPInsiderClusterBuy": INSIDER_CLUSTER_SETTINGS,
    }
    if args.expert not in settings_by_expert:
        raise SystemExit(
            f"hermetic smoke supports {sorted(settings_by_expert)} (the fixture cache plants "
            f"signals only for those); {args.expert!r} needs a real FMP key / imported cache."
        )

    backtest_id = int(datetime.now().timestamp())
    config = {
        "backtest_id": backtest_id,
        "name": args.name or f"cli-hermetic-{args.expert}",
        "start_date": TRADE_START,
        "end_date": TRADE_END,
        "enabled_instruments": list(UNIVERSE),
        "experts": [{"class": args.expert, "settings": settings_by_expert[args.expert]}],
        "initial_capital": float(args.initial_capital),
        "account_settings": {
            "starting_cash": float(args.initial_capital),
            "commission_per_trade": float(args.commission),
            "slippage_bps": float(args.slippage),
            "fill_model": str(args.fill_model),
        },
        "warmup_days": 30,
        "seed": int(args.seed),
        "subtype": "daily_expert",
    }
    override = _run_schedule_override(args.run_schedule, args.run_schedule_day)
    if override:
        config["run_schedule_override"] = override

    with hermetic_providers():
        return run_daily_backtest(config), config


def _print_report(results: dict, *, hermetic: bool, expert: str) -> None:
    """Print the metric blob + an equity-curve preview to stdout."""
    equity = results.get("equity_curve", []) or []
    metric_keys = [
        "total_trades",
        "winning_trades",
        "losing_trades",
        "win_rate",
        "total_return",
        "sharpe_ratio",
        "max_drawdown",
        "profit_factor",
        "final_equity",
        "equity_peak",
        "avg_trade_duration",
    ]
    print("=" * 68)
    print(f"DAILY BACKTEST RESULTS  ({'HERMETIC fixture' if hermetic else 'REAL FMP'} data)")
    print(f"  expert     : {expert}")
    print(f"  engine_type: daily_expert")
    print("=" * 68)
    print("METRICS:")
    for k in metric_keys:
        if k in results:
            print(f"  {k:22s}: {results[k]}")
    print(f"EQUITY CURVE: {len(equity)} points")
    if equity:
        head = equity[: min(3, len(equity))]
        tail = equity[-min(3, len(equity)):]
        for pt in head:
            print(f"  [head] {pt['date']}  equity={pt['equity']:.2f}")
        if len(equity) > 6:
            print("  ...")
        for pt in tail:
            print(f"  [tail] {pt['date']}  equity={pt['equity']:.2f}")
    print("=" * 68)
    if hermetic:
        print(
            "NOTE: no FMP_API_KEY found -> ran the HERMETIC fixture smoke (deterministic, "
            "no network). A REAL-data run needs FMP_API_KEY set (env or backend/.env) or an "
            "imported parquet OHLCV cache; the CLI command line is otherwise identical."
        )


def _persist_tracked(config: dict, results: dict, *, saved: bool) -> int:
    """Write the finished run as a tracked ``Backtest`` row (the SAME table the API/UI use).

    Reuses ``daily_backtest_handler._persist_results`` for the metric/curve mapping so a
    CLI-tracked run is indistinguishable from an API one. The results row id is independent
    of the per-run trading-DB id (two separate DBs, per the handler contract).
    """
    import app.models  # noqa: F401 — registers all ORM models on Base
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal, init_db
    from app.services.backtest.daily_backtest_handler import _persist_results

    init_db()  # ensure the results schema exists (same call the platform makes on startup)
    acct = config["account_settings"]
    experts = config.get("experts") or []
    first = experts[0] if experts else None
    expert_name = (first.get("class") if isinstance(first, dict) else first) if first else None
    db = SessionLocal()
    try:
        bt = Backtest(
            name=config["name"],
            model_id=None,  # daily expert runs are not model-driven
            engine_type="daily_expert",
            expert_name=expert_name,
            start_date=config["start_date"],
            end_date=config["end_date"],
            initial_capital=float(config["initial_capital"]),
            commission=float(acct["commission_per_trade"]),
            slippage=float(acct["slippage_bps"]),
            status="running",
            started_at=datetime.now(),
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        _persist_results(db, bt, results)
        bt.status = "completed"
        bt.completed_at = datetime.now()
        bt.is_saved = bool(saved)
        db.commit()
        return bt.id
    finally:
        db.close()


def main(argv: list) -> int:
    args = _parse_args(argv)
    hermetic = not _has_fmp_key()
    if hermetic:
        sys.stderr.write(
            "[run_daily_backtest] no FMP_API_KEY found -> hermetic fixture smoke.\n"
        )
        results, config = _run_hermetic(args)
    else:
        sys.stderr.write(
            "[run_daily_backtest] FMP_API_KEY found -> real daily-data run.\n"
        )
        results, config = _run_real(args)

    _print_report(results, hermetic=hermetic, expert=args.expert)

    if args.track or args.save:
        run_id = _persist_tracked(config, results, saved=args.save)
        print(f"Tracked run persisted -> Backtest id={run_id} (saved={bool(args.save)})")

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(results, indent=2, default=str))
        print(f"Wrote full results JSON -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
