"""``ba2-test`` — console CLI for the BA2 Test Platform (ML / backtest).

Installed as the ``ba2-test`` command (``pyproject.toml`` ``[project.scripts]``). It is a
subcommand dispatcher that runs against the ``backend/`` package (added to the path), so it
covers the platform's operations without the API:

  ba2-test serve [--host --port --reload]      launch the FastAPI API (uvicorn app.main:app)
  ba2-test backtest <run_daily_backtest args>  run a daily expert backtest (full passthrough)
  ba2-test fetch-cache --symbols .. [...]       populate the as-of OHLCV cache
  ba2-test build-screener-metrics --store .. [..] build the screener metric_store (parquet)
  ba2-test recompute-screener-drops [--store ..]  cache-only rebuild of price-drop columns (no FMP)
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


def _parse_symbols_arg(raw: str) -> list:
    """Comma list or ``@file`` (one symbol per line) — the idiom ``fetch-options``
    already uses for its ``--underlyings`` flag, extended to ``fetch-cache``/
    ``prewarm``'s ``--symbols`` so a large pinned universe file (e.g.
    ``tools/senate_universe.txt``, 498 symbols) doesn't need a giant comma line."""
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as f:
            return [s.strip().upper() for s in f.read().split() if s.strip()]
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


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
    symbols = _parse_symbols_arg(args.symbols)
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
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ba2_providers.fmp_common import (
        frozen_ttl_cache, _fmp_history_cache_dir, persist_empty_sentinel, set_ttl_frozen,
    )

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

    # FinnHub key (only required when warming FinnHubRating) — resolved like the expert does
    # (get_setting('finnhub_api_key')), with an env fallback.
    try:
        from ba2_common.core.db import get_setting as _get_setting
        finnhub_key = os.getenv("FINNHUB_API_KEY") or _get_setting("finnhub_api_key")
    except Exception:  # noqa: BLE001
        finnhub_key = os.getenv("FINNHUB_API_KEY")

    symbols = _parse_symbols_arg(args.symbols)
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
        fetch_analyst_grades_cached,
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
        fetch_analyst_grades_cached(key, sym)   # dated individual grades (rating-recency filter)

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

    # FMPSenateTraderWeight: warm the SAME fmp_history namespaces _gather reads — per-symbol
    # senate/house trades (congress_{chamber}_trades) + the symbol's full daily price history
    # (historical_price_full), plus each DISCLOSED trader's full history (congress_trader_history,
    # keyed by trader name, discovered from the trades). A bare instance (no DB row) carries just
    # the FMP key + a logger — all the fetch methods need. (The OHLCV current-price leg of _gather
    # is served by the fetch-cache parquet, not fmp_history, so it's out of prewarm's scope.)
    #
    # Dedup state is SHARED across every universe symbol's _do_senate call (not per-call locals):
    # the same prolific trader (e.g. one member of Congress with 10k+ disclosed trades) is
    # discovered from dozens of different universe symbols, and re-iterating their whole history
    # + re-warming their buy-symbols each time was pure wasted CPU (the disk/memory price cache
    # already prevented redundant NETWORK fetches, but not the redundant Python-side work of
    # getting there). Lock-guarded since the ThreadPoolExecutor runs _do_senate concurrently.
    _senate_expert = None
    _senate_seen_traders: set = set()
    _senate_warmed_skill_syms: set = set()
    _senate_lock = threading.Lock()

    def _do_senate(sym: str) -> None:
        nonlocal _senate_expert
        if _senate_expert is None:
            import logging as _lg
            from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight
            with _senate_lock:
                if _senate_expert is None:
                    s = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
                    s._api_key = key
                    s.logger = _lg.getLogger("senate-prewarm")
                    _senate_expert = s
        s = _senate_expert
        trades = (s._fetch_senate_trades(sym) or []) + (s._fetch_house_trades(sym) or [])
        s._get_price_at_date(sym, end_date)  # warms historical_price_full (full history, once)
        new_traders = []
        with _senate_lock:
            for trade in trades:
                name = s._trader_name(trade)
                if name and name not in _senate_seen_traders:
                    _senate_seen_traders.add(name)
                    new_traders.append(name)
        # Skill scoring (2026-07 upgrade) reads the price history of every symbol in each
        # trader's scored past BUYS. Warm ALL unique buy symbols — not just the most-recent-N
        # as of today — because at an early backtest as_of (e.g. 2022) the scorer's "most
        # recent completed buys" are OLDER trades whose symbols a today-anchored cap would
        # miss, hard-failing the hermetic run (FMPHistoryCacheMiss). Symbols FMP has no data
        # for (delisted/bonds) persist as the [] sentinel, which the scorer skips cleanly.
        for name in new_traders:
            history = s._fetch_trader_history(name) or []  # warms congress_trader_history (once)
            new_skill_syms = []
            with _senate_lock:
                for t in history:
                    ttype = str(t.get('type', '')).lower()
                    if 'purchase' not in ttype and 'buy' not in ttype:
                        continue
                    ssym = str(t.get('symbol', '')).upper()
                    if ssym and ssym not in _senate_warmed_skill_syms:
                        _senate_warmed_skill_syms.add(ssym)
                        new_skill_syms.append(ssym)
            for ssym in new_skill_syms:
                s._get_price_at_date(ssym, end_date)  # warms historical_price_full (once, ever)

    # FinnHubRating: warm the per-symbol finnhub_reco_trends namespace. Bare instance carries the
    # Finnhub key + a logger (all _fetch_recommendation_trends needs).
    _finnhub_expert = None

    def _do_finnhub(sym: str) -> None:
        nonlocal _finnhub_expert
        if _finnhub_expert is None:
            if not finnhub_key:
                sys.exit("ba2-test prewarm: finnhub_api_key not configured (set FINNHUB_API_KEY or "
                         "the app-setting) — required to warm FinnHubRating.")
            import logging as _lg
            from ba2_experts.FinnHubRating import FinnHubRating
            e = FinnHubRating.__new__(FinnHubRating)
            e._api_key = finnhub_key
            e.logger = _lg.getLogger("finnhub-prewarm")
            _finnhub_expert = e
        _finnhub_expert._fetch_recommendation_trends(sym)

    _EXPERT_FETCHERS = {
        "FMPRating": _do_fmprating,
        "FMPEarningsDrift": _do_earnings_drift,
        "FMPInsiderClusterBuy": _do_insider,
        "FactorRanker": _do_factorranker,
        "FMPSenateTraderWeight": _do_senate,
        "FinnHubRating": _do_finnhub,
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
    # persist_empty_sentinel(): a symbol FMP genuinely has no data for is cached as ``[]`` so it
    # reads back as "checked, no data" (not the fatal "not pre-warmed" of an absent file).
    #
    # CRITICAL: frozen_ttl_cache()/set_ttl_frozen() set a THREAD-LOCAL flag (by design — a live
    # backtest thread must never see a sibling thread's freeze state). threading.local() does
    # NOT propagate into a ThreadPoolExecutor's worker threads, so entering frozen_ttl_cache()
    # only in this (main) thread left every submitted fn() running UN-frozen: real network
    # fetches happened but fmp_history_disk_cached() took the "live: never persist" branch on
    # every call — a prewarm run could burn the full FMP rate-limit budget and write ZERO cache
    # files. The initializer runs ONCE per worker thread (before it processes any task) and sets
    # the SAME thread-local flag from inside that thread, so every task the pool ever runs on it
    # sees frozen=True. persist_empty_sentinel's flag is a plain module global (not thread-local)
    # so it already applied to worker threads correctly — only ttl_frozen needed this.
    with frozen_ttl_cache(), persist_empty_sentinel():
        with ThreadPoolExecutor(max_workers=max(1, args.workers),
                                initializer=set_ttl_frozen, initargs=(True,)) as ex:
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
        max_lookback=getattr(args, "max_lookback", 30) or 30,
        max_workers=getattr(args, "workers", 8) or 8)
    print(f"build-screener-metrics: {summary}")
    return 0


def _cmd_recompute_screener_drops(args) -> int:
    """CACHE-ONLY rebuild of ONLY the price-drop columns of an existing screener store — no FMP.

    Reads each store symbol's DAILY OHLCV straight from the native parquet cache
    (CACHE_FOLDER/FMPOHLCVProvider/<SYM>_1d.parquet) — NEVER calls the network — recomputes the
    legacy ``price_drop_pct`` (window --drop-days) AND the per-window ``price_drop_pct_2..max``
    columns, and writes them back in place (other metrics untouched). Use it to (1) fix a store
    built with a degenerate window (the old drop_days=1 -> all-zero bug) and (2) add the
    multi-window columns needed to OPTIMIZE the price-drop lookback Y, without re-fetching."""
    import app.models  # noqa: F401 — register ORM models on Base
    import pandas as _pd
    import ba2_common.config as _cfg
    from app.models.database import init_db
    from ba2_providers.screener import metric_store as ms
    init_db()
    store = args.store or _cfg.SCREENER_STORE_DIR
    if not os.path.isdir(store):
        sys.exit(f"recompute-screener-drops: store not found: {store}")
    ohlcv_dir = os.path.join(_cfg.CACHE_FOLDER, "FMPOHLCVProvider")

    def _ohlcv_cached(sym):
        # Direct parquet read = guaranteed offline (bypasses the provider's fetch-on-miss). Same
        # normalization the build's _ohlcv applies: tz-naive, ascending, date-indexed.
        p = os.path.join(ohlcv_dir, f"{sym}_1d.parquet")
        if not os.path.isfile(p):
            return None
        df = _pd.read_parquet(p)
        if df is None or len(df) == 0 or "Date" not in df.columns:
            return None
        idx = _pd.to_datetime(df["Date"])
        if idx.dt.tz is not None:
            idx = idx.dt.tz_localize(None)
        return df.set_index(idx).sort_index()

    summary = ms.recompute_price_drop_columns(
        store, _ohlcv_cached,
        max_lookback=getattr(args, "max_lookback", 30) or 30,
        drop_days=getattr(args, "drop_days", 5) or 5)
    print(f"recompute-screener-drops: {summary}")
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
    # Resolve Alpaca MARKET-DATA creds from the app-settings DB (stored lowercase, e.g.
    # alpaca_market_api_key) and pass them explicitly, mirroring the FMP_API_KEY mirror in
    # _bootstrap. Without this, build_cache only finds creds if the launching SHELL happened to
    # export ALPACA_MARKET_API_KEY/_SECRET — which a fresh CLI/background relaunch does not.
    _ak = _as = None
    try:
        from ba2_common.config import get_app_setting
        _ak = (get_app_setting("alpaca_market_api_key") or get_app_setting("alpaca_api_key")
               or os.getenv("ALPACA_MARKET_API_KEY") or os.getenv("ALPACA_API_KEY"))
        _as = (get_app_setting("alpaca_market_api_secret") or get_app_setting("alpaca_api_secret")
               or os.getenv("ALPACA_MARKET_API_SECRET") or os.getenv("ALPACA_SECRET_KEY"))
    except Exception:  # noqa: BLE001 — fall back to build_cache's own env-based resolution
        pass
    stats = fetch_options.build_cache(
        args.cache_db, unders, date.fromisoformat(args.start), date.fromisoformat(args.end),
        args.feed, api_key=_ak, api_secret=_as,
        max_workers=args.workers, resume=not args.no_resume, paper=not args.live)
    print(json.dumps(stats, indent=2))
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


def _daily_manage_schedule() -> dict:
    """Open-positions MANAGEMENT schedule for the backtest: EVERY weekday at the open bar.

    Mirrors live, where ``open_positions`` is scheduled independently of (and far more often than)
    ``enter_market``. Used as ``manage_schedule_override`` so the engine evaluates exit/trailing/
    days_opened rules each trading day even when ENTRY is weekly. (Weekends are off — no session.)"""
    wk = ("monday", "tuesday", "wednesday", "thursday", "friday")
    days = {d: (d in wk) for d in (*wk, "saturday", "sunday")}
    return {"days": days, "times": ["09:30"]}


# Per-expert optimizable numeric decision settings (model:*) + the fixed (non-optimized)
# settings each expert still needs. RM params + TP/SL ranges are set on the Strategy below.
_EXPERT_OPT = {
    "FMPRating": {
        "expert_params": {
            "profit_ratio": {"optimize": True, "min": 0.5, "max": 1.5, "step": 0.1, "type": "float"},
            "min_analysts": {"optimize": True, "min": 5, "max": 25, "step": 5, "type": "int"},
            "price_target_window_days": {"optimize": True, "min": 30, "max": 180, "step": 30, "type": "int"},
            # Min analyst price targets behind the consensus (degenerate-consensus guard). Range
            # INCLUDES 0 ("no check") so the GA compares gating thinly-targeted names (e.g. the ASC
            # 1-analyst $19 consensus) against not gating. Grid searches {0,3,6}.
            "min_price_targets_per_quarter": {"optimize": True, "min": 0, "max": 6, "step": 3, "type": "int"},
            # Rating-recency window (months): when >0, min_analysts counts only analysts active
            # within this many months (from dated individual grades). 0 = no recency filter (full
            # standing-analyst count). Grid searches {0,3,6,9,12}.
            "max_analyst_age_months": {"optimize": True, "min": 0, "max": 12, "step": 3, "type": "int"},
            # CATEGORICAL: which analyst reference price the rating + the (S4) target-anchored TP
            # use. Optimized as a choice; the offset-from-target is S4's entry_actions TP gene
            # (entry:s4_tp:action_value).
            "target_price_type": {"optimize": True, "type": "choice",
                                  "choices": ["low", "consensus", "median", "high", "low_consensus_avg"]},
        },
        "fixed_settings": {"sizing_mode": "risk_atr"},
    },
    "FMPEarningsDrift": {
        "expert_params": {
            "surprise_min_pct": {"optimize": True, "min": 2.0, "max": 15.0, "step": 1.0, "type": "float"},
            "max_days_since_report": {"optimize": True, "min": 5, "max": 45, "step": 5, "type": "int"},
        },
        "fixed_settings": {"sizing_mode": "risk_atr"},
    },
    "FMPInsiderClusterBuy": {
        "expert_params": {
            "lookback_days": {"optimize": True, "min": 30, "max": 120, "step": 15, "type": "int"},
            "min_insiders": {"optimize": True, "min": 2, "max": 6, "step": 1, "type": "int"},
        },
        "fixed_settings": {"sizing_mode": "risk_atr"},
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
            # Scoring-model knobs (2026-07 upgrade): trade-size filter/boost, sell-side
            # discount, focus cap, trader-count consensus, and historical trader-skill
            # weighting (hit rate of past disclosed buys over a forward horizon).
            "min_trade_amount": {"optimize": True, "min": 0.0, "max": 100000.0, "step": 25000.0, "type": "float"},
            "sell_signal_weight": {"optimize": True, "min": 0.0, "max": 1.0, "step": 0.25, "type": "float"},
            "symbol_focus_cap_pct": {"optimize": True, "min": 5.0, "max": 25.0, "step": 5.0, "type": "float"},
            "size_boost_max": {"optimize": True, "min": 0.0, "max": 30.0, "step": 10.0, "type": "float"},
            "consensus_bonus_per_trader": {"optimize": True, "min": 0.0, "max": 4.0, "step": 1.0, "type": "float"},
            "skill_horizon_days": {"optimize": True, "min": 30, "max": 90, "step": 30, "type": "int"},
            "skill_signal_weight": {"optimize": True, "min": 0.0, "max": 1.0, "step": 0.25, "type": "float"},
            "skill_confidence_weight": {"optimize": True, "min": 0.0, "max": 20.0, "step": 5.0, "type": "float"},
        },
        "fixed_settings": {"sizing_mode": "risk_atr"},
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
# expert's expert_params — there is no separate rm:* namespace. (max_concurrent_positions is
# omitted: the engine has no enforcement hook for it.)
#
# Widened for the "aggressive" pass (2026-07-01): the -tpsl grid's real safeguard stop cut
# both drawdown AND return vs the old (effectively-unprotected) baseline. risk_per_trade_pct
# max raised 5%->10% (bigger dollar risk per trade), atr_multiplier floor raised 1.5->3.0
# (a tight ATR multiple was the likely whipsaw driver — 50-71% of exits were stop_loss) with
# its ceiling extended 4.0->6.0, and min_stop_loss_pct ceiling raised 10%->15% so the floor
# itself can sit wider when ATR is disabled. use_atr_stop lets the GA drop the ATR leg of the
# stop entirely and rely purely on risk_per_trade_pct% (still floored at min_stop_loss_pct%),
# for symbols/regimes where ATR-implied stops are tighter than the risk% budget would allow.
_RM_OPT = {
    "risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 10.0, "step": 0.5, "type": "float"},
    "atr_multiplier": {"optimize": True, "min": 3.0, "max": 6.0, "step": 0.5, "type": "float"},
    "atr_period": {"optimize": True, "min": 7, "max": 28, "step": 7, "type": "int"},
    "min_stop_loss_pct": {"optimize": True, "min": 3.0, "max": 15.0, "step": 1.0, "type": "float"},
    "use_atr_stop": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
    "max_virtual_equity_per_instrument_percent": {"optimize": True, "min": 5.0, "max": 30.0, "step": 5.0, "type": "float"},
}

# Per-weekday entry-scan ON/OFF toggle genes (schedule:<day>): merged into expert_params
# pre-namespaced with `schedule:` so collect_param_space/decode_params route them to the schedule
# namespace (see _collect_schedule_days/decode_params in strategy_param_space.py). Replaces a
# single fixed --run-schedule-day pin with a per-individual, per-day search — the GA discovers
# which day(s) work best (e.g. a fast-decaying signal might want monday+thursday) instead of a
# hand-picked cadence. decode_params enforces at least one day stays ON. Applies to every
# non-bypass strategy (S1-S7); bypass experts (FactorRanker) don't use the classic per-day
# entry-scan gate at all, so they never get this merged in (see _cmd_optimize/_cmd_optimize_batch).
_SCHEDULE_DAY_OPT = {
    day: {"optimize": True}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
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
    # Lookback window Y (trading days) for the price-drop gate: selects the precomputed
    # price_drop_pct_<Y> column in a multi-window store (build with --max-lookback >= this max).
    # Decodes to a bare `price_drop_days` screener override. Cap 30 matches the default max_lookback.
    "screener_price_drop_days": {"min": 2, "max": 30, "step": 1, "type": "int", "optimize": True},
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


def _strategy_from_parts(name: str, buy_tree=None, exit_conditions=None, entry_actions=None):
    """Build a Strategy row on the UNIFIED RULE MODEL (migration 028) from the launcher's
    declarative legacy parts: the buy condition tree, single-action exit rows and the flat
    entry-bracket list convert via the shared ``trade_rules_from_legacy`` (one entry TradeRule
    per OR branch, base bullish+flat gates made explicit, bracket replicated per rule, exit
    rows lifted to one-action rules) — semantically identical to what the old seeder did
    implicitly, now explicit in the stored rules."""
    from app.models.strategy import Strategy
    from ba2_common.core.rule_models import trade_rules_from_legacy

    converted = trade_rules_from_legacy(
        buy_tree=buy_tree, entry_actions=entry_actions, exit_conditions=exit_conditions,
    )
    return Strategy(name=name, entry_rules=converted["entry_rules"],
                    exit_rules=converted["exit_rules"])


def _first_match_order(exit_conditions, order):
    """Reorder exit rules for the engine's FIRST-MATCH semantics (a matching rule stops
    evaluation unless continue_processing).

    The post-tp/sl-rework S2/S3/S5 lists put the always-matching floor stop (condition: just
    ``has_position``) FIRST — which silently shadowed every rule after it (take-profit, signal
    closes, trailing tiers, time exit could never fire). Correct first-match layout: signal/
    profit CLOSES first (they only match on their trigger), trailing tiers DEEPEST-first (the
    most aggressive applicable stop wins), break-even lock next, and the always-matching floor
    stop LAST as the pure fallback. ``order`` lists rule ids in the intended sequence; ids not
    listed keep their relative order after the listed ones (fail-loud on a listed id that
    doesn't exist, so a renamed rule can't silently fall out of order)."""
    by_id = {r.get("id"): r for r in exit_conditions if isinstance(r, dict)}
    missing = [rid for rid in order if rid not in by_id]
    if missing:
        raise ValueError(f"_first_match_order: unknown exit rule id(s) {missing}")
    ordered = [by_id[rid] for rid in order]
    ordered += [r for r in exit_conditions
                if isinstance(r, dict) and r.get("id") not in set(order)]
    return ordered


def _build_strategy_row(name: str):
    """A Strategy whose TP/SL + the 5 classic-RM params (the RM's sizing/stop conditions &
    actions) are marked optimizable with ranges — the numeric RM space the optimizer searches."""
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
        # ALWAYS ON (no toggle_optimize): this is S2's only condition-based floor protection before
        # exit_belock's break-even lock kicks in at +profit. Same class of vulnerability found in
        # S3 (the GA could disable its only floor stop to game the fitness) — tightness
        # (action_value_*) stays optimizable, only the on/off toggle is removed.
        {"id": "exit_stoploss", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -6.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": -3.0, "action_value_step": 2.0,
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
    # No entry bracket — exits are 100% condition-driven, matching the live engine. SL is
    # placed by the max-risk SAFEGUARD on entry (shared classic RM, so backtest == live).
    # FIRST-MATCH order: closes first, lock next, always-matching floor stop LAST (it used to
    # sit first and shadowed every other exit rule — see _first_match_order).
    exit_conditions = _first_match_order(exit_conditions, [
        "exit_bearish", "exit_downgrade", "exit_takeprofit", "exit_time",
        "exit_belock", "exit_stoploss"])
    return _strategy_from_parts(name, buy_tree=buy_entry_conditions,
                                exit_conditions=exit_conditions)


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
        # ALWAYS ON (no toggle_optimize): this is S3's only condition-based floor protection before
        # the trailing tiers below have a chance to ratchet the stop up in profit. Letting the GA
        # disable it let it "win" by simply not protecting the downside (near-zero trade counts on
        # large-cap, -55% max drawdown on small-cap) — the tightness (action_value_*) stays optimizable,
        # only the on/off toggle is removed.
        {"id": "exit_stoploss", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -8.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": -3.0, "action_value_step": 2.0,
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
    # No entry bracket — exits are 100% condition-driven (matches live); the protective stop
    # is the exit_stoploss rule and the trailing tiers ratchet it up. FIRST-MATCH order: time
    # close first, tiers DEEPEST-first (t1 first used to win at ANY profit level, so t2/t3
    # never applied), floor stop LAST (it used to sit first and shadowed everything).
    exit_conditions = _first_match_order(exit_conditions, [
        "exit_time", "trail_t3", "trail_t2", "trail_t1", "exit_stoploss"])
    return _strategy_from_parts(name, buy_tree=buy_entry_conditions,
                                exit_conditions=exit_conditions)


def _build_strategy_S5(name: str):
    """S5 — S2/S3 HYBRID: signal exits + trailing ladder. Data-driven design from the -goal grids:
    the best S2 config (186% archived S2-large winner) entered on nearly every signal (all gates
    off) and managed purely by exits — but capped every winner at a FIXED +32% TP while its trades
    included names that ran far higher. S5 keeps S2's proven core (bearish/downgrade signal exits,
    breakeven lock, no cooldown gates) but replaces the fixed TP with S3's staged trailing tiers,
    plus a very WIDE optimizable profit cap (40-80%) as the only hard ceiling — testing whether
    tail-capture beats the fixed target. Entry gate stays light like S3 (confidence +
    expected_profit only, both toggleable)."""
    from app.models.strategy import Strategy
    buy_entry_conditions = {
        "id": "root", "type": "AND", "conditions": [
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 80, "value_step": 5,
             "toggle_optimize": True},
            {"id": "gate_expected_profit", "field": "expected_profit", "op": ">", "value": 3,
             "optimize": True, "value_min": 0, "value_max": 15, "value_step": 1,
             "toggle_optimize": True},
        ],
    }
    exit_conditions = [
        # Floor stop (always on, tightness optimized) — same hygiene as S2/S3.
        {"id": "exit_stoploss", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -6.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": -3.0, "action_value_step": 2.0,
         "conditions": {"type": "AND", "conditions": [{"id": "sl_hold", "field": "has_position"}]}},
        # S2's signal-based exits: close when the expert turns bearish / rating goes negative.
        {"id": "exit_bearish", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xb", "field": "bearish"}]}},
        {"id": "exit_downgrade", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xd", "field": "current_rating_negative"}]}},
        # S2's breakeven lock: once +X% in profit, move the stop to entry +lock%.
        {"id": "exit_belock", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 0.0, "action_value_optimize": True,
         "action_value_min": -2.0, "action_value_max": 8.0, "action_value_step": 2.0,
         "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xlk", "field": "profit_loss_percent", "op": ">", "value": 5,
              "optimize": True, "value_min": 3, "value_max": 20, "value_step": 2}]}},
        # S3's staged trailing tiers replace the fixed TP: ratchet the stop up as profit grows.
        {"id": "trail_t1", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 4.0, "action_value_optimize": True,
         "action_value_min": 1.0, "action_value_max": 8.0, "action_value_step": 1.0, "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "t1", "field": "profit_loss_percent", "op": ">", "value": 10,
              "optimize": True, "value_min": 6, "value_max": 16, "value_step": 2}]}},
        {"id": "trail_t2", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 12.0, "action_value_optimize": True,
         "action_value_min": 6.0, "action_value_max": 18.0, "action_value_step": 2.0, "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "t2", "field": "profit_loss_percent", "op": ">", "value": 20,
              "optimize": True, "value_min": 14, "value_max": 28, "value_step": 2}]}},
        {"id": "trail_t3", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 24.0, "action_value_optimize": True,
         "action_value_min": 16.0, "action_value_max": 32.0, "action_value_step": 2.0, "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "t3", "field": "profit_loss_percent", "op": ">", "value": 32,
              "optimize": True, "value_min": 24, "value_max": 45, "value_step": 3}]}},
        # WIDE hard cap — the only fixed TP, far above the 186%-winner's +32% ceiling so the
        # trailing ladder (not the cap) normally decides the exit. Toggleable + optimized.
        {"id": "exit_cap", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xcap", "field": "profit_loss_percent", "op": ">", "value": 60,
              "optimize": True, "value_min": 40, "value_max": 80, "value_step": 5}]}},
    ]
    # FIRST-MATCH order: signal closes + wide cap first, trailing tiers DEEPEST-first,
    # break-even lock, always-matching floor stop LAST (see _first_match_order).
    exit_conditions = _first_match_order(exit_conditions, [
        "exit_bearish", "exit_downgrade", "exit_cap", "trail_t3", "trail_t2", "trail_t1",
        "exit_belock", "exit_stoploss"])
    return _strategy_from_parts(name, buy_tree=buy_entry_conditions,
                                exit_conditions=exit_conditions)


def _build_strategy_S6(name: str):
    """S6 — HIGH-FREQUENCY QUICK-CYCLE. Data-driven: the -tpsl S2-large run hit 519 trades
    (173/yr), calmar 3.21 and only -5.5% dd — many small, fast, diversified positions beat few
    concentrated ones on every risk metric, and the goal fitness (consistency x dd_guard) rewards
    exactly that regime. S6 bets on fast rotation: TIGHT take-profit (6-16%), SHORT time exit
    (10-30 days), no cooldown gates, plus the signal exits for protection. Position sizing stays
    in the RM genes (the GA can shrink max_virtual_equity to fit more concurrent names)."""
    from app.models.strategy import Strategy
    buy_entry_conditions = {
        "id": "root", "type": "AND", "conditions": [
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 80, "value_step": 5,
             "toggle_optimize": True},
            {"id": "gate_expected_profit", "field": "expected_profit", "op": ">", "value": 3,
             "optimize": True, "value_min": 0, "value_max": 15, "value_step": 1,
             "toggle_optimize": True},
        ],
    }
    # TP/SL AT ENTRY (entry_actions, Phase 1.5) — the tight quick-cycle bracket is now set the
    # instant the order fills, closing the "unprotected until the first scheduled manage check"
    # gap the exit-condition form had. Both GA-toggleable; the RM max-risk safeguard stop remains
    # the always-on floor even if the entry SL is toggled off.
    entry_actions = [
        {"id": "entry_tp", "action_type": "adjust_take_profit", "reference_value": "order_open_price",
         "action_value": 10.0, "action_value_optimize": True,
         "action_value_min": 6.0, "action_value_max": 16.0, "action_value_step": 1.0,
         "toggle_optimize": True},
        {"id": "entry_sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -5.0, "action_value_optimize": True,
         "action_value_min": -10.0, "action_value_max": -2.0, "action_value_step": 1.0,
         "toggle_optimize": True},
    ]
    exit_conditions = [
        # Signal exits for protection (same as S2/S5).
        {"id": "exit_bearish", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xb", "field": "bearish"}]}},
        {"id": "exit_downgrade", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xd", "field": "current_rating_negative"}]}},
        # SHORT time exit (always on, length optimized): a quick-cycle position that hasn't hit
        # TP within ~2-6 weeks is dead money — free the slot.
        {"id": "exit_time", "action_type": "close",
         "conditions": {"type": "AND", "conditions": [
             {"id": "xt", "field": "days_opened", "op": ">", "value": 20,
              "optimize": True, "value_min": 10, "value_max": 30, "value_step": 5}]}},
    ]
    return _strategy_from_parts(name, buy_tree=buy_entry_conditions,
                                exit_conditions=exit_conditions, entry_actions=entry_actions)


def _build_strategy_S7(name: str):
    """S7 — FAITHFUL REPLICA of the archived pre-tp/sl-rework S2-large winner (backtest #91,
    opt 23: 186.53% return, 181.07% adjusted, 149 trades/49.78 per yr, calmar 3.66, dd -11.52%
    -- reproduced byte-identically against today's code, so the peak is real).

    The FIRST S7 (58% max across only 21 distinct genomes) failed because it was NOT a replica:
    it swapped the winner's schedule-evaluated exit-condition TP/SL for an immediate entry-time
    hard bracket (a -8% stop live from the fill cuts volatile large-cap winners the winner's
    schedule-checked stop tolerated), dropped the confidence/expected-profit gates entirely, and
    its narrow steps collapsed the GA into near-total duplication. This rebuild restores the
    winner's exact structure:
      - entry: the cooldown gate the winner used (days_since_last_profitable_close) PLUS the
        confidence/expected-profit gates as TOGGLEABLE (the winner ran them disabled — carrying
        them lets the GA rediscover that rather than forcing it), NO entry-time bracket.
      - exits, ordered for the engine's FIRST-MATCH semantics (a matching rule stops evaluation
        unless continue_processing — so an always-matching floor stop placed first would shadow
        every rule after it, which is exactly what the post-rework S2/S3/S5 lists did):
          1-2. signal closes (bearish / downgrade) — match only on signal, shadow nothing;
          3.   fixed take-profit CLOSE at +32% (the winner's ceiling), value searched 24..42;
          4.   break-even lock: at +16% profit move the stop to entry +4% (dynamic ratchet);
          5.   floor stop entry -8% LAST — the always-matching fallback protects only when no
               deeper rule applied.
      - no exit_time (the winner had it disabled).
    Ranges are wide enough to actually search (step-1 cooldown, step-2 TP/SL/lock => thousands
    of distinct genomes, not 21) — see run_screener_capband_matrix.py's budget override.
    """
    buy_entry_conditions = {
        "id": "root", "type": "AND", "conditions": [
            {"id": "gate_days_since_profit", "field": "days_since_last_profitable_close", "op": ">",
             "value": 5, "optimize": True, "value_min": 2, "value_max": 10, "value_step": 1,
             "toggle_optimize": True},
            # The winner ran with BOTH of these disabled; keep them toggleable so the GA can
            # verify that finding instead of us hard-coding it.
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 80, "value_step": 5,
             "toggle_optimize": True},
            {"id": "gate_expected_profit", "field": "expected_profit", "op": ">", "value": 3,
             "optimize": True, "value_min": 0, "value_max": 15, "value_step": 1,
             "toggle_optimize": True},
        ],
    }
    exit_conditions = [
        # Signal closes FIRST (first-match: they only match on their signal, shadow nothing).
        {"id": "exit_bearish", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xb", "field": "bearish"}]}},
        {"id": "exit_downgrade", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "xd", "field": "current_rating_negative"}]}},
        # The winner's fixed +32% take-profit as the schedule-evaluated CLOSE it actually was.
        {"id": "exit_takeprofit", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xtp", "field": "profit_loss_percent", "op": ">", "value": 32,
              "optimize": True, "value_min": 24, "value_max": 42, "value_step": 2}]}},
        # Break-even lock BEFORE the floor stop: at +16% the stop ratchets to entry +4%; while
        # it matches, first-match picks it over the floor (the tighter, correct stop).
        {"id": "exit_belock", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": 4.0, "action_value_optimize": True,
         "action_value_min": 0.0, "action_value_max": 8.0, "action_value_step": 1.0,
         "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "xlk", "field": "profit_loss_percent", "op": ">", "value": 16,
              "optimize": True, "value_min": 10, "value_max": 22, "value_step": 2}]}},
        # Floor stop LAST — always matches while holding, so anywhere earlier it would shadow
        # every rule after it. As the final fallback it protects exactly when nothing else did.
        # TOGGLEABLE (unlike S2/S3's always-on floors): the WINNER ran with NO floor rule at
        # all — its downside protection was the RM max-risk safeguard stop, which is ALWAYS
        # placed on entry regardless, so disabling this never leaves a position unprotected.
        {"id": "exit_stoploss", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -8.0, "action_value_optimize": True,
         "action_value_min": -14.0, "action_value_max": -4.0, "action_value_step": 2.0,
         "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [{"id": "sl_hold", "field": "has_position"}]}},
    ]
    return _strategy_from_parts(name, buy_tree=buy_entry_conditions,
                                exit_conditions=exit_conditions)


def _build_strategy_minimal(name: str):
    """A placeholder Strategy for BYPASS experts (FactorRanker) that ignore enter/exit rulesets and
    rebalance by factor score. The optimization still needs a Strategy row; this one carries no
    conditions and no TP/SL genes — all search lives in the expert's factor model:* params."""
    from app.models.strategy import Strategy
    return Strategy(name=name, entry_rules=[], exit_rules=[])


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


def _uniquify_condition_ids(buy_tree, exit_rules) -> None:
    """Make every condition-node id globally unique across the buy tree + all exit-rule condition
    sub-trees. IN PLACE.

    Live rulesets restart leaf ids (c0,c1,c2,...) in EVERY rule and in the buy tree, so an id like
    `c3` names many unrelated leaves (the buy tree's `long_term` flag, an exit's `days_opened`
    numeric, ...). But the optimizer keys condition genes by bare id in ONE global namespace
    (strategy_param_space.decode_params builds a single cond_by_id and applies it to the buy tree
    AND every exit rule). With colliding ids, a single `cond:c3:value` gene was sprayed onto all of
    them at once — an exit's numeric range bled onto the buy tree's flag (e.g. `long_term 112`), and
    one `cond:cN:enabled=0` toggle silently dropped every same-id leaf, deleting the live `close`
    rules so trades never exited. Rewriting each node id to be unique makes every gene map to exactly
    one leaf. The exit RULE ids (live-30, ...) are PRESERVED — `exit:<id>` genes and decode match on
    them; only the condition nodes *inside* each rule are relabelled."""
    ctr = [0]

    def relabel(node):
        if not isinstance(node, dict):
            return
        ctr[0] += 1
        node["id"] = f"u{ctr[0]}"
        for child in (node.get("conditions") or []):
            relabel(child)

    relabel(buy_tree)
    for rule in (exit_rules or []):
        conds = rule.get("conditions") if isinstance(rule, dict) else None
        if conds:
            relabel(conds)


def _build_strategy_S1(name: str, expert: str):
    """S1 — the expert's LIVE dev-account "high conviction" ruleset (exported to
    docs/live_rulesets/{expert}.json), normalized to the canonical Strategy shape with optimize
    flags on every threshold + adjust-%, PLUS an entry-time TP/SL bracket that mirrors the live
    "Optimized Entry - High Conviction" ruleset (which sets a target-anchored take-profit and a
    protective stop AT ENTRY — see the enter ruleset's adjust_take_profit/adjust_stop_loss actions).

    Faithful to the live enter (buy/sell trees, OR groups preserved) + open_positions (exit) rules.

    S4 (the old target-anchored-TP variant) is now MERGED here: S1 carries the target-anchored
    take-profit as an ``entry_actions`` rule (fired ONCE at entry by the shared
    TradeActionEvaluator's Phase 1.5/2, the same mechanism live uses — see
    docs/plans/2026-07-03-entry-tp-sl-bracket-actions.md), plus an entry stop-loss. Both entry
    actions are GA on/off-TOGGLEABLE (``toggle_optimize`` → ``entry:<id>:enabled`` gene), so the
    optimizer decides whether the entry bracket helps — and the target-anchored TP self-disables
    for experts with no real analyst target (FMPEarningsDrift / FMPInsiderClusterBuy, whose
    ``expert_target_price`` is a static setting). The RM max-risk SAFEGUARD stop is ALWAYS placed
    on entry regardless (shared classic RM), so a toggled-off entry SL never leaves the position
    unprotected — the entry SL is an additional, tighter, optional bracket."""
    import json as _json
    from app.models.strategy import Strategy
    repo_root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(repo_root, "docs", "live_rulesets", f"{expert}.json")
    if not os.path.isfile(path):
        sys.exit(f"optimize: S1 needs {path}; run `python backend/scripts/export_live_rulesets.py` first.")
    with open(path, encoding="utf-8") as f:
        data = _json.load(f)

    # LOSSLESS file shape (re-exported via export_live_rulesets.py on the unified model):
    # TradeRule lists carrying live's OWN per-rule brackets / multi-action rules /
    # continue_processing / rule order. Preferred when present; the legacy tree shape below
    # keeps working for older files.
    if data.get("entry_rules"):
        entry_rules = data["entry_rules"]
        exit_rules = data.get("exit_rules") or []
        # Unique condition ids across every rule (live restarts c0,c1,... per rule; the
        # optimizer keys cond genes by bare id in ONE global namespace).
        ctr = [0]
        def _relabel(node):
            if not isinstance(node, dict):
                return
            ctr[0] += 1
            node["id"] = f"u{ctr[0]}"
            for child in (node.get("conditions") or []):
                _relabel(child)
        for rule in entry_rules + exit_rules:
            if isinstance(rule, dict) and rule.get("conditions"):
                _relabel(rule["conditions"])
        # Rules WITHOUT their own bracket get the default S1 target-anchored TP + entry SL
        # (merged-S4 behavior); rules that brought live's own bracket keep it untouched.
        for rule in entry_rules:
            kinds = {str(a.get("action_type")) for a in (rule.get("actions") or [])
                     if isinstance(a, dict)}
            if "adjust_take_profit" not in kinds:
                rule["actions"] = list(rule.get("actions") or []) + [
                    {"id": "s1_tp_target", "action_type": "adjust_take_profit",
                     "reference_value": "expert_target_price",
                     "action_value": -5.0, "action_value_optimize": True,
                     "action_value_min": -20.0, "action_value_max": 10.0,
                     "action_value_step": 2.0, "toggle_optimize": True},
                    {"id": "s1_sl_entry", "action_type": "adjust_stop_loss",
                     "reference_value": "order_open_price",
                     "action_value": -8.0, "action_value_optimize": True,
                     "action_value_min": -20.0, "action_value_max": -3.0,
                     "action_value_step": 2.0, "toggle_optimize": True},
                ]
            rule["toggle_optimize"] = True  # GA can retire a whole conviction tier
        from app.models.strategy import Strategy
        from ba2_common.core.rule_models import normalize_trade_rules
        return Strategy(name=name, entry_rules=normalize_trade_rules(entry_rules),
                        exit_rules=normalize_trade_rules(exit_rules))

    buy = _s1_norm_tree(data.get("buy_entry_conditions"))
    exits = [_s1_norm_exit_rule(r) for r in (data.get("exit_conditions") or [])]
    # Live rulesets reuse leaf ids (c0,c1,...) across every rule + the buy tree; the optimizer keys
    # condition genes by bare id in one global namespace, so colliding ids cross-contaminate (an
    # exit's numeric range leaking onto a buy-tree flag, one toggle dropping unrelated leaves). Make
    # them globally unique so each gene maps to exactly one leaf.
    _uniquify_condition_ids(buy, exits)
    # NOTE: the launcher's Strategy has no separate sell tree — shorts are mirrored from the buy
    # gates via the engine's enable_short flag. The live sell_entry_conditions (if any) is dropped;
    # these experts are long-only in practice, so S1 runs long.
    #
    # Entry-time TP/SL bracket mirroring the live "high conviction" ruleset. Both GA-toggleable
    # so the optimizer can disable either. The target-anchored TP is centred on the live value
    # (-5% below the analyst target) and searched as an offset-from-target (negative = below
    # target); the entry SL is anchored off the fill price. Merged from the old S4. On the
    # unified rule model the bracket is replicated onto EVERY entry rule (one per live OR
    # branch) — each branch's copy optimizes via its own entry:<rid>:a<i>:* genes, live's
    # per-tier-bracket shape.
    entry_actions = [
        {"id": "s1_tp_target", "action_type": "adjust_take_profit",
         "reference_value": "expert_target_price",
         "action_value": -5.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": 10.0, "action_value_step": 2.0,
         "toggle_optimize": True},
        {"id": "s1_sl_entry", "action_type": "adjust_stop_loss",
         "reference_value": "order_open_price",
         "action_value": -8.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": -3.0, "action_value_step": 2.0,
         "toggle_optimize": True},
    ]
    strat = _strategy_from_parts(name, buy_tree=buy, exit_conditions=exits,
                                 entry_actions=entry_actions)
    # NEW-STRUCTURE upgrade: each entry rule (one per live conviction tier / OR branch) is
    # GA-droppable as a WHOLE (rule-level toggle -> entry:<rid>:enabled gene) — the old split
    # model could only toggle individual leaves, never retire a tier outright. Combined with
    # the per-rule bracket genes this lets the GA discover e.g. "the fallback tier hurts" or
    # "tier 3 wants a looser stop than tier 1".
    for rule in strat.entry_rules:
        rule["toggle_optimize"] = True
    return strat


# S4 (REBORN, 2026-07-09): the old target-anchored-TP S4 was merged into S1. This NEW S4 is a
# STRUCTURE-NATIVE explorer for the two capabilities only the unified rule model can express:
#   1. MULTI-ACTION tier rules — each profit tier ratchets the STOP up AND extends the TP
#      ceiling in ONE rule (one action per rule was a hard limit before), so winners keep
#      running while the downside locks in.
#   2. continue_processing — the TP-follow rule (raise the TP when the analyst target moves
#      up) fires AND lets evaluation continue to the SL ladder in the same cycle; under pure
#      first-match it would shadow the ratchet every cycle the target moved.
def _build_strategy_S4(name: str):
    """S4 — structure-native trailing: multi-action tiers (SL ratchet + TP extension per
    tier) + a continue_processing TP-follow on target raises + the S7-replica entry/signal
    core. Exit order honors first-match: signal closes first, TP-follow (continue=True),
    tiers DEEPEST-first, toggleable floor stop LAST. Everything optimizable/toggleable."""
    from app.models.strategy import Strategy
    from ba2_common.core.rule_models import normalize_trade_rules

    entry_rules = [{
        "id": "s4-entry", "name": f"{name}-entry",
        "conditions": {"id": "root", "type": "AND", "conditions": [
            {"id": "s4-bullish", "field": "bullish", "field_type": "flag"},
            {"id": "s4-flat", "field": "has_no_position", "field_type": "flag"},
            {"id": "gate_days_since_profit", "field": "days_since_last_profitable_close",
             "op": ">", "value": 5, "optimize": True, "value_min": 2, "value_max": 10,
             "value_step": 1, "toggle_optimize": True},
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 80, "value_step": 5,
             "toggle_optimize": True},
        ]},
        "actions": [{"action_type": "buy"}],
        "continue_processing": False,
    }]

    def _tier(rid, gate, gate_rng, sl_lock, sl_rng, tp_ext, tp_rng):
        """One multi-action tier: at profit > gate, move SL to entry+sl_lock AND TP to
        entry+tp_ext — both values optimizable, whole tier toggleable."""
        return {
            "id": rid, "toggle_optimize": True, "continue_processing": False,
            "conditions": {"type": "AND", "conditions": [
                {"id": f"{rid}_gate", "field": "profit_loss_percent", "op": ">",
                 "value": gate, "optimize": True,
                 "value_min": gate_rng[0], "value_max": gate_rng[1], "value_step": gate_rng[2]}]},
            "actions": [
                {"id": f"{rid}_sl", "action_type": "adjust_stop_loss",
                 "reference_value": "order_open_price", "action_value": sl_lock,
                 "action_value_optimize": True, "action_value_min": sl_rng[0],
                 "action_value_max": sl_rng[1], "action_value_step": sl_rng[2]},
                {"id": f"{rid}_tp", "action_type": "adjust_take_profit",
                 "reference_value": "order_open_price", "action_value": tp_ext,
                 "action_value_optimize": True, "action_value_min": tp_rng[0],
                 "action_value_max": tp_rng[1], "action_value_step": tp_rng[2],
                 "toggle_optimize": True},
            ],
        }

    exit_rules = [
        # Signal closes FIRST (match only on their trigger, shadow nothing).
        {"id": "exit_bearish", "toggle_optimize": True, "continue_processing": False,
         "conditions": {"type": "AND", "conditions": [{"id": "xb", "field": "bearish"}]},
         "actions": [{"action_type": "close"}]},
        {"id": "exit_downgrade", "toggle_optimize": True, "continue_processing": False,
         "conditions": {"type": "AND", "conditions": [{"id": "xd", "field": "current_rating_negative"}]},
         "actions": [{"action_type": "close"}]},
        # TP-FOLLOW with continue_processing: when the analyst target moved up >X% vs the
        # current TP, raise the TP toward the new target — and KEEP EVALUATING so the SL
        # ladder below still ratchets in the same cycle (pure first-match would shadow it).
        {"id": "tp_follow", "toggle_optimize": True, "continue_processing": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "tf_gate", "field": "percent_to_new_target", "op": ">", "value": 5,
              "optimize": True, "value_min": 2, "value_max": 12, "value_step": 2}]},
         "actions": [
             {"id": "tf_tp", "action_type": "adjust_take_profit",
              "reference_value": "expert_target_price", "action_value": -5.0,
              "action_value_optimize": True, "action_value_min": -15.0,
              "action_value_max": 5.0, "action_value_step": 2.0}]},
        # Multi-action tiers, DEEPEST-first (first-match picks the most aggressive
        # applicable tier; both its actions fire).
        _tier("tier3", 30, (22, 40, 3), 20.0, (14.0, 26.0, 2.0), 50.0, (38.0, 60.0, 4.0)),
        _tier("tier2", 18, (12, 26, 2), 10.0, (6.0, 16.0, 2.0), 36.0, (28.0, 44.0, 4.0)),
        _tier("tier1", 8, (4, 14, 2), 2.0, (-2.0, 6.0, 1.0), 28.0, (20.0, 34.0, 2.0)),
        # Floor stop LAST (always matches while holding); toggleable — the RM max-risk
        # safeguard stop is always placed regardless, so off never means unprotected.
        {"id": "exit_stoploss", "toggle_optimize": True, "continue_processing": False,
         "conditions": {"type": "AND", "conditions": [{"id": "sl_hold", "field": "has_position"}]},
         "actions": [
             {"id": "floor_sl", "action_type": "adjust_stop_loss",
              "reference_value": "order_open_price", "action_value": -8.0,
              "action_value_optimize": True, "action_value_min": -14.0,
              "action_value_max": -4.0, "action_value_step": 2.0}]},
    ]
    return Strategy(name=name, entry_rules=normalize_trade_rules(entry_rules),
                    exit_rules=normalize_trade_rules(exit_rules))


# --- Option strategy entry-action configs (pure-option entries) -----------------------------------
# Each maps a strategy key -> the option ACTION config the enter_market ruleset fires directly (no
# equity leg; the engine's entry-option path submits it). Carries optimizable ranges (strike_param /
# dte / wing) so the GA searches them via the option_* genes. DTE/%OTM windows are tastytrade-ish
# (~25-45 DTE, sell ~10-20% OTM, wings 3-8%). The action_type maps to an _OptionEntryAction subclass
# in ba2_common.core.TradeActions; rule_builders.action_from_rule consumes these option_* keys.
_OPTION_STRATS = {
    "O_LC": {  # long call (debit)
        "action_type": "buy_call", "option_strike_method": "percent_otm",
        "option_strike_param": 2.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
        "option_strike_param_max": 8.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_VERT": {  # bear put vertical (debit)
        "action_type": "open_bear_put_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 2.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
        "option_strike_param_max": 6.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_SSTG": {  # short strangle (credit)
        "action_type": "open_short_strangle", "option_strike_method": "percent_otm",
        "option_strike_param": 12.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 6.0,
        "option_strike_param_max": 20.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
    "O_SSTD": {  # short straddle (credit)
        "action_type": "open_short_straddle", "option_strike_method": "percent_otm",
        "option_strike_param": 0.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
    "O_IC": {  # iron condor (credit, defined risk)
        "action_type": "open_iron_condor", "option_strike_method": "percent_otm",
        "option_strike_param": 12.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 8.0,
        "option_strike_param_max": 20.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0},
    "O_JL": {  # jade lizard (credit)
        "action_type": "open_jade_lizard", "option_strike_method": "percent_otm",
        "option_strike_param": 10.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 6.0,
        "option_strike_param_max": 16.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0},
    "O_BF": {  # long call butterfly (debit)
        "action_type": "open_call_butterfly", "option_strike_method": "percent_otm",
        "option_strike_param": 0.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 8.0, "option_wing_width_pct": 10.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 5.0,
        "option_wing_width_max": 15.0, "option_wing_width_step": 2.5},
    "O_RS": {  # put ratio spread (credit/even)
        "action_type": "open_put_ratio_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 5.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 15.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 2.0,
        "option_strike_param_max": 10.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0},
    "O_LP": {  # long put (debit) — the bearish mirror of O_LC; entry gates on the BEARISH
        # signal (see _OPTION_ENTRY_GATE), matching the live OPT-LongPut example which fired
        # on current_rating_negative. Needs the expert's sell signals enabled to ever trade.
        "action_type": "buy_put", "option_strike_method": "percent_otm",
        "option_strike_param": 2.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
        "option_strike_param_max": 8.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
}

# Directional entry gate per pure-option strategy: which signal flag the entry rule requires.
# Every original O_* key fires on the expert's BULLISH signal (including O_VERT — a bearish
# STRUCTURE opened on a bullish signal as a hedge-shaped premium play, the original grid
# semantics, kept unchanged). O_LP is the one true bearish-signal entry.
_OPTION_ENTRY_GATE = {k: "bullish" for k in _OPTION_STRATS}
_OPTION_ENTRY_GATE["O_LP"] = "bearish"

# GROUPED option strategies: ONE optimize job searching a FAMILY of similar structures.
# Each member becomes its own toggleable entry TradeRule (entry:<member>-entry:enabled gene)
# carrying its own option action + option_* genes, so the GA can turn structures on/off and
# tune each independently — top-5 individuals can land on DIFFERENT structures, giving the
# saved top-N variety instead of 5 near-clones of one structure.
_OPTION_GROUPS = {
    "OS1": ["O_LC", "O_LP", "O_VERT", "O_BF"],   # directional DEBIT (long premium / defined)
    "OS2": ["O_SSTG", "O_SSTD", "O_IC"],          # neutral CREDIT (short premium)
    "OS3": ["O_JL", "O_RS"],                      # skewed CREDIT (asymmetric short premium)
}

# Pure-option strategy keys (entry is the option action; no equity leg). O_CC/O_STK are equity.
_PURE_OPTION_STRATEGIES = set(_OPTION_STRATS) | set(_OPTION_GROUPS)
# All launcher option/equity strategy keys handled by the option builders.
_OPTION_STRATEGY_KEYS = _PURE_OPTION_STRATEGIES | {"O_CC", "O_STK"}


def _option_entry_action_for(kind: str) -> dict:
    """The option ENTRY action config for a pure-option strategy key (a fresh copy)."""
    return dict(_OPTION_STRATS[kind])


def _option_exit_rules(kind: str):
    """Close the held option at +50% premium profit, plus a time exit — both optimizable +
    on/off-toggleable. (CLOSE on the held option position via ``close_option``.)"""
    return [
        {"id": "opt_tp", "action_type": "close_option", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 50,
              "optimize": True, "value_min": 25, "value_max": 75, "value_step": 5}]}},
        {"id": "opt_time", "action_type": "close_option", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "td", "field": "days_opened", "op": ">", "value": 21,
              "optimize": True, "value_min": 10, "value_max": 35, "value_step": 5}]}},
    ]


def _build_strategy_option(kind: str):
    """A pure-option Strategy on the unified rule model: ONE entry TradeRule whose action IS
    the option action config (bullish+flat+confidence gate; no equity leg — the engine's
    entry-option path submits the option directly), exits = close at +50% / time. The option
    action config is ALSO carried as the transient ``entry_action`` attribute (not a mapped
    column, never persisted): the run-config assembly threads it into the backtest block,
    where the engine's ``_entry_is_option`` flag keys off ``config["entry_action"]``."""
    from app.models.strategy import Strategy
    from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy

    option_action = _option_entry_action_for(kind)
    entry_rules = normalize_trade_rules([_option_entry_rule(kind)])
    exit_rules = trade_rules_from_legacy(exit_conditions=_option_exit_rules(kind))["exit_rules"]
    s = Strategy(name=kind, entry_rules=entry_rules, exit_rules=exit_rules)
    s.entry_action = option_action  # type: ignore[attr-defined]
    return s


def _option_entry_rule(member: str, *, toggleable: bool = False) -> dict:
    """The entry TradeRule dict for one pure-option strategy key: directional signal gate
    (bullish for every original key, bearish for O_LP — see _OPTION_ENTRY_GATE) + flat +
    optimizable confidence gate, action = the member's option action config. Rule/condition
    ids are prefixed with the member key so a GROUP of these rules yields uniquely-keyed
    genes per member. ``toggleable`` adds the rule-level enabled gene (group members only —
    a single-strategy job keeps its one entry always-on)."""
    m = member.lower()
    rule = {
        "id": f"{m}-entry",
        "name": f"{member}-entry",
        "conditions": {"id": f"{m}-root", "type": "AND", "conditions": [
            {"id": f"{m}-signal", "field": _OPTION_ENTRY_GATE[member], "field_type": "flag"},
            {"id": f"{m}-flat", "field": "has_no_position", "field_type": "flag"},
            {"id": f"{m}-gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 75, "value_step": 5,
             "toggle_optimize": True}]},
        "actions": [_option_entry_action_for(member)],
        "continue_processing": False,
    }
    if toggleable:
        rule["toggle_optimize"] = True
    return rule


def _build_strategy_option_group(kind: str):
    """A GROUPED pure-option Strategy (OS1/OS2/...): one toggleable entry TradeRule per member
    structure, all sharing the option exit rules. First-match semantics pick the first ENABLED
    member whose gate matches, and the GA's per-rule enabled genes search which structure(s)
    to run — so one job explores the whole family and the persisted top-5 can differ in
    STRUCTURE, not just parameters. ``entry_action`` (the engine's option-entry-path flag) is
    set from the first member: every member is an option action, so the flag is identical
    whichever member fires."""
    from app.models.strategy import Strategy
    from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy

    members = _OPTION_GROUPS[kind]
    entry_rules = normalize_trade_rules(
        [_option_entry_rule(m, toggleable=True) for m in members])
    exit_rules = trade_rules_from_legacy(exit_conditions=_option_exit_rules(kind))["exit_rules"]
    s = Strategy(name=kind, entry_rules=entry_rules, exit_rules=exit_rules)
    s.entry_action = _option_entry_action_for(members[0])  # type: ignore[attr-defined]
    return s


def _build_strategy_covered_call(kind: str):
    """O_CC — equity entry (the S2 baseline) + a ``sell_covered_call`` OPEN_POSITIONS overlay rule
    (sell a ~5% OTM call against the held shares). Equity-entry, so NO entry_action."""
    from app.models.strategy import Strategy  # noqa: F401 — keep import parity with siblings
    s = _build_strategy_S2(kind)  # reuse equity entry + base exits
    s.exit_rules = list(s.exit_rules or []) + [{
        "id": "cc_sell",
        "conditions": {"type": "AND", "conditions": [{"id": "cc_hold", "field": "has_position"}]},
        "actions": [{"action_type": "sell_covered_call",
                     "option_strike_method": "percent_otm", "option_strike_param": 5.0,
                     "option_dte_min": 25, "option_dte_max": 45}],
        "continue_processing": False}]
    return s


def _build_strategy_stock(kind: str):
    """O_STK — plain equity long (the S2 baseline)."""
    return _build_strategy_S2(kind)


_STRATEGY_BUILDERS = {
    "S1": _build_strategy_S1,   # (name, expert) — live "high conviction" ruleset + entry TP/SL
                                #                  bracket (target-anchored TP, merged from S4)
    "S2": _build_strategy_S2,   # (name)
    "S3": _build_strategy_S3,   # (name)
    "S4": _build_strategy_S4,   # (name) — structure-native explorer: multi-action trailing tiers
                                #          (SL ratchet + TP extension per tier) + continue_processing
                                #          TP-follow on target raises
    "S5": _build_strategy_S5,   # (name) — S2/S3 hybrid: signal exits + trailing ladder
    "S6": _build_strategy_S6,   # (name) — high-frequency quick-cycle (tight TP/SL AT ENTRY + time exit)
    "S7": _build_strategy_S7,   # (name) — tight refinement around the archived 186% S2-large winner
    # Option/equity strategies (dispatch by `kind`, not `name`; see _build_strategy):
    "O_LC": _build_strategy_option, "O_VERT": _build_strategy_option,
    "O_SSTG": _build_strategy_option, "O_SSTD": _build_strategy_option,
    "O_IC": _build_strategy_option, "O_JL": _build_strategy_option,
    "O_BF": _build_strategy_option, "O_RS": _build_strategy_option,
    "O_LP": _build_strategy_option,
    "O_CC": _build_strategy_covered_call, "O_STK": _build_strategy_stock,
    # Grouped option families (one job searches the whole family; see _OPTION_GROUPS):
    "OS1": _build_strategy_option_group, "OS2": _build_strategy_option_group,
    "OS3": _build_strategy_option_group,
}

# Per-strategy GA population multiplier applied on top of --population in optimize-batch. S1 is the
# richest strategy (live "high conviction" conditions + entry TP/SL bracket + target-anchored TP +
# exit rules => the largest gene space), so it gets a bit more population to search it; unlisted
# strategies use --population unchanged (factor 1.0).
_STRATEGY_POP_FACTOR = {"S1": 1.5}


def _build_strategy(kind: str, name: str, expert: str):
    """Dispatch to the right strategy builder. S1 is expert-specific (loads the live JSON).
    Option/equity strategies (O_*) dispatch by `kind` (the builder names the Strategy off the kind
    and, for pure-option kinds, carries the entry_action)."""
    if kind == "S1":
        return _STRATEGY_BUILDERS[kind](name, expert)
    if kind in _OPTION_STRATEGY_KEYS:
        return _STRATEGY_BUILDERS[kind](kind)
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
        # --run-schedule-day accepts a comma-separated list (e.g. "monday,thursday") so a
        # signal that decays fast (e.g. FMPEarningsDrift's ~10-day freshness window) can scan
        # more than once/week without going fully daily. A single day still works unchanged.
        sched_days = {d.strip().lower() for d in args.run_schedule_day.split(",") if d.strip()}
        days = {d: (d in sched_days) for d in
                ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
        # `times` pins the ANALYSIS to the scheduled time-of-day so on a 5min fill clock the
        # expert analyses ONCE/day (at market open) instead of every intraday bar. FMP bars are
        # stamped in market-local 09:30-15:55, so "09:30" is the first regular-session bar.
        run_sched = {"days": days, "times": ["09:30"]}
    # Open-positions MANAGEMENT runs DAILY (mirrors live, which schedules open_positions far more
    # often than enter_market): every weekday at the open bar, regardless of the (weekly) entry day.
    # So trailing-SL / close / days_opened exit rules are evaluated each trading day, not weekly.
    manage_sched = _daily_manage_schedule()

    init_db()
    db = SessionLocal()
    try:
        bypass = bool(spec.get("bypass"))
        _sname = args.name or f"opt-{expert}-{args.strategy}"
        # Bypass experts (FactorRanker) have no S1-S4 variants — they size their own portfolio, so
        # they use the minimal strategy and ignore --strategy. Classic experts build the chosen variant.
        strat = _build_strategy_minimal(_sname) if bypass else _build_strategy(args.strategy, _sname, expert)
        # Pure-option strategies carry a transient `entry_action` (the option ENTRY action config).
        # Capture it BEFORE commit/refresh so a db.refresh (which reloads only mapped columns) can't
        # affect it, then thread it into the run config below so the handler's _build_experts (and
        # the optimization handler's _build_daily_trial_config) seed the enter ruleset with it.
        strat_entry_action = getattr(strat, "entry_action", None)
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
            "manage_schedule_override": manage_sched,
            "execution_interval": args.interval,
            "profit_cap_pct": (float(args.profit_cap_pct) if args.profit_cap_pct else None),
            "profit_share_cap_pct": (float(args.profit_share_cap_pct) if args.profit_share_cap_pct else None),
            "fitness_trade_scale": bool(getattr(args, "fitness_trade_scale", False)),
            "fitness_trade_scale_cap": (float(args.fitness_trade_scale_cap)
                                        if getattr(args, "fitness_trade_scale_cap", None) else None),
            "fitness_win_rate_factor": bool(getattr(args, "fitness_win_rate_factor", False)),
            "labels": [t.strip() for t in getattr(args, "labels", "").split(",") if t.strip()],
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

        # Target-anchored variant (S4): the TP-on-target anchoring lives on the Strategy row itself
        # (strat.entry_actions, seeded by _build_strategy_S4 with reference_value=
        # "expert_target_price") — nothing to thread onto the run config here.
        # Pure-option strategy: thread the option ENTRY action onto the run config. The optimization
        # handler's _build_daily_trial_config forwards backtest['entry_action'] into every trial
        # config, and daily_backtest_handler._build_experts reads config['entry_action'] to seed the
        # enter ruleset with the option action (no equity leg). Equity strategies leave it unset.
        if strat_entry_action:
            backtest_block["entry_action"] = strat_entry_action
        # Per-weekday entry-scan toggle genes (schedule:<day>) for every non-bypass strategy
        # (S1-S7) — FactorRanker (bypass) has no per-day entry-scan gate, so it never gets these.
        schedule_genes = {} if bypass else {f"schedule:{k}": v for k, v in _SCHEDULE_DAY_OPT.items()}
        cfg = {
            "populationSize": int(args.population),
            "generations": int(args.generations),
            "crossoverProb": 0.6, "mutationProb": float(args.mutation_prob),
            "earlyStoppingGenerations": int(args.early_stop),
            "elitismPercent": 0.1, "seed": int(args.seed),
            "parallelIndividuals": int(args.parallel),
            # Expert decision params (+ classic-RM sizing for ruleset experts; bypass experts size
            # their own portfolio so they carry NO RM block). Screener genes (screener:* namespace)
            # are merged in ONLY when --screener is set.
            "expert_params": ({**spec["expert_params"], **screener_genes} if bypass
                              else {**spec["expert_params"], **_RM_OPT, **screener_genes, **schedule_genes}),
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
    nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top), parallel=int(args.parallel))
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
        # --run-schedule-day accepts a comma-separated list (e.g. "monday,thursday") so a
        # signal that decays fast can scan more than once/week without going fully daily.
        sched_days = {d.strip().lower() for d in args.run_schedule_day.split(",") if d.strip()}
        # `times` pins ANALYSIS to market open so a 5min fill clock analyses once/day, not per bar.
        run_sched = {"days": {d: (d in sched_days) for d in
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
            # Pure-option kinds carry a transient `entry_action`; capture before commit/refresh.
            strat_entry_action = getattr(strat, "entry_action", None)
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
                "manage_schedule_override": _daily_manage_schedule(),
                "execution_interval": args.interval,
                "profit_cap_pct": (float(args.profit_cap_pct) if args.profit_cap_pct else None),
                "profit_share_cap_pct": (float(args.profit_share_cap_pct) if args.profit_share_cap_pct else None),
                "fitness_trade_scale": bool(getattr(args, "fitness_trade_scale", False)),
                "fitness_trade_scale_cap": (float(args.fitness_trade_scale_cap)
                                            if getattr(args, "fitness_trade_scale_cap", None) else None),
                "fitness_win_rate_factor": bool(getattr(args, "fitness_win_rate_factor", False)),
                "labels": [t.strip() for t in getattr(args, "labels", "").split(",") if t.strip()],
                "backtest_id": int(_dt.now().timestamp()),
                "name": f"{name}-trial",
            }
            # Target-anchored variants (S4): the TP-on-target anchoring lives on the Strategy row
            # itself (strat.entry_actions, seeded by _build_strategy_S4) — nothing to thread onto
            # the run config here.
            # Pure-option strategy: thread the option ENTRY action onto the run config so every trial
            # seeds the enter ruleset with the option action (forwarded by _build_daily_trial_config).
            if strat_entry_action:
                backtest_block["entry_action"] = strat_entry_action
            pop_for_strat = int(round(args.population * _STRATEGY_POP_FACTOR.get(strat_kind, 1.0)))
            cfg = {
                "populationSize": pop_for_strat,
                "generations": int(args.generations),
                "crossoverProb": 0.6, "mutationProb": float(args.mutation_prob),
                "earlyStoppingGenerations": int(args.early_stop),
                "elitismPercent": 0.1, "seed": int(args.seed),
                "parallelIndividuals": int(args.parallel),
                # Bypass experts (FactorRanker) carry no classic-RM block (they size their own
                # portfolio) and no per-day schedule genes; ruleset experts get the expert params +
                # the RM sizing/stop params + per-weekday entry-scan toggle genes.
                "expert_params": (dict(spec["expert_params"]) if bypass
                                  else {**spec["expert_params"], **_RM_OPT,
                                        **{f"schedule:{k}": v for k, v in _SCHEDULE_DAY_OPT.items()}}),
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
            description=f"{expert} {strat_kind} x {len(universe)} syms, {args.fitness}, pop={pop_for_strat}",
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
            nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top), parallel=int(args.parallel))
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


def _persist_top_backtests(opt_id: int, expert: str, n: int = 5, parallel: int = 1) -> int:
    """Re-run the optimization's TOP-N distinct param sets and persist each as a tagged,
    saved Backtest (best params + their metrics) so the top performers are kept for
    comparison and to warm-start future optimizations. Returns how many were persisted.

    The re-runs are the slow post-GA phase (~minutes each at 5min/multi-year). They are
    INDEPENDENT, so with ``parallel`` > 1 they fan out across a bounded local process pool
    (each worker returns the full results blob; the master does all the DB writes through its
    single session). Bounded to ``parallel`` to respect the per-run ~2.5GB universe-memo
    footprint — the same local cap the GA uses."""
    import json as _json
    from datetime import datetime as _dt
    import app.models  # noqa: F401
    from app.models.database import SessionLocal
    from app.models.backtest import Backtest
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.strategy_optimization_handler import _build_daily_trial_config, _build_hoisted_state  # noqa: SLF001
    from app.services.backtest.daily_backtest_handler import _persist_results  # re-run via _persist_trial_worker
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
        # Apply the SAME screener hoisted state the GA scored each individual with, so the persisted
        # top-N are the actual optimized SCREENER runs (universe_source=screener + the per-individual
        # screener genes) — not static-universe runs. Without this the persisted top-N silently
        # diverge from their fitness (and from the UI "Load + run" / the in-place re-run).
        hoisted = _build_hoisted_state(bt_block) if bt_block.get("screener_opt") else None

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

        # 1) Build every re-run's spec in the MASTER (cheap: decode + config + display params).
        #    Store the raw optimized genes (for the "Optimized Parameters" display) AND the CONCRETE
        #    decoded ruleset that actually ran (buy/sell/exit trees + TP/SL) so Load/export can
        #    restore the optimized conditions directly. Keys mirror what _derive_export_payload reads.
        specs = []  # (rank, trial_cfg, strategy_params)
        for rank, params in enumerate(ranked, start=1):
            decoded = decode_params(strat, params)
            trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
            trial_cfg["name"] = f"TOP{rank}-{opt.name or expert}"
            # Persist this top-N run's trading DB (orders/transactions/recommendations) to disk
            # for post-mortem inspection — the GA trials run RAM-only for speed. The path is keyed
            # by the trial's UNIQUE backtest_id, so concurrent re-runs never collide.
            trial_cfg["persist_trading_db"] = True
            strategy_params = dict(params)
            # Unified rule model (migration 028): the CONCRETE decoded TradeRule lists that
            # actually ran (genes applied, disabled rules/actions pruned) — what Load/export
            # restores.
            if decoded.get("entry_rules") is not None:
                strategy_params["entryRules"] = decoded["entry_rules"]
            if decoded.get("exit_rules") is not None:
                strategy_params["exitRules"] = decoded["exit_rules"]
            specs.append((rank, trial_cfg, strategy_params))

        # 2) Run the re-runs (the slow part) and persist each as a tagged, saved Backtest the moment
        #    it finishes — MASTER-only DB writes. Committing INCREMENTALLY (not collect-then-commit)
        #    keeps progress visible (1/N, 2/N, ...) so the persist phase doesn't look stalled, lets a
        #    crash keep the done ones, frees each (large) result blob as we go, and unblocks the next
        #    matrix job as soon as the last one lands. Fan the re-runs across a bounded local process
        #    pool (parallel > 1) AND, if the optimization used remote workers, THOSE workers too --
        #    reusing the same worker_ids the GA phase already resolved+synced, via the worker's
        #    ``/run-trial-full`` endpoint (which runs the identical ``_persist_trial_worker`` on the
        #    remote box, so a top-N re-run is byte-identical whether it lands local or remote). Only
        #    the trading-db post-mortem file lands on whichever box actually ran it -- the master
        #    still gets the full results blob either way.
        from app.services.strategy_optimization_handler import _persist_trial_worker, _resolve_workers
        from app.services.sync_client import push_backtest

        def _persist_one(rank, trial_cfg, strategy_params, out) -> bool:
            if not out or not out.get("ok"):
                print(f"    TOP{rank} re-run failed: {(out or {}).get('error', 'no result')}")
                return False
            bt = Backtest(
                name=trial_cfg["name"], model_id=None, engine_type="daily_expert",
                expert_name=expert, optimization_id=opt_id,
                labels=bt_block.get("labels") or None,
                strategy_params=strategy_params,
                start_date=_dt.fromisoformat(str(bt_block["start_date"])),
                end_date=_dt.fromisoformat(str(bt_block["end_date"])),
                initial_capital=float(bt_block["initial_capital"]),
                status="running", started_at=_dt.now(),
            )
            db.add(bt); db.commit(); db.refresh(bt)
            _persist_results(db, bt, out["results"])
            bt.status = "completed"; bt.completed_at = _dt.now()
            bt.is_saved = True  # top performers of a job are kept
            db.commit()
            push_backtest(bt, db)
            return True

        persisted = 0
        n_local = max(1, min(int(parallel or 1), len(specs)))
        remote_workers = _resolve_workers(db, getattr(opt, "worker_ids", None))
        if remote_workers:
            from app.services.self_update import get_version_info
            from app.services.worker_client import ensure_synced
            master_version = get_version_info().get("app_version")
            remote_workers = [w for w in remote_workers if ensure_synced(w, master_version, log=print)]

        if (n_local > 1 or remote_workers) and len(specs) > 1:
            import multiprocessing as _mp
            import os as _os
            from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
            from app.services.strategy_optimization_handler import (
                _BACKEND_DIR, _WORKER_ENV_KEYS, _worker_init,
            )
            from app.services.worker_client import run_trial_full
            env = {k: _os.environ[k] for k in _WORKER_ENV_KEYS if _os.environ.get(k)}
            fitness_metric = opt.fitness_metric or "consistent_annual_return"
            print(f"    persisting top {len(specs)} across {n_local} local + "
                  f"{len(remote_workers)} remote worker(s)...")
            with ProcessPoolExecutor(
                max_workers=n_local, mp_context=_mp.get_context("spawn"),
                initializer=_worker_init, initargs=(_BACKEND_DIR, env),
            ) as local_ex, ThreadPoolExecutor(max_workers=max(1, len(remote_workers))) as remote_ex:
                # Round-robin each spec across every available slot (n_local local + one per
                # remote worker) -- with n <= save-top (default 5) this just spreads a handful of
                # re-runs across whatever capacity is on hand, no need for real load balancing.
                slots = (["local"] * n_local) + remote_workers
                futs = {}
                for idx, (rk, tc, sp2) in enumerate(specs):
                    slot = slots[idx % len(slots)]
                    fut = (local_ex.submit(_persist_trial_worker, tc) if slot == "local"
                           else remote_ex.submit(run_trial_full, slot, tc, fitness_metric))
                    futs[fut] = (rk, tc, sp2)
                for fut in as_completed(futs):
                    rk, tc, sp2 = futs[fut]
                    try:
                        out = fut.result()
                    except Exception as exc:  # noqa: BLE001 — a dead remote/local worker must not abort the rest
                        print(f"    TOP{rk} re-run raised: {exc!r}")
                        continue
                    if _persist_one(rk, tc, sp2, out):
                        persisted += 1
                        print(f"    persisted TOP{rk} ({persisted}/{len(specs)})")
        else:
            for rk, cfg, sp2 in specs:
                if _persist_one(rk, cfg, sp2, _persist_trial_worker(cfg)):
                    persisted += 1
                    print(f"    persisted TOP{rk} ({persisted}/{len(specs)})")
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
    fc.add_argument("--symbols", required=True, help="Comma-separated symbols, or @file.")
    fc.add_argument("--timeframes", default="1d", help="Comma-separated intervals (default 1d).")
    fc.add_argument("--start", required=True, help="ISO start date.")
    fc.add_argument("--end", required=True, help="ISO end date.")
    fc.add_argument("--provider", default="fmp", help="OHLCV provider (default fmp).")
    fc.add_argument("--workers", type=int, default=5)

    pw = sub.add_parser("prewarm",
                        help="Pre-build the per-symbol FMP history disk cache for the grid experts "
                             "(ratings/earnings/insider) before the GA pool spawns.")
    pw.add_argument("--symbols", required=True, help="Comma-separated symbols, or @file.")
    pw.add_argument("--experts", default="FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy",
                    help="Comma-separated experts to pre-warm. Supported: FMPRating, "
                         "FMPEarningsDrift, FMPInsiderClusterBuy, FactorRanker, "
                         "FMPSenateTraderWeight, FinnHubRating. Default: the 3 core rating/signal "
                         "experts (pass the others explicitly).")
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
    bm.add_argument("--cadence-days", "--cadence", dest="cadence_days", type=int, default=7,
                    help="Scan cadence in days (default 7 = weekly; use 1 for a DAILY store, e.g. for "
                         "FactorRanker byte-identical daily ranking). Match the analysis schedule.")
    bm.add_argument("--drop-days", type=int, default=5,
                    help="Lookback window (trading days) for the price_drop_pct metric = drop from the "
                         "trailing-window peak. MUST be >= 2 — with 1 the peak window is just today, so "
                         "price_drop_pct is always 0 and any price_drop_pct>0 screen selects nothing "
                         "(default 5 ~= 1 week).")
    bm.add_argument("--max-lookback", type=int, default=30,
                    help="Max lookback window (trading days) for the OPTIMIZABLE price-drop metric: "
                         "stores price_drop_pct_2..max columns so the GA can search the window Y<=max "
                         "from ONE store (no rebuild per value). Default 30 (~6 weeks).")
    bm.add_argument("--workers", type=int, default=8,
                    help="Parallel per-symbol fetch threads (default 8). Historical market-cap + "
                         "float fetches are disk-cached, so re-builds are fast regardless.")

    rs = sub.add_parser("recompute-screener-drops",
                        help="CACHE-ONLY rebuild of an existing store's price-drop columns (no FMP).")
    rs.add_argument("--store", default=_DEFAULT_SCREENER_STORE_DIR,
                    help=f"Path to the parquet metric-store dir (default {_DEFAULT_SCREENER_STORE_DIR}).")
    rs.add_argument("--max-lookback", type=int, default=30,
                    help="Max lookback window for the price_drop_pct_2..max columns (default 30).")
    rs.add_argument("--drop-days", type=int, default=5,
                    help="Window for the legacy price_drop_pct column (default 5; MUST be >= 2).")

    fo = sub.add_parser("fetch-options", help="Build the offline options cache from Alpaca.")
    fo.add_argument("--underlyings", required=True, help="Comma-separated symbols, or @file.")
    fo.add_argument("--start", required=True, help="ISO start date (>= 2024-02-01).")
    fo.add_argument("--end", required=True, help="ISO end date.")
    fo.add_argument("--cache-db", default=_DEFAULT_OPTIONS_CACHE_DB,
                    help=f"Path to the options-history SQLite cache (default {_DEFAULT_OPTIONS_CACHE_DB}).")
    fo.add_argument("--feed", default="indicative", help="Option chain feed (default indicative).")
    fo.add_argument("--workers", type=int, default=None,
                    help="Parallel underlyings (ThreadPoolExecutor; default $OPTIONS_FETCH_WORKERS or 6). "
                         "Each Alpaca call backs off + retries on transient drops.")
    fo.add_argument("--no-resume", action="store_true",
                    help="Re-fetch every underlying even if already cached (default: resume — skip "
                         "underlyings already in option_chain).")
    fo.add_argument("--live", action="store_true",
                    help="Authenticate contract discovery against the LIVE Alpaca environment "
                         "instead of paper — required when the resolved key is a live-only "
                         "account's (paper=True gets 40110000 'request is not authorized'). "
                         "Read-only (GetOptionContractsRequest), places no orders.")

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
    op.add_argument("--strategy", choices=sorted(_STRATEGY_BUILDERS), default="S2",
                    help="Strategy/exit variant for a ruleset expert: S1 live-import / S2 bracket / "
                         "S3 trailing / S4 target-anchored; or an option/equity strategy "
                         "(O_LC long-call, O_VERT bear-put, O_SSTG short-strangle, O_SSTD "
                         "short-straddle, O_IC iron-condor, O_JL jade-lizard, O_BF call-butterfly, "
                         "O_RS put-ratio-spread, O_CC covered-call, O_STK equity). "
                         "Ignored for bypass experts (FactorRanker).")
    op.add_argument("--universe", required=True, help="Comma-separated symbols.")
    op.add_argument("--start", required=True, help="ISO start date.")
    op.add_argument("--end", required=True, help="ISO end date.")
    op.add_argument("--fitness", default="sharpe_ratio",
                    help="Fitness metric (default sharpe_ratio). 'consistent_annual_return' (aliases "
                         "'car'/'goal') targets ~30%%/yr EVERY year: (adjusted) annualized return, "
                         "hard >=30 trades/yr gate, soft drawdown penalty beyond 20%%, x worst-year/"
                         "mean-year consistency (--fitness-trade-scale is a no-op for it).")
    op.add_argument("--generations", type=int, default=6)
    op.add_argument("--population", type=int, default=10)
    op.add_argument("--parallel", type=int, default=4, help="Parallel trials (ThreadPoolExecutor).")
    op.add_argument("--early-stop", type=int, default=4)
    op.add_argument("--mutation-prob", type=float, default=0.3,
                    help="Per-gene mutation probability (higher = more exploration). Default 0.3.")
    op.add_argument("--save-top", type=int, default=5,
                    help="Persist the top-N distinct param sets as saved Backtests (default 5).")
    op.add_argument("--seed", type=int, default=42, help="RNG seed (determinism).")
    op.add_argument("--initial-capital", type=float, default=10000.0)
    op.add_argument("--profit-cap-pct", type=float, default=None,
                    help="Cap each trade's gain at this %% of its cost basis when computing the "
                         "ADJUSTED fitness/return (e.g. 2000 = a trade can't count as more than "
                         "20x its cost). Stops one lucky mega-winner dominating the GA. Default: off.")
    op.add_argument("--profit-share-cap-pct", type=float, default=None,
                    help="Cap each trade's gain at this %% of the run's NET profit for the ADJUSTED "
                         "fitness/return (e.g. 25 = no single trade may contribute >25%% of total "
                         "return). Complements --profit-cap-pct: a trade can pass the cost-basis cap "
                         "yet still dominate the book's return; this bounds that. Default: off.")
    op.add_argument("--fitness-trade-scale", action="store_true",
                    help="Multiply each trial's fitness by min(avg_trades_per_year, cap)/100, so "
                         "statistically thin (few-trade) configs are down-weighted (~100 trades/yr = "
                         "break-even). The cap (--fitness-trade-scale-cap) bounds the factor so the GA "
                         "is NOT rewarded for over-trading (scalping). Stops a 16-trade lottery winner "
                         "from topping the search on calmar. Default: off.")
    op.add_argument("--fitness-trade-scale-cap", type=float, default=100.0,
                    help="Cap (trades/year) for --fitness-trade-scale: avg_trades_per_year is clamped "
                         "to this before scaling, so above it the factor stops growing (no scalper "
                         "incentive). Default 100 = factor maxes at 1.0 (pure thinness penalty); a "
                         "higher value allows some up-weighting up to that rate.")
    op.add_argument("--fitness-win-rate-factor", action="store_true",
                    help="Multiply each trial's (positive) fitness by 2 * win_rate_fraction, so "
                         "50%% win rate is break-even (1.0x), 100%% win rate doubles the fitness, and "
                         "0%% win rate zeroes it out. Applies to every fitness metric including "
                         "consistent_annual_return (win rate isn't part of its own formula). "
                         "Default: off.")
    op.add_argument("--labels", default="",
                    help="Comma-separated free-form tags stored on every persisted top-N Backtest "
                         "(e.g. 'goal5,S4' — one for the grid/batch id, one for the strategy). Lets "
                         "runs be filtered by ANY one of these tags via GET /api/backtests?label=..., "
                         "independent of cap-band or optimization_id. Default: none.")
    op.add_argument("--commission", type=float, default=1.0)
    op.add_argument("--slippage", type=float, default=0.0)
    op.add_argument("--fill-model", default="next_bar_open")
    op.add_argument("--interval", default="5min", help="Execution/fill clock interval (default 5min for "
                    "precise intraday TP/SL; analysis cadence is set by --run-schedule).")
    op.add_argument("--run-schedule", default="weekly", choices=["daily", "weekly"])
    op.add_argument("--run-schedule-day", default="monday",
                    help="Comma-separated day(s) for weekly --run-schedule (e.g. 'monday,thursday' "
                         "for a fast-decaying signal). Default 'monday' (single day). NOTE: for a "
                         "non-bypass expert this only seeds the static fallback (time-of-day + a "
                         "starting point) — every strategy (S1-S7) searches WHICH day(s) itself "
                         "via the schedule:<day> GA genes; this flag has no effect on the day "
                         "selection for those runs. Bypass experts (FactorRanker) don't get the "
                         "schedule genes, so this flag still fully controls their day(s).")
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
                         "S2 bracket / S3 trailing / S4 target-anchored; or option/equity strategies "
                         "O_LC,O_VERT,O_SSTG,O_SSTD,O_IC,O_JL,O_BF,O_RS,O_CC,O_STK). Each is "
                         "dispatched through _build_strategy. Bypass experts (FactorRanker) ignore this.")
    ob.add_argument("--universe", required=True, help="Comma-separated symbols (shared by all jobs).")
    ob.add_argument("--start", required=True, help="ISO start date.")
    ob.add_argument("--end", required=True, help="ISO end date.")
    ob.add_argument("--fitness", default="calmar_ratio",
                    help="Fitness metric (default calmar_ratio). See optimize --fitness for "
                         "'consistent_annual_return' ('car'/'goal').")
    ob.add_argument("--generations", type=int, default=8)
    ob.add_argument("--population", type=int, default=40)
    ob.add_argument("--parallel", type=int, default=6, help="Process-pool workers per job.")
    ob.add_argument("--early-stop", type=int, default=4)
    ob.add_argument("--mutation-prob", type=float, default=0.3,
                    help="Per-gene mutation probability (higher = more exploration). Default 0.3.")
    ob.add_argument("--save-top", type=int, default=5)
    ob.add_argument("--seed", type=int, default=42)
    ob.add_argument("--initial-capital", type=float, default=10000.0)
    ob.add_argument("--profit-cap-pct", type=float, default=None,
                    help="Cap each trade's gain at this %% of its cost basis for the ADJUSTED "
                         "fitness/return (e.g. 2000). Stops one lucky mega-winner dominating the GA.")
    ob.add_argument("--profit-share-cap-pct", type=float, default=None,
                    help="Cap each trade's gain at this %% of the run's NET profit for the ADJUSTED "
                         "fitness/return (e.g. 25 = no single trade may contribute >25%% of total "
                         "return). Complements --profit-cap-pct. Default: off.")
    ob.add_argument("--commission", type=float, default=1.0)
    ob.add_argument("--slippage", type=float, default=0.0)
    ob.add_argument("--fill-model", default="next_bar_open")
    ob.add_argument("--interval", default="5min",
                    help="Fill-clock interval (default 5min for precise intraday TP/SL).")
    ob.add_argument("--run-schedule", default="weekly", choices=["daily", "weekly"])
    ob.add_argument("--run-schedule-day", default="monday",
                    help="Comma-separated day(s) for weekly --run-schedule (e.g. 'monday,thursday'). "
                         "NOTE: for a non-bypass expert this only seeds the static fallback — every "
                         "strategy searches WHICH day(s) itself via the schedule:<day> GA genes.")
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
        "recompute-screener-drops": lambda: _cmd_recompute_screener_drops(args),
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
