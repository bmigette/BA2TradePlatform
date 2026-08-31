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
from datetime import date as _date, datetime, timedelta, timedelta as _timedelta


def _parse_symbols_arg(raw: str) -> list:
    """Comma list or ``@file`` — the idiom ``fetch-options`` already uses for its
    ``--underlyings`` flag, extended to ``fetch-cache``/``prewarm``'s ``--symbols`` so a large
    pinned universe file (e.g. ``tools/senate_universe.txt``, 498 symbols) doesn't need a giant
    comma line.

    ``@file`` splits on WHITESPACE **and** COMMAS. It used to split on whitespace alone
    ("one symbol per line"), which silently mangled the very file this project keeps its universe
    in: ``~/Documents/ba2/senate_universe.csv`` is ONE comma-separated line with no whitespace, so
    ``.split()`` returned a single 3,000-character "symbol". FMP predictably had no data for it,
    and the fetch reported success having downloaded nothing (2026-07-29). A ticker never contains
    a comma or a space, so accepting both separators cannot be ambiguous.
    """
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as f:
            raw = f.read()
    return [s.strip().upper() for s in raw.replace(",", " ").split() if s.strip()]


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
    zero_row = []  # (symbol, timeframe) pairs that "succeeded" with nothing to show for it
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        for fut in as_completed([ex.submit(_one, s) for s in symbols]):
            sym, res = fut.result()
            (overall["fetched"] if res.get("status") == "completed" else overall["failed"]).append({sym: res})
            # A handler can report status=completed having written ZERO rows. That is a failure
            # wearing a success label, and it is how a 498-symbol fetch once reported
            # "1/1 symbols (0 failed)" after downloading nothing at all (2026-07-29 --
            # a mangled symbol list meant FMP was asked for one 3,000-char ticker).
            for tf, tf_res in (res.get("results") or {}).items():
                if int((tf_res or {}).get("rows") or 0) == 0:
                    zero_row.append(f"{sym}:{tf}")
            done += 1
            if done % 25 == 0 or done == len(symbols):
                print(f"  fetch-cache: {done}/{len(symbols)} symbols "
                      f"({len(overall['failed'])} failed, {len(zero_row)} zero-row)", flush=True)
    print(json.dumps(overall, indent=2, default=str))

    if zero_row:
        # LOUD and non-zero exit: an empty fetch must never look like a completed one. Some
        # symbol/timeframe pairs legitimately have no data (delisted, pre-IPO, a thin ETF over a
        # short window) -- hence a report rather than an exception -- but the operator has to SEE
        # it, because the failure mode this guards is a whole run silently doing nothing.
        shown = ", ".join(zero_row[:15]) + (" ..." if len(zero_row) > 15 else "")
        print(f"\n!! fetch-cache: {len(zero_row)}/{len(symbols) * max(len(timeframes), 1)} "
              f"symbol/timeframe pair(s) returned ZERO rows: {shown}", flush=True)
        if len(zero_row) == len(symbols) * max(len(timeframes), 1):
            print("!! EVERY pair was empty — treat this run as FAILED (bad symbol list, "
                  "bad date range, or an exhausted API plan), not as a completed fetch.",
                  flush=True)
            return 1
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

    # start_date only drives FMPSenateTraderWeight's post-loop skill-score prewarm below
    # (_do_senate_scores) — no other expert's fetch is date-ranged (they all pull FULL
    # histories and filter in Python at read time). Left None -> that step is skipped
    # (with a printed reminder) rather than guessing a range.
    start_date = None
    if args.start:
        start_date = datetime.fromisoformat(args.start)
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=_tz.utc)

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

    # DeterministicScorer: warm the SAME fmp_history namespaces its _gather reads --
    # annual income/balance/cashflow statements (point-in-time F-Score / Altman Z /
    # quality / value / growth inputs; the backtest filters them by filing date in
    # Python) + the dated analyst-grade history for the OPTIONAL analyst section
    # (weight default 0, but the grid may switch it on). OHLCV comes from the
    # fetch-cache parquet (same reminder as FactorRanker below).
    def _do_deterministic_scorer(sym: str) -> None:
        nonlocal _details_provider
        if _details_provider is None:
            _details_provider = FMPCompanyDetailsProvider()
        for fn in (_details_provider.get_income_statement,
                   _details_provider.get_balance_sheet,
                   _details_provider.get_cashflow_statement):
            fn(symbol=sym, frequency="annual", end_date=end_date,
               lookback_periods=6, as_of=end_date, format_type="dict")
        fetch_grades_historical_cached(key, sym)
        # EARNINGS/PEAD + the ANALYST price-target leg read these two namespaces.
        # They MUST be warmed here or a hermetic trial with w_earnings>0 /
        # w_analyst>0 aborts on a cache miss -- loudly now that the fetchers no
        # longer swallow it, but still an aborted trial.
        _details_provider.get_past_earnings(symbol=sym, frequency="quarterly",
                                            end_date=end_date, lookback_periods=16,
                                            format_type="dict")
        fetch_price_target_history_cached(key, sym)

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
    # Scalper-skip floor: the GENTLEST scalper filter any GA trial for this expert will ever
    # use (the grid's min_trader_avg_hold_days floor + the fixed min_trader_hold_roundtrips).
    # A trader who fails EVEN this gentlest setting fails every stricter setting in the grid
    # too, so their skill-symbols are unreachable by any trial — safe to skip warming them.
    # Read from _EXPERT_OPT (not hardcoded) so a future grid change can't silently desync
    # prewarm from what the GA actually searches.
    _senate_opt = _EXPERT_OPT["FMPSenateTraderWeight"]
    _senate_hold_floor_days = float(_senate_opt["expert_params"]["min_trader_avg_hold_days"]["min"])
    _senate_hold_min_roundtrips = int(_senate_opt["fixed_settings"]["min_trader_hold_roundtrips"])

    def _ensure_senate_expert():
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
        return _senate_expert

    def _warm_new_traders(s, trades) -> None:
        """Discover new (not-yet-seen) traders from ``trades`` and warm their full disclosure
        history + skill-relevant buy-symbol price history. Shared by ``_do_senate`` (traders
        discovered via the per-symbol ``-trades`` endpoint) and ``_do_senate_latest`` (traders
        discovered via the unscoped ``-latest`` endpoint) -- a trader who only shows up in the
        unscoped feed (e.g. one whose per-symbol history was never queried because none of
        THIS run's universe symbols happen to trigger it) still needs their
        ``congress_trader_history`` cache entry warmed, or ``_gather_all``'s Stage 2 hits a
        hermetic ``FMPHistoryCacheMiss`` for them mid-backtest (found empirically running
        senate_profile_basket_verify.py after the ``_do_senate_latest`` fix alone: the unscoped
        feed surfaces traders like "Debbie Wasserman Schultz" that the per-symbol loop over a
        498-symbol universe never happened to discover)."""
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
            # Scalper skip: a trader excluded by even the grid's gentlest filter setting
            # contributes to NO GA trial's signal, so their (potentially thousands of)
            # buy-symbols are dead weight — skip the price-history warm entirely for them.
            hold_info = s._calculate_trader_avg_hold_days(history)
            if (hold_info["avg_hold_days"] is not None
                    and hold_info["roundtrips"] >= _senate_hold_min_roundtrips
                    and hold_info["avg_hold_days"] < _senate_hold_floor_days):
                continue
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

    def _do_senate(sym: str) -> None:
        s = _ensure_senate_expert()
        trades = (s._fetch_senate_trades(sym) or []) + (s._fetch_house_trades(sym) or [])
        s._get_price_at_date(sym, end_date)  # warms historical_price_full (full history, once)
        _warm_new_traders(s, trades)

    def _do_senate_scores(start: datetime, end: datetime) -> None:
        """Proactively compute FMPSenateTraderWeight's trader-SKILL cache
        (``congress_skill_scores.json``) for every trading day in ``[start, end]``, instead of
        leaving it to lazy per-trial computation.

        Why this is needed: ``_get_trader_skill_cached``'s key includes the exact calendar
        day (backtest mode buckets by day, not month — see its docstring), and a GA trial only
        ever scores the days its OWN schedule genes actually walk. Different individuals walk
        DIFFERENT subsets of days (e.g. "every Monday" vs "every day"), so cache reuse across
        trials is far lower than it looks: a population=4 priming run measured this live and,
        after 2+ hours and multiple full passes, still only covered 550 scattered days versus
        the thousands needed for a multi-year grid — a genuine gap the plain per-symbol fetch
        warming above does nothing to close. Looping every trading day here, once, up front,
        makes every trial's lazy lookup a guaranteed cache hit.

        Scope: only ``horizon_days``/``lookback_months`` are GA-optimized (see
        ``_EXPERT_OPT["FMPSenateTraderWeight"]``) — 3 x 2 = 6 combos. ``min_past_trades``/
        ``max_past_trades`` are fixed (not GA genes), read from the expert's own settings
        defaults so this can't silently desync from what a live/backtest run actually uses.
        Confidence scoring (``_get_trader_confidence_cached``) is intentionally NOT covered
        here: its key additionally depends on the current symbol/trade-type pair and is only
        reachable through real trade-qualification logic in ``_calculate_recommendation`` — a
        far bigger, non-grid-shaped key space that doesn't fit this same day x combo loop.
        """
        if _senate_expert is None or not _senate_seen_traders:
            return
        s = _senate_expert
        from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight
        min_past = int(FMPSenateTraderWeight._setting_or_default(None, "skill_min_past_trades"))
        max_past = int(FMPSenateTraderWeight._setting_or_default(None, "skill_max_past_trades"))

        def _grid_values(param: Dict[str, Any]) -> List[int]:
            return list(range(int(param["min"]), int(param["max"]) + 1, int(param["step"])))

        horizon_values = _grid_values(_senate_opt["expert_params"]["skill_horizon_days"])
        lookback_values = _grid_values(_senate_opt["expert_params"]["skill_lookback_months"])

        # Real trading days, not a synthetic weekday calendar: reuse the fullest already-warmed
        # price history above (any gaps in a thin/delisted symbol would silently under-cover).
        price_maps = getattr(s, "_hp_price_map", {}) or {}
        if not price_maps:
            print("!! senate skill prewarm: no warmed price history to derive trading days from "
                  "(did _do_senate run first?) — skipping.")
            return
        calendar_sym = max(price_maps, key=lambda sym: len(price_maps[sym]))
        days = sorted(
            datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=end.tzinfo)
            for d in price_maps[calendar_sym]
            if start <= datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=end.tzinfo) <= end
        )
        if not days:
            print(f"!! senate skill prewarm: {calendar_sym} has no bars in [{start.date()}, "
                  f"{end.date()}] — skipping.")
            return

        # Size the resident-shard cap to PREWARM's working set, not a trial's. The horizon x
        # lookback loops below are the INNERMOST ones, so all combos rotate on every (trader, day)
        # -- at the 3-shard trial default that is continuous eviction, and any shard evicted
        # before the final flush loses its scores entirely.
        from ba2_experts.FMPSenateTraderWeight import set_scoring_cache_max
        set_scoring_cache_max(len(horizon_values) * len(lookback_values))

        total = len(days) * len(horizon_values) * len(lookback_values) * len(_senate_seen_traders)
        print(f">> senate skill prewarm: {len(days)} trading days x {len(horizon_values)} horizons "
              f"x {len(lookback_values)} lookbacks x {len(_senate_seen_traders)} traders "
              f"({total} score computations)", flush=True)
        done_scores = 0
        t_scores = time.time()
        from ba2_experts.FMPSenateTraderWeight import _parse_ymd_utc as _parse_disc_date
        for trader_idx, name in enumerate(_senate_seen_traders, start=1):
            history = s._fetch_trader_history(name) or []
            # CRITICAL: slice the history to disclosures known as-of EACH day, exactly like
            # _gather does (FMPSenateTraderWeight.py, "Stage 2": ``[h for h in history if
            # _disclosure_date_ok(h, ceiling)]``) — the skill-cache key includes
            # ``len(history)``, so scoring the FULL history under a mid-backtest as-of day
            # writes keys no real trial ever looks up (found live 2026-07-18: 1.4M prewarmed
            # entries all keyed at today's full length, e.g. n=1003 for a trader whose
            # mid-backtest lookups used n=854..895 — near-zero cache-hit rate, and the sen-*
            # grid jobs ground at cold-cache speed as if never prewarmed). Disclosure dates
            # are parsed ONCE per trader here (not per day) — same lru-cached parser +
            # keep-on-unparseable semantics as _disclosure_date_ok.
            parsed_disc_dates = []
            for t in history:
                ds = t.get('disclosureDate', '')
                try:
                    parsed_disc_dates.append(_parse_disc_date(ds) if ds else None)
                except (ValueError, TypeError):
                    parsed_disc_dates.append(None)  # unparseable -> always kept, like _disclosure_date_ok
            for day in days:
                sliced = [t for t, d in zip(history, parsed_disc_dates) if d is None or d <= day]
                for horizon in horizon_values:
                    for lookback in lookback_values:
                        s._get_trader_skill_cached(
                            name, sliced, now=day, horizon_days=horizon,
                            min_past_trades=min_past, max_past_trades=max_past,
                            lookback_months=lookback, is_live=False)
                        done_scores += 1
            if trader_idx % 25 == 0:
                print(f"   senate skill prewarm: {trader_idx}/{len(_senate_seen_traders)} traders, "
                      f"{done_scores}/{total} scores ({time.time() - t_scores:.0f}s elapsed)", flush=True)
        # Flush EVERY shard this loop touched, each to its OWN path. The loops above walk
        # len(horizon_values) x len(lookback_values) skill shards, and scoring runs with
        # is_live=False (no throttled delta writes), so a single _save_scoring_cache of
        # self._skill_cache would persist only the LAST shard touched -- and to the unsharded
        # _SKILL_CACHE_FILE, which no trial reads. See flush_all_scoring_caches' docstring.
        from ba2_experts.FMPSenateTraderWeight import flush_all_scoring_caches
        n_shards = flush_all_scoring_caches()
        print(f">> senate skill prewarm done: {total} scores in {time.time() - t_scores:.0f}s "
              f"({n_shards} shards flushed)", flush=True)
        expected_shards = len(horizon_values) * len(lookback_values)
        if n_shards < expected_shards:
            # Loud, because the failure mode this replaced was SILENT: a multi-hour prewarm that
            # reported success while persisting a fraction of its work. The usual cause is the
            # scoring LRU evicting shards mid-loop -- prewarm's working set is every combo, so it
            # must run with BA2_SCORING_LRU_MAX >= expected_shards.
            print(f"!! senate skill prewarm: flushed {n_shards} shards but the grid spans "
                  f"{expected_shards}. Shards were evicted before the flush and their scores are "
                  f"LOST. Re-run with BA2_SCORING_LRU_MAX={expected_shards} or higher.", flush=True)

    def _do_senate_latest() -> None:
        """Warm the UNSCOPED 'latest disclosures' cache entries (``congress_senate_latest/
        ALL_FULL_HISTORY``, ``congress_house_latest/ALL_FULL_HISTORY``) that
        FMPSenateTraderWeight's basket-mode ``_gather_all`` (``analyzes_as_basket = True``,
        senate-basket-dispatch plan Task 5) reads via ``_fetch_senate_trades(symbol=None,
        full_history=True)``/``_fetch_house_trades(symbol=None, full_history=True)`` -- a
        DIFFERENT disk-cache namespace from the per-symbol ``congress_senate_trades__<SYM>``/
        ``congress_house_trades__<SYM>`` entries ``_do_senate`` above warms (those are keyed per
        symbol; this is keyed by the fixed name ``"ALL_FULL_HISTORY"``, see
        ``_fetch_congress_trades`` in ``expert_mixins.py``).

        Nothing warmed this before Task 5 added the basket dispatch path: the per-symbol prewarm
        loop above only ever calls ``_fetch_senate_trades(sym)``/``_fetch_house_trades(sym)`` with
        a real symbol, never ``symbol=None``. A real hermetic backtest of basket-mode
        FMPSenateTraderWeight therefore failed immediately with ``FMPHistoryCacheMiss`` on every
        bar ("congress_senate_latest/ALL_FULL_HISTORY not pre-warmed") until this was added.

        DEEP PAGINATION (not the original page-0-only fetch): confirmed empirically 2026-07-18
        that the original single-page fetch (``full_history=False``, ~4 months of disclosures)
        left basket-mode FMPSenateTraderWeight scoring ``trades=0, fitness=-1e9`` for EVERY
        individual across a full 2023-2026 GA matrix grid -- the unscoped fetch simply didn't
        reach back far enough for the backtest to ever see a trade. ``full_history=True`` here
        paginates the ``{chamber}-latest`` feed to its end (verified in
        ``build_senate_universe.py`` to reach back to ~2012/2019 for senate/house respectively)
        instead of a single page, and writes to the SEPARATE ``"ALL_FULL_HISTORY"`` cache key so
        a shallow-cached "ALL" entry from before this fix (or from some other still-shallow
        caller, e.g. FMPSenateTraderCopy's live path) can never silently satisfy this deep read
        -- see ``_fetch_congress_trades``'s "Pagination-depth design" docstring for the full
        reasoning. One (slower, multi-page) fetch each -- still independent of any universe
        symbol, so it runs once regardless of how many symbols are being pre-warmed.

        Also warms every trader DISCOVERED via this unscoped feed through the same
        ``_warm_new_traders`` path ``_do_senate`` uses -- the unscoped feed's trader set does
        NOT equal the per-symbol loop's trader set (a trader can appear in the disclosures
        without ever being surfaced by any of THIS run's universe symbols' own per-symbol
        ``-trades`` history), so skipping this would leave ``_gather_all``'s Stage 2 hitting a
        hermetic miss on those traders mid-backtest.
        """
        s = _ensure_senate_expert()
        print(">> senate: warming unscoped 'latest disclosures' feed (congress_senate_latest/"
              "congress_house_latest, ALL_FULL_HISTORY, full pagination)...", flush=True)
        senate_latest = s._fetch_senate_trades(symbol=None, full_history=True) or []
        house_latest = s._fetch_house_trades(symbol=None, full_history=True) or []
        print(f"   senate: {len(senate_latest)} senate + {len(house_latest)} house rows "
              f"fetched (full pagination)", flush=True)
        _warm_new_traders(s, senate_latest + house_latest)

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
        "DeterministicScorer": _do_deterministic_scorer,
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

        # Unscoped "latest disclosures" warm (basket-mode _gather_all's congress_senate_latest/
        # congress_house_latest, "ALL_FULL_HISTORY" key, deep-paginated) -- independent of any
        # universe symbol, so it runs once regardless of order relative to the per-symbol loop
        # above; placed here (serially, still inside the freeze gate) alongside the other
        # one-shot senate prewarm steps below.
        #
        # FMPSenateTraderCopy also needs this now (review 2026-07-18, finding H2):
        # basket-mode Copy's _gather was fixed to fetch full_history=True too (same
        # ALL_FULL_HISTORY cache key Weight reads), so a hermetic backtest/GA run including
        # Copy without Weight in the same run must warm it too, or it hits a cache miss on
        # the first bar.
        if "FMPSenateTraderWeight" in experts or "FMPSenateTraderCopy" in experts:
            _do_senate_latest()

        # Skill-score prewarm runs AFTER the per-symbol loop above (needs its fully-populated
        # _senate_seen_traders), and serially (not thread-pooled — it's CPU-bound in-memory work
        # over already-warmed price history, not network fetches). Still inside the freeze gate:
        # a trader-history/price cache miss here would otherwise fall through to the "live: never
        # persist" branch same as the per-symbol fetchers above.
        if "FMPSenateTraderWeight" in experts:
            if start_date is not None:
                _do_senate_scores(start_date, end_date)
            else:
                print("!! senate skill prewarm skipped: pass --start to prewarm trader-skill scores "
                      "for the full backtest date range (otherwise they're computed lazily per-trial, "
                      "which under-covers a multi-year grid — see _do_senate_scores docstring).")
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
    # Fundamentals are read AS-OF each scan date (ffill from the latest row <= that date), so a
    # series that begins exactly ON --start leaves the first scan dates with nothing to fill FROM.
    # Measured 2026-08-01: market_cap was 20.2% NaN in the build's first month (2020-01) and 0.3%
    # in every later month -- purely a boundary artifact, not missing data. Fetch fundamentals from
    # a lead-in before the window so the first scan date already has a prior value.
    _FUND_LEADIN_DAYS = 120
    _fund_start = (datetime.fromisoformat(args.start) - timedelta(days=_FUND_LEADIN_DAYS)).strftime("%Y-%m-%d")

    def _mcap(sym):
        return ms.fetch_historical_market_cap(sym, api_key, _fund_start, args.end)

    def _float(sym):
        return ms.fetch_historical_float(sym, api_key, _fund_start, args.end)

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


def _expert_run_settings(spec: dict, universe: list, overrides: "dict | None" = None) -> dict:
    """Expert settings for a run: the spec's fixed_settings, plus the run universe injected into
    the expert's own universe setting when the spec names one (PremiumSeller's static_universe —
    it reads its universe from that setting, not from enabled_instruments).

    ``overrides`` wins over fixed_settings. Used by ``--sizing-mode`` so the SAME expert spec can
    be run once per sizing mode without editing _EXPERT_OPT: sizing_mode is a 2-value categorical
    the GA has never searched (it is pinned risk_atr for the classic experts), and it is
    deliberately compared as two separate runs rather than made a gene — under notional the five
    ATR genes face no selection pressure and drift random, so a crossover that flips the mode
    would judge it with unselected parameters and bias the comparison toward whichever mode
    happens to dominate the population."""
    settings = dict(spec["fixed_settings"])
    if spec.get("universe_setting"):
        settings[spec["universe_setting"]] = ",".join(universe)
    if overrides:
        settings.update(overrides)
    return settings


def _sizing_overrides(args) -> "dict | None":
    """``{"sizing_mode": ...}`` when --sizing-mode was given, else None (spec default stands)."""
    mode = getattr(args, "sizing_mode", None)
    return {"sizing_mode": mode} if mode else None


def _apply_options_seam(spec: dict, backtest_block: dict) -> None:
    """Options experts need the offline options-cache seam: a non-None options_cache_db makes
    run_daily_backtest build + inject the HistoricalOptionsProvider (the value is forwarded
    per-trial by strategy_optimization_handler._build_daily_trial_config). No-op for equity
    experts (byte-identical)."""
    if spec.get("options"):
        from app.services.backtest.daily_backtest_handler import default_options_cache_db
        backtest_block["options_cache_db"] = default_options_cache_db()


def _apply_options_store(args, backtest_block: dict) -> None:
    """Write the RESOLVED option store onto the run's backtest block (--options-store, else
    ``BACKTEST_OPTIONS_STORE``, else ``sqlite`` — resolve_options_store owns that order).

    Unconditional, and it stores the DECISION rather than the raw flag, because a grid does not
    run in one process: ``_cmd_optimize_batch`` hands the block to the task queue and the GA
    fans trials out to remote workers, which receive ONLY {config, fitness_metric, cache_root,
    inmem_trades} (worker_client.run_trial). No environment travels with a trial, so a store
    selected by exporting BACKTEST_OPTIONS_STORE reached the master and nothing else: the worker
    re-resolved to the sqlite default and served Alpaca history for a job the master reported as
    parquet. Recording it here also makes the persisted optimization_config replayable
    (rerun_handler feeds the SAME block back through _build_daily_trial_config).

    ``sqlite`` is what an unflagged run already resolved to, so writing it changes nothing for
    existing jobs — it only stops the answer depending on who is asking.
    """
    from app.services.backtest.options_store import resolve_options_store
    backtest_block["options_store"] = resolve_options_store(
        {"options_store": getattr(args, "options_store", None)})


def _bypass_gene_space(spec: dict) -> dict:
    """GA gene space for a BYPASS expert: its expert_params plus the narrow _BYPASS_RM_OPT block
    UNLESS the spec opts out (no_bypass_rm — PremiumSeller's manager owns its exits, so the
    engine stop pass has no reader for risk_per_trade_pct and the gene would be dead weight)."""
    return {**spec["expert_params"], **({} if spec.get("no_bypass_rm") else _BYPASS_RM_OPT)}


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
            "expected_profit_percent": {"optimize": True, "min": 3.0, "max": 20.0, "step": 1.0, "type": "float"},
            # 2026-07 addition: was a hardcoded flat 8% for every BUY regardless of signal
            # strength. 'static' preserves that exact behaviour (now GA-tunable instead of
            # fixed at the class default); 'dynamic' scales expected_profit up with how far
            # the EPS surprise exceeds surprise_min_pct. dynamic_scale=0 makes 'dynamic'
            # numerically identical to 'static', so the GA can freely discover either.
            # 2026-08-27: 'model' added (ba2_experts.analyst_target_model, commit a30794ac) --
            # a fundamentals-only P/E x forward-EPS price target, replacing the surprise
            # heuristic when selected. Falls back to 'static' behaviour whenever the model
            # can't compute an estimate, so this is always safe for the GA to pick.
            "expected_profit_mode": {"optimize": True, "type": "choice",
                                     "choices": ["static", "dynamic", "model"]},
            "dynamic_scale": {"optimize": True, "min": 0.0, "max": 2.0, "step": 0.25, "type": "float"},
            # 2026-08-26 addition: a real earnings beat against a near-zero/negative analyst
            # estimate (surprise_pct's denominator) produces a mathematically-correct but
            # meaningless surprise -- e.g. reported 0.76 vs estimated -0.04 is a genuine beat,
            # but "2000% surprise" is not a number 'dynamic' mode should scale a price target
            # from. Hit live 2026-08-26 (instance 6, SA): expected_profit_percent=3977%,
            # unreachable and distorting order-priority scoring against every other candidate
            # (TradeRiskManagement.compute_order_priority_score is weighted by it directly).
            # Range 20-500: below the GA's own expected_profit_percent ceiling (20) the cap
            # would silently override 'static' mode's own tuned value, which defeats the point.
            "max_expected_profit_percent": {"optimize": True, "min": 20.0, "max": 500.0,
                                            "step": 20.0, "type": "float"},
        },
        "fixed_settings": {"sizing_mode": "risk_atr"},
    },
    "FMPInsiderClusterBuy": {
        "expert_params": {
            "lookback_days": {"optimize": True, "min": 30, "max": 120, "step": 15, "type": "int"},
            "min_insiders": {"optimize": True, "min": 2, "max": 6, "step": 1, "type": "int"},
            # 2026-08-27 addition (ba2_experts.analyst_target_model, commit a30794ac): this
            # expert's expected_profit_percent was previously a flat, never-GA-tuned constant --
            # 'model' replaces it with a fundamentals-only P/E x forward-EPS estimate, falling
            # back to the flat value whenever the model can't compute one. No 'dynamic' mode
            # exists for this expert (that's FMPEarningsDrift-specific, keyed on EPS-surprise
            # magnitude, which this expert doesn't have).
            "expected_profit_mode": {"optimize": True, "type": "choice",
                                     "choices": ["static", "model"]},
            # Same range and rationale as FMPEarningsDrift's cap of the same name (added after
            # a live 3977% blowup there): a sane anchor P/E can still compound with a large
            # following-period EPS growth estimate into an unrealistic multi-bagger target.
            "max_expected_profit_percent": {"optimize": True, "min": 20.0, "max": 500.0,
                                            "step": 20.0, "type": "float"},
        },
        "fixed_settings": {"sizing_mode": "risk_atr"},
    },
    # DeterministicScorer — LLM-free multi-section scorer. Gene choices stay PARSIMONIOUS
    # on purpose (memo §5.3: free-weight sweeps over many signals overfit fast): section
    # weights, decision thresholds, ATR multiples, and the analyst-section switch (the
    # bias question: w_analyst searches {0,0.1,0.2,0.3,0.4} so the grid itself measures
    # whether ratings add value or double-count momentum). z_veto / k_compress / periods
    # stay fixed in v1 — widen the space only after this first grid reports OOS.
    "DeterministicScorer": {
        "expert_params": {
            "w_technical": {"optimize": True, "min": 0.2, "max": 0.8, "step": 0.1, "type": "float"},
            "w_fundamental": {"optimize": True, "min": 0.1, "max": 0.7, "step": 0.1, "type": "float"},
            "w_analyst": {"optimize": True, "min": 0.0, "max": 0.4, "step": 0.1, "type": "float"},
            # Same bias question as w_analyst, for the PEAD section: FMPEarningsDrift
            # already trades this signal standalone, so the grid — not the default —
            # decides whether it ALSO pays for its weight inside the composite. Both
            # ranges start at 0.0 so "this section adds nothing" stays reachable.
            "w_earnings": {"optimize": True, "min": 0.0, "max": 0.4, "step": 0.1, "type": "float"},
            "macro_mode": {"optimize": True, "type": "choice", "choices": ["multiply", "gate", "off"]},
            # RANGE MEASURED, not guessed (2026-08-17). Every final score over 5,970 bars was
            # captured: the distribution tops out at +0.562, so the OLD 0.3-0.7 range spent
            # 0.6 and 0.7 on values that can NEVER fire (0.00% of bars) and 0.5 on 1.83% --
            # three of its five values were dead or near-dead, which is most of why 34 of 40
            # genomes in opt 333 disqualified on trade count. 0.15-0.45 is entirely live
            # (57.7% down to ~10% of bars).
            "theta_buy": {"optimize": True, "min": 0.15, "max": 0.45, "step": 0.05, "type": "float"},
            "theta_sell": {"optimize": True, "min": 0.1, "max": 0.4, "step": 0.1, "type": "float"},
            "k_stop": {"optimize": True, "min": 1.5, "max": 3.0, "step": 0.5, "type": "float"},
            "k_target": {"optimize": True, "min": 3.0, "max": 6.0, "step": 1.0, "type": "float"},
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
            # 2026-07-28: ceilings raised 60->270 / 120->365. The old fence made a 9-12 month
            # lookback UNSEARCHABLE, so "old but still-open positions carry signal" was never
            # rejected on evidence -- it was never asked. Two winners had already pushed toward
            # the old ceiling (S2 chose exec=105 of max 120), which hints the optimum may sit
            # outside it. Steps widened to keep the grid coarse rather than exploding the space.
            "max_disclose_date_days": {"optimize": True, "min": 15, "max": 270, "step": 15, "type": "int"},
            "max_trade_exec_days": {"optimize": True, "min": 30, "max": 365, "step": 30, "type": "int"},
            # Distinct genes on purpose: require_still_held is a FILTER (drop disclosers who
            # sold out), min_still_holders is a CONSENSUS floor on open positions. Bundling them
            # with min_traders would hide which one carries the signal. Only meaningful once the
            # window is long enough for sales to have happened, which is why they land together
            # with the widened ceilings above.
            "require_still_held": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
            "min_still_holders": {"optimize": True, "min": 0, "max": 4, "step": 1, "type": "int"},
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
            # Recency window for skill scoring (2026-07-15): a trader's RECENT pattern is more
            # relevant than activity from years ago, and it bounds the scan for a very
            # prolific trader. GA searches {6, 12} months.
            "skill_lookback_months": {"optimize": True, "min": 6, "max": 12, "step": 6, "type": "int"},
            # Scalper filter (2026-07-15): excludes a trader whose disclosed buy/sell
            # round-trips (FIFO-paired, same symbol) average below this many days — e.g.
            # the live-discovered case of a member of Congress with 12,958 disclosed trades,
            # functionally a day-trader rather than a conviction-position insider signal.
            # DELIBERATELY floored > 0 (never "disabled") — the senate prewarm skips
            # skill-symbol warming for any trader who fails the GRID'S OWN MINIMUM here
            # (see _SENATE_SCALPER_FLOOR_DAYS below): a 0-floor would make that prewarm
            # optimization unsound, since a trial with the filter off would need symbols
            # prewarm never fetched -> FMPHistoryCacheMiss mid-run.
            "min_trader_avg_hold_days": {"optimize": True, "min": 1.0, "max": 15.0, "step": 2.0, "type": "float"},
        },
        # min_trader_hold_roundtrips is intentionally FIXED (not GA-optimized): letting it vary
        # too would require reconciling two grid bounds for the prewarm-skip safety check below
        # instead of one. 3 round-trips is enough to distinguish "genuine scalper" from "a
        # trader who happened to flip one position quickly."
        "fixed_settings": {"sizing_mode": "risk_atr", "min_trader_hold_roundtrips": 3},
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
    # PremiumSeller is a BYPASS *options* income expert (design: docs/superpowers/specs/
    # 2026-07-24-premium-seller-expert-design.md): it sells defined-risk put credit spreads on
    # large caps and manages its own exits on manage bars (OptionPortfolioManager.manage_open).
    # options=True  -> the run gets the offline options-cache seam (options_cache_db forwarded
    #                  per-trial by _build_daily_trial_config).
    # universe_setting -> --universe is injected into its static_universe setting (it reads its
    #                  universe from that setting, NOT from enabled_instruments).
    # no_bypass_rm  -> _BYPASS_RM_OPT's risk_per_trade_pct has no reader for it: the gene prices
    #                  FactorRanker's resting protective stop (protective_stop_price), and
    #                  PremiumSeller's OptionPortfolioManager has no such stop (it owns its exits
    #                  via manage_open), so the gene would be dead weight.
    "PremiumSeller": {
        "expert_params": {
            "iv_rank_min": {"optimize": True, "min": 20.0, "max": 60.0, "step": 10.0, "type": "float"},
            "iv_hv_enabled": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
            "trend_filter_enabled": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
            "target_delta": {"optimize": True, "min": 0.15, "max": 0.40, "step": 0.05, "type": "float"},
            "target_dte": {"optimize": True, "min": 21, "max": 45, "step": 6, "type": "int"},
            "spread_width": {"optimize": True, "min": 2.5, "max": 10.0, "step": 2.5, "type": "float"},
            "min_credit_ratio": {"optimize": True, "min": 0.05, "max": 0.20, "step": 0.05, "type": "float"},
            "profit_capture_pct": {"optimize": True, "min": 25.0, "max": 75.0, "step": 25.0, "type": "float"},
            "roll_dte": {"optimize": True, "min": 14, "max": 28, "step": 7, "type": "int"},
            "risk_per_structure_pct": {"optimize": True, "min": 1.0, "max": 5.0, "step": 1.0, "type": "float"},
            "max_deployment_pct": {"optimize": True, "min": 20.0, "max": 60.0, "step": 10.0, "type": "float"},
            "dr_stop_enabled": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
            "dr_stop_credit_mult": {"optimize": True, "min": 1.5, "max": 3.0, "step": 0.5, "type": "float"},
            "tested_delta_enabled": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
        },
        # Naked structures stay OFF in v1 (defined-risk only); the earnings filter stays pinned ON.
        "fixed_settings": {"enable_short_put": False, "enable_short_strangle": False,
                           "earnings_filter_enabled": True},
        "bypass": True,
        "options": True,
        "universe_setting": "static_universe",
        "no_bypass_rm": True,
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
# Regime overlay genes (ba2_common.core.regime_overlay). Every scale applies ONLY on bars the
# benchmark is classified STRESSED and is 1.0 -- an exact no-op -- otherwise, so adding these to
# the space cannot change what a pre-existing genome scores.
#
# TWO-SIDED 0.5-2.0 ON PURPOSE. A de-risk-only range (<=1.0) would presuppose that stress means
# "take less"; two-sided lets the GA express the opposite and so TESTS the hypothesis instead of
# encoding it. 1.0 sits inside every range, which is also the leak check: enabled=1 with all three
# at 1.0 must score identically to enabled=0.
#
# regime_tp_scale is the best-motivated of the three: the stop is already ATR-scaled per symbol
# while the take-profit is a fixed percent, so reward:risk drifts with each symbol's volatility
# and the GA can otherwise only pick one compromise TP%. See
# docs/plans/2026-08-04-regime-overlay-and-car-drawdown-design.md.
_REGIME_OPT = {
    "regime_overlay_enabled": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
    "regime_risk_scale": {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
    "regime_stop_scale": {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
    "regime_tp_scale": {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
}

_RM_OPT = {
    "risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 10.0, "step": 0.5, "type": "float"},
    # SIZING budget for risk_atr, decoupled from risk_per_trade_pct (which sets the STOP DISTANCE
    # in both modes). Range 0.25-3.0 because that is where the sizing decision is REAL: measured
    # against realized daily ATR (median 2.29% of price, n=278), the per-instrument cap binds --
    # making risk_atr identical to notional -- for 71% of the grid at 3.0, 91% at 5.0 and 99% at
    # 10.0. Searching above ~3 spends the GA's budget in a region where the sizing mode provably
    # does nothing (28 of 56 goal2020 pairs came back byte-identical).
    "atr_risk_budget_pct": {"optimize": True, "min": 0.25, "max": 3.0, "step": 0.25, "type": "float"},
    "atr_multiplier": {"optimize": True, "min": 3.0, "max": 6.0, "step": 0.5, "type": "float"},
    "atr_period": {"optimize": True, "min": 7, "max": 28, "step": 7, "type": "int"},
    "min_stop_loss_pct": {"optimize": True, "min": 3.0, "max": 15.0, "step": 1.0, "type": "float"},
    "use_atr_stop": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"},
    "max_virtual_equity_per_instrument_percent": {"optimize": True, "min": 5.0, "max": 30.0, "step": 5.0, "type": "float"},
    **_REGIME_OPT,
}

#: Option jobs need a higher per-instrument ceiling than equity ones, and the setting is shared.
#:
#: A cash-secured put at spot $100 reserves strike*100 = $10,000, exactly 50% of the grid's $20k
#: account. The sizing budget is `equity * min(option_sizing%, max_virtual_equity_per_instrument
#: _percent%)`, so BOTH ranges must reach 50% — raising either alone changes nothing (see
#: _OPTION_SIZING_BANDS' full-notional row). At the old 30% ceiling the full-notional structures
#: topped out at spot $60 and could not open on most of a large-cap universe.
#:
#: SCOPED, not global: the classic equity risk manager reads the same setting, so editing
#: `_RM_OPT` in place would move every equity grid and make new results incomparable to old.
_OPTION_RM_OVERRIDE = {
    "max_virtual_equity_per_instrument_percent": {
        "optimize": True, "min": 5.0, "max": 50.0, "step": 5.0, "type": "float"},
}


def _rm_opt_for(kind: str) -> dict:
    """The classic-RM gene block for a strategy kind: ``_RM_OPT``, plus the option override.

    EVERY option kind gets the 50% ceiling EXCEPT ``O_STK``, and that exclusion is the whole
    point of the function. ``O_STK`` is ``_build_strategy_stock`` -> ``_build_strategy_S2``, i.e.
    the plain-equity BASELINE the option strategies are measured against. Widening its
    per-instrument cap would make it incomparable both to ``S2`` (still 30) and to every prior
    ``O_STK`` run — destroying the control arm in the name of helping the treatment arms.

    ``O_CC`` and ``O_PP`` DO get it, even though they are equity-entry: a covered call must fund
    100 shares, which at spot $100 is $10,000 — 50% of the grid's $20k account, exactly the same
    constraint as a cash-secured put. Capping them at 30% would pin them to spot $60, which is
    the constraint the raise exists to relieve.

    Gating on ``_PURE_OPTION_STRATEGIES`` instead would be the natural-looking fix and is wrong
    for that reason.
    """
    if kind in _OPTION_STRATEGY_KEYS and kind != "O_STK":
        return {**_RM_OPT, **_OPTION_RM_OVERRIDE}
    return dict(_RM_OPT)


# Bypass experts (FactorRanker) size their own portfolio and skip the classic per-trade RM
# entirely, EXCEPT for one piece it still reuses: risk_per_trade_pct is the per-name
# max-loss-vs-equity budget, which FactorPortfolioManager.protective_stop_price turns into the
# RESTING stop price attached to each entry (before 2026-08-06 the same number instead drove a
# per-bar stop pass inside the engine; the gene's meaning is unchanged). Before 2026-07-19 this was left off bypass experts' gene space entirely,
# so it sat frozen at MarketExpertInterface's 1.0% default for every band/individual — measured
# as a major contributor to small-band FactorRanker underperformance (26.2% of small-band trades
# were quick same-day stop-outs vs 3.1% for large). Exposed as its own (narrower) gene set so the
# GA can actually tune it, instead of the full _RM_OPT block (whose other keys — ATR/min-stop/
# max-virtual-equity — have no bypass-path reader).
_BYPASS_RM_OPT = {
    "risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 10.0, "step": 0.5, "type": "float"},
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
    # Floor lowered 1.0->0.0 (2026-07-19): FactorRanker's own docs say this gate is
    # penny-momentum-oriented and should be 0 for factor strategies, but the floor never let the
    # GA reach 0. Measured against the live metric_store at the GA's winning mid/small-band gene
    # combos: 0-5 candidates/week (mid), 0 candidates on 4/5 sampled dates (small) — this gate was
    # the primary cause of small-band FactorRanker trade starvation, not a lack of factor edge.
    # screener_price_drop_pct already floors at 0 for every expert; this brings rvol_min in line.
    "screener_relative_volume_min": {"min": 0.0, "max": 3.0, "step": 0.1, "type": "float", "optimize": True},
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
    """S1 -- graded-conviction entry tiers + entry TP/SL bracket. EXPERT-AGNOSTIC, like S2-S7.

    WHAT CHANGED (2026-08-17) AND WHY. S1 used to be the ONLY strategy that loaded a per-expert
    JSON (docs/live_rulesets/{expert}.json, falling back to docs/default_rulesets/). Every other
    builder (S2, S3, S5, S6, S7) takes just a name and constructs its rules in code, so S1 was
    the odd one out in three ways that all cost real time:

      * CHICKEN-AND-EGG for any new expert. S1 needed a LIVE export, a live export needs a
        deployed instance, and you would not deploy before optimizing. DeterministicScorer hit
        exactly this: all 6 of its S1 jobs (3 bands x 2 sizing modes) died instantly with
        exit=1 while the grid scrolled past, so a third of its matrix silently never ran.
      * DRIFT. A live export changes whenever someone edits the live ruleset, so re-running an
        old S1 job could search a different space than it did the first time.
      * INCONSISTENCY. "S1" meant "replicate THIS expert's live config" for four experts and
        "does not exist" for the rest, so S1 numbers were not comparable across experts.

    The tiers key off fields EVERY expert's recommendation carries -- confidence,
    expected_profit, the term/risk flags -- not anything expert-specific, which is why one
    template serves all of them. The expert contributes its own DEFAULT SETTINGS (plus whatever
    `model:` genes the run searches); the strategy contributes the rules. That is exactly how
    S2-S7 already worked.

    SHAPE (preserved from the live rulesets this replaces): an OR of AND-tiers, each a
    conviction band -- high conviction takes a bigger expected-profit demand, lower tiers relax
    it -- plus a GA-toggleable entry bracket (target-anchored TP, protective SL). Every
    threshold is optimizable and every tier is droppable via its rule-level toggle, so the GA
    can retire a tier outright rather than only loosening its leaves.
    """
    from app.models.strategy import Strategy

    def _tier(tid, conf, conf_min, conf_max, prof, prof_min, prof_max, extra_flags):
        conds = [
            {"id": f"{tid}_bull", "field": "bullish"},
            {"id": f"{tid}_conf", "field": "confidence", "op": ">=", "value": conf,
             "optimize": True, "value_min": conf_min, "value_max": conf_max, "value_step": 5,
             "toggle_optimize": True},
            {"id": f"{tid}_prof", "field": "expected_profit", "op": ">=", "value": prof,
             "optimize": True, "value_min": prof_min, "value_max": prof_max, "value_step": 1,
             "toggle_optimize": True},
        ]
        # Term/risk flags are ADVISORY here and individually droppable: an expert that never
        # sets them (they are optional recommendation attributes) would otherwise have every
        # tier permanently false -- the silent-inertness failure this whole rewrite is about.
        for f in extra_flags:
            conds.append({"id": f"{tid}_{f}", "field": f, "toggle_optimize": True})
        conds.append({"id": f"{tid}_flat", "field": "has_no_position"})
        return {"id": f"grp_{tid}", "type": "AND", "conditions": conds}

    buy = {"id": "root", "type": "OR", "conditions": [
        _tier("t1", 70, 40, 90, 8, 3, 15, ["long_term", "lowrisk"]),
        _tier("t2", 60, 35, 85, 6, 2, 12, ["medium_term", "mediumrisk"]),
        _tier("t3", 50, 30, 75, 4, 1, 10, []),
    ]}

    exits = [
        {"id": "s1_sl_hold", "action_type": "adjust_stop_loss",
         "reference_value": "order_open_price",
         "action_value": -8.0, "action_value_optimize": True,
         "action_value_min": -20.0, "action_value_max": -3.0, "action_value_step": 2.0,
         "conditions": {"type": "AND", "conditions": [
             {"id": "s1_hold", "field": "has_position"}]}},
        {"id": "s1_exit_signal", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "s1_bear", "field": "bearish"}]}},
        {"id": "s1_exit_time", "action_type": "close", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "s1_days", "field": "days_opened", "op": ">", "value": 120,
              "optimize": True, "value_min": 30, "value_max": 200, "value_step": 30}]}},
    ]
    exits = _first_match_order(exits, ["s1_exit_time", "s1_exit_signal", "s1_sl_hold"])

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
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
    "O_JL": {  # jade lizard (credit)
        "action_type": "open_jade_lizard", "option_strike_method": "percent_otm",
        "option_strike_param": 10.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 6.0,
        "option_strike_param_max": 16.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
    "O_BF": {  # long call butterfly (debit)
        "action_type": "open_call_butterfly", "option_strike_method": "percent_otm",
        "option_strike_param": 0.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 8.0, "option_wing_width_pct": 10.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 5.0,
        "option_wing_width_max": 15.0, "option_wing_width_step": 2.5,
        # The BODY (the 2x short leg) is where a butterfly's whole thesis lives -- it is the
        # price the structure is betting the underlying pins to. It was frozen at 0.0 (always
        # ATM), so the GA could only ever tune how WIDE the wings were around a bet it was
        # never allowed to place. Kept modest (0-6% OTM): past that the debit collapses and
        # the structure degenerates into a lottery ticket.
        "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
        "option_strike_param_max": 6.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_RS": {  # put ratio spread (credit/even)
        "action_type": "open_put_ratio_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 5.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 15.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 2.0,
        "option_strike_param_max": 10.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
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
    "O_BULLCS": {  # bull call vertical (debit) — the bullish mirror of O_VERT (bear put
        # vertical). Same 2-leg select_vertical_spread mechanism, single strike_param shared
        # by both legs (OpenBullCallSpreadAction._spread_params dedups by strike).
        "action_type": "open_bull_call_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 2.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
        "option_strike_param_max": 6.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_BEARCS": {  # bear call vertical (credit, defined risk) — sell the near-the-money leg,
        # buy further OTM as protection. Directional-bearish credit, so it sits alongside
        # O_JL/O_RS's skewed-credit group (OS3) rather than OS2's delta-neutral one.
        "action_type": "open_bear_call_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 8.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 15.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 4.0,
        "option_strike_param_max": 16.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_BULLPS": {  # bull put vertical (credit, defined risk) — the PUT mirror of O_BEARCS:
        # sell the nearer-the-money put, buy further OTM as protection. Directional-BULLISH
        # credit, so it sits in OS3's skewed-credit group alongside O_BEARCS rather than in
        # OS2's delta-neutral one. It is the group's (and, after the affordability filter,
        # the whole searched credit set's) only BULLISH defined-risk short-premium
        # expression — before it, the sell arm could bet down (O_BEARCS) or sideways (O_IC)
        # and nothing else. Reserves (width - credit)*100 like any vertical: $160-$1,280 per
        # contract on this universe, i.e. affordable everywhere O_BEARCS is.
        "action_type": "open_bull_put_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 8.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 15.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 4.0,
        "option_strike_param_max": 16.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_CSP": {  # cash-secured put (credit, income) — sized off strike*100 reserve, not
        # premium (see SellCashSecuredPutAction). Further OTM than O_LP's debit purchase
        # is typical (reduce assignment risk while still collecting premium).
        "action_type": "sell_cash_secured_put", "option_strike_method": "percent_otm",
        "option_strike_param": 10.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 5.0,
        "option_strike_param_max": 20.0, "option_strike_param_step": 2.5,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
    "O_STRD": {  # long straddle (debit, non-directional) — profits from a big move EITHER
        # way (e.g. ahead of earnings). OpenStraddleAction always selects ATM internally
        # (ignores strike_param), so only dte/sizing matter here.
        "action_type": "open_straddle", "option_strike_method": "percent_otm",
        "option_strike_param": 0.0, "option_dte_min": 20, "option_dte_max": 40,
        "option_sizing": 5.0,
        "option_dte_optimize": True, "option_dte_min_range": 10,
        "option_dte_max_range": 55, "option_dte_step": 5},
    "O_STRG": {  # long strangle (debit, non-directional) — cheaper than the straddle, needs
        # a bigger move to pay off. strike_param is the OTM% for BOTH legs (symmetric).
        "action_type": "open_strangle", "option_strike_method": "percent_otm",
        "option_strike_param": 5.0, "option_dte_min": 20, "option_dte_max": 40,
        "option_sizing": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 2.0,
        "option_strike_param_max": 12.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 10,
        "option_dte_max_range": 55, "option_dte_step": 5},
}

# Directional entry gate per pure-option strategy: which signal flag the entry rule requires.
# Every original O_* key fires on the expert's BULLISH signal (including O_VERT — a bearish
# STRUCTURE opened on a bullish signal as a hedge-shaped premium play, the original grid
# semantics, kept unchanged). O_LP and O_BEARCS (both bearish structures) are the true
# bearish-signal entries. O_STRD/O_STRG (non-directional vol plays) keep the "bullish"
# default here too, but the gate condition is toggle_optimize=True in _option_entry_rule, so
# the GA can turn direction-gating off entirely and let them fire on either signal.
_OPTION_ENTRY_GATE = {k: "bullish" for k in _OPTION_STRATS}
_OPTION_ENTRY_GATE["O_LP"] = "bearish"
_OPTION_ENTRY_GATE["O_BEARCS"] = "bearish"

# FULL-NOTIONAL structures: those whose per-contract buying-power reserve scales with the
# STRIKE (cash-secured / un-netted naked notional) rather than with a defined-risk spread
# width or a Reg-T margin bracket. On this large-cap universe at the grid's $20k capital they
# cannot open at all on the expensive names, and eat the account on the mid-priced ones.
# Measured per-contract reserve (option_reserve_required, 2026-07-25) vs $20,000:
#
#     structure           spot 40   spot 100   spot 200   spot 320
#     cash_secured_put      3,600      9,000     18,000    28,800  <- over the whole account
#     jade_lizard           3,760      9,400     18,800    30,080  <- over the whole account
#     put_ratio_spread      3,580      8,950     17,900    28,640  <- over the whole account
#     ---- vs the affordable ones ----
#     short_strangle          400      1,000      2,000      3,200
#     short_straddle          800      2,000      4,000      6,400
#     bear_call_spread        160        400        800      1,280
#     bull_put_spread         160        400        800      1,280
#     iron_condor             160        400        800      1,280
#
# That is why v8's OS2/OS3 only ever traded the cheapest underlyings (BAC $41, INTC $35) and
# died out after a handful of positions. They stay defined in _OPTION_STRATS and remain
# runnable as EXPLICIT single-strategy jobs (`--strategies O_CSP`) for a larger-capital run;
# they are only excluded from the DEFAULT grouped search at $20k. Re-add them here (or raise
# --initial-capital to ~$100k) to search them again.
_FULL_NOTIONAL_OPTION_KINDS = {"O_CSP", "O_JL", "O_RS"}

# GROUPED option strategies: ONE optimize job searching a FAMILY of similar structures.
# Each member becomes its own toggleable entry TradeRule (entry:<member>-entry:enabled gene)
# carrying its own option action + option_* genes, so the GA can turn structures on/off and
# tune each independently — top-5 individuals can land on DIFFERENT structures, giving the
# saved top-N variety instead of 5 near-clones of one structure.
#
# The FULL taxonomy lives here; the affordability filter below derives what actually gets
# searched, so the two can never drift apart.
_OPTION_GROUPS_ALL = {
    "OS1": ["O_LC", "O_LP", "O_VERT", "O_BF", "O_BULLCS"],  # directional DEBIT (long premium / defined)
    "OS2": ["O_SSTG", "O_SSTD", "O_IC", "O_CSP"],           # neutral CREDIT (short premium)
    # OS3 is the DIRECTIONAL/skewed credit family. O_BULLPS is its (and, after the
    # affordability filter below, the entire searched credit set's) only BULLISH member:
    # without it the sell arm could express bearish (O_BEARCS) and neutral (OS2's O_IC)
    # short premium and nothing else.
    "OS3": ["O_JL", "O_RS", "O_BEARCS", "O_BULLPS"],        # skewed CREDIT (asymmetric short premium)
    "OS4": ["O_STRD", "O_STRG"],                            # volatility DEBIT (non-directional)
}
_OPTION_GROUPS = {
    key: [m for m in members if m not in _FULL_NOTIONAL_OPTION_KINDS]
    for key, members in _OPTION_GROUPS_ALL.items()
}
# A group whose every member was filtered out would silently produce a job with no entries
# (and a -1e9 zero-trade sentinel for every trial) — fail loudly at import instead.
_empty_groups = [k for k, v in _OPTION_GROUPS.items() if not v]
if _empty_groups:
    raise RuntimeError(
        f"_OPTION_GROUPS {_empty_groups} have no affordable members left after excluding "
        f"{sorted(_FULL_NOTIONAL_OPTION_KINDS)}; drop the group or relax the exclusion.")

# Pure-option strategy keys (entry is the option action; no equity leg). O_CC/O_STK are equity.
#
# ``O_WHEEL`` is listed EXPLICITLY because it is a COMPOSITE: it has no ``_OPTION_STRATS`` row of
# its own (it reuses O_CSP's entry wholesale — see ``_build_strategy_wheel``), so the derived
# ``set(_OPTION_STRATS)`` cannot find it. It is nonetheless pure-option in every sense the two
# consumers of this set care about, and both would be WRONG if it were classed with the
# equity-entry overlays:
#
#   * ``_resolve_fitness`` — the wheel's book is an option book, and calmar/sharpe on an option
#     book rewards barely-trading low-drawdown configs (the v6 OS evidence in that docstring).
#   * ``_assert_option_window_excludes_holdout`` — the wheel reads the options cache, so a
#     window running into 2026 spends the reserved walk-forward set exactly as OS1 would.
#
# Nothing indexes ``_OPTION_STRATS[kind]`` off this set (checked: the only two readers are the
# two above), so a member with no row is safe here.
_PURE_OPTION_STRATEGIES = set(_OPTION_STRATS) | set(_OPTION_GROUPS) | {"O_WHEEL"}
# All launcher option/equity strategy keys handled by the option builders.
_OPTION_STRATEGY_KEYS = _PURE_OPTION_STRATEGIES | {"O_CC", "O_PP", "O_STK"}


# ==============================================================================================
# BEFORE YOU LAUNCH AN OPTION GRID — READ THIS
# ==============================================================================================
#
# Four preconditions, as of 2026-08-27. One will stop a run dead, one will let it run and
# waste the compute, the third is now CLOSED and recorded so nobody re-opens it, and the fourth
# blocks nothing today — it blocks COMPARING today's run against one launched after phase 3.
# None is a defect in this file — they are the ragged edges left by the option work of
# 2026-08-25/27, recorded here because this is where somebody stands when they decide to press
# go.
#
# 1. THE BACKTEST READS AN ALPACA STORE, SO 2023 IS STILL REFUSED.  ** BLOCKING **
#    The floor is no longer global: `ba2_providers.options.options_history_floor` answers PER
#    VENDOR (Alpaca 2024-01-18 measured; dxfeed/TastyTrade 2022-10-01, env-overridable), and
#    `daily_backtest_handler.validate_options_window` consults the floor of the vendor serving
#    the store the run actually reads. That much is fixed.
#
#    UPDATED 2026-08-28 — THE PARQUET STORE IS NOW READABLE BY THE BACKTEST, BUT YOU MUST ASK
#    FOR IT. There are two readers behind one seam (`backtest/options_store.py`):
#
#      * `sqlite` (THE DEFAULT) -> `HistoricalOptionsProvider` over the Alpaca-built
#        `OptionsHistoryCache`. Vendor `alpaca`, floor 2024-01-18. Every backtest number on
#        record came from here, and it stays the default for exactly that reason.
#      * `parquet` -> `ParquetOptionsProvider` over `CACHE_FOLDER/TastyTradeOptionsProvider/`.
#        Vendor `tastytrade`, floor 2022-10-01.
#
#    A pure-option job over the 2023-01-01 window below therefore STILL raises unless the run
#    selects the parquet store — set `options_store: "parquet"` on the backtest block (it is
#    forwarded per trial by `strategy_optimization_handler._build_daily_trial_config`), or
#    export `BACKTEST_OPTIONS_STORE=parquet` for the whole job. Check the tree covers the
#    window first: locally it holds 686 underlyings over 2023-01-03..2023-03-31 ONLY, so a
#    2023-01-01..2025-12-31 grid would read an empty store for 33 of its 36 months.
#    A run still cannot span two vendors: one reader is built, and
#    `backtest_options_provider()` answers for that one.
#
#    Do NOT instead lower the Alpaca number: measured on the shared 10.9 GB cache (2026-08-26)
#    it holds 0 bars before 2024-01-18, its earliest bar is 2024-02-01, and its only three
#    chain snapshots are 2024-02-01 / 2026-03-23 / 2026-06-09. There is no 2023 in it, and a
#    floor that lies produces a backtest that trades on nothing and reports it as a result —
#    strictly worse than this refusal.
#
#    (Noted in passing: even Alpaca's 2024-01-18 is ~2 weeks optimistic against that store,
#    whose first chain snapshot is 2024-02-01. A vendor floor bounds what COULD have been
#    fetched, not what was.)
#
# 2. THE GREEKS ARE NOT IN THE CACHE YET.  ** WASTES THE RUN **
#    `iv_rank` and `iv_to_realized_vol` became live genes on 2026-08-26 (OPT-C1, OPT-C3). Both
#    fail CLOSED when implied volatility cannot be measured, which is correct — but the current
#    option cache carries no greeks at all, so `get_atm_iv` returns None on every bar. With each
#    gate independently enabled at p=0.5, roughly 75% of every generation will score the
#    zero-trade sentinel, and a plain (non-optimize) option backtest will trade nothing at all.
#    The search recovers once the data lands; until then the run is mostly burning CPU on
#    -1e9. TastyTrade collection was in progress on another machine — confirm it finished,
#    AND that the run actually reads it (see precondition 1: the parquet store is readable
#    now, but only when `options_store: "parquet"` is selected; the sqlite default still has
#    no greeks). On the parquet store `get_atm_iv` DOES return a number — the greeks are
#    Black-Scholes-inverted per bar at read time (`option_greeks.compute_iv_and_greeks`,
#    the same function that filled the sqlite store's) rather than baked in at build time.
#    Sharper since 2026-08-26: `_compute_atm_iv` no longer falls back to the frozen
#    chain-snapshot row, so where a stale row used to supply a number it now honestly supplies
#    None. That removes a lookahead, and it also removes the last thing masking this gap.
#
#    RELATED, AND IT WILL BITE THE LONG TERMS SPECIFICALLY: `fetch_options._EXPIRY_TAIL_DAYS`
#    is 60, so the cache only holds contracts expiring within 60 days of the run's END date.
#    From a bar at date `d` the largest DTE any chain can contain is `run_end + 60 - d`. Once
#    `OptionTerm` becomes a gene (phase 5) with HARD windows and no widening, THREE_MONTHS
#    (76-149), SIX_MONTHS (150-269) and LEAPS (270-1095) therefore refuse outright near the end
#    of every run, and LEAPS is unusable in any run shorter than about fifteen months. The GA
#    would learn "LEAPS is bad" for a data-availability reason with no economic content. Either
#    raise the tail before searching terms, or restrict the term gene's choice set to what the
#    fetch window can actually serve.
#
# 3. THE ARC RICHNESS GATE IS ENFORCED AND SEARCHED.  ** CLOSED 2026-08-26 **
#    All eight credit builders in `ba2_common.core.TradeActions` now call
#    `_refuse_if_arc_below_floor` beside their `net_credit <= 0` check, so a credit structure
#    is no longer admitted on a positive credit alone, and an UNMEASURABLE return on
#    collateral refuses rather than passes. `option_min_arc` is a searched gene per credit
#    structure (`_OPTION_ARC_BANDS` below), banded by collateral family because ARC's
#    denominator comes from the structure's own reserve branch.
#    Two things to know before reading the results: the bands are DERIVED from the reserve
#    arithmetic, not measured against a realised ARC distribution (there has been no option
#    grid to measure), so re-centre the ceilings after the first run; and the gate is a no-op
#    for every debit structure and for O_CC/O_PP, which post no collateral.
#
# 4. THE SIZING GENES ARE ABOUT TO CHANGE HANDS, AND THAT MAKES OLD RESULTS INCOMPARABLE.
#    ** NOT BLOCKING TODAY; BLOCKING FOR ANY CROSS-PHASE COMPARISON **
#    `option_sizing`, `option_min_arc` and the strike-method genes are consumed by exactly one
#    thing: the entry action's own sizing tail. Until 2026-08-27 that tail was `_size` /
#    `_size_by_reserve` inlined at the bottom of each `_build_and_submit`. Phase 2a (landed
#    2026-08-27) split the 7 premium-sized builders into a `_resolve()` that prices the
#    structure and a shared `_size_and_submit` that sizes it, folding both sizers into a single
#    `_size_by_cost` = `floor(budget / cost_per_contract)`, which is what each already computed.
#    Grids run before and after that split ARE comparable. Phase 3 is where that stops being
#    true: sizing moves to an option risk manager that triages several structures against
#    ONE budget, and `option_sizing` stops meaning "this structure's share of equity" and starts
#    meaning "this structure's CAP within a shared per-instrument budget". Same name, different
#    quantity. A grid run across THAT boundary is comparing two different experiments.
#
#    There are three sizing families, not one, which is why the split lands in three parts:
#    7 builders size off premium (`_size`, converted in 2a), 8 off collateral
#    (`_size_by_reserve`, phase 2b), and 2 — covered call and protective put — size off HELD
#    SHARES and ignore `option_sizing` entirely (phase 2c). Any conclusion drawn about
#    `option_sizing` from an O_CC or O_PP arm is a conclusion about a gene that arm never read.
#
#    Also recorded here because this is where somebody stands when they press go: option entry
#    actions are reached from FOUR paths — the enter_market ruleset, the open-positions overlay
#    ruleset, the unified TradeRule entry path, and the PremiumSeller bypass expert, which opens
#    option positions with no TradeAction and no risk manager at all (its only rails are
#    `_within_rails` / `_book_totals`). A grid arm using PremiumSeller is not exercising any of
#    the option RM work.
#
# CLOSED 2026-08-26: `_option_consistent_annual_return` -- the DEFAULT fitness for pure-option
# grids -- now takes its trade frequency from `_trades_per_year` (STRUCTURES) instead of
# `avg_trades_per_year` (LEGS), the substitution Track C already applied to CAR. Three iron
# condors a year no longer clears the 12/yr disqualification floor, and the ramp is measured in
# bets rather than legs. If you are comparing against results banked before this date, note
# that thin multi-leg genomes ranked higher then than they will now. The equity metric is
# unchanged (the 798-literal frozen corpus is green), and a drift guard now refuses any read of
# `avg_trades_per_year` outside `_trades_per_year`.
#
# CLOSED 2026-08-26: `options_provider._compute_atm_iv` no longer falls back to the
# chain-snapshot row (OPT-C8). That row's IV had no as-of guarantee and, in the case where the
# fallback actually fired, was inverted from a future price by construction — which mattered
# more once iv_rank became a searched gene. It now reads only the as-of-clamped bar and
# returns None otherwise. See precondition 2: this makes the missing greeks MORE visible, not
# less, which is the point.
#
# ==============================================================================================


# --- the 2026 walk-forward holdout ----------------------------------------------------------
#
# The option grid searches 2023-01-01 .. 2025-12-31. 2026 is RESERVED: walk-forward validation
# on it is a separate exercise, and it is only worth anything if the search never saw the data.
# A grid run that quietly extends past the boundary spends the answer key on the exam.
#
# This is a RAIL, not a setting. There is no flag to switch it off: the walk-forward exercise
# will move this constant deliberately, as a reviewed change, rather than as an argument
# someone can paste into a shell script and forget.
#
# Scoped to PURE-OPTION jobs. Non-option backtests are running against 2026 windows right now
# and must not be disturbed by an option-grid policy.
#
# NOTE the interaction with precondition 1 above: this rail guards the END of the window, and
# the history floor guards the START. They currently disagree about whether 2023 exists. The
# rail is right; the disagreement is what needs resolving.
_OPTION_HOLDOUT_START = _date(2026, 1, 1)


def _assert_option_window_excludes_holdout(strat_kinds, end) -> None:
    """Refuse a pure-option optimize job whose window reaches into the reserved 2026 holdout."""
    option_kinds = sorted(set(strat_kinds) & _PURE_OPTION_STRATEGIES)
    if not option_kinds:
        return
    try:
        last = _date.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        sys.exit(f"ba2-test: --end {end!r} is not an ISO date")
    if last >= _OPTION_HOLDOUT_START:
        sys.exit(
            f"ba2-test: --end {last.isoformat()} reaches into the reserved walk-forward "
            f"holdout (everything from {_OPTION_HOLDOUT_START.isoformat()}). The option grid "
            f"must stop at {(_OPTION_HOLDOUT_START - _timedelta(days=1)).isoformat()} or the "
            f"validation set is spent on the search. Affected strategies: "
            f"{', '.join(option_kinds)}. If you are deliberately running the walk-forward "
            f"exercise, move _OPTION_HOLDOUT_START in ba2test_launcher.py as a reviewed "
            f"change -- there is no flag for this.")


def _resolve_fitness(cli_fitness: str | None, strat_kind: str, stock_default: str) -> str:
    """Effective fitness metric for an optimize job. An explicit --fitness always wins.
    Otherwise PURE-OPTION kinds (O_* except the equity-entry O_CC/O_PP/O_STK, and the
    OS1-OS4 groups) default to ``option_consistent_annual_return`` — the option-specific
    variant of the ~30%/yr goal metric (annualized return x drawdown guard x
    worst-year/mean-year consistency x trade-frequency gate). Calmar/sharpe on option books
    rewards barely-trading low-drawdown configs (v6 OS runs on calmar: 2-27 trades, TR
    3.6-18%). STOCK kinds (and the equity-entry option overlays) keep the command's
    historical default (``stock_default``) so equity tuning is untouched.

    WHY A SEPARATE METRIC RATHER THAN A FLAG INSIDE ``consistent_annual_return``. There are
    non-option backtests running against the equity metric right now, and their scores must
    not move. A metric that is not SELECTED is never called; a flag inside a shared metric has
    to be read correctly on every path, and can be read wrongly. So the option grid points at
    its own metric and ``_consistent_annual_return`` is left bit-for-bit alone.

    ``option_consistent_annual_return`` (aliases ``option_car`` / ``ocar``) is registered in
    ``strategy_fitness.py`` on the ``option-fitness`` branch; this returns its NAME, and
    ``compute_fitness`` resolves it once that branch is merged.
    """
    if cli_fitness:
        return cli_fitness
    return ("option_consistent_annual_return" if strat_kind in _PURE_OPTION_STRATEGIES
            else stock_default)


# Minimum DAILY TRADED VOLUME a contract must show to be SELECTABLE (2026-07-25).
#
# NOT a strategy parameter and deliberately NOT GA-searchable -- it is a TRADABILITY floor, the
# same category as option_selector._MIN_TRADEABLE_PREMIUM. Exposed to the GA it would simply be
# driven to 0, because relaxing it unlocks fills the backtest will happily model and the market
# would never give.
#
# Why 25: the fill engine independently caps an order at _OPTION_FILL_MAX_VOLUME_PARTICIPATION
# (10%) of the bar's volume, so a contract must trade >= 10x the order size for the order to
# fill at all. At $20k with defined-risk structures orders run 1-3 contracts, so ~25 is the
# floor at which a 2-3 lot is fillable. Measured over 13.7M cached bars the distribution is
# p10=1, p25=3, p50=14, p75=71, p90=319 contracts/day -- i.e. this rejects roughly the bottom
# half of the chain, which is exactly the half the engine could not have filled anyway.
_OPTION_MIN_VOLUME_DEFAULT = 25
# Set from --option-min-volume at command entry; module-level because _option_entry_action_for
# is called deep in the strategy builders, far from the parsed args.
_OPTION_MIN_VOLUME = _OPTION_MIN_VOLUME_DEFAULT

# Default cap on the UNDERLYING price for the gate-only screener entry gate
# (--screener-gate-store): the options grid runs at $20k, where full-notional structures on
# $100+ underlyings reserve more than the account (see the reserve table above
# _FULL_NOTIONAL_OPTION_KINDS). 100 keeps every grid structure openable on gated names.
_MAX_STOCK_PRICE_DEFAULT = 100.0


def _option_entry_action_for(kind: str) -> dict:
    """The option ENTRY action config for a pure-option strategy key (a fresh copy).

    Injects ``option_min_volume`` (see _OPTION_MIN_VOLUME_DEFAULT). Before 2026-07-25 the
    min_volume gate existed all the way down the stack -- passes_liquidity, the three
    selectors, the action classes, the rule-builder aliases -- but NO caller ever set a value,
    so it was inert and the selector kept handing the fill engine contracts it would reject.
    """
    cfg = dict(_OPTION_STRATS[kind])
    _apply_option_min_volume(cfg)
    _apply_option_strike_method_gene(cfg)
    _apply_option_sizing_gene(cfg)
    _apply_option_min_arc_gene(cfg)
    _apply_option_entry_cross_gene(cfg)
    return cfg


# --- premium richness as a gene (OPT-C1) ----------------------------------------------------
#
# Credit structures were admitted on `net_credit > 0` alone -- no minimum credit, no
# credit-as-a-fraction-of-width, no return floor. `TradeActions` now consults
# `option_economics.annualized_return_on_collateral` in every credit builder, and this is what
# lets the GA SEARCH the floor rather than inherit somebody's guess at it.
#
# THE BAND IS PER COLLATERAL FAMILY, not shared, because ARC is a ratio whose denominator is
# set by the structure's reserve branch (`OptionsAccountInterface.option_reserve_required`),
# and those branches differ by an order of magnitude. One shared window would be unsatisfiable
# for the full-notional structures and inert for the defined-risk ones -- the OPT-C5 defect
# (a gene whose live domain is set by a different gene), reintroduced deliberately.
#
#   family          collateral / contract              worked example @ 35 DTE (x365/35=10.43)
#   -------------   --------------------------------   --------------------------------------
#   full notional   strike x 100  (also jade lizard's   CSP strike 90, credit 1.00:
#                   put_strike + wing - credit, and       100/9000 x 10.43 = 0.12
#                   the ratio spread's strike-credit)
#   Reg-T naked     ~20% of notional less the OTM      ATM straddle, credit 7.43 on 2,000:
#                   amount, floored at 10%               743/2000 x 10.43 = 3.87
#   defined risk    (width - credit) x 100             5-wide bull put, credit 0.60:
#                                                        60/440 x 10.43 = 1.42
#
# THESE BANDS ARE DERIVED FROM THE RESERVE ARITHMETIC AND A PLAUSIBLE CREDIT, NOT MEASURED --
# there is no option grid to measure against yet (see precondition 1). Re-centre them on the
# realised ARC distribution once one has run; the shape (three families, floor at 0) is what
# should survive, not the ceilings.
#
# 0.0 IS A LEVEL, AND IT IS NOT "OFF". `admits_credit_structure` treats a configured 0.0 as a
# gate that still refuses an UNMEASURABLE ARC, so the bottom of each band is "the credit may be
# arbitrarily thin, but it must be priceable" -- a real, distinct hypothesis for the GA, and
# the natural control arm against the higher levels.
_ARC_FULL_NOTIONAL = (0.0, 0.30, 0.05)
_ARC_REG_T_NAKED = (0.0, 6.0, 1.0)
_ARC_DEFINED_RISK = (0.0, 3.0, 0.5)

#: option ACTION TYPE -> (reserve-table strategy name, ARC band). Only the CREDIT builders
#: appear: those are the ones that consult the gate, and they are exactly the reserve table's
#: `RESERVING_STRATEGIES` that have a builder of their own (`credit_spread` / `naked_put` /
#: `debit_spread` are pricing aliases with no action). A DEBIT structure posts no collateral,
#: so a floor there would refuse every one of them -- see the ZERO_RESERVE note below.
_OPTION_ARC_BANDS = {
    "sell_cash_secured_put": ("cash_secured_put", _ARC_FULL_NOTIONAL),
    "open_jade_lizard": ("jade_lizard", _ARC_FULL_NOTIONAL),
    "open_put_ratio_spread": ("put_ratio_spread", _ARC_FULL_NOTIONAL),
    "open_short_straddle": ("short_straddle", _ARC_REG_T_NAKED),
    "open_short_strangle": ("short_strangle", _ARC_REG_T_NAKED),
    "open_bear_call_spread": ("bear_call_spread", _ARC_DEFINED_RISK),
    "open_bull_put_spread": ("bull_put_spread", _ARC_DEFINED_RISK),
    "open_iron_condor": ("iron_condor", _ARC_DEFINED_RISK),
}


def _apply_option_min_arc_gene(cfg: dict) -> dict:
    """Make the ARC floor searchable, in place, on CREDIT actions only.

    A no-op for every debit / zero-reserve structure. That is not an oversight: a long call,
    a butterfly and a COVERED CALL all reserve nothing (they are in
    `OptionsAccountInterface.ZERO_RESERVE_STRATEGIES`), so `annualized_return_on_collateral`
    has no denominator and returns None -- and a configured floor turns None into a refusal.
    Emitting the gene there would silently delete those structures from the search the moment
    the GA sampled any level at all, including the bottom one.
    """
    at = str(cfg.get("action_type") or "")
    band = _OPTION_ARC_BANDS.get(at)
    if band is None:
        return cfg
    lo, hi, step = band[1]
    cfg.setdefault("option_min_arc", lo)
    cfg.setdefault("option_min_arc_optimize", True)
    cfg.setdefault("option_min_arc_min", lo)
    cfg.setdefault("option_min_arc_max", hi)
    cfg.setdefault("option_min_arc_step", step)
    return cfg


# --- position SIZE as a gene (OPT-C6) -------------------------------------------------------
#
# ``option_sizing`` (% of equity committed per structure) is bounded, symbol-comparable and
# exactly the same category of knob as %OTM / DTE / wing width -- and it was a per-structure
# CONSTANT the GA could not touch: 0 sizing genes across all 19 built option strategies.
#
# It also GATES the fitness work: contracts x max_loss IS ``option_sizing`` % of equity by
# construction, so any return-on-collateral measure divides by a constant while sizing is
# frozen and collapses back into plain return.
#
# The band is keyed on the structure's AUTHORED size rather than shared, because 20 % of equity
# in a defined-risk condor and 20 % in a long call are not the same risk -- a single global
# window would either starve the credit structures or let the debit ones bet the account. The
# table must be TOTAL over the authored values (asserted below): a structure whose size is not
# covered would silently keep a frozen size while every sibling searched one.
_OPTION_SIZING_BANDS = {
    #  authored: (min, max, step)
    5.0:  (1.0, 10.0, 1.0),    # long premium: floor at 1% (a real 1-contract bet), cap at 2x
    8.0:  (2.0, 16.0, 2.0),    # butterfly
    15.0: (5.0, 30.0, 2.5),    # defined-risk / skewed credit
    # neutral credit + full-notional. The 50% top is set by the full-notional structures: a
    # cash-secured put at spot $100 reserves $10,000 = 50% of the grid's $20k account, and the
    # budget is equity * MIN(this, max_virtual_equity_per_instrument_percent) — so this row and
    # _OPTION_RM_OVERRIDE have to move together or neither moves anything.
    20.0: (5.0, 50.0, 5.0),
}
_missing_sizings = sorted({cfg["option_sizing"] for cfg in _OPTION_STRATS.values()
                           if cfg.get("option_sizing") is not None}
                          - set(_OPTION_SIZING_BANDS))
if _missing_sizings:
    raise RuntimeError(
        f"_OPTION_SIZING_BANDS has no band for authored option_sizing {_missing_sizings}; "
        f"those structures would keep a frozen size while their siblings search one.")


def _apply_option_sizing_gene(cfg: dict) -> dict:
    """Make ``option_sizing`` searchable, in place. No-op when the action does not size off it
    (O_CC / O_PP size off the HELD share count -- one contract per round lot -- so a sizing
    gene there would be inert)."""
    sizing = cfg.get("option_sizing")
    if sizing is None:
        return cfg
    lo, hi, step = _OPTION_SIZING_BANDS[float(sizing)]
    cfg.setdefault("option_sizing_optimize", True)
    cfg.setdefault("option_sizing_min", lo)
    cfg.setdefault("option_sizing_max", hi)
    cfg.setdefault("option_sizing_step", step)
    return cfg


# --- delta-based strike selection (OPT-C3) --------------------------------------------------
#
# ``percent_otm`` was the ONLY strike gene in all 16 option grids, and it is volatility-BLIND:
# 5 % OTM on a 15-vol utility and on a 90-vol biotech are not remotely the same proposition, so
# a threshold that is right for one symbol is wrong for the next and the GA cannot converge on
# a portable number. Delta IS normalised across symbols (and across vol regimes), is
# implemented in ``option_selector._pick_by``, is supported by the backtest chain, and is the
# LIVE default -- and was never searched.
#
# The METHOD is a categorical gene so the GA chooses per structure; the two parameters are
# SEPARATE genes because they are separate quantities on separate scales (that conflation is
# precisely the OPT-C3 naming bug). ``strategy_param_space._apply_option_strike`` writes
# whichever one matches the decoded method.
#
# ONLY where the builder honours it. Eight of the seventeen ``_OptionEntryAction`` subclasses
# hard-code ``method="percent_otm"`` and leave ``strike_method`` a dead attribute (OPT-S2), so
# offering the choice there would be a gene the simulation cannot see -- the exact defect this
# whole track is fixing. ``types.honours_strike_method`` is the registry, drift-guarded against
# the builders' own source by packages/common/tests/test_strike_method_registry.py.
_OPTION_DELTA_RANGE = {"option_strike_delta": 0.30, "option_strike_delta_optimize": True,
                       "option_strike_delta_min": 0.05, "option_strike_delta_max": 0.50,
                       "option_strike_delta_step": 0.05}


def _apply_option_strike_method_gene(cfg: dict) -> dict:
    """Make the strike METHOD searchable (percent_otm | delta) on actions that honour it.

    A no-op for the eight builders that ignore ``strike_method``, and for an action whose
    percent param is not itself optimizable (there would be nothing to switch between).
    """
    from ba2_common.core.types import honours_strike_method

    at = str(cfg.get("action_type") or "")
    if not honours_strike_method(at):
        return cfg
    if not cfg.get("option_strike_param_optimize"):
        return cfg
    cfg.setdefault("option_strike_method_optimize", True)
    cfg.setdefault("option_strike_method_choices", ["percent_otm", "delta"])
    for k, v in _OPTION_DELTA_RANGE.items():
        cfg.setdefault(k, v)
    return cfg


# --- the ENTRY QUOTE as a gene (F3) ---------------------------------------------------------
#
# Option entry limits are quoted at the ANALYSIS bar's close, but the default `next_bar_open`
# fill model makes the NEXT bar's open cross that stale quote. And the quote is a MID, not a
# touch: the historical option store carries `bid == ask` on every row it fills in at all (the
# parquet store has no bid/ask column whatsoever), so `contract.ask` and `contract.bid` are both
# just the close, while the tradeable spread is MODELLED at fill time by --option-spread-pct.
# A seller therefore fills only if the premium RISES by a whole modelled half-spread overnight
# -- which for decaying OTM premium is the wrong way round, so the DAY order expires unfilled
# and premium sellers structurally almost never trade. Measured head-to-head on INTC Feb-Dec
# 2024: O_CSP got 6 trades under next_bar_open and 9 under same_bar_close; an earlier AAPL probe
# got 0 against 17.
#
# `next_bar_open` STAYS THE DEFAULT (no look-ahead, and every existing equity grid used it, so
# numbers stay comparable) and the QUOTE side is what becomes searchable instead.
#
# A FRACTION, NOT AN OFFSET, for the same reason the selection-policy features are chain-
# relative: an absolute $0.05 means something completely different on a $0.40 put and a $12
# call, while a fraction of that contract's own modelled spread is scale-free across symbols
# and premium levels.
#
# ONE BAND FOR EVERY STRUCTURE, unlike option_sizing / option_min_arc. Those are quantities
# whose meaning is set by the structure (20 % of equity, or a return on a collateral branch
# that differs by an order of magnitude); this one is already normalised BY the structure --
# it is a fraction of that structure's own legs' own spreads -- so a shared band is the same
# hypothesis everywhere.
#
# 0.0 IS THE AUTHORED DEFAULT AND IT IS AN EXACT NO-OP: the entry keeps quoting the builder's
# `contract.ask`/`contract.bid`/net untouched, so no existing option result moves. 1.0 quotes
# at the far touch `_option_cross` already models the fill at. 0.25 steps give the GA five
# levels including both ends.
_OPTION_ENTRY_CROSS_BAND = (0.0, 1.0, 0.25)


def _apply_option_entry_cross_gene(cfg: dict) -> dict:
    """Make the entry-quote concession searchable, in place, on ANY option entry action.

    No exemption list, deliberately: every one of the seventeen entry builders ends at
    ``_OptionEntryAction._submit_option_order``, which is where the concession is applied, so
    there is no builder for which this gene is inert -- the failure mode that forced
    ``_apply_option_sizing_gene`` and ``_apply_option_min_arc_gene`` to carry one.
    """
    lo, hi, step = _OPTION_ENTRY_CROSS_BAND
    cfg.setdefault("option_entry_cross", lo)
    cfg.setdefault("option_entry_cross_optimize", True)
    cfg.setdefault("option_entry_cross_min", lo)
    cfg.setdefault("option_entry_cross_max", hi)
    cfg.setdefault("option_entry_cross_step", step)
    return cfg


def _apply_option_min_volume(cfg: dict) -> dict:
    """Stamp the tradability floor onto ANY option action config, in place.

    Split out of _option_entry_action_for (2026-08-23) because O_CC and O_PP do not have an
    _OPTION_STRATS entry -- their option action is an OVERLAY on an equity strategy, hand-
    written inline in the builder -- and so were the only two option paths in the grid with
    no min_volume floor at all."""
    if _OPTION_MIN_VOLUME and _OPTION_MIN_VOLUME > 0:
        cfg.setdefault("option_min_volume", int(_OPTION_MIN_VOLUME))
    return cfg


def _option_overlay_action(action_type: str, *, strike_param: float,
                           dte_min: int = 25, dte_max: int = 45,
                           strike_min: float, strike_max: float, strike_step: float,
                           dte_min_range: int = 20, dte_max_range: int = 60,
                           dte_step: int = 5) -> dict:
    """The option action config for an EQUITY-entry overlay (O_CC's covered call, O_PP's
    protective put).

    These two were inline literals carrying only strike_method/strike_param/dte, which made
    them the grid's only ungated AND unsearchable option legs: no ``option_min_volume``, and
    no ``option_*_optimize`` flags, so their %OTM and DTE were constants the GA could not
    touch while the same knobs were searched on every pure-option key. Sizing genuinely is
    not a knob here -- both actions size off the HELD share count (1 contract per 100
    shares), not option_sizing."""
    return _apply_option_entry_cross_gene(_apply_option_strike_method_gene(
      _apply_option_min_volume({
        "action_type": action_type,
        "option_strike_method": "percent_otm", "option_strike_param": strike_param,
        "option_dte_min": dte_min, "option_dte_max": dte_max,
        "option_strike_param_optimize": True, "option_strike_param_min": strike_min,
        "option_strike_param_max": strike_max, "option_strike_param_step": strike_step,
        "option_dte_optimize": True, "option_dte_min_range": dte_min_range,
        "option_dte_max_range": dte_max_range, "option_dte_step": dte_step,
    })))


def _screener_gate_base_for_strategy(kind: str) -> dict:
    """Per-strategy gate-only screener overrides declared on _OPTION_STRATS members
    (``screener_gate_base``). A group (OS1-4) merges its ACTIVE members' dicts in order
    (later member wins). Equity/unknown keys -> {}."""
    if kind in _OPTION_STRATS:
        return dict(_OPTION_STRATS[kind].get("screener_gate_base") or {})
    merged: dict = {}
    for member in _OPTION_GROUPS.get(kind, []):
        merged.update(_OPTION_STRATS[member].get("screener_gate_base") or {})
    return merged


def _screener_gate_opt_block(args, strategy_key: str) -> "dict | None":
    """The gate-only screener_opt block for --screener-gate-store (None when the flag is unset).

    Gate-only = the metric store is attached PURELY as a per-bar entry gate: the run universe
    stays the static --universe, no screener:* genes enter the search, and the optimization
    handler skips its candidate-bound universe restriction. Base settings are most-admitting
    except the price cap, so ONLY --max-stock-price bites unless --screener-base-json or a
    per-strategy screener_gate_base says otherwise. Precedence (high -> low): per-strategy
    screener_gate_base > --screener-base-json > the --max-stock-price default block.
    """
    store = getattr(args, "screener_gate_store", None)
    if not store:
        return None
    if getattr(args, "screener", False):
        sys.exit("optimize: --screener-gate-store cannot be combined with --screener "
                 "(full screener mode already gates entries; pick one).")
    base: dict = {
        "market_cap_min": 0.0,
        "relative_volume_min": 0.0,
        "price_drop_pct": 0.0,
        "weinstein_stage2_only": 0,
        "max_stocks": 10000,
    }
    max_price = float(getattr(args, "max_stock_price", _MAX_STOCK_PRICE_DEFAULT))
    if max_price > 0:
        base["price_max"] = max_price
    if getattr(args, "screener_base_json", None):
        with open(args.screener_base_json) as _f:
            base.update(json.load(_f))
    base.update(_screener_gate_base_for_strategy(strategy_key))
    return {
        "store": store,
        "base_settings": base,
        "cadence_days": int(args.screener_cadence_days),
        "apply_to_expert_settings": False,
        "gate_only": True,
    }


# DEBIT structures (long premium): OS1/OS4 and their members. The TP band differs by payoff
# profile (see _option_exit_rules).
_DEBIT_OPTION_KINDS = {"O_LC", "O_LP", "O_VERT", "O_BF", "O_BULLCS", "O_STRD", "O_STRG",
                       "OS1", "OS4"}

# The same split at MEMBER granularity (the group keys removed). Entry gates are authored per
# MEMBER -- a group's rules are one per member -- so a set that mixes members and group keys
# cannot answer "is THIS structure long or short premium?".
_DEBIT_OPTION_MEMBERS = _DEBIT_OPTION_KINDS & set(_OPTION_STRATS)
_CREDIT_OPTION_MEMBERS = set(_OPTION_STRATS) - _DEBIT_OPTION_MEMBERS
# Every member must land on exactly one side: an unclassified structure would silently get the
# credit half's iv_rank gate (the wrong thesis) with nothing to notice it.
if _DEBIT_OPTION_MEMBERS | _CREDIT_OPTION_MEMBERS != set(_OPTION_STRATS):
    raise RuntimeError("option members are not partitioned into debit/credit halves")

# Members whose worst case is UNBOUNDED -- ``option_payoff.max_loss`` has no number for them,
# so the submit path stamps no ``max_loss_per_contract`` and ``loss_pct_of_max_loss`` has no
# denominator: the ``opt_sl_ml`` exit rule is never emitted for a strategy built solely of
# these (design 2026-08-29 S6).
#
# THE PREDICATE IS THE MEASURED PAYOFF, NOT "is this a naked short" (the corrected S6 rule,
# 2026-08-30): only a net-uncovered short CALL is genuinely unbounded, because only the
# upside is infinite. Concretely, per structure:
#   * O_SSTG / O_SSTD -- a short call no other leg of the order covers: UNBOUNDED. In here.
#   * O_CSP -- a naked short PUT is bounded below at (strike - credit) x 100, the underlying
#     stopping at zero: MEASURED, stamped, carries the rule. (The pre-correction design table
#     called this unbounded; packages/common/tests/test_max_loss_persisted_at_submit.py pins
#     the corrected behaviour at the stamping seam.)
#   * O_JL -- its short call is covered by the long wing, its short put bounded at zero:
#     MEASURED (worst of the two sides).
#   * O_RS -- all puts (1x2): bounded at an underlying of zero: MEASURED.
#   * everything else is a debit or defined-risk structure: MEASURED trivially.
# This set is the strategy-level APPLICABILITY gate only -- the safety mechanism is Task 8's
# absence rule (no stamp => the condition can never fire), which is what keeps the rule
# harmless on a group's unbounded members and on the wheel's unstamped covered-call legs.
_UNDEFINED_RISK_MEMBERS = {"O_SSTG", "O_SSTD"}


def _option_exit_rules(kind: str):
    """Close the held option at a premium-profit TP, an ELAPSED-time exit and a
    REMAINING-life (DTE) exit — all optimizable + on/off-toggleable. (CLOSE on the held
    option position via ``close_option``.)

    Bands differ by payoff profile. DEBIT kinds get a WIDE TP band (default 100%, range
    25-200%): long premium lives off the right tail — a 25-75% cap truncates the few big
    winners that pay for the many small losers (v6 OS1 evidence: the GA disabled O_LC/O_LP
    outright and converged to barely-trading configs). CREDIT kinds keep the tastytrade-style
    tight band (default 50% of credit) and additionally get a toggleable STOP-LOSS at -100%
    of credit (range -200..-50) so the GA can manage the short-premium left tail (v6 OS2/OS3:
    56-87% win rate but only 3.8-18% TR — small wins eaten by uncapped losers).

    ``opt_dte`` (``days_to_expiry <= N``) is the roll-at-DTE exit, and it is NOT split by
    payoff profile — one band for debit and credit alike:

    * ``days_opened`` cannot express it. The entry DTE window is itself a gene
      (``option_dte``, decoded to a >= 14-day-wide window), so "28 days after opening"
      lands on a different remaining life in every trial. Elapsed and remaining are
      different quantities the moment the entry tenor moves.
    * Until this rule existed, roll-at-DTE lived only inside the hardcoded
      ``OptionPortfolioManager``: no other expert could roll and the GA could not optimise
      the roll point at all.
    * Both profiles want it. Short premium wants out of the terminal gamma window (21 DTE
      is the tastytrade convention for 30-45 DTE structures); long premium wants out of
      the terminal theta cliff. Gating it on ``_DEBIT_OPTION_KINDS`` like the TP band
      would be denying half the grid an exit that applies to it.

    Band: 0..21 step 3 (8 levels), default 21, toggleable. It must REACH 0 — a 0DTE arm's
    only exit criterion is "close on the expiry day" — and 21 is the natural cap: the
    grid's entry windows bottom out near 10-13 DTE (``option_dte`` centre 20 with a
    +/-10 half-width), so a higher threshold only buys a degenerate open-and-immediately-
    close region that burns GA budget. Step 3 lands exactly on the conventional 21 / 14 /
    7 / 0 points without inflating the search.

    ``opt_sl_ml`` (``loss_pct_of_max_loss > N``) is the max-loss-scaled stop, emitted only
    for strategies with at least one MEASURED-max-loss structure — see the inline comment at
    its append site and ``_UNDEFINED_RISK_MEMBERS`` for the applicability derivation.
    """
    debit = kind in _DEBIT_OPTION_KINDS
    tp = ({"value": 100, "value_min": 25, "value_max": 200, "value_step": 25} if debit
          else {"value": 50, "value_min": 25, "value_max": 75, "value_step": 5})
    td = ({"value": 28, "value_min": 10, "value_max": 45, "value_step": 5} if debit
          else {"value": 21, "value_min": 10, "value_max": 35, "value_step": 5})
    rules = [
        {"id": "opt_tp", "action_type": "close_option", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "tp", "field": "profit_loss_percent", "op": ">",
              "optimize": True, **tp}]}},
        {"id": "opt_time", "action_type": "close_option", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "td", "field": "days_opened", "op": ">",
              "optimize": True, **td}]}},
        # Its OWN rule, not another leaf on opt_time: leaves inside one rule are ANDed, so
        # folding it in would demand "held N days AND M days left" and would cost the DTE
        # exit its own on/off gene.
        {"id": "opt_dte", "action_type": "close_option", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "dte", "field": "days_to_expiry", "op": "<=", "value": 21,
              "optimize": True, "value_min": 0, "value_max": 21, "value_step": 3}]}},
    ]
    if not debit:
        rules.append(
            {"id": "opt_sl", "action_type": "close_option", "toggle_optimize": True,
             "conditions": {"type": "AND", "conditions": [
                 {"id": "sl", "field": "profit_loss_percent", "op": "<", "value": -100,
                  "optimize": True, "value_min": -200, "value_max": -50, "value_step": 25}]}})
    # ``opt_sl_ml`` -- close when the loss reaches N% of the structure's MEASURED max loss
    # (design 2026-08-29 S6). A SEPARATE rule, not a "basis" gene on ``opt_sl``: the sensible
    # threshold range differs by basis (-200..-50% of credit vs 25..75% of max loss), so one
    # threshold gene would need a domain conditional on another gene; two independently
    # toggleable rules let the GA select the basis the way it already selects
    # opt_tp/opt_time/opt_dte/opt_sl -- by toggling which rule is live. Both stops may be live
    # at once; first match wins, as the OPEN_POSITIONS ruleset already does. They are
    # correlated but not redundant: N% of max loss is scale-free (always that fraction of the
    # defined risk) while -100% of credit drifts with however much credit the trial collected.
    #
    # Emitted only where at least one of the strategy's structures has a MEASURED max loss
    # (see _UNDEFINED_RISK_MEMBERS for the corrected per-structure derivation). A group emits
    # it if ANY member is measured -- the exit list is shared, and on an unbounded member's
    # positions the condition self-disarms for want of a persisted max_loss_per_contract, so
    # dropping the rule would deny the measured members a stop to protect the ones that never
    # had a denominator anyway.
    members = _OPTION_GROUPS.get(kind, [kind])
    if any(m not in _UNDEFINED_RISK_MEMBERS for m in members):
        rules.append(
            {"id": "opt_sl_ml", "action_type": "close_option", "toggle_optimize": True,
             "conditions": {"type": "AND", "conditions": [
                 {"id": "sl_ml", "field": "loss_pct_of_max_loss", "op": ">",
                  "value": 50, "optimize": True,
                  "value_min": 25, "value_max": 75, "value_step": 5}]}})
    return rules


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


# --- price-vs-analyst-target entry gates: REMOVED 2026-08-27, do not re-add ------------------
#
# The option entry rule used to carry FOUR gates on ``price_vs_target_low_percent`` /
# ``price_vs_target_high_percent`` (two floor+width bands, chained through
# ``value_offset_from``). They are gone, replaced by the single ``_expected_profit_gate`` below,
# for a reason that no amount of re-parameterising could fix:
#
#   ``PriceVsTargetLowCondition`` / ``PriceVsTargetHighCondition`` are hard-keyed to
#   ``expert_recommendation.data["FMPRating"]["target_low"|"target_high"]``. ONLY FMPRating
#   writes that key. Under any other expert — DeterministicScorer, or anything the grid adds
#   later — all four gates fail CLOSED, so 8 of ~28 genes per structure were dead weight and any
#   genome that switched one ON traded nothing and scored the zero-trade sentinel.
#
# The earlier work on these gates (OPT-C5's 25.8 %-guaranteed-empty joint space, OPT-C12's
# inert -20..+20 high-target window) was real, but it was optimising the shape of a gate that
# only one expert can answer at all. ``expected_profit_percent`` is non-nullable on
# ``ExpertRecommendation`` and carries the same "how far from here to the target" information,
# so one expert-independent gate replaces the four.
#
# Removing them also removed the launcher's ONLY user of ``value_offset_from``. That mechanism
# resolves its base id against the GLOBAL gene map, so a shared condition id (see the
# ``shared-*`` leaves) would have silently coupled members across a family — keep it unused
# here. The mechanism itself is still exercised by
# tests/test_strategy_param_space_value_offset_from.py.


# --- implied-volatility-rank entry gate (OPT-C1 / OPT-C4-of-R3) -----------------------------
#
# `iv_rank` is the one genuinely BOUNDED (0-100), symbol-COMPARABLE option quantity the
# platform owns. It is fully implemented (TradeConditions.IVRankCondition,
# BacktestAccount.get_iv_rank, OptionsAccountInterface._iv_rank_from_series), registered in
# every condition registry (ExpertEventType.N_IV_RANK, get_numeric_event_values, CONDITION_MAP,
# rule_builders.FIELD_EVENT, rules_export_import._FIELD_ABBR) -- and until now NO GRID BUILT A
# LEAF FOR IT. Measured across all 19 built option strategies before this change: 256 condition
# leaves, 499 genes, ZERO iv_rank leaves and ZERO iv_rank genes.
#
# THE TWO HALVES MUST ASK FOR OPPOSITE THINGS (OPT-C4). A premium SELLER wants implied vol
# expensive; a premium BUYER wants it cheap. The GA's gene space never searches a condition's
# OPERATOR -- only its threshold and its ON/OFF flag -- so a single shared gate could only ever
# express ONE of those theses, and the other half would be gated on the opposite of what it
# wants. The operator is therefore SET PER HALF, from the same debit/credit split the exit
# bands already use, and each half gets its own searched window:
#
#     debit  (long premium)  iv_rank <  V   V in 10..60   "only buy vol when it is cheap"
#     credit (short premium) iv_rank >  V   V in 20..70   "only sell vol when it is expensive"
#
# matching the documented recipes in rules_documentation.py ("Buy calls only in cheap
# volatility: iv_rank <= 30", "Sell premium when iv_rank is high").
#
# FAIL-CLOSED, and that is deliberate. `IVRankCondition` returns False for EVERY operator when
# the rank is unavailable (fewer than IV_RANK_MIN_SAMPLES=5 usable ATM-IV points -- the case on
# any options cache built before the greeks columns existed). So an individual that switches
# this gate ON against a cache with no IV trades NOTHING and scores the zero-trade sentinel.
# That is the correct reading of "we cannot measure the entry criterion", not a bug: the
# alternative (treat unknown IV as passing) would let the GA collect the gate's credit while
# never actually applying it. The gate carries its own ``toggle_optimize`` gene, so the search
# can and will switch it off where the data is not there.
_IV_RANK_GATE = {
    True:  {"op": "<", "value": 30.0, "value_min": 10.0, "value_max": 60.0},   # debit
    False: {"op": ">", "value": 60.0, "value_min": 20.0, "value_max": 70.0},   # credit
}
_IV_RANK_STEP = 5.0


def _iv_rank_gate(m: str, member: str) -> dict:
    """The iv_rank entry gate leaf for member prefix ``m`` (see above)."""
    spec = _IV_RANK_GATE[member in _DEBIT_OPTION_MEMBERS]
    return {"id": f"{m}-iv_rank", "field": "iv_rank", "op": spec["op"],
            "value": spec["value"], "optimize": True,
            "value_min": spec["value_min"], "value_max": spec["value_max"],
            "value_step": _IV_RANK_STEP, "toggle_optimize": True}


# --- volume / volatility entry gates --------------------------------------------------------
#
# RELATIVE VOLUME. The UNDERLYING's volume over its own trailing 20-bar average (current bar
# excluded). One direction for both halves -- real participation behind the signal is a
# confirmation whether you are buying or selling premium -- so the searched threshold is the
# only per-half difference, and there is none. 0.5..3.0 brackets both "any liquidity at all"
# and "genuinely unusual"; the authored default sits at the permissive end.
#
# Not volume/OPEN INTEREST, which would be the better CONTRACT-level unusual-activity signal:
# `open_interest` is NULL on every cached option row (see option_selector.passes_liquidity), so
# it is not computable here today.
_RELATIVE_VOLUME_GATE = {"value": 0.5, "value_min": 0.5, "value_max": 3.0, "value_step": 0.25}

# IV / REALISED VOL -- the variance risk premium, i.e. the actual edge in premium selling: you
# are paid implied and you pay out realised. OPPOSITE PER HALF for the same reason as iv_rank
# (the gene space never searches an operator): a seller wants the ratio HIGH, a buyer wants it
# LOW. The window brackets 1.0 on both sides so either half can express "no edge here".
_IV_RV_GATE = {
    True:  {"op": "<", "value": 1.6},   # debit: buy premium only when it is cheap vs realised
    False: {"op": ">", "value": 0.8},   # credit: sell premium only when it is genuinely rich
}
_IV_RV_RANGE = {"value_min": 0.8, "value_max": 1.6, "value_step": 0.1}


def _relative_volume_gate() -> dict:
    """The relative-volume entry gate leaf. SHARED across every member of a group.

    Shared because its semantics genuinely do not vary by structure: real participation behind
    the signal confirms a trade whether you are buying or selling premium, and this gate has no
    per-half difference at all -- it was replicated per member as pure duplication.

    Contrast ``_iv_rank_gate`` / ``_iv_rv_gate``, which must stay per-member: their operator
    flips between debit and credit halves and the GA never searches an operator, so one shared
    node cannot express both.

    The id is deliberately the same in a SINGLE-structure job and in a group, so a stage-1
    winner's gene keys survive being encoded into the stage-2 group space. Sharing is safe on
    both sides of the fence: ``decode_params`` builds ONE ``cond_by_id`` map for the whole
    strategy and ``_apply_to_tree`` substitutes by node id, so one gene lands on every member's
    leaf; and the engine's ``triggers_from_condition_tree`` keys triggers by POSITION within a
    single rule (``cond_0``, ``cond_1``, ...), never by condition id, so duplicate ids across
    sibling rules cannot collide when the ruleset is seeded.
    """
    return {"id": "shared-rel_volume", "field": "relative_volume", "op": ">",
            "optimize": True, "toggle_optimize": True, **_RELATIVE_VOLUME_GATE}


def _iv_rv_gate(m: str, member: str) -> dict:
    """The IV/realised-vol entry gate leaf for member prefix ``m``."""
    spec = _IV_RV_GATE[member in _DEBIT_OPTION_MEMBERS]
    return {"id": f"{m}-iv_rv", "field": "iv_to_realized_vol", "op": spec["op"],
            "value": spec["value"], "optimize": True, "toggle_optimize": True,
            **_IV_RV_RANGE}


# EXPECTED PROFIT — the entry's only signal-strength gate, and the ONLY one every expert can
# answer. `ExpertRecommendation.expected_profit_percent` is non-nullable, so an expert cannot
# omit it; `target_price` is nullable and DERIVES from it when absent (see the field's own
# description), so the two are the same signal and this gate covers both.
#
# It REPLACES the four price_vs_target_* gates. Those read
# `expert_recommendation.data["FMPRating"]["target_low"]` via a hard-keyed condition, and only
# FMPRating writes that key — so under DeterministicScorer (or any future expert) all four
# failed CLOSED, taking 8 of ~28 genes per structure with them and making any genome that
# enabled one trade nothing.
#
# Range is AUTHORED, not measured: 2-20% brackets "any positive edge" through "a call the expert
# is loud about", and the grid searches the threshold. Re-centre it on the realised
# expected_profit distribution once a grid has run.
_EXPECTED_PROFIT_GATE = {"value": 5.0, "value_min": 2.0, "value_max": 20.0, "value_step": 2.0}


def _expected_profit_gate(m: str) -> dict:
    """The expected-profit entry gate leaf for member prefix ``m``."""
    return {"id": f"{m}-exp_profit", "field": "expected_profit_target_percent", "op": ">",
            "optimize": True, "toggle_optimize": True, **_EXPECTED_PROFIT_GATE}


# Strategy kinds whose OWN rules manage stock delivered by an option assignment, and which
# therefore need the backtest to STOP liquidating that stock at the next bar's open. Only the
# wheel: its covered-call overlay is gated on ``has_assigned_shares``, so the assigned shares
# are the position it exists to manage.
#
# PER-STRATEGY, not a CLI flag. The switch is a property of what the ruleset does, not an
# operator preference -- a flag would let an O_CSP grid hold stock nothing in it can sell, which
# is the orphaned-stock blow-up the liquidation exists to prevent. Keeping the set here means
# adding a stock-managing structure later is one line, next to the reason.
_HOLDS_ASSIGNED_STOCK = {"O_WHEEL"}


def _hold_assigned_stock(kind: Optional[str]) -> bool:
    """Whether ``kind``'s run config should set ``BacktestAccount.hold_assigned_stock``.

    ``kind`` is None for BYPASS experts (FactorRanker), which ignore ``--strategy`` and build
    the minimal strategy -- so whatever ``--strategy`` says about them is meaningless and must
    not leak into their account settings.

    A GROUP KEY COUNTS IF ANY MEMBER NEEDS IT, and that is not defensive coding -- it is the
    single most likely way for this whole change to be undone. ``--strategy`` accepts group keys
    (``OS1``..``OS4``, ``OS_ALL``) that expand to member lists via ``_OPTION_GROUPS``, and adding
    O_WHEEL to a family is the NATURAL way to search it alongside O_CSP. Matching the bare key
    against ``_HOLDS_ASSIGNED_STOCK`` would silently drop the flag for that arm, and the arm
    would revert to writing covered calls whose shares are sold the same bar -- the exact
    naked-call defect this branch exists to remove, reintroduced by the obvious next step. The
    grid spec's own OS_ALL instruction would have triggered it.
    """
    if kind is None:
        return False
    if kind in _HOLDS_ASSIGNED_STOCK:
        return True
    return any(m in _HOLDS_ASSIGNED_STOCK for m in _OPTION_GROUPS.get(kind, ()))


# SMOKE MODE (--gates-off): drop every OPTIONAL entry gate so a run exercises the PIPELINE
# rather than the strategy. Set from --gates-off at command entry; module-level for the same
# reason as _OPTION_MIN_VOLUME -- _option_entry_rule is called deep inside the strategy
# builders (_build_strategy dispatches _STRATEGY_BUILDERS[kind](kind) for option kinds), far
# from the parsed args.
_OPTION_GATES_OFF = False


def _option_entry_rule(member: str, *, toggleable: bool = False,
                       gates_off: "bool | None" = None) -> dict:
    """The entry TradeRule dict for one pure-option strategy key: directional signal gate
    (bullish for every original key, bearish for O_LP — see _OPTION_ENTRY_GATE) + flat +
    optimizable confidence gate + the iv_rank / relative-volume / iv-vs-realised-vol gates +
    ONE expected-profit gate, action = the member's option action config. Rule/condition ids
    are prefixed with the member key so a GROUP of these rules yields uniquely-keyed genes per
    member — EXCEPT the two expert-independent gates, and that exception is the point:
    ``shared-gate_confidence`` and ``shared-rel_volume`` carry the SAME id in every member, so
    a family searches ONE threshold for each instead of one per structure (OS1: 20 genes
    collapse to 4). They are shared because their semantics do not vary by structure — expert
    conviction in the symbol, and real participation behind the signal, read the same whichever
    way the premium flows. The same id is used in a SINGLE-structure job so a stage-1 winner's
    gene keys are still known in the stage-2 group space (``encode_params`` drops unknown keys
    silently). ``toggleable`` adds the rule-level enabled gene (group members only — a
    single-strategy job keeps its one entry always-on).

    Every gate except ``-flat`` is independently ``toggle_optimize=True``: ``-flat``
    (``has_no_position``) is a correctness guard, not a strategy opinion, so the GA may not
    switch it off. Op is fixed per gate — the GA's gene space only ever searches a condition's
    threshold value and its enabled flag, never its operator (see
    docs/plans/2026-07-21-options-price-target-conditions.md's "Design reference"). That is why
    ``_iv_rank_gate`` and ``_iv_rv_gate`` are built PER MEMBER: their direction flips between
    the debit and credit halves, and one shared leaf could only ever express one of the two.

    SIGNAL STRENGTH is gated by ``_expected_profit_gate`` alone. It replaced four
    ``price_vs_target_*`` gates on 2026-08-27; see the tombstone comment above
    ``_iv_rank_gate`` for why those could never work under a non-FMPRating expert.

    ``gates_off`` (default: the module-level ``_OPTION_GATES_OFF``, set by --gates-off) REMOVES
    every optional gate for the smoke stage — see the block at the end of this function.
    """
    m = member.lower()
    rule = {
        "id": f"{m}-entry",
        "name": f"{member}-entry",
        "conditions": {"id": f"{m}-root", "type": "AND", "conditions": [
            {"id": f"{m}-signal", "field": _OPTION_ENTRY_GATE[member], "field_type": "flag",
             "toggle_optimize": True},
            {"id": f"{m}-flat", "field": "has_no_position", "field_type": "flag"},
            {"id": "shared-gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 75, "value_step": 5,
             "toggle_optimize": True},
            _iv_rank_gate(m, member),
            _relative_volume_gate(),
            _iv_rv_gate(m, member),
            _expected_profit_gate(m),
        ]},
        "actions": [_option_entry_action_for(member)],
        "continue_processing": False,
    }
    # An explicit argument wins; None (the normal case) defers to the CLI-set module global.
    if _OPTION_GATES_OFF if gates_off is None else gates_off:
        # SMOKE MODE. Every OPTIONAL gate comes OUT of the tree so the run exercises the
        # pipeline rather than the strategy. ``toggle_optimize`` is precisely the marker for
        # "the GA may switch this off", which makes it the right discriminator: a leaf carrying
        # it is a strategy opinion, a leaf without it is a correctness guard (``has_no_position``)
        # that must stay on — with it off, a smoke run would stack duplicate positions and mask
        # the plumbing it is testing.
        #
        # REMOVED, not flagged ``enabled: False``, because a flag would be inert TWICE OVER:
        # ``ConditionLeaf.to_canonical_dict`` rebuilds a leaf from DECLARED fields only, so
        # ``normalize_trade_rules`` (which both option builders call) deletes an ``enabled``
        # key; and nothing reads one anyway — ``triggers_from_condition_tree`` seeds a trigger
        # for every leaf it walks, and the GA's own ON/OFF toggle works by DELETING the child
        # node (``strategy_param_space._apply_to_tree``). Removal also takes the gate's
        # ``cond:<id>:enabled`` gene with it, so the GA cannot switch a gate back on for half
        # the population — which no static flag could have prevented.
        rule["conditions"]["conditions"] = [
            leaf for leaf in rule["conditions"]["conditions"] if not leaf.get("toggle_optimize")]
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


def _with_round_lot_entry(s, lot: int = 100):
    """Force the strategy's equity ENTRY to size in whole ``lot``-share lots (in place).

    Required by the option-OVERLAY strategies (O_CC / O_PP). ``SellCoveredCallAction`` and
    ``BuyProtectivePutAction`` both size as ``floor(held_shares / 100)`` — one contract per
    round lot — so an odd-lot equity position can never carry even ONE contract and the
    overlay silently no-ops, leaving a plain long-equity strategy behind.

    That is exactly what v8 shipped: O_CC and O_PP produced BYTE-IDENTICAL top-5 results
    (46.90% / 438.65% / 40.82% / 45.70% / 44.65%), ZERO trades carrying a contract_symbol,
    and a winning gene set with no covered-call/protective-put keys at all — both jobs were
    silently running the same plain S2 equity baseline. At $20k the RM sizes a position at
    5-25% of equity, i.e. 3-27 shares on a $180-320 name and 24-85 on a $30-40 name: never
    the 100 needed.

    With the lot constraint the RM floors to whole lots and rejects anything under one lot as
    unfunded, so O_CC/O_PP now trade ONLY where a round lot actually fits the per-instrument
    cap (at $20k: the cheap names, and only at the higher cap settings). Fewer entries, but
    they are real covered calls / protective puts instead of mislabelled equity."""
    for rule in (s.entry_rules or []):
        for action in (rule.get("actions") or []):
            if action.get("action_type") in ("buy", "sell"):
                action["lot_size"] = int(lot)
    return s


def _insert_option_overlay(exit_rules, guard, overlay, *, anchor: str = "adjust"):
    """Splice an option OVERLAY pair (guard + overlay) into an exit list so it is REACHABLE.

    The engine evaluates an OPEN_POSITIONS ruleset FIRST-MATCH: ``TradeActionEvaluator``
    breaks out of the rule loop as soon as one rule's conditions are met, unless that rule
    sets ``continue_processing`` (default False). S2's list ends with ``exit_stoploss``,
    conditioned only on ``has_position`` — which is true for every position the manage pass
    is invoked for — so an overlay APPENDED after it could NEVER run (OPT-B1). The GA could
    not route around it either: ``exit_stoploss`` declares no ``toggle_optimize``, so
    ``collect_param_space`` emits no ``exit:exit_stoploss:enabled`` gene and the shadow was
    unconditional in every genome. Every O_CC / O_PP number ever produced is therefore a
    mislabelled plain-equity run, and the two jobs were byte-identical to each other.

    Placement (``anchor="adjust"``, the EQUITY-entry default): AFTER the closing rules, BEFORE
    the first stop-adjusting rule.

      * a matched CLOSE still breaks first, so no option is written against shares that are
        being sold on that same bar (that would leave a naked short call);
      * the overlay carries ``continue_processing=True``, so the adjust rules behind it keep
        their own first-match priority intact — ``exit_belock``'s tighter break-even stop
        still beats the ``exit_stoploss`` floor, which it would not if the overlay let both
        run and the later (looser) one overwrote it (``adjust_sl`` overwrites, it does not
        ratchet);
      * the guard's ``stop_processing`` halts the chain while an overlay is already open,
        which is also what stops the stop-adjust rules re-arming a stop that would sell the
        shares out from under a live short call. The bracket levels already recorded on the
        transaction keep firing regardless — ``_apply_bracket_exits`` reads them directly,
        not through the rule chain.

    Fails loud when no stop-adjusting rule exists: the insertion point would then be a
    guess, and guessing is how the overlay got appended past the floor stop in the first
    place.

    ``anchor="front"`` is the PURE-OPTION exit list's placement (O_WHEEL), and it is opt-in
    precisely so the loud failure above still protects the equity lists. A pure-option exit
    list (``_option_exit_rules``) has NO ``adjust_*`` rule at all — every rule is a
    ``close_option`` — so "after the closes, before the adjusts" has no referent, and the two
    constraints resolve differently:

      * the "a matched close breaks first" constraint does NOT bind. Those closes act on the
        OPTION leg, not on shares, and the wheel's overlay gate (``has_assigned_shares``) can
        only be true once the short put is GONE. The two states are mutually exclusive by
        construction, so there is no bar on which a close and the overlay both want to act.
      * the REACHABILITY constraint binds, and points the other way. ``opt_tp``
        (``profit_loss_percent >``) and ``opt_time`` (``days_opened >``) compare fields an
        assigned-STOCK position also carries, so either can match on the very position the
        overlay exists to cover and break the walk with a ``close_option`` that has no option
        to close. Front placement removes that shadow outright rather than reasoning about
        when it bites.

    The guard still precedes the overlay in both modes — it is the codebase's NOT idiom and
    only works if it evaluates first.
    """
    rules = list(exit_rules or [])
    if anchor == "front":
        return [guard, overlay] + rules
    if anchor != "adjust":
        raise ValueError(f"_insert_option_overlay: unknown anchor {anchor!r}")
    for idx, rule in enumerate(rules):
        actions = [str(a.get("action_type") or a.get("action") or "")
                   for a in (rule.get("actions") or [])]
        if any(a.startswith("adjust_") for a in actions):
            return rules[:idx] + [guard, overlay] + rules[idx:]
    raise ValueError(
        "_insert_option_overlay: no adjust_* exit rule to anchor the overlay against; "
        f"got rule ids {[r.get('id') for r in rules]}. The overlay must sit after the "
        "closing rules and before the stop-adjusting rules — see this function's docstring."
    )


def _build_strategy_covered_call(kind: str):
    """O_CC — equity entry (the S2 baseline) + a ``sell_covered_call`` OPEN_POSITIONS overlay rule
    (sell a ~5% OTM call against the held shares). Equity-entry, so NO entry_action.

    The overlay is gated against re-firing every manage cycle (bug B2): rules evaluate in
    order, so a ``stop_processing`` guard rule ahead of it — the codebase's negation idiom
    (test_files/setup_option_rulesets.py; rules_documentation.py: "require NOT
    has_covered_call before sell_covered_call") — halts the ruleset whenever a covered call
    is ALREADY open, and ``cc_sell`` only fires while the held shares have no overlay.

    The pair is SPLICED into the exit list by ``_insert_option_overlay`` (after the closes,
    before the stop adjusts) rather than appended — appended, it sat behind S2's
    always-matching floor stop and could never fire at all (OPT-B1)."""
    from app.models.strategy import Strategy  # noqa: F401 — keep import parity with siblings
    s = _with_round_lot_entry(_build_strategy_S2(kind))  # equity entry in 100-share lots
    s.exit_rules = _insert_option_overlay(
        s.exit_rules,
        {"id": "cc_guard",
         "conditions": {"type": "AND", "conditions": [
             {"id": "cc_guard_has_cc", "field": "has_covered_call"}]},
         "actions": [{"action_type": "stop_processing"}],
         "continue_processing": False},
        {"id": "cc_sell",
         "conditions": {"type": "AND", "conditions": [{"id": "cc_hold", "field": "has_position"}]},
         "actions": [_option_overlay_action(
             "sell_covered_call", strike_param=5.0,
             strike_min=2.0, strike_max=12.0, strike_step=2.0)],
         # Writing the call must NOT consume the bar's single first-match slot: the exit
         # rules behind it (the break-even lock, the floor stop) still have to run.
         "continue_processing": True})
    return s


def _build_strategy_wheel(kind: str):
    """O_WHEEL — sell a cash-secured put; when it is ASSIGNED, write calls against the shares.

    A composition of two existing builders, not new machinery: ``O_CSP``'s pure-option entry
    rule plus ``O_CC``'s guard/overlay pair, with ONE deliberate change — the overlay is gated
    on ``has_assigned_shares`` rather than ``has_position``.

    That gate IS the wheel. ``has_position`` would write calls against any stock the expert
    happens to hold, including shares bought outright by some other rule; ``has_assigned_shares``
    writes them only against shares this strategy's own put delivered. The condition exists and
    is covered as a rule trigger by tests/test_wheel_assignment_order.py.

    THE ENTRY IS O_CSP'S, IDS INCLUDED (``o_csp-entry``, ``o_csp-signal``, ...). Deliberate: it
    is literally the same entry, so an O_CSP job and an O_WHEEL job produce IDENTICAL entry gene
    keys and a stage-1 O_CSP winner can be encoded into an O_WHEEL space without
    ``encode_params`` dropping anything. Only the exit list differs.

    SPLICED, never appended (OPT-B1). An overlay appended after an always-matching rule can
    never fire, and the GA cannot route around one that declares no toggle gene. That defect is
    why O_CC and O_PP — opposite strategies — once produced byte-identical top-5 results with
    zero trades carrying a contract symbol. Here the splice is ``anchor="front"``: the
    pure-option exit list has no ``adjust_*`` rule to sit in front of, and ``opt_tp`` /
    ``opt_time`` can match on an assigned-stock position and shadow the overlay. See
    ``_insert_option_overlay``.

    NOT round-lot constrained, unlike O_CC/O_PP: the shares arrive from assignment in exact
    100-share lots by construction, so there is no odd-lot entry to floor.

    THE ENGINE PRECONDITION, and how it is met. Until 2026-08-27 this builder REFUSED to run:
    ``BacktestAccount.settle_single_leg_expiry`` physically assigns every ITM short option and
    then scheduled the resulting stock for FULL liquidation at the next bar's open
    (``process_pending_assignment_liquidations``, the "no orphaned stock" policy) — and
    ``daily_engine`` runs the MANAGE pass (step 3) BEFORE that liquidation (step 4a-pre), so the
    overlay wrote a call against the assigned shares and the liquidation sold them out from
    under it on the same bar. Every wheel position the engine opened was a NAKED SHORT CALL.

    Plan Task 10 fixed that with the ``hold_assigned_stock`` account setting (DEFAULT OFF, so no
    existing option run moved). O_WHEEL is the one kind in ``_HOLDS_ASSIGNED_STOCK``, which is
    what puts the setting into its run config — see ``_hold_assigned_stock``. That wiring is not
    optional decoration: without it this strategy silently becomes a naked-call grid again, so
    ``test_option_grid_foundations.py`` asserts the run config carries it.

    KNOWN LIMIT of the composition: with the shares held, the only thing that CLOSES them is the
    covered call finishing ITM (the assignment delivers them). The exit list is all
    ``close_option`` and ``cc_guard`` halts the chain while a call is open, so a call that keeps
    expiring worthless leaves the stock held to the end of the run (reported ``open_at_end``).
    That is the wheel's real shape, not a defect of this builder — but it means an O_WHEEL run's
    capital efficiency depends on the strike gene, and it is pinned in
    ``tests/backtest/test_wheel_assignment.py``.
    """
    s = _build_strategy_option("O_CSP")
    s.name = kind
    s.exit_rules = _insert_option_overlay(
        s.exit_rules,
        {"id": "cc_guard",
         "conditions": {"type": "AND", "conditions": [
             {"id": "cc_guard_has_cc", "field": "has_covered_call"}]},
         "actions": [{"action_type": "stop_processing"}],
         "continue_processing": False},
        {"id": "cc_sell",
         "conditions": {"type": "AND", "conditions": [
             {"id": "cc_assigned", "field": "has_assigned_shares"}]},
         "actions": [_option_overlay_action(
             "sell_covered_call", strike_param=5.0,
             strike_min=2.0, strike_max=12.0, strike_step=2.0)],
         # See O_CC's cc_sell: writing the call must not consume the bar's first-match slot.
         "continue_processing": True},
        anchor="front")
    return s


def _build_strategy_stock(kind: str):
    """O_STK — plain equity long (the S2 baseline)."""
    return _build_strategy_S2(kind)


def _build_strategy_protective_put(kind: str):
    """O_PP — equity entry (the S2 baseline) + a ``buy_protective_put`` OPEN_POSITIONS overlay
    rule (buy a put ~8% OTM against the held shares). Mirrors _build_strategy_covered_call's
    shape exactly, swapping the overlay action; BuyProtectivePutAction sizes off the HELD
    equity quantity (1 contract per 100 shares), not option_sizing. Equity-entry, so NO
    entry_action. The overlay carries the same anti-stacking guard as O_CC (bug B2): a
    ``stop_processing`` guard rule on ``has_protective_put`` (a dedicated condition —
    TradeConditions.py ``HasProtectivePutCondition``) ahead of the overlay halts the ruleset
    once a protective put is already open. The pair is spliced in by
    ``_insert_option_overlay`` for the same reason O_CC's is (OPT-B1)."""
    from app.models.strategy import Strategy  # noqa: F401 — keep import parity with siblings
    s = _with_round_lot_entry(_build_strategy_S2(kind))  # equity entry in 100-share lots
    s.exit_rules = _insert_option_overlay(
        s.exit_rules,
        {"id": "pp_guard",
         "conditions": {"type": "AND", "conditions": [
             {"id": "pp_guard_has_pp", "field": "has_protective_put"}]},
         "actions": [{"action_type": "stop_processing"}],
         "continue_processing": False},
        {"id": "pp_buy",
         "conditions": {"type": "AND", "conditions": [{"id": "pp_hold", "field": "has_position"}]},
         "actions": [_option_overlay_action(
             "buy_protective_put", strike_param=8.0,
             strike_min=3.0, strike_max=15.0, strike_step=3.0)],
         # See cc_sell: buying the hedge must not consume the bar's first-match slot.
         "continue_processing": True})
    return s


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
    "O_BULLCS": _build_strategy_option, "O_BEARCS": _build_strategy_option,
    "O_BULLPS": _build_strategy_option,
    "O_CSP": _build_strategy_option, "O_STRD": _build_strategy_option,
    "O_STRG": _build_strategy_option,
    "O_CC": _build_strategy_covered_call, "O_STK": _build_strategy_stock,
    "O_PP": _build_strategy_protective_put,
    # O_CSP's option entry + O_CC's covered-call overlay, gated on has_assigned_shares.
    "O_WHEEL": _build_strategy_wheel,
    # Grouped option families (one job searches the whole family; see _OPTION_GROUPS):
    "OS1": _build_strategy_option_group, "OS2": _build_strategy_option_group,
    "OS3": _build_strategy_option_group, "OS4": _build_strategy_option_group,
}

# Per-strategy GA population multiplier applied on top of --population in optimize-batch. S1 is the
# richest strategy (live "high conviction" conditions + entry TP/SL bracket + target-anchored TP +
# exit rules => the largest gene space), so it gets a bit more population to search it; unlisted
# strategies use --population unchanged (factor 1.0).
_STRATEGY_POP_FACTOR = {"S1": 1.5}


# CONFIDENCE CEILINGS: the highest `confidence` an expert can ever emit.
#
# The strategy templates (S1-S7) gate on `confidence` with ranges sized for an expert that can
# reach 100. That is true of the analyst-driven experts, but NOT of a scorer whose confidence is a
# bounded function of its own composite: DeterministicScorer computes confidence = 100 * |final|
# where final = tanh(weighted sum / k_compress), and the score was MEASURED to top out at +0.562
# over 5,970 bars. A gate above that can never pass, so the GA spends population on genomes that
# are arithmetically incapable of trading -- `cond:gate_confidence:enabled` was the single
# strongest separator between trading and dead genomes in opt 333 (0.80 in dead vs 0.14 in
# traders). Clamping the RANGE is better than removing the gate: the gate is still useful below
# the ceiling, it just must not exceed it.
_EXPERT_CONFIDENCE_CEILING = {
    "DeterministicScorer": 50.0,   # measured max |final| 0.562 -> 56.2; 50 leaves headroom
}


def _clamp_confidence_genes(strat, expert: str):
    """Cap every `confidence` condition's value/range at the expert's reachable ceiling."""
    ceiling = _EXPERT_CONFIDENCE_CEILING.get(expert)
    if ceiling is None or strat is None:
        return strat

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        if node.get("field") == "confidence":
            for k in ("value", "value_min", "value_max"):
                v = node.get(k)
                if isinstance(v, (int, float)) and v > ceiling:
                    node[k] = ceiling
            # a collapsed range (min == max) would make the gene a constant; drop the step so
            # the param space treats it as fixed rather than emitting a zero-width sweep
            if node.get("value_min") is not None and node.get("value_min") == node.get("value_max"):
                node.pop("optimize", None)
        for v in node.values():
            walk(v)

    for attr in ("entry_rules", "exit_rules"):
        walk(getattr(strat, attr, None))
    return strat


def _build_strategy(kind: str, name: str, expert: str):
    """Dispatch to the right strategy builder.

    Every S-strategy is EXPERT-AGNOSTIC (2026-08-17): S1 used to load a per-expert live/default
    JSON and died for any expert without one; it now builds from the same kind of code template as
    S2-S7, with the expert contributing its own default settings. The only expert-specific step
    left is clamping confidence gates to what the expert can actually emit."""
    if kind == "S1":
        strat = _STRATEGY_BUILDERS[kind](name, expert)
    elif kind in _OPTION_STRATEGY_KEYS:
        strat = _STRATEGY_BUILDERS[kind](kind)
    else:
        builder = _STRATEGY_BUILDERS.get(kind)
        if builder is None:
            sys.exit(f"optimize: unknown strategy {kind!r}; have {sorted(_STRATEGY_BUILDERS)}")
        strat = builder(name)
    return _clamp_confidence_genes(strat, expert)


def _cmd_optimize(args) -> int:
    """Create a Strategy + StrategyOptimization and run a joint genetic optimization headless.

    Optimizes the expert's numeric decision settings + the 5 classic-RM params (sizing/stop
    'conditions & actions') + TP/SL, scored by --fitness, with parallel trials and suppressed
    per-trial logging. Persists the best trial as a tagged Backtest (optimization_id) and writes
    the HTML report.
    """
    global _OPTION_MIN_VOLUME, _OPTION_GATES_OFF
    _OPTION_MIN_VOLUME = int(getattr(args, "option_min_volume", _OPTION_MIN_VOLUME_DEFAULT))
    # Read BEFORE _build_strategy below — the option builders consult the module global.
    _OPTION_GATES_OFF = bool(getattr(args, "gates_off", False))
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
    # Pure-option kinds AND options experts (PremiumSeller — --strategy is ignored for it)
    # default to the ~30%/yr goal metric; stock kinds keep sharpe_ratio.
    fitness = _resolve_fitness(args.fitness, args.strategy,
                               "consistent_annual_return" if spec.get("options") else "sharpe_ratio")
    _assert_option_window_excludes_holdout([args.strategy], args.end)
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
            "experts": [{"class": expert, "settings": _expert_run_settings(spec, universe, _sizing_overrides(args))}],
            "start_date": args.start, "end_date": args.end,
            "initial_capital": float(args.initial_capital),
            "account_settings": {
                "starting_cash": float(args.initial_capital),
                "commission_per_trade": float(args.commission),
                "slippage_bps": float(args.slippage),
                "spread_bps": float(getattr(args, "spread_bps", 0.0)),
                "option_spread_pct": float(getattr(args, "option_spread_pct", 0.0)),
                "option_spread_min_tick": float(getattr(args, "option_spread_min_tick", 0.0)),
                "fill_model": args.fill_model,
                # RUN-LEVEL, never a gene (see --equity-cap): every individual in the
                # population must face the same capital, or they are scored against
                # different denominators. None = off.
                "equity_cap": getattr(args, "equity_cap", None),
                # PER-STRATEGY (see _hold_assigned_stock): True only for kinds whose own
                # rules manage assigned stock -- the wheel. False everywhere else keeps the
                # no-orphaned-stock liquidation, so every non-wheel run is unchanged.
                "hold_assigned_stock": _hold_assigned_stock(None if bypass else args.strategy),
            },
            "warmup_days": derive_warmup_days([expert]),
            "seed": int(args.seed),
            "subtype": "daily_expert",
            "run_schedule_override": run_sched,
            "manage_schedule_override": manage_sched,
            "execution_interval": args.interval,
            "profit_cap_pct": (float(args.profit_cap_pct) if args.profit_cap_pct else None),
            "profit_share_cap_pct": (float(args.profit_share_cap_pct) if args.profit_share_cap_pct else None),
            "stress_spread_bps": (float(args.stress_spread_bps) if getattr(args, "stress_spread_bps", 0) else 0.0),
            "robust_fitness": bool(getattr(args, "robust_fitness", False)),
            "fitness_trade_scale": bool(getattr(args, "fitness_trade_scale", False)),
            "fitness_trade_scale_cap": (float(args.fitness_trade_scale_cap)
                                        if getattr(args, "fitness_trade_scale_cap", None) else None),
            "fitness_trade_scale_target": (float(args.fitness_trade_scale_target)
                                        if getattr(args, "fitness_trade_scale_target", None) else None),
            "fitness_win_rate_factor": bool(getattr(args, "fitness_win_rate_factor", False)),
            "labels": [t.strip() for t in getattr(args, "labels", "").split(",") if t.strip()],
            "backtest_id": int(_dt.now().timestamp()),
            "name": f"opt-{expert}-trial",
        }
        # Options experts get the offline options-cache seam (no-op for equity experts).
        _apply_options_seam(spec, backtest_block)
        # WHICH store the run reads, resolved and recorded here rather than left to whatever
        # environment each trial process happens to have. Applies to every run, not just the
        # options-EXPERT ones above: a pure-option STRATEGY (--strategy O_LC) runs a classic
        # expert, so _apply_options_seam is a no-op for it while it is exactly the kind of job
        # that needs the parquet store.
        _apply_options_store(args, backtest_block)

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

        # GATE-ONLY screener (--screener-gate-store): attach the metric store PURELY as a
        # per-bar entry gate (the options grid's max-stock-price cap) — the run universe stays
        # the static --universe and NO screener genes enter the search. The optimization
        # handler reads gate_only to skip its candidate-bound universe restriction.
        _gate_opt = _screener_gate_opt_block(args, args.strategy)
        if _gate_opt:
            backtest_block["screener_opt"] = _gate_opt
            from ba2_providers.screener import metric_store as _gate_ms
            _gate_df = _gate_ms.load_store(_gate_opt["store"])
            if _gate_df.empty:
                sys.exit(f"optimize: --screener-gate-store {_gate_opt['store']!r} has no symbols")
            # Coverage guard: a symbol with no store row can NEVER pass the per-bar gate, so a
            # store that doesn't cover the static universe silently starves those names. Warn
            # loud instead of trading a quietly-shrunk universe.
            _covered = set(_gate_df["symbol"].unique())
            _static = list(backtest_block["enabled_instruments"])
            _uncovered = [s for s in _static if s not in _covered]
            if len(_uncovered) > max(1, len(_static) // 10):
                print(f"optimize: WARNING screener-gate store covers only "
                      f"{len(_static) - len(_uncovered)}/{len(_static)} universe symbols; the "
                      f"uncovered {len(_uncovered)} can NEVER enter — rebuild/extend the store "
                      f"(ba2-test build-screener-metrics) for this universe.")

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
            # their own portfolio so they carry only the narrow _BYPASS_RM_OPT block — unless the
            # spec opts out via no_bypass_rm — not the full _RM_OPT). Screener genes (screener:*
            # namespace) are merged in ONLY when --screener is set.
            "expert_params": ({**_bypass_gene_space(spec), **screener_genes} if bypass
                              else {**spec["expert_params"], **_rm_opt_for(args.strategy),
                                    **screener_genes, **schedule_genes}),
            "backtest": backtest_block,
        }
        if getattr(args, "warm_start_from", None) is not None:
            cfg["warmStartFromOptimizationId"] = int(args.warm_start_from)
        _worker_ids = _worker_ids_from_args(args)
        opt = StrategyOptimization(
            strategy_id=strat.id, name=args.name or f"opt-{expert}",
            fitness_metric=fitness, optimization_type="genetic",
            optimization_config=cfg, worker_ids=(_worker_ids or None), status="pending",
        )
        db.add(opt); db.commit(); db.refresh(opt)
        opt_id = opt.id
        if _worker_ids:
            print(f"optimize: distributing across worker ids {_worker_ids} + local")
        print(f"optimize: strategy #{strat.id} + StrategyOptimization #{opt_id} "
              f"({expert} x {len(universe)} syms, pop={args.population} gen={args.generations} "
              f"parallel={args.parallel} fitness={fitness})")
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
            description=f"{expert} x {len(universe)} syms, {fitness}, pop={args.population} gen={args.generations}",
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
    from app.services.strategy_optimization_handler import _last_gen_full_results_by_opt
    nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top), parallel=int(args.parallel),
                                     last_gen_full_results=_last_gen_full_results_by_opt.pop(opt_id, None))
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
    global _OPTION_MIN_VOLUME
    _OPTION_MIN_VOLUME = int(getattr(args, "option_min_volume", _OPTION_MIN_VOLUME_DEFAULT))
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

    # Same module-level toggle _cmd_optimize sets. Without this, --gates-off parses on the
    # batch command and then does NOTHING -- the worst kind of flag, because the run completes
    # and reports "no trades" for a reason the operator has just tried to rule out.
    global _OPTION_GATES_OFF
    _OPTION_GATES_OFF = bool(getattr(args, "gates_off", False))
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
    _assert_option_window_excludes_holdout([k for _e, k in jobs], args.end)
    init_db()
    tq = get_task_queue()
    print(f"optimize-batch: {len(jobs)} job(s) {jobs} x {len(universe)} syms, "
          f"fitness={args.fitness or 'auto (car for pure-option, calmar_ratio otherwise)'}, "
          f"pop={args.population} gen={args.generations} parallel={args.parallel}")

    for n, (expert, strat_kind) in enumerate(jobs, 1):
        spec = _EXPERT_OPT[expert]
        bypass = bool(spec.get("bypass"))
        prefix = args.name_prefix or "phase1"
        # Per-job resolution: pure-option kinds AND options experts (PremiumSeller — bypass,
        # so strat_kind is "FACTOR" and carries no option-kind default) default to
        # consistent_annual_return; stock kinds keep this command's historical calmar_ratio
        # default.
        fitness = _resolve_fitness(args.fitness, strat_kind,
                                   "consistent_annual_return" if spec.get("options") else "calmar_ratio")
        name = f"{prefix}-{expert}-{strat_kind}-{fitness}"
        db = SessionLocal()
        try:
            strat = _build_strategy_minimal(name) if bypass else _build_strategy(strat_kind, name, expert)
            # Pure-option kinds carry a transient `entry_action`; capture before commit/refresh.
            strat_entry_action = getattr(strat, "entry_action", None)
            db.add(strat); db.commit(); db.refresh(strat)
            backtest_block = {
                "engine": "daily",
                "enabled_instruments": universe,
                "experts": [{"class": expert, "settings": _expert_run_settings(spec, universe, _sizing_overrides(args))}],
                "start_date": args.start, "end_date": args.end,
                "initial_capital": float(args.initial_capital),
                "account_settings": {
                    "starting_cash": float(args.initial_capital),
                    "commission_per_trade": float(args.commission),
                    "slippage_bps": float(args.slippage),
                    "spread_bps": float(getattr(args, "spread_bps", 0.0)),
                    "option_spread_pct": float(getattr(args, "option_spread_pct", 0.0)),
                    "option_spread_min_tick": float(getattr(args, "option_spread_min_tick", 0.0)),
                    "fill_model": args.fill_model,
                    # RUN-LEVEL, never a gene (see --equity-cap). None = off.
                    "equity_cap": getattr(args, "equity_cap", None),
                    # PER-STRATEGY (see _hold_assigned_stock): the wheel manages its
                    # assigned stock, so it must not be liquidated at the next bar's open.
                    "hold_assigned_stock": _hold_assigned_stock(None if bypass else strat_kind),
                },
                "warmup_days": derive_warmup_days([expert]),
                "seed": int(args.seed),
                "subtype": "daily_expert",
                "run_schedule_override": run_sched,
                "manage_schedule_override": _daily_manage_schedule(),
                "execution_interval": args.interval,
                "profit_cap_pct": (float(args.profit_cap_pct) if args.profit_cap_pct else None),
                "profit_share_cap_pct": (float(args.profit_share_cap_pct) if args.profit_share_cap_pct else None),
            "stress_spread_bps": (float(args.stress_spread_bps) if getattr(args, "stress_spread_bps", 0) else 0.0),
                "robust_fitness": bool(getattr(args, "robust_fitness", False)),
                "fitness_trade_scale": bool(getattr(args, "fitness_trade_scale", False)),
                "fitness_trade_scale_cap": (float(args.fitness_trade_scale_cap)
                                            if getattr(args, "fitness_trade_scale_cap", None) else None),
                "fitness_trade_scale_target": (float(args.fitness_trade_scale_target)
                                            if getattr(args, "fitness_trade_scale_target", None) else None),
                "fitness_win_rate_factor": bool(getattr(args, "fitness_win_rate_factor", False)),
                "labels": [t.strip() for t in getattr(args, "labels", "").split(",") if t.strip()],
                "backtest_id": int(_dt.now().timestamp()),
                "name": f"{name}-trial",
            }
            # Options experts get the offline options-cache seam (no-op for equity experts).
            _apply_options_seam(spec, backtest_block)
            # The store decision, resolved once and recorded on the block. THIS driver is the one
            # that fans out to remote workers, so it is the one for which "the store came from an
            # exported env var" is a silent lie (see _apply_options_store).
            _apply_options_store(args, backtest_block)
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
                # Bypass experts (FactorRanker, PremiumSeller) size their own portfolio and skip
                # the full RM block + per-day schedule genes, but carry _BYPASS_RM_OPT
                # (risk_per_trade_pct, which prices FactorRanker's resting protective stop)
                # UNLESS the spec opts out via no_bypass_rm (PremiumSeller's manager owns its
                # exits — the gene would be dead weight); ruleset experts get the full
                # RM sizing/stop params + per-weekday entry-scan toggle genes.
                "expert_params": (_bypass_gene_space(spec) if bypass
                                  else {**spec["expert_params"], **_rm_opt_for(strat_kind),
                                        **{f"schedule:{k}": v for k, v in _SCHEDULE_DAY_OPT.items()}}),
                "backtest": backtest_block,
            }
            opt = StrategyOptimization(
                strategy_id=strat.id, name=name, fitness_metric=fitness,
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
            description=f"{expert} {strat_kind} x {len(universe)} syms, {fitness}, pop={pop_for_strat}",
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
            # NOTE: this batch driver submits each optimization to the RUNNING serve queue and
            # polls it (it never calls handle_strategy_optimization in THIS process), so
            # _last_gen_full_results_by_opt here is process-local to the CLI, not the serve
            # process that actually ran the GA -- this pop is a no-op (always None) until the
            # serve process's own buffer is collected too (tracked separately; see the "KNOWN
            # GAP" comment on _last_gen_full_results_by_opt's declaration). Harmless: falls back
            # to the existing full re-run path exactly like today.
            from app.services.strategy_optimization_handler import _last_gen_full_results_by_opt
            nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top), parallel=int(args.parallel),
                                             last_gen_full_results=_last_gen_full_results_by_opt.pop(opt_id, None))
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


# How long to wait before the ONE retry in _remote_then_local. Short: the failure this covers
# (a worker mid self-update returning 503 for its still-restarting endpoints, or a dropped
# connection) typically clears within seconds, not minutes.
_REMOTE_RETRY_BACKOFF_S = 5.0


def _remote_then_local(worker: Dict[str, Any], trial_cfg: Dict[str, Any], fitness_metric: str) -> Dict[str, Any]:
    """Try *worker* for a top-N re-run, retrying once after a short backoff, then fall back to
    running the trial directly (the same path a "local" slot uses) rather than permanently
    losing this rank.

    Before this, a remote-dispatched top-N re-run got exactly one shot: a leaked pool slot
    (opt 361/362) or a worker mid self-update returning 503 for its still-restarting endpoints
    (opt 364) both silently dropped that rank, and someone had to notice the gap and recover it
    by hand. Both were transient -- a retry a few seconds later, or falling back to a box that's
    definitely not restarting, gets the row on the first attempt instead."""
    import time
    from app.services.strategy_optimization_handler import _persist_trial_worker
    from app.services.worker_client import run_trial_full
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            return run_trial_full(worker, trial_cfg, fitness_metric)
        except Exception as e:  # noqa: BLE001 -- transient remote failure; retry/fallback below
            last_exc = e
            if attempt == 0:
                print(f"    remote {worker.get('name')} failed ({e!r}); retrying once...")
                time.sleep(_REMOTE_RETRY_BACKOFF_S)
    print(f"    remote {worker.get('name')} failed twice ({last_exc!r}); falling back to local")
    return _persist_trial_worker(trial_cfg)


def _persist_top_backtests(opt_id: int, expert: str, n: int = 5, parallel: int = 1,
                            last_gen_full_results: Optional[Dict[str, Any]] = None) -> int:
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

        # Fixed (non-GA-tunable) settings this optimization pinned for the expert, e.g.
        # sizing_mode=risk_atr (see _EXPERT_OPT[...]["fixed_settings"]). These live only in
        # bt_block["experts"][i]["settings"], sourced from StrategyOptimization.optimization_config
        # -- a row that can be pruned by `server db-cleanup` long after this Backtest is "starred"
        # and kept. Stash them directly onto each persisted Backtest.strategy_params so a later
        # export/deploy (_derive_export_payload) doesn't depend on the optimization row surviving;
        # without this, a pruned optimization silently drops sizing_mode from the deploy and the
        # live instance ends up with zero safeguard stop-loss protection (see the PKE/CALX incident).
        _fixed_settings = {}
        for _spec in (bt_block.get("experts") or []):
            if isinstance(_spec, dict) and _spec.get("class") == expert:
                _fixed_settings = dict(_spec.get("settings") or {})
                break

        # Top-N param sets by DISTINCT fitness (fall back to best_params if all_results is thin).
        # Dedup on fitness, not raw params: a converged GA yields many param sets that differ only
        # in INERT genes (e.g. exit:<id>:action_value while exit:<id>:enabled=0) yet score the same
        # and produce identical backtests — keying on params would persist N behaviourally-identical
        # rows. Distinct fitness gives genuinely different performers across the search landscape.
        last_gen_full_results = last_gen_full_results or {}
        seen, ranked = set(), []
        for r in sorted(opt.all_results or [], key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9), reverse=True):
            fit = r.get("fitness")
            dedup_key = round(fit, 6) if isinstance(fit, (int, float)) else _json.dumps(r.get("params"), sort_keys=True, default=str)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            # Carry the FITNESS, not just the params. It is the only record of how the GA
            # actually ranked these rows -- the persisted metric columns are its inputs, and
            # re-deriving the order from any one of them drops the other three terms.
            ranked.append((r["params"], r.get("key"), fit))
            if len(ranked) >= n:
                break
        if not ranked and opt.best_params:
            # No trial key known -> always falls back to re-run. best_fitness is the score of
            # exactly this genome (it is what made it `best`), so it is the right value here.
            ranked = [(opt.best_params, None, opt.best_fitness)]

        # 1) Build every re-run's spec in the MASTER (cheap: decode + config + display params).
        #    Store the raw optimized genes (for the "Optimized Parameters" display) AND the CONCRETE
        #    decoded ruleset that actually ran (buy/sell/exit trees + TP/SL) so Load/export can
        #    restore the optimized conditions directly. Keys mirror what _derive_export_payload reads.
        ready = []  # (rank, trial_cfg, strategy_params, full_results) -- no re-run needed
        specs = []  # (rank, trial_cfg, strategy_params) -- must be re-run (existing path)
        for rank, (params, trial_key_, ga_fitness_) in enumerate(ranked, start=1):
            decoded = decode_params(strat, params)
            trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
            trial_cfg["name"] = f"TOP{rank}-{opt.name or expert}"
            # Persist this top-N run's trading DB (orders/transactions/recommendations) to disk
            # for post-mortem inspection — the GA trials run RAM-only for speed. The path is keyed
            # by the trial's UNIQUE backtest_id, so concurrent re-runs never collide.
            trial_cfg["persist_trading_db"] = True
            # Carried onto the persisted row (migration 030) so the GA ranking is a stored
            # fact rather than something re-derived from one component metric.
            trial_cfg["ga_fitness"] = ga_fitness_
            strategy_params = dict(params)
            if _fixed_settings:
                strategy_params["expertFixedSettings"] = _fixed_settings
            # Unified rule model (migration 028): the CONCRETE decoded TradeRule lists that
            # actually ran (genes applied, disabled rules/actions pruned) — what Load/export
            # restores.
            if decoded.get("entry_rules") is not None:
                strategy_params["entryRules"] = decoded["entry_rules"]
            if decoded.get("exit_rules") is not None:
                strategy_params["exitRules"] = decoded["exit_rules"]
            buffered = last_gen_full_results.get(trial_key_) if trial_key_ else None
            if buffered is not None:
                ready.append((rank, trial_cfg, strategy_params, buffered))
            else:
                specs.append((rank, trial_cfg, strategy_params))
        if ready:
            print(f"    {len(ready)}/{len(ranked)} top individual(s) available from the GA's "
                  f"last generation (no re-run); re-running the remaining {len(specs)}.")

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
            # Record BOTH fitness views onto the results blob before it is mapped to the row.
            # compute_fitness writes fitness_raw / fitness_robust / robustness INTO the dict it is
            # given, and _persist_results copies everything that is not a curve/trade list into
            # bt.results -- so this is what makes a persisted row decomposable ("was this genome
            # ranked up by its raw CAR, or did the concentration factor cut it?").
            # It runs on the MASTER because _persist_trial_worker deliberately returns the raw
            # engine blob and computes no fitness; doing it here covers the local pool, the remote
            # /run-trial-full path and the no-re-run (final-generation) rows identically.
            try:
                from app.services.strategy_fitness import compute_fitness as _cf
                _cf(opt.fitness_metric, out["results"])
            except Exception as _e:  # noqa: BLE001 -- never lose a persisted row over telemetry
                print(f"    TOP{rank} fitness annotation failed: {_e!r}")
            _persist_results(db, bt, out["results"])
            # The GA's composite score for this genome (migration 030). It comes from the
            # OPTIMIZER, not the engine, so _persist_results (which maps `results`) cannot
            # supply it. Without this the TOP<n> name prefix is the only record of the ranking.
            if trial_cfg.get("ga_fitness") is not None:
                bt.ga_fitness = float(trial_cfg["ga_fitness"])
            bt.status = "completed"; bt.completed_at = _dt.now()
            bt.is_saved = True  # top performers of a job are kept
            db.commit()
            push_backtest(bt, db)
            return True

        persisted = 0
        for rank, trial_cfg, strategy_params, full_results in ready:
            if _persist_one(rank, trial_cfg, strategy_params, {"ok": True, "results": full_results}):
                persisted += 1
                print(f"    persisted TOP{rank} ({persisted}/{len(ranked)}) [no re-run]")
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
                           else remote_ex.submit(_remote_then_local, slot, tc, fitness_metric))
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
                        print(f"    persisted TOP{rk} ({persisted}/{len(ranked)})")
        else:
            for rk, cfg, sp2 in specs:
                if _persist_one(rk, cfg, sp2, _persist_trial_worker(cfg)):
                    persisted += 1
                    print(f"    persisted TOP{rk} ({persisted}/{len(ranked)})")
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

    # The two option READERS a run can be served by (backtest/options_store.py). Taken from the
    # seam rather than spelled out here so a third store cannot exist without the CLI offering it.
    from app.services.backtest.options_store import OPTIONS_STORES as _OPTIONS_STORES

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
    pw.add_argument("--start", default=None,
                    help="ISO start date. Only consumed by FMPSenateTraderWeight, to proactively "
                         "compute trader-skill scores for every trading day in [start, end] instead "
                         "of leaving them to lazy per-trial computation (see _do_senate_scores). "
                         "Ignored by the other experts.")

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
    fo.add_argument("--start", required=True, help="ISO start date (>= 2024-01-18, Alpaca options-history floor).")
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
    op.add_argument("--fitness", default=None,
                    help="Fitness metric. Default: 'option_consistent_annual_return' (aliases "
                         "'option_car'/'ocar') for pure-option strategies (OS1-OS4 + O_* option "
                         "entries; NOT the equity-entry O_CC/O_PP/O_STK), 'sharpe_ratio' for "
                         "stock strategies. "
                         "'consistent_annual_return' (aliases 'car'/'goal') targets ~30%%/yr "
                         "EVERY year: (adjusted) annualized return, hard >=30 trades/yr gate, "
                         "soft drawdown penalty beyond 20%%, x worst-year/mean-year consistency "
                         "(--fitness-trade-scale is a no-op for it); the option default is that "
                         "shape applied to an option book.")
    op.add_argument("--generations", type=int, default=6)
    op.add_argument("--population", type=int, default=10)
    op.add_argument("--parallel", type=int, default=4, help="Parallel trials (ThreadPoolExecutor).")
    op.add_argument("--early-stop", type=int, default=4)
    op.add_argument("--mutation-prob", type=float, default=0.3,
                    help="Per-gene mutation probability (higher = more exploration). Default 0.3.")
    op.add_argument("--save-top", type=int, default=5,
                    help="Persist the top-N distinct param sets as saved Backtests (default 5).")
    op.add_argument("--seed", type=int, default=42, help="RNG seed (determinism).")
    op.add_argument("--warm-start-from", type=int, default=None,
                    help="StrategyOptimization id to seed this job's STARTING population from "
                         "(that job's evaluated individuals, most-recent -- i.e. approx. its "
                         "final generation -- first). This is a warm start, NOT a resume: this "
                         "job still runs its own fresh --generations budget from generation 0, "
                         "and --seed still applies (so a different seed explores differently "
                         "from the same starting point). Use to keep evolving a converged/"
                         "plateaued run instead of re-searching from scratch. Default: off "
                         "(fresh random population).")
    op.add_argument("--initial-capital", type=float, default=10000.0)
    op.add_argument("--equity-cap", type=float, default=None,
                    help="Optional FIXED equity the risk manager sizes against, in dollars "
                         "(default: off, i.e. the account compounds). With a cap set, profits "
                         "above it are not deployed and losses below it are real, so every year "
                         "faces the same capital -- use this to test whether a setting is stable "
                         "across years independent of its start date. Run-level, never a gene.")
    op.add_argument("--profit-cap-pct", type=float, default=2000.0,
                    help="Cap each trade's gain at this %% of its cost basis when computing the "
                         "ADJUSTED fitness/return (2000 = a trade can't count as more than 20x its "
                         "cost). Stops one lucky mega-winner dominating the GA. DEFAULT-ON since 2026-07-31: the senate grid ran uncapped and its S5 TOP5 reached rank 5 with 96.9%% of net P&L in ONE trade (370%% total return, ~flat without it) -- the capband matrix driver had defaulted these to 2000/25 for ages, the senate scripts never passed them, and nobody noticed until the concentration was checked by hand. Pass 0 to disable.")
    op.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each trade's gain at this %% of the run's NET profit for the ADJUSTED "
                         "fitness/return (25 = no single trade may contribute >25%% of total return). "
                         "Complements --profit-cap-pct: a trade can pass the cost-basis cap yet still "
                         "dominate the book's return; this bounds that. Stops one lucky mega-winner dominating the GA. DEFAULT-ON since 2026-07-31: the senate grid ran uncapped and its S5 TOP5 reached rank 5 with 96.9%% of net P&L in ONE trade (370%% total return, ~flat without it) -- the capband matrix driver had defaulted these to 2000/25 for ages, the senate scripts never passed them, and nobody noticed until the concentration was checked by hand. Pass 0 to disable.")
    op.add_argument("--stress-spread-bps", type=float, default=0.0,
                    help="Also score every genome as if the spread were this many bps "
                         "WIDER and rank on the WORSE of the two. Selects against configs "
                         "whose per-trade edge barely clears the modelled cost. 0 = off "
                         "(default). NOTE: a non-zero value RESCALES fitness, so scores "
                         "are not comparable with runs made at a different level.")
    op.add_argument("--robust-fitness", action="store_true",
                    help="Rank on a ROBUSTNESS-ADJUSTED fitness instead of the raw metric: the "
                         "metric is multiplied by a concentration factor (how much of net P&L came "
                         "from the top 1/5 trades), a Monte-Carlo factor (1000-path bootstrap of the "
                         "trade sequence; penalises a genome whose 5th-percentile path loses money) "
                         "and a spread factor (fraction of profit surviving a wider spread). A big "
                         "winner that is not reproducible therefore stops being rewarded. BOTH "
                         "numbers are stored (fitness_raw + fitness_robust + every component), but "
                         "scores are NOT comparable with a run made without this flag. Default: off.")
    op.add_argument("--fitness-trade-scale", action="store_true",
                    help="Multiply each trial's fitness by min(avg_trades_per_year, cap)/target, so "
                         "statistically thin (few-trade) configs are down-weighted (~target trades/yr "
                         "= break-even, default 100). The cap (--fitness-trade-scale-cap) bounds the "
                         "factor so the GA is NOT rewarded for over-trading (scalping). Stops a "
                         "16-trade lottery winner from topping the search on calmar. Default: off.")
    op.add_argument("--fitness-trade-scale-cap", type=float, default=100.0,
                    help="Cap (trades/year) for --fitness-trade-scale: avg_trades_per_year is clamped "
                         "to this before scaling, so above it the factor stops growing (no scalper "
                         "incentive). Default 100 = factor maxes at 1.0 (pure thinness penalty); a "
                         "higher value allows some up-weighting up to that rate.")
    op.add_argument("--fitness-trade-scale-target", type=float, default=100.0,
                    help="Trades/year that earns FULL credit (factor 1.0) for --fitness-trade-scale. "
                         "Default 100 (matches equities cadence); lower it (e.g. 50) for an asset "
                         "class/strategy that naturally trades less often (options), so a healthy "
                         "config there isn't crushed just for not hitting an equities-scale trade "
                         "count. Independent of --fitness-trade-scale-cap, which bounds the numerator, "
                         "not the target denominator.")
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
    op.add_argument("--commission", type=float, default=0.1,
                    help="Flat $ commission per FILL (charged twice per round trip). Default 0.1. "
                         "WAS 1.0 until 2026-08-16: at the $10k account size these runs use, $1 a "
                         "fill is ~0.07%% of a typical position against an average trade of "
                         "+0.19%% -- i.e. the assumed commission alone consumed roughly a third "
                         "of the modelled edge, which is not what a real commission-free equity "
                         "broker charges. Scores are NOT comparable across a change to this "
                         "value: every stored result before that date carries the 1.0 cost model.")
    op.add_argument("--slippage", type=float, default=0.0)
    op.add_argument("--spread-bps", type=float, default=0.0,
                    help="Round-trip bid-ask spread in basis points, modeled properly at the "
                         "fill-engine level (widens LIMIT/TP trigger thresholds + degrades "
                         "MARKET/STOP fill prices) -- see BacktestAccount._slip/"
                         "_limit_trigger_price. Default 0.0 (off).")
    op.add_argument("--option-spread-pct", type=float, default=5.0,
                    help="Modeled OPTION bid-ask spread as a PERCENT OF PREMIUM (full width; "
                         "half charged per fill, adverse direction), widened x2 for contracts "
                         "under 100 contracts/day. Separate from --spread-bps because bps-of-price "
                         "is the wrong shape for a premium (5 bps of a $1.00 option is $0.0005). "
                         "The cached chain has NO real quotes (every row is bid==ask or NULL), so "
                         "without this an option round trip costs ~nothing and multi-leg credit "
                         "structures are systematically overstated. Default 5.0; pass 0 to "
                         "reproduce pre-2026-07-25 results.")
    op.add_argument("--option-spread-min-tick", type=float, default=0.02,
                    help="Absolute floor on the modeled option spread in premium dollars (full "
                         "width). Percent-of-premium alone under-charges cheap contracts, which "
                         "is where fabricated edge concentrates. Default 0.02.")
    op.add_argument("--option-min-volume", type=int, default=_OPTION_MIN_VOLUME_DEFAULT,
                    help="Minimum DAILY TRADED VOLUME for an option contract to be selectable. "
                         "The fill engine caps an order at 10%% of a bar's volume, so a contract "
                         "trading below ~10x the order size can never fill -- without this the "
                         "selector hands the filler candidates it rejects, and the order just sits "
                         "pending. Cached-bar distribution: p25=3, p50=14, p75=71. A tradability "
                         "floor, NOT a GA gene (exposed, the GA would drive it to 0). 0 disables.")
    op.add_argument("--options-store", default=None, choices=list(_OPTIONS_STORES),
                    help="WHICH option store the run reads, and therefore whose history floor "
                         "applies: 'sqlite' (default -- the Alpaca-built OptionsHistoryCache, "
                         "floor 2024-01-18, the store every recorded backtest number came from) "
                         "or 'parquet' (the TastyTrade/dxfeed tree, floor 2022-10-01, the only "
                         "one holding 2023). Omitted -> $BACKTEST_OPTIONS_STORE, then sqlite. "
                         "State it on the command line for a DISTRIBUTED run: the env var is "
                         "read on whichever process resolves it, and no environment travels "
                         "with a trial shipped to a remote worker.")
    op.add_argument("--gates-off", action="store_true",
                    help="SMOKE RUNS: drop every OPTIONAL option-entry condition gate (the "
                         "directional signal, confidence, iv_rank, relative volume, "
                         "iv_to_realized_vol and expected profit), genes included. iv_rank and "
                         "iv_to_realized_vol fail CLOSED when IV is unmeasurable, so on a cache "
                         "without greeks a gated individual trades nothing and scores the "
                         "zero-trade sentinel; with the gates off, 'traded nothing' can only mean "
                         "data or wiring. Correctness guards (has_no_position) and the EXIT rules "
                         "stay on, and so do the equity gates of the overlay kinds (O_CC/O_PP), "
                         "which do not enter through the option rule. Not for a real grid: the "
                         "entry then fires on every evaluated symbol.")
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
    op.add_argument("--sizing-mode", choices=("notional", "risk_atr"), default=None,
                    help="Override the expert spec's pinned sizing_mode for this run. Omit to "
                         "keep the spec default (risk_atr for the classic experts). Exists so "
                         "notional vs risk_atr can be compared as TWO separate optimizations "
                         "instead of one gene: under notional the five ATR genes "
                         "(risk_per_trade_pct, atr_multiplier, atr_period, min_stop_loss_pct, "
                         "use_atr_stop) are inert and drift random, so a crossover that flipped "
                         "the mode would score it with unselected parameters and bias the "
                         "comparison toward whichever mode dominates the population. No effect "
                         "on bypass experts (FactorRanker, PremiumSeller) — they skip "
                         "TradeRiskManagement entirely, so sizing_mode is never read. ALWAYS "
                         "give the two runs different --name suffixes, or the second is SKIPped "
                         "as an already-completed run.")
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
    op.add_argument("--screener-gate-store", default=None,
                    help="GATE-ONLY screener mode: path to the parquet metric store used PURELY "
                         "as a per-bar entry gate — the run universe stays the static "
                         "--universe and no screener:* genes enter the search. Pairs with "
                         "--max-stock-price so the options grid skips underlyings a $20k "
                         "account cannot structure. Cannot be combined with --screener.")
    op.add_argument("--max-stock-price", type=float, default=_MAX_STOCK_PRICE_DEFAULT,
                    help="Max UNDERLYING price admitted by the gate-only screener entry gate "
                         "(default 100 — the $20k-account cap for the options grid). "
                         "Point-in-time: a name above the cap is only excluded while above it. "
                         "0 disables the price filter. Per-strategy overrides live in "
                         "_OPTION_STRATS[].screener_gate_base.")
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
    ob.add_argument("--gates-off", action="store_true",
                    help="Disable every OPTIONAL option-entry condition gate (smoke runs). Same "
                         "flag as `optimize --gates-off`; without it on THIS command the module "
                         "toggle stays False and a batch smoke run silently keeps its gates ON, "
                         "which produces exactly the ambiguous 'traded nothing' the smoke stage "
                         "exists to eliminate.")
    ob.add_argument("--fitness", default=None,
                    help="Fitness metric, resolved PER JOB when omitted: "
                         "'option_consistent_annual_return' for pure-option kinds "
                         "(OS1-OS4/O_*), 'calmar_ratio' for stock kinds (the historical batch "
                         "default). See optimize --fitness for what those metrics mean. Equity-ENTRY option kinds (O_CC/O_PP/O_STK) are excluded from that and keep the stock default, matching _resolve_fitness.")
    ob.add_argument("--generations", type=int, default=8)
    ob.add_argument("--population", type=int, default=40)
    ob.add_argument("--parallel", type=int, default=6, help="Process-pool workers per job.")
    ob.add_argument("--early-stop", type=int, default=4)
    ob.add_argument("--mutation-prob", type=float, default=0.3,
                    help="Per-gene mutation probability (higher = more exploration). Default 0.3.")
    ob.add_argument("--save-top", type=int, default=5)
    ob.add_argument("--seed", type=int, default=42)
    ob.add_argument("--initial-capital", type=float, default=10000.0)
    ob.add_argument("--equity-cap", type=float, default=None,
                    help="Optional FIXED equity the risk manager sizes against, in dollars "
                         "(default: off). See `optimize --equity-cap`. Run-level, never a gene.")
    ob.add_argument("--profit-cap-pct", type=float, default=2000.0,
                    help="Cap each trade's gain at this %% of its cost basis for the ADJUSTED "
                         "fitness/return (2000). Default-on; see `optimize --profit-cap-pct`.")
    ob.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each trade's gain at this %% of the run's NET profit for the ADJUSTED "
                         "fitness/return (25). Default-on; see `optimize --profit-share-cap-pct`.")
    ob.add_argument("--commission", type=float, default=0.1,
                    help="Flat $ commission per FILL (see optimize --commission; default lowered "
                         "from 1.0 on 2026-08-16). Kept in step with the optimize default so a "
                         "single backtest and a GA trial price the same trade identically.")
    ob.add_argument("--slippage", type=float, default=0.0)
    ob.add_argument("--spread-bps", type=float, default=0.0,
                    help="Round-trip bid-ask spread in basis points (see optimize --spread-bps).")
    ob.add_argument("--option-spread-pct", type=float, default=5.0,
                    help="Modeled OPTION bid-ask spread as a PERCENT OF PREMIUM (full width; "
                         "half charged per fill, adverse direction), widened x2 for contracts "
                         "under 100 contracts/day. Separate from --spread-bps because bps-of-price "
                         "is the wrong shape for a premium (5 bps of a $1.00 option is $0.0005). "
                         "The cached chain has NO real quotes (every row is bid==ask or NULL), so "
                         "without this an option round trip costs ~nothing and multi-leg credit "
                         "structures are systematically overstated. Default 5.0; pass 0 to "
                         "reproduce pre-2026-07-25 results.")
    ob.add_argument("--option-spread-min-tick", type=float, default=0.02,
                    help="Absolute floor on the modeled option spread in premium dollars (full "
                         "width). Percent-of-premium alone under-charges cheap contracts, which "
                         "is where fabricated edge concentrates. Default 0.02.")
    ob.add_argument("--option-min-volume", type=int, default=_OPTION_MIN_VOLUME_DEFAULT,
                    help="Minimum DAILY TRADED VOLUME for an option contract to be selectable. "
                         "The fill engine caps an order at 10%% of a bar's volume, so a contract "
                         "trading below ~10x the order size can never fill -- without this the "
                         "selector hands the filler candidates it rejects, and the order just sits "
                         "pending. Cached-bar distribution: p25=3, p50=14, p75=71. A tradability "
                         "floor, NOT a GA gene (exposed, the GA would drive it to 0). 0 disables.")
    ob.add_argument("--options-store", default=None, choices=list(_OPTIONS_STORES),
                    help="WHICH option store every job in the batch reads: 'sqlite' (default, "
                         "Alpaca, floor 2024-01-18) or 'parquet' (TastyTrade/dxfeed, floor "
                         "2022-10-01, the only one holding 2023). Omitted -> "
                         "$BACKTEST_OPTIONS_STORE, then sqlite. THIS is the driver that fans "
                         "trials out to remote workers, and no environment travels with a "
                         "trial -- state the store here.")
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
