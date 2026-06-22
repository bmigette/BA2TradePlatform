"""``ba2-test`` — console CLI for the BA2 Test Platform (ML / backtest).

Installed as the ``ba2-test`` command (``pyproject.toml`` ``[project.scripts]``). It is a
subcommand dispatcher that runs against the ``backend/`` package (added to the path), so it
covers the platform's operations without the API:

  ba2-test serve [--host --port --reload]      launch the FastAPI API (uvicorn app.main:app)
  ba2-test backtest <run_daily_backtest args>  run a daily expert backtest (full passthrough)
  ba2-test fetch-cache --symbols .. [...]       populate the as-of OHLCV cache
  ba2-test build-screener-metrics --store .. [..] build the screener metric_store (parquet)
  ba2-test fetch-options --underlyings .. [...]  build the offline options cache from Alpaca
  ba2-test cache-usage                          show cache disk usage per type
  ba2-test cache-clear [--type T] [--before D]  clear cache (all, or one type, optional date)
  ba2-test runs list [--saved-only]             list tracked backtest runs (shared results table)
  ba2-test runs save <id> [--name N]            mark a run saved (survives clear-unsaved)
  ba2-test runs clear-unsaved                    delete all runs not marked saved
  ba2-test runs delete <id>                      delete one run

  (persist a CLI run with: ba2-test backtest ... --track  [or --save to keep it])

Run ``ba2-test <cmd> -h`` for per-command help. Works for an editable/source install (the
repo root is resolved from this module's location).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime


def _enter_backend() -> str:
    """Put ``backend/`` on the path and chdir into it (the app's import + cwd root)."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    backend = os.path.join(repo_root, "backend")
    if not os.path.isdir(backend):
        sys.exit(
            f"ba2-test: backend dir not found at {backend}. The console command requires "
            f"an editable/source install of the test-platform repo."
        )
    if backend not in sys.path:
        sys.path.insert(0, backend)
    os.chdir(backend)
    # Load .env (FMP_API_KEY etc.), mirroring run_daily_backtest.py.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(backend, ".env"))
        load_dotenv(os.path.join(repo_root, ".env"))
    except Exception:  # noqa: BLE001 — dotenv optional
        pass
    # Point the shared ba2_common engine at the test DB (DATABASE_URL -> test/dl_forecasting.db),
    # exactly like app.main does at serve startup, so get_app_setting (API keys / settings) resolves
    # from the SAME DB for EVERY ba2-test command (worker, build-screener-metrics, ...). Without it a
    # fresh CLI process reads ba2_common's neutral default DB (BA2_HOME/db.sqlite) and keys come back
    # empty — which is why `ba2-test worker` failed with "FMP API key not configured".
    try:
        from app.models.database import DATABASE_URL as _DB_URL
        if _DB_URL.startswith("sqlite:///"):
            from ba2_common.core import db as _ba2_db
            _ba2_db.configure_db(_DB_URL.replace("sqlite:///", "", 1))
    except Exception:  # noqa: BLE001 — non-fatal; key reads would surface their own error later
        pass
    # The test platform's legacy OHLCV providers read FMP_API_KEY from the ENV, but the key is
    # configured in the app-settings DB (ba2_common). Mirror it into the env (in-process
    # only — never written to disk) so fetch-cache/build-screener-metrics resolve it, matching how the
    # backtest path forwards the key. No-op if already set or unavailable.
    if not os.getenv("FMP_API_KEY"):
        try:
            from ba2_common.config import get_app_setting
            _k = get_app_setting("FMP_API_KEY")
            if _k:
                os.environ["FMP_API_KEY"] = _k
        except Exception:  # noqa: BLE001 — best-effort; absence just means env-only resolution
            pass
    return backend


def _find_npm() -> "str | None":
    """Locate npm: PATH first, then the standard Windows nodejs install."""
    import shutil
    for cand in ("npm", "npm.cmd"):
        p = shutil.which(cand)
        if p:
            return p
    for p in (r"C:\Program Files\nodejs\npm.cmd", r"C:\Program Files (x86)\nodejs\npm.cmd"):
        if os.path.isfile(p):
            return p
    return None


def _start_frontend(repo_root: str, port: int):
    """Launch the Vite dev server (npm run dev) as a subprocess. Returns the Popen or None."""
    import subprocess
    fe = os.path.join(repo_root, "frontend")
    if not os.path.isdir(os.path.join(fe, "node_modules")):
        print(f"ba2-test: frontend deps not installed; run `npm install` in {fe} first.")
        return None
    npm = _find_npm()
    if not npm:
        print("ba2-test: npm not found (install Node.js); cannot start the frontend.")
        return None
    env = dict(os.environ)
    # Node on PATH for the child (so vite's own node resolves).
    nodedir = os.path.dirname(npm)
    env["PATH"] = nodedir + os.pathsep + env.get("PATH", "")
    proc = subprocess.Popen([npm, "run", "dev", "--", "--port", str(port)], cwd=fe, env=env)
    print(f"frontend (vite)  -> http://localhost:{port}")
    return proc


def _cmd_serve(args) -> int:
    repo_root = os.path.dirname(os.path.abspath(__file__))
    mode = args.mode
    fe_proc = None
    if mode in ("both", "front"):
        fe_proc = _start_frontend(repo_root, args.frontend_port)

    if mode in ("both", "back"):
        try:
            import uvicorn
        except ImportError:
            if fe_proc:
                fe_proc.terminate()
            sys.exit("ba2-test: uvicorn not installed. Install backend/requirements.txt into this venv.")
        print(f"backend (api)    -> http://localhost:{args.port}  (docs: /docs)")
        try:
            uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
        finally:
            if fe_proc:
                fe_proc.terminate()
    elif mode == "front":
        if fe_proc is None:
            return 1
        try:
            fe_proc.wait()
        except KeyboardInterrupt:
            fe_proc.terminate()
    return 0


def _cmd_backtest(rest: list) -> int:
    # Full passthrough to the daily-backtest CLI (every flag it supports: --expert,
    # --universe, --start/--end, --interval, --seed, --initial-capital, --out, ...).
    from scripts.run_daily_backtest import main as bt_main
    return int(bt_main(rest) or 0)


def _cmd_fetch_cache(args) -> int:
    """Populate the as-of OHLCV cache. SYMBOLS are fetched in PARALLEL (a thread per symbol,
    ``--workers`` threads) — each symbol writes its OWN per-symbol cache file under its own
    lock, so concurrent DIFFERENT-symbol fetches are safe and a SAME-symbol race can't occur
    (one thread owns each symbol). The global FMP rate-limit gate (fmp_common) serialises/backs
    off so the extra concurrency never 429-storms. Per-symbol chunk-parallelism is kept small so
    total concurrency stays ~= workers x chunk."""
    from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch
    from concurrent.futures import ThreadPoolExecutor, as_completed
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    overall = {"fetched": [], "failed": []}
    n_workers = max(1, int(args.workers))
    chunk_workers = max(1, min(3, n_workers))  # per-symbol chunk threads (bounded)

    def _one(sym: str):
        payload = {
            "provider": args.provider, "symbol": sym, "timeframes": timeframes,
            "start_date": args.start, "end_date": args.end, "executor_workers": chunk_workers,
        }
        return sym, handle_ohlcv_cache_fetch(f"cli-fetch-{sym}", payload)

    done = 0
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        for fut in as_completed([ex.submit(_one, s) for s in symbols]):
            sym, res = fut.result()
            (overall["fetched"] if res.get("status") == "completed" else overall["failed"]).append({sym: res})
            done += 1
            if done % 25 == 0 or done == len(symbols):
                print(f"  fetch-cache: {done}/{len(symbols)} symbols "
                      f"({len(overall['failed'])} failed)", flush=True)
    print(json.dumps(overall, indent=2, default=str))
    return 0


def _cmd_prewarm(args) -> int:
    """Pre-build the per-symbol FMP history disk cache for the optimization-grid experts
    BEFORE the GA process pool spawns, so the first individuals read it from disk instead
    of each paying a cold network fetch.

    Mirrors how fetch-cache / the providers resolve the FMP key (env FMP_API_KEY, mirrored
    in from the trade app-settings DB by _enter_backend). Runs each expert's per-symbol
    history fetch in a ThreadPoolExecutor, INSIDE frozen_ttl_cache() so the BACKTEST-ONLY
    disk cache layer is engaged (the freeze gate is what enables disk writes; live passes
    through to the API). FactorRanker is skipped — its factor data is not disk-cached.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ba2_providers.fmp_common import frozen_ttl_cache, _fmp_history_cache_dir

    # Resolve the FMP key the same way the providers / fetch-cache do.
    key = os.getenv("FMP_API_KEY")
    if not key:
        try:
            from ba2_common.config import get_app_setting
            key = get_app_setting("FMP_API_KEY")
        except Exception:  # noqa: BLE001
            key = None
    if not key:
        sys.exit("ba2-test prewarm: FMP_API_KEY not configured (set it in .env or the app-settings DB).")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    if not symbols:
        sys.exit("ba2-test prewarm: --symbols is empty.")

    # end_date bounds only the in-Python filtering (the per-symbol histories are full
    # fetches), but thread it through for correctness. Default = now. Use a tz-aware
    # datetime to match the real cached_get path (datetime.now(timezone.utc)) — the
    # insider provider compares end_date against tz-aware filingDates, so a naive value
    # would raise inside its (post-fetch) filter (the disk cache is written either way).
    from datetime import timezone as _tz
    if args.end:
        end_date = datetime.fromisoformat(args.end)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=_tz.utc)
    else:
        end_date = datetime.now(_tz.utc)

    # Build the (expert, symbol) work items. Each item is a callable doing the cached fetch.
    from ba2_experts.FMPRating import (
        fetch_grades_historical_cached, fetch_price_target_history_cached,
    )
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider

    # Lazily construct the providers once (thread-safe enough: they only hold the API key
    # + do stateless reads through the shared disk cache).
    _details_provider = None
    _insider_provider = None

    def _do_fmprating(sym: str) -> None:
        fetch_grades_historical_cached(key, sym)
        fetch_price_target_history_cached(key, sym)

    def _do_earnings_drift(sym: str) -> None:
        nonlocal _details_provider
        if _details_provider is None:
            _details_provider = FMPCompanyDetailsProvider()
        _details_provider.get_past_earnings(
            sym, frequency="quarterly", end_date=end_date,
            lookback_periods=8, format_type="dict")

    def _do_insider(sym: str) -> None:
        nonlocal _insider_provider
        if _insider_provider is None:
            _insider_provider = FMPInsiderProvider()
        _insider_provider.get_insider_transactions(
            sym, end_date=end_date, lookback_days=400, as_of=end_date,
            format_type="dict")

    # FactorRanker (bypass/rebalance expert): warm ALL of its factor inputs by calling the SAME
    # data-layer fetchers the rebalance path uses (so coverage auto-tracks the real fetch surface
    # and can't drift). Per symbol this writes the fmp_history namespaces income_statement_annual /
    # balance_sheet_annual / cashflow_statement_annual (value+quality), past_earnings_quarterly +
    # earnings_estimates_quarterly (pead), AND the 1d OHLCV parquet (momentum + value as_of price).
    # All factor inputs are fetched regardless of weight because the GA varies factor_weight_* per
    # individual — any factor can be active. ohlcv_provider is intentionally omitted so the fetchers
    # construct an FMPOHLCVProvider() and the parquet path engages.
    # NOTE: this warms the FACTOR stage of the default static universe. It does NOT warm the
    # min_price universe price-guard or the live screener path — neither is reachable from the
    # static NDQ30 grid (FactorRanker pins universe_source=static; min_price/screener are not in its
    # optimize params). OHLCV is warmed only for ~400d ending at end_date; for a multi-bar backtest
    # span run `ba2-test fetch-cache --timeframes 1d` over [start-warmup, end] (reminder printed below).
    from ba2_experts.FactorRanker import data as _fr_data

    def _do_factorranker(sym: str) -> None:
        _fr_data.fetch_value_inputs([sym], as_of=end_date)    # income/balance/cashflow annual + OHLCV as_of price
        _fr_data.fetch_quality_inputs([sym], as_of=end_date)  # income/balance/cashflow annual (disk hits)
        _fr_data.fetch_pead_inputs([sym], as_of=end_date)     # past_earnings + earnings_estimates quarterly
        _fr_data.fetch_close_prices([sym], as_of=end_date)    # momentum: 1d OHLCV parquet

    _EXPERT_FETCHERS = {
        "FMPRating": _do_fmprating,
        "FMPEarningsDrift": _do_earnings_drift,
        "FMPInsiderClusterBuy": _do_insider,
        "FactorRanker": _do_factorranker,
    }

    work = []  # list of (expert, symbol, fetch_callable)
    for expert in experts:
        fetcher = _EXPERT_FETCHERS.get(expert)
        if fetcher is None:
            print(f">> skipping unknown expert '{expert}' (no disk-cached history fetcher)")
            continue
        for sym in symbols:
            work.append((expert, sym, fetcher))

    if not work:
        print("ba2-test prewarm: no disk-cached experts to pre-warm; nothing to do.")
        return 0

    counts = {}  # expert -> number of symbols successfully cached
    errors = 0
    t0 = time.time()
    # The freeze gate engages the BACKTEST-ONLY disk cache (live would pass through).
    with frozen_ttl_cache():
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futures = {ex.submit(fn, sym): (expert, sym) for (expert, sym, fn) in work}
            for fut in as_completed(futures):
                expert, sym = futures[fut]
                try:
                    fut.result()
                    counts[expert] = counts.get(expert, 0) + 1
                except Exception as e:  # noqa: BLE001 — one bad symbol must not abort
                    errors += 1
                    print(f"!! prewarm {expert}/{sym} failed: {e}")
    elapsed = time.time() - t0

    print("\n>> pre-warm summary")
    for expert in experts:
        print(f"   {expert}: {counts.get(expert, 0)}/{len(symbols)} symbols cached")
    print(f"   errors: {errors}")
    print(f"   elapsed: {elapsed:.1f}s")
    print(f"   cache dir: {_fmp_history_cache_dir()}")
    # FactorRanker's momentum/value factors read the 1d OHLCV PARQUET cache (separate from the
    # fmp_history JSON cache warmed above). This prewarm only warmed ~400d of 1d bars ending at
    # end_date; a multi-bar backtest rebalances across [start, end] and needs ~400d ending at EACH
    # bar. If the 1d parquet does not already span the full backtest range, also run:
    if "FactorRanker" in experts:
        print(
            "   note: FactorRanker also needs 1d OHLCV parquet spanning the full backtest range — "
            "if not already cached, run: ba2-test fetch-cache --symbols <universe> --timeframes 1d "
            "--start <backtest_start_minus_~450d> --end <backtest_end>"
        )
    return 0


def _cmd_build_screener_metrics(args) -> int:
    """Build/extend the screener METRIC store (parquet) from the as-of OHLCV cache.

    Wires ba2_providers.screener.metric_store.build_store to the as-of OHLCV cache
    (get_provider("ohlcv","fmp") + cached_get.ohlcv_get) and a per-symbol shares source.
    The FMP screener row carries no per-symbol method on the fundamentals-details provider
    (no shares_outstanding), so shares are derived from the screener row itself as
    marketCap / price (current-filing-ish), giving a meaningful as-of market_cap = shares ×
    close. If a row lacks usable marketCap/price, shares fall back to None (mcap -> NaN,
    acceptable for v1 per the plan)."""
    import app.models  # noqa: F401 — register ORM models on Base
    import pandas as _pd
    from datetime import datetime as _dt
    from app.models.database import init_db
    from ba2_common.config import get_app_setting
    from ba2_providers.screener import metric_store as ms
    from ba2_providers.cache.cached_get import ohlcv_get  # as-of OHLCV cache accessor
    from ba2_providers import get_provider
    init_db()
    api_key = os.getenv("FMP_API_KEY") or get_app_setting("FMP_API_KEY")
    if not api_key:
        sys.exit("build-screener-metrics: FMP_API_KEY not configured")

    # Shares map derived once from the screener rows (marketCap / price). The fundamentals
    # details provider exposes no shares_outstanding method, so this is the minimal, real
    # source of a latest-filing-ish share count without N extra per-symbol API calls.
    _shares_by_sym = {}
    for _r in ms._fetch_screener_rows(api_key):
        _sym = _r.get("symbol")
        _cap = _r.get("marketCap") or 0
        _px = _r.get("price") or 0
        if _sym and _cap > 0 and _px > 0:
            _shares_by_sym[_sym] = _cap / _px

    prov = get_provider("ohlcv", "fmp")

    def _ohlcv(sym, end):
        # The as-of OHLCV cache returns a DataFrame with a `Date` COLUMN + int index, rows not
        # guaranteed sorted, and `Date` parsed tz-AWARE (UTC). compute_daily_metrics expects a
        # tz-naive, ascending, date-INDEXED frame (and rolling needs ascending order), and the
        # scan grid is tz-naive — so normalize here (verified against the real cache in a perf
        # pass; the synthetic unit-test fixture was already clean so it didn't surface this).
        df = ohlcv_get(prov, sym, as_of=_dt.fromisoformat(end), lookback=4000)
        if df is None or len(df) == 0:
            return df
        idx = _pd.to_datetime(df["Date"])
        if idx.dt.tz is not None:
            idx = idx.dt.tz_localize(None)
        return df.set_index(idx).sort_index()

    def _shares(sym):
        return _shares_by_sym.get(sym)

    # Point-in-time fundamentals — fetched ONCE here, disk-cached by the metric_store helpers, and
    # baked into the store so the optimizer's per-day screen stays a pure in-memory filter:
    #  * market cap: FMP historical-market-capitalization (correct across buybacks/issuance/splits)
    #  * free float: FMP v4 historical/shares_float
    # ``shares_get`` stays only as the legacy mcap fallback for a symbol with no historical series.
    def _mcap(sym):
        return ms.fetch_historical_market_cap(sym, api_key, args.start, args.end)

    def _float(sym):
        return ms.fetch_historical_float(sym, api_key, args.start, args.end)

    os.makedirs(args.store, exist_ok=True)
    summary = ms.build_store(
        args.store, api_key, args.start, args.end,
        market_cap_min=args.market_cap_min, price_min=args.price_min, volume_min=args.volume_min,
        ohlcv_get=_ohlcv, mcap_get=_mcap, float_get=_float, shares_get=_shares,
        cadence_days=args.cadence_days, drop_days=args.drop_days,
        max_workers=getattr(args, "workers", 8) or 8)
    print(f"build-screener-metrics: {summary}")
    return 0


def _cmd_fetch_options(args) -> int:
    # Build the offline options cache from Alpaca. alpaca-py imports lazily inside
    # fetch_options.build_cache, so the editable venv (~/ba2-venvs/test) is required at runtime.
    from app.services.backtest import fetch_options
    from datetime import date
    unders = (open(args.underlyings[1:]).read().split() if args.underlyings.startswith("@")
              else [s.strip() for s in args.underlyings.split(",") if s.strip()])
    _parent = os.path.dirname(args.cache_db)
    if _parent:
        os.makedirs(_parent, exist_ok=True)
    fetch_options.build_cache(args.cache_db, unders, date.fromisoformat(args.start),
                              date.fromisoformat(args.end), args.feed)
    return 0


def _cmd_cache_usage(_args) -> int:
    from app.services.cache_manager import get_usage
    print(json.dumps(get_usage(), indent=2, default=str))
    return 0


def _cmd_cache_clear(args) -> int:
    from app.services import cache_manager
    before = datetime.fromisoformat(args.before) if args.before else None
    if args.type:
        res = cache_manager.clear_type(args.type, before=before)
    else:
        res = cache_manager.clear_all(before=before)
    print(json.dumps(res, indent=2, default=str))
    return 0


# --- backtest run tracking (the shared `backtests` results table) ----------------------
def _runs_db():
    # Ensure the results schema exists so `runs`/`--track` work even before the API's
    # first start (init_db = Base.metadata.create_all, same call the platform makes).
    import app.models  # noqa: F401 — registers all ORM models on Base
    from app.models.database import SessionLocal, init_db
    init_db()
    return SessionLocal()


# Fitness/sort metric -> Backtest column (higher = better for all of these).
_METRIC_COL = {
    "sharpe": "sharpe_ratio",
    "calmar": "calmar_ratio",
    "return": "total_return",
    "total_return": "total_return",
    "profit_factor": "profit_factor",
    "sortino": "sortino_ratio",
}


def _cmd_report(args) -> int:
    """Write an HTML summary of tracked backtests: per-expert best performer + counts,
    overall leaderboard, and per-optimization-job stats."""
    import html as _html
    from app.models.backtest import Backtest
    db = _runs_db()
    try:
        # Only the expert (daily_expert) optimization runs — the report's subject. Excludes the
        # legacy 'ml' engine fixtures (pytest e2e/repro/perf rows that land in the same DB with
        # degenerate 1-trade metrics and no expert_name), which otherwise drown out real results.
        rows = (db.query(Backtest)
                .filter(Backtest.status == "completed", Backtest.engine_type == "daily_expert")
                .order_by(Backtest.sharpe_ratio.desc()).all())
    finally:
        db.close()

    def esc(v):
        return _html.escape(str(v)) if v is not None else "-"

    def num(v, n=2):
        return f"{v:.{n}f}" if isinstance(v, (int, float)) else "-"

    # Per-expert grouping.
    by_expert: dict = {}
    for r in rows:
        by_expert.setdefault(r.expert_name or "(untagged)", []).append(r)

    parts = [
        "<!doctype html><meta charset='utf-8'><title>BA2 Backtest Report</title>",
        "<style>body{font:14px/1.5 system-ui,Segoe UI,Arial;margin:24px;color:#1e293b}"
        "h1{margin:0 0 4px}h2{margin:24px 0 8px;border-bottom:2px solid #e2e8f0;padding-bottom:4px}"
        "table{border-collapse:collapse;width:100%;margin:8px 0}"
        "th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:right}"
        "th:first-child,td:first-child,td.l{text-align:left}"
        "th{background:#f1f5f9}tr:nth-child(even){background:#f8fafc}"
        ".pos{color:#16a34a}.neg{color:#dc2626}.muted{color:#64748b}</style>",
        f"<h1>BA2 Backtest Optimization Report</h1>",
        f"<div class='muted'>Generated {datetime.now():%Y-%m-%d %H:%M} · "
        f"{len(rows)} completed run(s) · {len(by_expert)} expert(s)</div>",
    ]

    # Per-expert best performer + counts.
    parts.append("<h2>Per-expert summary (best by Sharpe)</h2>")
    parts.append("<table><tr><th>Expert</th><th>Runs</th><th>Best Sharpe</th>"
                 "<th>Best Return %</th><th>Best run</th><th>Trades</th></tr>")
    for expert, group in sorted(by_expert.items()):
        best = max(group, key=lambda r: (r.sharpe_ratio if r.sharpe_ratio is not None else -1e9))
        rc = "pos" if (best.total_return or 0) >= 0 else "neg"
        parts.append(
            f"<tr><td class='l'>{esc(expert)}</td><td>{len(group)}</td>"
            f"<td>{num(best.sharpe_ratio)}</td><td class='{rc}'>{num(best.total_return)}</td>"
            f"<td class='l'>#{best.id} {esc(best.name)}</td><td>{esc(best.total_trades)}</td></tr>")
    parts.append("</table>")

    # Overall leaderboard (top 20 by Sharpe).
    parts.append("<h2>Leaderboard (top 20 by Sharpe)</h2>")
    parts.append("<table><tr><th>#</th><th>Expert</th><th>Opt</th><th>Sharpe</th><th>Return %</th>"
                 "<th>MaxDD %</th><th>Win %</th><th>PF</th><th>Trades</th><th>Saved</th><th>Name</th></tr>")
    for r in rows[:20]:
        rc = "pos" if (r.total_return or 0) >= 0 else "neg"
        parts.append(
            f"<tr><td>{r.id}</td><td class='l'>{esc(r.expert_name)}</td>"
            f"<td>{esc(r.optimization_id)}</td><td>{num(r.sharpe_ratio)}</td>"
            f"<td class='{rc}'>{num(r.total_return)}</td><td>{num(r.max_drawdown)}</td>"
            f"<td>{num(r.win_rate,1)}</td><td>{num(r.profit_factor)}</td>"
            f"<td>{esc(r.total_trades)}</td><td>{'★' if r.is_saved else ''}</td>"
            f"<td class='l'>{esc(r.name)}</td></tr>")
    parts.append("</table>")

    # Per optimization-job stats.
    by_opt: dict = {}
    for r in rows:
        if r.optimization_id is not None:
            by_opt.setdefault(r.optimization_id, []).append(r)
    if by_opt:
        parts.append("<h2>Per optimization job</h2>")
        parts.append("<table><tr><th>Opt #</th><th>Expert</th><th>Trials</th>"
                     "<th>Best Sharpe</th><th>Avg Sharpe</th><th>Best Return %</th></tr>")
        for oid, group in sorted(by_opt.items()):
            shp = [r.sharpe_ratio for r in group if r.sharpe_ratio is not None]
            ret = [r.total_return for r in group if r.total_return is not None]
            exp = group[0].expert_name
            parts.append(
                f"<tr><td>{oid}</td><td class='l'>{esc(exp)}</td><td>{len(group)}</td>"
                f"<td>{num(max(shp)) if shp else '-'}</td>"
                f"<td>{num(sum(shp)/len(shp)) if shp else '-'}</td>"
                f"<td>{num(max(ret)) if ret else '-'}</td></tr>")
        parts.append("</table>")

    # Default INSIDE the repo (tracked ``reports/``) so the HTML is committed and syncs across
    # machines — not an out-of-tree absolute path. Resolve from this module's location (the
    # repo root), since _enter_backend() has chdir'd into backend/ by now.
    if args.out:
        out = args.out
    else:
        repo_root = os.path.dirname(os.path.abspath(__file__))
        out = os.path.join(repo_root, "reports", "ba2_backtest_report.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    print(f"wrote report -> {out} ({len(rows)} runs, {len(by_expert)} experts)")
    return 0


# Per-expert optimizable numeric decision settings (model:*) + the fixed (non-optimized)
# settings each expert still needs. RM params + TP/SL ranges are set on the Strategy below.
_EXPERT_OPT = {
    "FMPRating": {
        "expert_params": {
            "profit_ratio": {"optimize": True, "min": 0.5, "max": 1.5, "step": 0.1, "type": "float"},
            "min_analysts": {"optimize": True, "min": 5, "max": 25, "step": 5, "type": "int"},
            "price_target_window_days": {"optimize": True, "min": 30, "max": 180, "step": 30, "type": "int"},
            # CATEGORICAL: which analyst reference price the rating + the (S4) target-anchored TP
            # use. Optimized as a choice; the offset-from-target is the initial_tp gene (S4).
            "target_price_type": {"optimize": True, "type": "choice",
                                  "choices": ["low", "consensus", "median", "high", "low_consensus_avg"]},
        },
        "fixed_settings": {},
    },
    "FMPEarningsDrift": {
        "expert_params": {
            "surprise_min_pct": {"optimize": True, "min": 2.0, "max": 15.0, "step": 1.0, "type": "float"},
            "max_days_since_report": {"optimize": True, "min": 5, "max": 45, "step": 5, "type": "int"},
        },
        "fixed_settings": {},
    },
    "FMPInsiderClusterBuy": {
        "expert_params": {
            "lookback_days": {"optimize": True, "min": 30, "max": 120, "step": 15, "type": "int"},
            "min_insiders": {"optimize": True, "min": 2, "max": 6, "step": 1, "type": "int"},
        },
        "fixed_settings": {},
    },
    # NOTE: FinnHubRating is intentionally NOT optimized — it is REDUNDANT with FMPRating (both
    # are analyst-consensus rating experts on the same large-cap universe).
    # FMPSenateTraderWeight — congressional (senate) disclosed-trade signal. Sparse per symbol,
    # so it needs a BROAD universe where senators actually trade (NDQ30 is too narrow; assess a
    # wider list). Optimizes the disclosure/recency/consensus knobs.
    "FMPSenateTraderWeight": {
        "expert_params": {
            "max_disclose_date_days": {"optimize": True, "min": 15, "max": 60, "step": 5, "type": "int"},
            "max_trade_exec_days": {"optimize": True, "min": 30, "max": 120, "step": 15, "type": "int"},
            "max_trade_price_delta_pct": {"optimize": True, "min": 5.0, "max": 20.0, "step": 2.5, "type": "float"},
            "growth_confidence_multiplier": {"optimize": True, "min": 2.0, "max": 8.0, "step": 1.0, "type": "float"},
            "confidence_to_profit_factor": {"optimize": True, "min": 0.05, "max": 0.30, "step": 0.05, "type": "float"},
            "min_traders": {"optimize": True, "min": 1, "max": 4, "step": 1, "type": "int"},
            "min_trades": {"optimize": True, "min": 1, "max": 4, "step": 1, "type": "int"},
        },
        "fixed_settings": {},
    },
    # FactorRanker is a BYPASS expert: it ignores enter/exit rulesets and the classic RM, and
    # rebalances a portfolio by factor score. So its optimization searches ONLY the factor-model
    # params (one strategy, no S1/S2/S3 variants, no RM block). Marked bypass=True for the grid.
    "FactorRanker": {
        "expert_params": {
            "factor_weight_momentum": {"optimize": True, "min": 0.0, "max": 2.0, "step": 0.25, "type": "float"},
            "factor_weight_value": {"optimize": True, "min": 0.0, "max": 2.0, "step": 0.25, "type": "float"},
            "factor_weight_quality": {"optimize": True, "min": 0.0, "max": 2.0, "step": 0.25, "type": "float"},
            "factor_weight_pead": {"optimize": True, "min": 0.0, "max": 2.0, "step": 0.25, "type": "float"},
            "top_n": {"optimize": True, "min": 10, "max": 40, "step": 5, "type": "int"},
            "max_weight_per_name": {"optimize": True, "min": 0.05, "max": 0.20, "step": 0.05, "type": "float"},
        },
        "fixed_settings": {"universe_source": "static", "weighting": "equal"},
        "bypass": True,
    },
}


# Classic-RM sizing/stop params the RM reads off the expert. The optimizer now searches RM
# through the model:* namespace (keyed by the REAL ba2 setting names), merged into each
# expert's expert_params — there is no separate rm:* namespace. risk_per_trade_pct spans
# 0.5%..5%. (max_concurrent_positions is omitted: the engine has no enforcement hook for it.)
_RM_OPT = {
    "risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 5.0, "step": 0.5, "type": "float"},
    "atr_multiplier": {"optimize": True, "min": 1.5, "max": 4.0, "step": 0.5, "type": "float"},
    "min_stop_loss_pct": {"optimize": True, "min": 3.0, "max": 10.0, "step": 1.0, "type": "float"},
    "max_virtual_equity_per_instrument_percent": {"optimize": True, "min": 5.0, "max": 30.0, "step": 5.0, "type": "float"},
}

# Screener-settings genes (only added to the search when --screener is passed). The STATIC cap
# range is kept small — its loosest bound sizes the metric store's shortlist superset — while the
# dynamic ranges (RVOL / price-drop / max_stocks) may be wide. These are merged into expert_params
# pre-namespaced with `screener:` so collect_param_space / decode_params route them to the screener
# namespace (see _collect_screener / decode_params in strategy_param_space.py).
_SCREENER_OPT = {
    "screener_market_cap_min": {"min": 2e9, "max": 1e10, "step": 1e9, "type": "float", "optimize": True},
    "screener_relative_volume_min": {"min": 1.0, "max": 3.0, "step": 0.1, "type": "float", "optimize": True},
    "screener_price_drop_pct": {"min": 0.0, "max": 25.0, "step": 1.0, "type": "float", "optimize": True},
    "screener_max_stocks": {"min": 10, "max": 50, "step": 10, "type": "int", "optimize": True},
    # Weinstein stage-2 gate (price above a rising 30-week SMA = confirmed uptrend). Optimized
    # as a 0/1 toggle: the GA decides whether requiring Stage-2 helps. (A richer "which stage(s)"
    # categorical would need a multi-stage screener setting; stage2-only is the current knob.)
    "screener_weinstein_stage2_only": {"min": 0, "max": 1, "step": 1, "type": "int", "optimize": True},
}

# Per-cap-band jobs (small/mid/large): run the SAME screener gene set on a DISJOINT cap universe per
# band, so 5min stays feasible (each band's screened union is far smaller than the whole store) and
# small/mid/large get their own optimized settings. Only the market-cap gene RANGE + a fixed
# market_cap_max change per band; every other gene (RVOL / price-drop / max_stocks / weinstein) is
# unchanged. Selected via --screener-cap-band. Bands (current-cap $): small $50M-$2B, mid $2B-$10B,
# large >=$10B. The cap-min gene optimizes the floor WITHIN the band; market_cap_max pins the ceiling.
_SCREENER_CAP_BANDS = {
    "small": {"min": 5e7,  "max": 2e9,  "step": 1e8,  "cap_max": 2e9},
    "mid":   {"min": 2e9,  "max": 1e10, "step": 1e9,  "cap_max": 1e10},
    "large": {"min": 1e10, "max": 2e11, "step": 1e10, "cap_max": None},
}


def _build_strategy_row(name: str):
    """A Strategy whose TP/SL + the 5 classic-RM params (the RM's sizing/stop conditions &
    actions) are marked optimizable with ranges — the numeric RM space the optimizer searches."""
    from app.models.strategy import Strategy
    # Entry-gate tree: confidence + expected-profit thresholds, each value-optimizable AND
    # on/off-toggleable. The engine builds the enter ruleset from this (seed_ruleset_from_tree),
    # so these are the optimizer's "RM/entry conditions" — tuned thresholds + steps turned on/off.
    buy_entry_conditions = {
        "id": "root", "type": "AND", "conditions": [
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 80, "value_step": 5,
             "toggle_optimize": True},
            {"id": "gate_expected_profit", "field": "expected_profit", "op": ">", "value": 3,
             "optimize": True, "value_min": 0, "value_max": 15, "value_step": 1,
             "toggle_optimize": True},
            # Cooldown gates: only re-enter a symbol once N days have passed since the last
            # close (any / profitable / losing). Each is value-optimizable AND on/off-toggleable
            # so the optimizer can decide whether a cooldown helps and how long it should be.
            # 0 days never blocks; the optimizer can also turn the gate off entirely.
            {"id": "gate_days_since_close", "field": "days_since_last_close", "op": ">", "value": 0,
             "optimize": True, "value_min": 0, "value_max": 30, "value_step": 5,
             "toggle_optimize": True},
            {"id": "gate_days_since_profit", "field": "days_since_last_profitable_close", "op": ">",
             "value": 0, "optimize": True, "value_min": 0, "value_max": 30, "value_step": 5,
             "toggle_optimize": True},
            {"id": "gate_days_since_loss", "field": "days_since_last_losing_close", "op": ">",
             "value": 0, "optimize": True, "value_min": 0, "value_max": 60, "value_step": 10,
             "toggle_optimize": True},
        ],
    }
    # Exit (open_positions) ruleset: the dynamic-exit "movements", each a rule the backtest
    # evaluates via the real TradeActionEvaluator on the analysis cadence (identical to live).
    # Every rule is on/off-toggleable (toggle_optimize -> exit:<id>:enabled gene); numeric
    # condition thresholds (cond:<id>:value) and adjust-action %s (exit:<id>:action_value) are
    # value-optimized with steps. (The initial SL bracket stays via the sl gene; TAKE-PROFIT is a
    # toggleable profit_loss_percent CLOSE rule below — not a global initial-TP bracket, which never
    # fired in practice.)
    exit_conditions = [
        # Protective stop (condition form, matches live): while holding, set the SL at entry -X%
        # (adjust_stop_loss ref=order_open_price, negative offset). Value-optimized + toggleable.
        # Replaces the global SL bracket so exits are 100% condition-driven like the live engine.
        {"id": "exit_stoploss", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -6.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": -3.0, "action_value_step": 2.0,
         "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "sl_hold", "field": "has_position"}]}},
        # Take-profit (condition form): close once up +X%. Value-optimized + on/off-toggleable so the
        # GA tunes or disables it — replaces the dead global initial-TP bracket.
        {"id": "exit_takeprofit", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xtp", "field": "profit_loss_percent", "op": ">", "value": 20,
              "optimize": True, "value_min": 8, "value_max": 60, "value_step": 4}]}},
        # Close the position when the expert turns bearish (sell signal).
        {"id": "exit_bearish", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xb", "field": "bearish"}]}},
        # Close when the expert's current rating goes negative (downgrade exit).
        {"id": "exit_downgrade", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xd", "field": "current_rating_negative"}]}},
        # Profit-lock: once +X% in profit, move the stop to entry +lock% (break-even / lock-in).
        {"id": "exit_belock", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 0.0, "action_value_optimize": True,
         "action_value_min": -2.0, "action_value_max": 8.0, "action_value_step": 2.0,
         "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xlk", "field": "profit_loss_percent", "op": ">", "value": 5,
              "optimize": True, "value_min": 3, "value_max": 20, "value_step": 2}]}},
        # Time exit: close after N days held (caps dead-money holds).
        {"id": "exit_time", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xt", "field": "days_opened", "op": ">", "value": 60,
              "optimize": True, "value_min": 20, "value_max": 120, "value_step": 20}]}},
    ]
    return Strategy(
        name=name,
        buy_entry_conditions=buy_entry_conditions,
        exit_conditions=exit_conditions,
        # No global TP/SL brackets — exits are 100% condition-driven, matching the live engine
        # (which attaches no baseline bracket on entry). TP = the exit_takeprofit CLOSE rule; SL =
        # the exit_stoploss adjust_stop_loss rule above (both optimized + toggleable).
        initial_tp_percent=200.0, initial_tp_optimize=False,
        initial_sl_percent=200.0, initial_sl_optimize=False,
    )


# S2 is the canonical "bracket + light exits" strategy above; alias it for the strategy grid.
_build_strategy_S2 = _build_strategy_row


def _build_strategy_S3(name: str):
    """S3 — momentum / trailing. Light entry gate (confidence + expected-profit, optimized) and a
    STAGED TRAILING STOP exit (3 profit-tiers that ratchet the stop up, all optimized) + a time
    exit. NO fixed TP (a very wide, non-optimized cap) so winners run under the trail. Every value
    is optimizable and every rule is on/off-toggleable — no statics."""
    from app.models.strategy import Strategy
    buy_entry_conditions = {
        "id": "root", "type": "AND", "conditions": [
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 55,
             "optimize": True, "value_min": 40, "value_max": 80, "value_step": 5, "toggle_optimize": True},
            {"id": "gate_expected_profit", "field": "expected_profit", "op": ">", "value": 5,
             "optimize": True, "value_min": 0, "value_max": 15, "value_step": 1, "toggle_optimize": True},
        ],
    }
    # Staged trailing stop: as profit crosses each tier, raise the stop to entry +lock%. Tiers and
    # locks are optimized; rules toggle on/off. The time exit caps dead-money holds.
    exit_conditions = [
        # Initial protective stop (condition form, matches live): set SL at entry -X% while holding.
        # The trailing tiers below only ratchet it UP in profit, so this is the floor before them.
        {"id": "exit_stoploss", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -8.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": -3.0, "action_value_step": 2.0,
         "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "sl_hold", "field": "has_position"}]}},
        {"id": "trail_t1", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 1.0, "action_value_optimize": True,
         "action_value_min": -2.0, "action_value_max": 6.0, "action_value_step": 1.0, "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "t1", "field": "profit_loss_percent", "op": ">", "value": 6,
              "optimize": True, "value_min": 3, "value_max": 12, "value_step": 1}]}},
        {"id": "trail_t2", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 5.0, "action_value_optimize": True,
         "action_value_min": 2.0, "action_value_max": 12.0, "action_value_step": 2.0, "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "t2", "field": "profit_loss_percent", "op": ">", "value": 12,
              "optimize": True, "value_min": 8, "value_max": 20, "value_step": 2}]}},
        {"id": "trail_t3", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 12.0, "action_value_optimize": True,
         "action_value_min": 6.0, "action_value_max": 20.0, "action_value_step": 2.0, "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "t3", "field": "profit_loss_percent", "op": ">", "value": 20,
              "optimize": True, "value_min": 14, "value_max": 30, "value_step": 2}]}},
        {"id": "exit_time", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xt", "field": "days_opened", "op": ">", "value": 90,
              "optimize": True, "value_min": 30, "value_max": 150, "value_step": 30}]}},
    ]
    return Strategy(
        name=name,
        buy_entry_conditions=buy_entry_conditions,
        exit_conditions=exit_conditions,
        # No global TP/SL brackets — exits are 100% condition-driven (matches live). The protective
        # stop is the exit_stoploss adjust_stop_loss rule above; the trailing tiers ratchet it up.
        initial_tp_percent=200.0, initial_tp_optimize=False,
        initial_sl_percent=200.0, initial_sl_optimize=False,
    )


def _build_strategy_minimal(name: str):
    """A placeholder Strategy for BYPASS experts (FactorRanker) that ignore enter/exit rulesets and
    rebalance by factor score. The optimization still needs a Strategy row; this one carries no
    conditions and no TP/SL genes — all search lives in the expert's factor model:* params."""
    from app.models.strategy import Strategy
    return Strategy(name=name, buy_entry_conditions=None, exit_conditions=[])


# --- S1: the expert's LIVE ruleset (exported JSON), normalized to the launcher's canonical shape ---
def _s1_norm_leaf(leaf: dict) -> dict:
    """Importer leaf {field, comparison, value, optimize, optimize_enabled, value_min/max/step}
    -> canonical {field, op, value, optimize, value_min/max/step, toggle_optimize}."""
    out = {"id": leaf.get("id"), "field": leaf.get("field")}
    if leaf.get("value") is not None:
        out["op"] = leaf.get("comparison") or leaf.get("op") or ">"
        out["value"] = leaf.get("value")
    for k in ("optimize",):
        if leaf.get(k) is not None:
            out[k] = leaf[k]
    for k in ("value_min", "value_max", "value_step"):
        if leaf.get(k) is not None:
            out[k] = leaf[k]
    if leaf.get("optimize_enabled") is not None:
        out["toggle_optimize"] = leaf["optimize_enabled"]
    return out


def _s1_norm_tree(node: dict) -> dict:
    """Recursively normalize an importer condition tree (operator->type, comparison->op)."""
    if node is None:
        return None
    if node.get("conditions") is not None:  # group node
        return {
            "id": node.get("id", "grp"),
            "type": (node.get("operator") or node.get("type") or "AND"),
            "conditions": [_s1_norm_tree(c) for c in node["conditions"]],
        }
    return _s1_norm_leaf(node)  # leaf


def _s1_norm_exit_rule(rule: dict) -> dict:
    """Importer exit rule {action, action_value, reference_value, conditions{operator,...}} ->
    canonical {action_type, action_value(+optimize range for adjust rules), reference_value,
    conditions{type,...}, toggle_optimize}."""
    action = rule.get("action") or rule.get("action_type")
    out: dict = {
        "id": rule.get("id"),
        "action_type": action,
        "toggle_optimize": bool(rule.get("toggle_optimize", True)),
    }
    if rule.get("reference_value") is not None:
        out["reference_value"] = rule["reference_value"]
    # Adjust actions carry a % offset -> make it optimizable around the live value (no statics).
    if action in ("adjust_stop_loss", "adjust_take_profit") and rule.get("action_value") is not None:
        av = float(rule["action_value"])
        span = max(2.0, abs(av) * 0.6)
        out.update({
            "action_value": av, "action_value_optimize": True,
            "action_value_min": round(av - span, 2), "action_value_max": round(av + span, 2),
            "action_value_step": 1.0,
        })
    conds = rule.get("conditions")
    if conds is not None:
        out["conditions"] = {
            "type": (conds.get("operator") or conds.get("type") or "AND"),
            "conditions": [_s1_norm_leaf(c) if c.get("conditions") is None else _s1_norm_tree(c)
                           for c in conds.get("conditions", [])],
        }
    return out


def _build_strategy_S1(name: str, expert: str):
    """S1 — the expert's LIVE dev-account ruleset (exported to docs/live_rulesets/{expert}.json),
    normalized to the canonical Strategy shape with optimize flags on every threshold + adjust-%.
    Faithful to the live enter (buy/sell trees, OR groups preserved) + open_positions (exit) rules.
    An optimizable initial SL bracket is added; the entry-anchored global TP is left off (it never
    fires in practice — TP for FMPRating lives in the exit conditions, and S4 re-anchors TP on the
    analyst target price)."""
    import json as _json
    from app.models.strategy import Strategy
    repo_root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(repo_root, "docs", "live_rulesets", f"{expert}.json")
    if not os.path.isfile(path):
        sys.exit(f"optimize: S1 needs {path}; run `python backend/scripts/export_live_rulesets.py` first.")
    with open(path, encoding="utf-8") as f:
        data = _json.load(f)
    buy = _s1_norm_tree(data.get("buy_entry_conditions"))
    exits = [_s1_norm_exit_rule(r) for r in (data.get("exit_conditions") or [])]
    # NOTE: the launcher's Strategy has no separate sell tree — shorts are mirrored from the buy
    # gates via the engine's enable_short flag. The live sell_entry_conditions (if any) is dropped;
    # these experts are long-only in practice, so S1 runs long.
    return Strategy(
        name=name,
        buy_entry_conditions=buy,
        exit_conditions=exits,
        # No global TP/SL brackets — S1 runs the LIVE ruleset's conditions verbatim (its
        # adjust_stop_loss / adjust_take_profit / close rules), exactly like the live engine.
        initial_tp_percent=200.0, initial_tp_optimize=False,
        initial_sl_percent=200.0, initial_sl_optimize=False,
    )


def _build_strategy_S4(name: str, expert: str):
    """S4 — TARGET-TRAIL. Same live trailing ruleset as S1 (trail-TP-to-target rule live-34 +
    the trailing-SL ladder), but the INITIAL TP is ANCHORED on the expert's analyst target
    price (optimize-batch sets initial_tp_reference="expert_target_price" for S4) instead of
    off entry. The optimizable initial_tp gene becomes the OFFSET-FROM-TARGET and is allowed
    NEGATIVE (TP below target, e.g. -10 -> TP = target*0.90). Validated: target-anchoring rode
    NVDA to +1065% and exited via take_profit (~2.3x the entry-anchored return). Pairs with the
    optimizable target_price_type (which analyst reference price to anchor on)."""
    strat = _build_strategy_S1(name, expert)
    # Conditions-only target-anchoring (no global bracket — matches live): add an always-on rule
    # that sets the TP to the analyst target price while holding. The NEGATIVE-CAPABLE offset is the
    # optimizable gene — TP = target * (1 + offset/100), so e.g. -10 -> TP at 90% of target. This is
    # S4's distinguishing mechanism, now expressed as an exit condition (like live-34) rather than a
    # global initial-TP bracket. (Validated entry-anchored ~2.3x: rode NVDA to +1065% via take_profit.)
    strat.exit_conditions = list(strat.exit_conditions or []) + [
        {"id": "tp_target", "action_type": "adjust_take_profit", "reference_value": "expert_target_price",
         "action_value": 0.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": 10.0, "action_value_step": 2.0,
         "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "tp_hold", "field": "has_position"}]}},
    ]
    return strat


_STRATEGY_BUILDERS = {
    "S1": _build_strategy_S1,   # (name, expert)
    "S2": _build_strategy_S2,   # (name)
    "S3": _build_strategy_S3,   # (name)
    "S4": _build_strategy_S4,   # (name, expert) — target-anchored TP (expert_target_price)
}

# Strategy kinds whose INITIAL TP anchors on the expert's analyst target price (the
# optimize-batch run config sets initial_tp_reference="expert_target_price" for these).
_TARGET_ANCHORED_STRATEGIES = {"S4"}


def _build_strategy(kind: str, name: str, expert: str):
    """Dispatch to the right strategy builder. S1/S4 are expert-specific (load the live JSON)."""
    if kind in ("S1", "S4"):
        return _STRATEGY_BUILDERS[kind](name, expert)
    builder = _STRATEGY_BUILDERS.get(kind)
    if builder is None:
        sys.exit(f"optimize: unknown strategy {kind!r}; have {sorted(_STRATEGY_BUILDERS)}")
    return builder(name)


def _cmd_optimize(args) -> int:
    """Create a Strategy + StrategyOptimization and run a joint genetic optimization headless.

    Optimizes the expert's numeric decision settings + the 5 classic-RM params (sizing/stop
    'conditions & actions') + TP/SL, scored by --fitness, with parallel trials and suppressed
    per-trial logging. Persists the best trial as a tagged Backtest (optimization_id) and writes
    the HTML report.
    """
    from datetime import datetime as _dt
    import app.models  # noqa: F401 — register ORM models
    from app.models.database import SessionLocal, init_db
    from app.models.backtest import Backtest
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.backtest.daily_backtest_handler import derive_warmup_days
    from app.services.strategy_optimization_handler import handle_strategy_optimization

    expert = args.expert
    spec = _EXPERT_OPT.get(expert)
    if spec is None:
        sys.exit(f"ba2-test: optimize not configured for expert {expert!r}; have {sorted(_EXPERT_OPT)}")
    universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    if not universe:
        sys.exit("ba2-test: --universe must list at least one symbol")
    run_sched = None
    if args.run_schedule == "weekly":
        days = {d: (d == args.run_schedule_day) for d in
                ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
        # `times` pins the ANALYSIS to the scheduled time-of-day so on a 5min fill clock the
        # expert analyses ONCE/day (at market open) instead of every intraday bar. FMP bars are
        # stamped in market-local 09:30-15:55, so "09:30" is the first regular-session bar.
        run_sched = {"days": days, "times": ["09:30"]}

    init_db()
    db = SessionLocal()
    try:
        bypass = bool(spec.get("bypass"))
        _sname = args.name or f"opt-{expert}-{args.strategy}"
        # Bypass experts (FactorRanker) have no S1-S4 variants — they size their own portfolio, so
        # they use the minimal strategy and ignore --strategy. Classic experts build the chosen variant.
        strat = _build_strategy_minimal(_sname) if bypass else _build_strategy(args.strategy, _sname, expert)
        db.add(strat); db.commit(); db.refresh(strat)

        backtest_block = {
            "engine": "daily",
            "enabled_instruments": universe,
            "experts": [{"class": expert, "settings": dict(spec["fixed_settings"])}],
            "start_date": args.start, "end_date": args.end,
            "initial_capital": float(args.initial_capital),
            "account_settings": {
                "starting_cash": float(args.initial_capital),
                "commission_per_trade": float(args.commission),
                "slippage_bps": float(args.slippage),
                "fill_model": args.fill_model,
            },
            "warmup_days": derive_warmup_days([expert]),
            "seed": int(args.seed),
            "subtype": "daily_expert",
            "run_schedule_override": run_sched,
            "execution_interval": args.interval,
            "backtest_id": int(_dt.now().timestamp()),
            "name": f"opt-{expert}-trial",
        }

        # Screener-settings optimization: when --screener, attach a screener_opt block to the
        # backtest config (store + base settings + scan cadence — an OPTIMIZATION config option,
        # default weekly) and merge the screener genes into expert_params (pre-namespaced so the
        # param-space router sends them to the screener namespace). The run-level universe becomes
        # the metric store's FULL symbol union so the engine has OHLCV for any per-day pick.
        screener_genes: dict = {}
        if getattr(args, "screener", False):
            if not args.screener_store:
                sys.exit("optimize: --screener requires --screener-store")
            base = json.load(open(args.screener_base_json)) if args.screener_base_json else {}
            # Cap-band job: override the market-cap gene RANGE for the band and pin market_cap_max in
            # the base settings, so each band optimizes a DISJOINT, smaller cap universe (5min-feasible).
            # Other genes unchanged. Default (no band) keeps the original large-cap-floor behaviour.
            _scr_opt = _SCREENER_OPT
            _cap_band = getattr(args, "screener_cap_band", None)
            if _cap_band:
                _b = _SCREENER_CAP_BANDS[_cap_band]
                _scr_opt = dict(_SCREENER_OPT)
                _scr_opt["screener_market_cap_min"] = {"min": _b["min"], "max": _b["max"],
                                                       "step": _b["step"], "type": "float", "optimize": True}
                base = dict(base)
                if _b.get("cap_max") is not None:
                    base["market_cap_max"] = _b["cap_max"]
            backtest_block["screener_opt"] = {
                "store": args.screener_store,
                "base_settings": base,
                "cadence_days": int(args.screener_cadence_days),  # default 7 = weekly
                # BYPASS experts (e.g. FactorRanker) ignore the classic entry-gate path, so the
                # CLASSIC `screener_runtime` gate (which gates entries to the per-day screened
                # universe) has no effect on them. Instead they read `universe_source` /
                # `screener_store` / `screener_*` straight off their OWN expert settings to build
                # their DYNAMIC universe from the fast metric_store. This flag tells
                # `_build_daily_trial_config` to push the store + decoded screener genes onto the
                # bypass expert's per-trial settings each generation. For NON-bypass experts the
                # flag is False and only the classic `screener_runtime` path applies (unchanged).
                "apply_to_expert_settings": bool(spec.get("bypass")),
            }
            from ba2_providers.screener import metric_store as _ms
            _store_df = _ms.load_store(args.screener_store)
            if _store_df.empty:
                sys.exit(f"optimize: --screener-store {args.screener_store!r} has no symbols")
            # Preload only the symbols ANY individual could screen in — the union under the LOOSEST
            # end of every screener gene (most-admitting thresholds + max_stocks at its ceiling).
            # This is the correct superset for the whole population (tighter individuals select a
            # subset) and is far smaller than the raw store union (e.g. ~26-150 vs 868), so the
            # OHLCV preload doesn't load/hold ~800 never-selected symbols. The per-bar
            # screener_runtime gate still applies each individual's actual thresholds.
            _loosest = dict(base)   # base carries market_cap_max for a cap-band job
            _loosest.update({
                "market_cap_min": _scr_opt["screener_market_cap_min"]["min"],
                "relative_volume_min": _scr_opt["screener_relative_volume_min"]["min"],
                "price_drop_pct": _scr_opt["screener_price_drop_pct"]["min"],
                "weinstein_stage2_only": 0,
                "max_stocks": _scr_opt["screener_max_stocks"]["max"],
            })
            enabled = _ms.screened_symbol_union(_store_df, args.start, args.end, _loosest)
            if not enabled:
                sys.exit(f"optimize: --screener-store {args.screener_store!r} selected zero symbols "
                         f"for {args.start}..{args.end} under the loosest gene settings")
            # Drop screened symbols with NO cached OHLCV for the run interval. The backtest is
            # hermetic (never fetches mid-run), so preloading a symbol without bars hard-fails the
            # whole run. The metric store (built from DAILY) legitimately contains names with no
            # INTRADAY series — preferred shares / baby bonds (e.g. AQNB, DUKB, ELC) and a few thin
            # tickers. The screener can still rank them; we just can't fill what has no bars. Match
            # the native cache file CACHE_FOLDER/FMPOHLCVProvider/<SYM>_<interval>.parquet (with the
            # provider's symbol sanitisation: '-' -> '_'/'.').
            import os as _os
            from ba2_common.config import CACHE_FOLDER as _CF
            _cdir = _os.path.join(_CF, "FMPOHLCVProvider")
            _iv = args.interval
            def _has_bars(sym: str) -> bool:
                for cand in (sym, sym.replace("-", "_"), sym.replace("-", ".")):
                    if _os.path.exists(_os.path.join(_cdir, f"{cand}_{_iv}.parquet")):
                        return True
                return False
            _before = len(enabled)
            enabled = [s for s in enabled if _has_bars(s)]
            _dropped = _before - len(enabled)
            if _dropped:
                print(f"optimize: dropped {_dropped}/{_before} screened symbols with no cached "
                      f"{_iv} OHLCV (e.g. preferred/baby-bond tickers) -> {len(enabled)} tradeable.")
            if not enabled:
                sys.exit(f"optimize: 0 of the screened union has cached {_iv} OHLCV — fetch it first "
                         f"(ba2-test fetch-cache --timeframes {_iv} ...) or pick a different interval.")
            backtest_block["enabled_instruments"] = enabled
            universe = enabled  # for the progress line / submit description below
            screener_genes = {f"screener:{k}": v for k, v in _scr_opt.items()}

        # Target-anchored variant (S4): the initial TP bracket references the expert's analyst target
        # price (the initial_tp gene becomes the offset-from-target). Not applicable to bypass experts.
        if (not bypass) and args.strategy in _TARGET_ANCHORED_STRATEGIES:
            backtest_block["initial_tp_reference"] = "expert_target_price"
        cfg = {
            "populationSize": int(args.population),
            "generations": int(args.generations),
            "crossoverProb": 0.6, "mutationProb": 0.3,
            "earlyStoppingGenerations": int(args.early_stop),
            "elitismPercent": 0.1, "seed": int(args.seed),
            "parallelIndividuals": int(args.parallel),
            # Expert decision params (+ classic-RM sizing for ruleset experts; bypass experts size
            # their own portfolio so they carry NO RM block). Screener genes (screener:* namespace)
            # are merged in ONLY when --screener is set.
            "expert_params": ({**spec["expert_params"], **screener_genes} if bypass
                              else {**spec["expert_params"], **_RM_OPT, **screener_genes}),
            "backtest": backtest_block,
        }
        _worker_ids = _worker_ids_from_args(args)
        opt = StrategyOptimization(
            strategy_id=strat.id, name=args.name or f"opt-{expert}",
            fitness_metric=args.fitness, optimization_type="genetic",
            optimization_config=cfg, worker_ids=(_worker_ids or None), status="pending",
        )
        db.add(opt); db.commit(); db.refresh(opt)
        opt_id = opt.id
        if _worker_ids:
            print(f"optimize: distributing across worker ids {_worker_ids} + local")
        print(f"optimize: strategy #{strat.id} + StrategyOptimization #{opt_id} "
              f"({expert} x {len(universe)} syms, pop={args.population} gen={args.generations} "
              f"parallel={args.parallel} fitness={args.fitness})")
    finally:
        db.close()

    if getattr(args, "submit", False):
        # Enqueue on the SERVE process's DB-backed task queue (the running `ba2-test serve`
        # worker picks it up) so the job shows live in the UI's Running-jobs strip with
        # per-generation progress. NOTE: the serve handler does NOT yet persist the top-N as
        # tagged Backtests (that is the CLI in-process path's _persist_top_backtests / task #37),
        # so UI-launched runs land their result on the StrategyOptimization row; History
        # persistence of the top-N is a follow-up.
        from app.services.task_queue import get_task_queue
        task_id = get_task_queue().queue_task(
            task_type="strategy_optimization",
            name=args.name or f"opt-{expert}",
            payload={"optimization_id": opt_id},
            description=f"{expert} x {len(universe)} syms, {args.fitness}, pop={args.population} gen={args.generations}",
        )
        print(f"optimize: SUBMITTED to serve queue (task {task_id}, optimization_id={opt_id}). "
              f"Watch it in the UI: Backtesting -> History -> Running jobs.")
        return 0

    res = handle_strategy_optimization("cli-optimize", {"optimization_id": opt_id})
    if res.get("status") != "completed":
        print(json.dumps(res, indent=2, default=str))
        sys.exit(f"ba2-test: optimization {opt_id} did not complete")

    # Re-run the best params as ONE tracked, tagged Backtest so it lands in runs/report.
    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        print(f"optimize: done. best_fitness={opt.best_fitness} best_params={json.dumps(opt.best_params, default=str)}")
    finally:
        db.close()
    nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top))
    print(f"optimize: top {nsaved} persisted as tagged, saved Backtests (optimization_id={opt_id}); "
          f"run `ba2-test runs list --group {opt_id}` or `ba2-test report`.")
    return 0


def _cmd_optimize_batch(args) -> int:
    """Self-advancing optimization batch driver.

    Submits each expert's optimization to the RUNNING serve queue (so it shows live in the UI
    Running tab with per-generation progress), polls it to completion, then persists its top-N as
    tagged Backtests AND regenerates the HTML report before advancing to the next expert. Jobs run
    ONE AT A TIME (each gets the full process pool) to avoid oversubscribing the CPU. The serve
    process must be running (`ba2-test serve`); this driver only enqueues + polls + persists.
    """
    import time as _time
    from datetime import datetime as _dt
    from types import SimpleNamespace
    import app.models  # noqa: F401
    from app.models.database import SessionLocal, init_db
    from app.models.strategy import Strategy  # noqa: F401
    from app.models.strategy_optimization import StrategyOptimization
    from app.models.task_queue import TaskQueue
    from app.services.backtest.daily_backtest_handler import derive_warmup_days
    from app.services.task_queue import get_task_queue

    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    batch_worker_ids = _worker_ids_from_args(args)  # resolved once; applied to every job
    if batch_worker_ids:
        print(f"optimize-batch: distributing across worker ids {batch_worker_ids} + local")
    universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    if not universe:
        sys.exit("optimize-batch: --universe must list at least one symbol")
    for e in experts:
        if e not in _EXPERT_OPT:
            sys.exit(f"optimize-batch: expert {e!r} not configured; have {sorted(_EXPERT_OPT)}")
    # Build the (expert, strategy) job grid. Bypass experts (FactorRanker) have no enter/exit
    # rulesets, so they run ONCE (their factor-model params), not per strategy variant.
    jobs = []  # (expert, strategy_kind)
    for e in experts:
        if _EXPERT_OPT[e].get("bypass"):
            jobs.append((e, "FACTOR"))
        else:
            jobs.extend((e, k) for k in strategies)
    run_sched = None
    if args.run_schedule == "weekly":
        # `times` pins ANALYSIS to market open so a 5min fill clock analyses once/day, not per bar.
        run_sched = {"days": {d: (d == args.run_schedule_day) for d in
                              ("monday", "tuesday", "wednesday", "thursday", "friday",
                               "saturday", "sunday")},
                     "times": ["09:30"]}
    init_db()
    tq = get_task_queue()
    print(f"optimize-batch: {len(jobs)} job(s) {jobs} x {len(universe)} syms, "
          f"{args.fitness}, pop={args.population} gen={args.generations} parallel={args.parallel}")

    for n, (expert, strat_kind) in enumerate(jobs, 1):
        spec = _EXPERT_OPT[expert]
        bypass = bool(spec.get("bypass"))
        prefix = args.name_prefix or "phase1"
        name = f"{prefix}-{expert}-{strat_kind}-{args.fitness}"
        db = SessionLocal()
        try:
            strat = _build_strategy_minimal(name) if bypass else _build_strategy(strat_kind, name, expert)
            db.add(strat); db.commit(); db.refresh(strat)
            backtest_block = {
                "engine": "daily",
                "enabled_instruments": universe,
                "experts": [{"class": expert, "settings": dict(spec["fixed_settings"])}],
                "start_date": args.start, "end_date": args.end,
                "initial_capital": float(args.initial_capital),
                "account_settings": {
                    "starting_cash": float(args.initial_capital),
                    "commission_per_trade": float(args.commission),
                    "slippage_bps": float(args.slippage),
                    "fill_model": args.fill_model,
                },
                "warmup_days": derive_warmup_days([expert]),
                "seed": int(args.seed),
                "subtype": "daily_expert",
                "run_schedule_override": run_sched,
                "execution_interval": args.interval,
                "backtest_id": int(_dt.now().timestamp()),
                "name": f"{name}-trial",
            }
            # Target-anchored variants (S4): the INITIAL TP bracket references the expert's
            # analyst target price; the optimizable initial_tp gene is the (negative-capable)
            # offset-from-target. Other kinds keep the default percent-off-entry TP.
            if strat_kind in _TARGET_ANCHORED_STRATEGIES:
                backtest_block["initial_tp_reference"] = "expert_target_price"
            cfg = {
                "populationSize": int(args.population),
                "generations": int(args.generations),
                "crossoverProb": 0.6, "mutationProb": 0.3,
                "earlyStoppingGenerations": int(args.early_stop),
                "elitismPercent": 0.1, "seed": int(args.seed),
                "parallelIndividuals": int(args.parallel),
                # Bypass experts (FactorRanker) carry no classic-RM block (they size their own
                # portfolio); ruleset experts get the expert params + the RM sizing/stop params.
                "expert_params": (dict(spec["expert_params"]) if bypass
                                  else {**spec["expert_params"], **_RM_OPT}),
                "backtest": backtest_block,
            }
            opt = StrategyOptimization(
                strategy_id=strat.id, name=name, fitness_metric=args.fitness,
                optimization_type="genetic", optimization_config=cfg,
                worker_ids=(batch_worker_ids or None), status="pending",
            )
            db.add(opt); db.commit(); db.refresh(opt)
            opt_id = opt.id
        finally:
            db.close()

        task_id = tq.queue_task(
            task_type="strategy_optimization", name=name,
            payload={"optimization_id": opt_id},
            description=f"{expert} {strat_kind} x {len(universe)} syms, {args.fitness}, pop={args.population}",
        )
        print(f"[{n}/{len(jobs)}] SUBMITTED {expert}/{strat_kind} opt#{opt_id} (task {task_id}); polling every {args.poll}s...")

        last_msg = None
        st = "queued"
        while True:
            _time.sleep(int(args.poll))
            db = SessionLocal()
            try:
                t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
                st = t.status if t else "missing"
                pr = round((t.progress or 0), 1) if t else 0.0
                msg = ((t.progress_message if t else "") or "")
            finally:
                db.close()
            if msg != last_msg:
                print(f"    [{expert} opt#{opt_id}] {st} {pr}% {msg}")
                last_msg = msg
            if st in ("completed", "failed", "cancelled", "missing"):
                break

        if st != "completed":
            print(f"[{n}/{len(jobs)}] {expert} opt#{opt_id} ended status={st}; moving on.")
            continue

        try:
            nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top))
            print(f"[{n}/{len(jobs)}] {expert} opt#{opt_id} COMPLETE; persisted top {nsaved} backtests.")
        except Exception as exc:  # noqa: BLE001
            print(f"[{n}/{len(jobs)}] persist top-N failed for opt#{opt_id}: {exc}")
        try:
            _cmd_report(SimpleNamespace(out=None))
            print(f"    report regenerated.")
        except Exception as exc:  # noqa: BLE001
            print(f"    report regen failed: {exc}")

    print("optimize-batch: all jobs complete.")
    return 0


def _persist_top_backtests(opt_id: int, expert: str, n: int = 5) -> int:
    """Re-run the optimization's TOP-N distinct param sets and persist each as a tagged,
    saved Backtest (best params + their metrics) so the top performers are kept for
    comparison and to warm-start future optimizations. Returns how many were persisted."""
    import json as _json
    from datetime import datetime as _dt
    import app.models  # noqa: F401
    from app.models.database import SessionLocal
    from app.models.backtest import Backtest
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.strategy_optimization_handler import _build_daily_trial_config  # noqa: SLF001
    from app.services.backtest.daily_backtest_handler import run_daily_backtest, _persist_results
    from app.services.strategy_param_space import decode_params

    # The top-N re-runs invoke the full run_daily_backtest per saved backtest — the same
    # per-bar ruleset/RM/order INFO spam the GA loop already suppresses. Nobody reads those
    # logs during a headless optimize, so silence them here too (global disable short-circuits
    # before LogRecord creation; floor is INFO so a failed re-run still surfaces at WARNING+).
    import logging as _logging
    _prior_disable = _logging.root.manager.disable
    _logging.disable(_logging.INFO)

    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        strat = db.query(Strategy).filter_by(id=opt.strategy_id).first()
        cfg = opt.optimization_config or {}
        bt_block = dict(cfg["backtest"])

        # Top-N param sets by DISTINCT fitness (fall back to best_params if all_results is thin).
        # Dedup on fitness, not raw params: a converged GA yields many param sets that differ only
        # in INERT genes (e.g. exit:<id>:action_value while exit:<id>:enabled=0) yet score the same
        # and produce identical backtests — keying on params would persist N behaviourally-identical
        # rows. Distinct fitness gives genuinely different performers across the search landscape.
        seen, ranked = set(), []
        for r in sorted(opt.all_results or [], key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9), reverse=True):
            fit = r.get("fitness")
            key = round(fit, 6) if isinstance(fit, (int, float)) else _json.dumps(r.get("params"), sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            ranked.append(r["params"])
            if len(ranked) >= n:
                break
        if not ranked and opt.best_params:
            ranked = [opt.best_params]

        persisted = 0
        for rank, params in enumerate(ranked, start=1):
            decoded = decode_params(strat, params)
            trial_cfg = _build_daily_trial_config(bt_block, decoded)
            trial_cfg["name"] = f"TOP{rank}-{opt.name or expert}"
            # Persist this top-N run's trading DB (orders/transactions/recommendations) to disk
            # for post-mortem inspection — the GA trials run RAM-only for speed.
            trial_cfg["persist_trading_db"] = True
            results = run_daily_backtest(trial_cfg)
            # Store the raw optimized genes (for the "Optimized Parameters" display) AND the
            # CONCRETE decoded ruleset that actually ran (buy/sell/exit trees + TP/SL). The
            # latter lets Load/export restore the optimized conditions directly — no need to
            # reconstruct from genes + base tree. Keys mirror what _derive_export_payload reads.
            strategy_params = dict(params)
            if decoded.get("buy_tree") is not None:
                strategy_params["buyEntryConditions"] = decoded["buy_tree"]
            if decoded.get("sell_tree") is not None:
                strategy_params["sellEntryConditions"] = decoded["sell_tree"]
            if decoded.get("exit_rules") is not None:
                strategy_params["exitConditions"] = decoded["exit_rules"]
            if decoded.get("tp") is not None:
                strategy_params["initialTpPercent"] = decoded["tp"]
            if decoded.get("sl") is not None:
                strategy_params["initialSlPercent"] = decoded["sl"]
            bt = Backtest(
                name=trial_cfg["name"], model_id=None, engine_type="daily_expert",
                expert_name=expert, optimization_id=opt_id,
                strategy_params=strategy_params,
                start_date=_dt.fromisoformat(str(bt_block["start_date"])),
                end_date=_dt.fromisoformat(str(bt_block["end_date"])),
                initial_capital=float(bt_block["initial_capital"]),
                status="running", started_at=_dt.now(),
            )
            db.add(bt); db.commit(); db.refresh(bt)
            _persist_results(db, bt, results)
            bt.status = "completed"; bt.completed_at = _dt.now()
            bt.is_saved = True  # top performers of a job are kept
            db.commit()
            persisted += 1
        return persisted
    finally:
        db.close()
        _logging.disable(_prior_disable)


def _cmd_runs(args) -> int:
    from app.models.backtest import Backtest
    db = _runs_db()
    try:
        if args.runs_cmd == "prune":
            # Keep the best --keep runs per expert (by --metric, completed only); delete the
            # rest. Saved runs (is_saved) are ALWAYS kept and never counted against the budget.
            col = _METRIC_COL.get(args.metric)
            if col is None:
                sys.exit(f"ba2-test: unknown metric {args.metric!r}; use {sorted(_METRIC_COL)}")
            q = db.query(Backtest).filter(Backtest.status == "completed")
            if args.expert:
                q = q.filter(Backtest.expert_name == args.expert)
            rows = q.all()
            by_expert: dict = {}
            for r in rows:
                by_expert.setdefault(r.expert_name or "(none)", []).append(r)
            deleted = 0
            for expert, group in by_expert.items():
                keepers = [r for r in group if r.is_saved]
                cands = [r for r in group if not r.is_saved]
                cands.sort(key=lambda r: (getattr(r, col) if getattr(r, col) is not None else -1e9),
                           reverse=True)
                survivors = cands[: max(0, args.keep)]
                losers = cands[args.keep:]
                for r in losers:
                    db.delete(r)
                    deleted += 1
                print(f"{expert}: kept {len(survivors)} top + {len(keepers)} saved, "
                      f"deleted {len(losers)} (by {args.metric})")
            db.commit()
            print(f"-- pruned {deleted} run(s) total")
            return 0

        if args.runs_cmd == "stats":
            q = db.query(Backtest).filter(Backtest.status == "completed")
            if args.expert:
                q = q.filter(Backtest.expert_name == args.expert)
            if args.group is not None:
                q = q.filter(Backtest.optimization_id == args.group)
            rows = q.all()
            buckets: dict = {}
            key = (lambda r: r.optimization_id) if args.group is not None else (lambda r: r.expert_name or "(none)")
            for r in rows:
                buckets.setdefault(key(r), []).append(r)
            for k, group in sorted(buckets.items(), key=lambda kv: str(kv[0])):
                def _vals(c):
                    return [getattr(r, c) for r in group if getattr(r, c) is not None]
                shp = _vals("sharpe_ratio"); ret = _vals("total_return")
                best = max(shp) if shp else None
                avg = (sum(shp) / len(shp)) if shp else None
                label = ("opt#" + str(k)) if args.group is not None else str(k)
                print(f"{label}: n={len(group)} best_sharpe={best if best is None else round(best,2)} "
                      f"avg_sharpe={avg if avg is None else round(avg,2)} "
                      f"best_return={max(ret) if ret else None}")
            print(f"-- {len(rows)} run(s)")
            return 0

        if args.runs_cmd == "list":
            q = db.query(Backtest)
            if args.saved_only:
                q = q.filter(Backtest.is_saved == True)  # noqa: E712 (SQLAlchemy needs ==)
            if args.engine:
                q = q.filter(Backtest.engine_type == args.engine)
            if getattr(args, "expert", None):
                q = q.filter(Backtest.expert_name == args.expert)
            if getattr(args, "group", None) is not None:
                q = q.filter(Backtest.optimization_id == args.group)
            rows = q.order_by(Backtest.created_at.desc()).limit(args.limit).all()
            print(f"{'id':>5}  {'expert':<16} {'opt':>5} {'status':<10} {'ret%':>8} {'sharpe':>7} "
                  f"{'saved':<5} name")
            for r in rows:
                ret = f"{r.total_return:.2f}" if r.total_return is not None else "-"
                shp = f"{r.sharpe_ratio:.2f}" if r.sharpe_ratio is not None else "-"
                opt = str(r.optimization_id) if r.optimization_id is not None else "-"
                print(f"{r.id:>5}  {(r.expert_name or '-'):<16} {opt:>5} {(r.status or ''):<10} "
                      f"{ret:>8} {shp:>7} {('yes' if r.is_saved else 'no'):<5} {r.name}")
            print(f"-- {len(rows)} run(s)")
            return 0

        if args.runs_cmd == "save":
            r = db.query(Backtest).filter(Backtest.id == args.id).first()
            if r is None:
                sys.exit(f"ba2-test: run {args.id} not found")
            if args.name:
                r.name = args.name
            r.is_saved = True
            db.commit()
            print(f"saved run {r.id}: {r.name}")
            return 0

        if args.runs_cmd == "delete":
            r = db.query(Backtest).filter(Backtest.id == args.id).first()
            if r is None:
                sys.exit(f"ba2-test: run {args.id} not found")
            db.delete(r)
            db.commit()
            print(f"deleted run {args.id}")
            return 0

        if args.runs_cmd == "clear-unsaved":
            unsaved = db.query(Backtest).filter(Backtest.is_saved == False).all()  # noqa: E712
            n = len(unsaved)
            for r in unsaved:
                db.delete(r)
            db.commit()
            print(f"deleted {n} unsaved run(s)")
            return 0
        return 0
    finally:
        db.close()


# --- remote worker (PUSH model: the master dispatches trials to this server) ----------------
def _resolve_worker_names(names: list) -> list:
    """Resolve worker NAMES to {id,name,url,password} dicts against the local Worker table.

    The CLI runs on the master, so it reads the same DB the serve process configured. Exits if a
    name is unknown or has no URL/password (a worker must be added in the UI/API first)."""
    import app.models  # noqa: F401 — register ORM models
    from app.models.database import SessionLocal, init_db
    from app.models import Worker
    init_db()
    db = SessionLocal()
    try:
        out = []
        for n in names:
            w = db.query(Worker).filter(Worker.name == n, Worker.is_local == False).first()  # noqa: E712
            if not w:
                sys.exit(f"ba2-test: worker '{n}' not found (add it in Settings/the API first).")
            out.append({"id": w.id, "name": w.name, "url": w.url, "password": w.password})
        return out
    finally:
        db.close()


def _worker_ids_from_args(args) -> list:
    """Collect --worker (repeatable) + --workers a,b,c into a list of Worker ids (or [])."""
    names: list = []
    for n in (getattr(args, "worker", None) or []):
        names.append(n.strip())
    if getattr(args, "workers_csv", None):
        names.extend(s.strip() for s in args.workers_csv.split(",") if s.strip())
    names = [n for n in names if n]
    if not names:
        return []
    return [w["id"] for w in _resolve_worker_names(names)]


def _cmd_sync_cache(args) -> int:
    """Push the master's cache (diff, one tar stream) to a configured remote worker."""
    from app.services import worker_client
    worker = _resolve_worker_names([args.worker])[0]
    res = worker_client.push_cache(worker, log=print)
    print(json.dumps(res, indent=2, default=str))
    return 0


def _cmd_worker(args) -> int:
    """Run THIS machine as a remote worker SERVER the master pushes trials to.

    DB-less: exposes /run-trial (runs the hermetic backtest in a local process pool), /cache/push
    (receive the master's cache as a tar), /version + /update (stay in lock-step with the master),
    all gated by --password. The master dispatches trials and pushes cache to it.
    """
    from app.worker_server import run_worker_server
    password = args.password or os.getenv("BA2_WORKER_PASSWORD")
    if not password:
        sys.exit("ba2-test worker: --password (or $BA2_WORKER_PASSWORD) is required.")
    n_workers = args.workers or max(1, (os.cpu_count() or 2) - 1)
    run_worker_server(host=args.host, port=args.port, password=password, n_workers=n_workers)
    return 0


def main(argv: "list | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _enter_backend()

    # Default cache locations for the data-build commands. Resolved from the shared
    # ba2_common config so NOTHING is cached inside the repo: screener caches under
    # the trade bucket, options under common. All remain overridable via the flags.
    try:
        from ba2_common.config import (
            SCREENER_STORE_DIR as _DEFAULT_SCREENER_STORE_DIR,
            OPTIONS_CACHE_DB as _DEFAULT_OPTIONS_CACHE_DB,
        )
    except Exception:  # pragma: no cover - ba2_common always installed in practice
        _DEFAULT_SCREENER_STORE_DIR = None
        _DEFAULT_OPTIONS_CACHE_DB = None

    p = argparse.ArgumentParser(prog="ba2-test", description="BA2 Test Platform CLI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Launch the API and/or the React frontend.")
    s.add_argument("--mode", default="both", choices=["both", "back", "front"],
                   help="What to start: both (default), back (API only), front (Vite UI only).")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000, help="Backend API port (default 8000).")
    s.add_argument("--frontend-port", type=int, default=5173, help="Vite dev-server port (default 5173).")
    s.add_argument("--reload", action="store_true")

    # backtest: parse_known_args so the rest passes through to run_daily_backtest.
    sub.add_parser("backtest", help="Run a daily expert backtest (args forwarded to run_daily_backtest).",
                   add_help=False)

    fc = sub.add_parser("fetch-cache", help="Populate the as-of OHLCV cache.")
    fc.add_argument("--symbols", required=True, help="Comma-separated symbols.")
    fc.add_argument("--timeframes", default="1d", help="Comma-separated intervals (default 1d).")
    fc.add_argument("--start", required=True, help="ISO start date.")
    fc.add_argument("--end", required=True, help="ISO end date.")
    fc.add_argument("--provider", default="fmp", help="OHLCV provider (default fmp).")
    fc.add_argument("--workers", type=int, default=5)

    pw = sub.add_parser("prewarm",
                        help="Pre-build the per-symbol FMP history disk cache for the grid experts "
                             "(ratings/earnings/insider) before the GA pool spawns.")
    pw.add_argument("--symbols", required=True, help="Comma-separated symbols.")
    pw.add_argument("--experts", default="FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy",
                    help="Comma-separated experts to pre-warm (FactorRanker is skipped — not "
                         "disk-cached). Default: the 3 disk-cached history experts.")
    pw.add_argument("--workers", type=int, default=5, help="Parallel fetch threads (default 5).")
    pw.add_argument("--end", default=None,
                    help="ISO end date for the earnings/insider in-Python filter (default today).")

    bm = sub.add_parser("build-screener-metrics", help="Build/extend the screener METRIC store (parquet).")
    bm.add_argument("--store", default=_DEFAULT_SCREENER_STORE_DIR,
                    help=f"Path to the parquet metric-store dir (default {_DEFAULT_SCREENER_STORE_DIR}).")
    bm.add_argument("--start", required=True)
    bm.add_argument("--end", required=True)
    bm.add_argument("--market-cap-min", type=float, required=True, help="LOOSEST cap bound (shortlist superset).")
    bm.add_argument("--price-min", type=float, default=0.0)
    bm.add_argument("--volume-min", type=float, default=0.0)
    bm.add_argument("--cadence-days", type=int, default=7, help="Scan cadence in days (default 7 = weekly). Match the analysis schedule.")
    bm.add_argument("--drop-days", type=int, default=1)
    bm.add_argument("--workers", type=int, default=8,
                    help="Parallel per-symbol fetch threads (default 8). Historical market-cap + "
                         "float fetches are disk-cached, so re-builds are fast regardless.")

    fo = sub.add_parser("fetch-options", help="Build the offline options cache from Alpaca.")
    fo.add_argument("--underlyings", required=True, help="Comma-separated symbols, or @file.")
    fo.add_argument("--start", required=True, help="ISO start date (>= 2024-02-01).")
    fo.add_argument("--end", required=True, help="ISO end date.")
    fo.add_argument("--cache-db", default=_DEFAULT_OPTIONS_CACHE_DB,
                    help=f"Path to the options-history SQLite cache (default {_DEFAULT_OPTIONS_CACHE_DB}).")
    fo.add_argument("--feed", default="indicative", help="Option chain feed (default indicative).")

    cc = sub.add_parser("cache-clear", help="Clear cache (all, or one type).")
    cc.add_argument("--type", default=None, help="Cache type to clear (omit = all).")
    cc.add_argument("--before", default=None, help="Only clear entries older than this ISO date.")

    sub.add_parser("cache-usage", help="Show cache disk usage per type.")

    # runs: manage tracked backtest runs (the shared `backtests` results table).
    rp = sub.add_parser("runs", help="List / save / delete tracked backtest runs.")
    rsub = rp.add_subparsers(dest="runs_cmd", required=True)
    rl = rsub.add_parser("list", help="List tracked runs (newest first).")
    rl.add_argument("--limit", type=int, default=50)
    rl.add_argument("--saved-only", action="store_true", help="Only runs marked saved.")
    rl.add_argument("--engine", default=None, help="Filter by engine_type (ml/daily_expert).")
    rl.add_argument("--expert", default=None, help="Filter by expert_name.")
    rl.add_argument("--group", type=int, default=None, help="Filter by optimization_id.")
    rs = rsub.add_parser("save", help="Mark a run saved (survives clear-unsaved).")
    rs.add_argument("id", type=int)
    rs.add_argument("--name", default=None, help="Optionally rename the run.")
    rd = rsub.add_parser("delete", help="Delete one run by id.")
    rd.add_argument("id", type=int)
    rsub.add_parser("clear-unsaved", help="Delete all runs not marked saved.")
    rpr = rsub.add_parser("prune", help="Keep best N runs per expert (by metric); delete the rest.")
    rpr.add_argument("--keep", type=int, default=10, help="How many top runs to keep per expert.")
    rpr.add_argument("--metric", default="sharpe", help="Ranking metric (sharpe/calmar/return/...).")
    rpr.add_argument("--expert", default=None, help="Only prune this expert (else all).")
    rst = rsub.add_parser("stats", help="Per-expert (or per opt-job) summary stats.")
    rst.add_argument("--expert", default=None, help="Filter to one expert.")
    rst.add_argument("--group", type=int, default=None, help="Group by optimization_id (this job).")

    rep = sub.add_parser("report", help="Write an HTML summary of tracked backtests.")
    rep.add_argument("--out", default=None,
                     help="Output HTML path (default: <repo>/reports/ba2_backtest_report.html, "
                          "tracked in git so it syncs across machines).")

    op = sub.add_parser("optimize", help="Joint genetic optimization (expert + RM params + TP/SL).")
    op.add_argument("--expert", required=True, help="Expert class (FMPRating/FMPEarningsDrift/...).")
    op.add_argument("--strategy", choices=["S1", "S2", "S3", "S4"], default="S2",
                    help="Strategy/exit variant for a ruleset expert: S1 live-import / S2 bracket / "
                         "S3 trailing / S4 target-anchored. Ignored for bypass experts (FactorRanker).")
    op.add_argument("--universe", required=True, help="Comma-separated symbols.")
    op.add_argument("--start", required=True, help="ISO start date.")
    op.add_argument("--end", required=True, help="ISO end date.")
    op.add_argument("--fitness", default="sharpe_ratio", help="Fitness metric (default sharpe_ratio).")
    op.add_argument("--generations", type=int, default=6)
    op.add_argument("--population", type=int, default=10)
    op.add_argument("--parallel", type=int, default=4, help="Parallel trials (ThreadPoolExecutor).")
    op.add_argument("--early-stop", type=int, default=4)
    op.add_argument("--save-top", type=int, default=5,
                    help="Persist the top-N distinct param sets as saved Backtests (default 5).")
    op.add_argument("--seed", type=int, default=42, help="RNG seed (determinism).")
    op.add_argument("--initial-capital", type=float, default=10000.0)
    op.add_argument("--commission", type=float, default=1.0)
    op.add_argument("--slippage", type=float, default=0.0)
    op.add_argument("--fill-model", default="next_bar_open")
    op.add_argument("--interval", default="5min", help="Execution/fill clock interval (default 5min for "
                    "precise intraday TP/SL; analysis cadence is set by --run-schedule).")
    op.add_argument("--run-schedule", default="weekly", choices=["daily", "weekly"])
    op.add_argument("--run-schedule-day", default="monday")
    op.add_argument("--name", default=None)
    op.add_argument("--screener", action="store_true",
                    help="Optimize a screener-selected dynamic universe (screener:* genes). "
                         "Requires --screener-store; the run universe becomes the store's full "
                         "symbol union and entries are gated to each day's screened picks.")
    op.add_argument("--screener-store", default=None,
                    help="Path to the parquet metric store (build-screener-metrics).")
    op.add_argument("--screener-base-json", default=None,
                    help="JSON file of base (non-optimized) screener settings merged under the genes.")
    op.add_argument("--screener-cadence-days", type=int, default=7,
                    help="Scan cadence in days (default 7 = weekly). Must match the metric store's "
                         "build cadence; align with --run-schedule.")
    op.add_argument("--screener-cap-band", choices=["small", "mid", "large"], default=None,
                    help="Constrain the screener universe to a cap band (small $50M-$2B / mid $2B-$10B "
                         "/ large >=$10B): overrides the market-cap gene range + pins market_cap_max so "
                         "each band optimizes a smaller, disjoint universe (keeps 5min feasible). Other "
                         "genes unchanged. Run one job per band.")
    op.add_argument("--submit", action="store_true",
                    help="Enqueue on the running serve queue (live in the UI Running-jobs strip) "
                         "instead of running in-process. Submit jobs one at a time to avoid "
                         "process-pool oversubscription (the serve queue has 4 workers).")
    op.add_argument("--worker", action="append", default=None, metavar="NAME",
                    help="Remote worker NAME to fan trials out to (repeatable). Default: local only.")
    op.add_argument("--workers", dest="workers_csv", default=None, metavar="A,B,C",
                    help="Comma-separated remote worker names (alternative to repeated --worker).")

    ob = sub.add_parser("optimize-batch",
                        help="Self-advancing batch: submit each expert's optimization to the serve "
                             "queue, poll to completion, persist top-N + refresh report, then next.")
    ob.add_argument("--experts", default="FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy",
                    help="Comma-separated expert classes (default: the 3 in-scope equity experts).")
    ob.add_argument("--strategies", default="S1,S2,S3",
                    help="Comma-separated strategy variants per ruleset expert (S1 live-import / "
                         "S2 bracket / S3 trailing). Bypass experts (FactorRanker) ignore this.")
    ob.add_argument("--universe", required=True, help="Comma-separated symbols (shared by all jobs).")
    ob.add_argument("--start", required=True, help="ISO start date.")
    ob.add_argument("--end", required=True, help="ISO end date.")
    ob.add_argument("--fitness", default="calmar_ratio", help="Fitness metric (default calmar_ratio).")
    ob.add_argument("--generations", type=int, default=8)
    ob.add_argument("--population", type=int, default=40)
    ob.add_argument("--parallel", type=int, default=6, help="Process-pool workers per job.")
    ob.add_argument("--early-stop", type=int, default=4)
    ob.add_argument("--save-top", type=int, default=5)
    ob.add_argument("--seed", type=int, default=42)
    ob.add_argument("--initial-capital", type=float, default=10000.0)
    ob.add_argument("--commission", type=float, default=1.0)
    ob.add_argument("--slippage", type=float, default=0.0)
    ob.add_argument("--fill-model", default="next_bar_open")
    ob.add_argument("--interval", default="5min",
                    help="Fill-clock interval (default 5min for precise intraday TP/SL).")
    ob.add_argument("--run-schedule", default="weekly", choices=["daily", "weekly"])
    ob.add_argument("--run-schedule-day", default="monday")
    ob.add_argument("--name-prefix", default=None, help="Strategy/opt name prefix (default phase1-).")
    ob.add_argument("--poll", type=int, default=15, help="Poll interval seconds (default 15).")
    ob.add_argument("--worker", action="append", default=None, metavar="NAME",
                    help="Remote worker NAME to fan trials out to (repeatable). Default: local only.")
    ob.add_argument("--workers", dest="workers_csv", default=None, metavar="A,B,C",
                    help="Comma-separated remote worker names (alternative to repeated --worker).")

    # worker: run THIS machine as a worker SERVER the master pushes trials to.
    wk = sub.add_parser("worker", help="Run a worker server the master pushes GA trials to.")
    wk.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0).")
    wk.add_argument("--port", type=int, default=8100, help="Worker server port (default 8100).")
    wk.add_argument("--password", default=None,
                    help="Auth password the master must present (else $BA2_WORKER_PASSWORD).")
    wk.add_argument("--workers", type=int, default=None,
                    help="Trial process slots / capacity (default: CPU count - 1).")

    # sync-cache: push the master's cache (diff, one tar) to a configured remote worker.
    sc = sub.add_parser("sync-cache", help="Push the master's cache to a configured worker.")
    sc.add_argument("--worker", required=True, help="Worker NAME (as configured on the master).")

    # Split out the backtest passthrough before full parsing.
    if argv and argv[0] == "backtest":
        return _cmd_backtest(argv[1:])

    args = p.parse_args(argv)
    return {
        "serve": lambda: _cmd_serve(args),
        "fetch-cache": lambda: _cmd_fetch_cache(args),
        "prewarm": lambda: _cmd_prewarm(args),
        "build-screener-metrics": lambda: _cmd_build_screener_metrics(args),
        "fetch-options": lambda: _cmd_fetch_options(args),
        "cache-usage": lambda: _cmd_cache_usage(args),
        "cache-clear": lambda: _cmd_cache_clear(args),
        "runs": lambda: _cmd_runs(args),
        "report": lambda: _cmd_report(args),
        "optimize": lambda: _cmd_optimize(args),
        "optimize-batch": lambda: _cmd_optimize_batch(args),
        "worker": lambda: _cmd_worker(args),
        "sync-cache": lambda: _cmd_sync_cache(args),
    }[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
