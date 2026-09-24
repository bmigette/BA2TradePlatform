"""Does the market-condition gate cost a trial anything measurable?

NOT a pytest test (lives in tests_scripts/ deliberately; ``--quick`` is exercised by
``testplatform/backend/tests/test_bench_market_conditions.py``).

THE ACCEPTANCE BAR (plan Task 9, operator 2026-09-15 "ensure no impact on existing bt and
test perf of these new conditions"): with a prepared manifest, a gate is ONE indexed lookup
and ONE float comparison per evaluated leaf. The gates must add < 1% to trial time. A larger
number is not a result to report -- it means a DataFrame, a calculator or a cache-tree scan
leaked into the decision path, and the thing to do with it is find that, not write it down.

PHASES (``--phases``, default all):

  coverage  which of the universe file's symbols the pinned snapshot actually serves. The
            bench runs on the COVERED subset: a symbol with no rows reads missing_session in
            microseconds and would flatter every number below.
  observe   P50/P95 of ``MappedMarketConditionReader.observe`` on a memo MISS and on a HIT.
            The miss is the real per-(symbol, session) cost; the hit is what a second leaf on
            the same bar pays.
  gate      the whole per-evaluation cost through the REAL condition class -- resolver, memo,
            by_field, comparison -- which is the number the trial arithmetic multiplies.
  trial     N full backtests of one fixed genome with the profile OFF, then ON with three
            active gates. Median seconds each way; the verdict is the ratio.
  workers   ``--workers`` processes each opening the mapping, with each child's peak RSS and
            open descriptor/handle count. The intended grid worker count is 30.

The D§4.6 build counters (cold build, repeat warmup, prepare-host) are NOT re-measured here:
rebuilding the real store to time it would cost 143 s and a provider bill for numbers Task 6/7
already measured on that exact snapshot. They are quoted in the report.

USAGE (Windows, the real store)
    C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe \
        testplatform/backend/tests_scripts/bench_market_conditions.py \
        --manifest 1136d489...5cb3 --bt 1688 --out bench.json

USAGE (fabricated store, no caches needed)
    python bench_market_conditions.py --quick --out quick.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_BACKEND))
# THE WORKTREE'S packages, ahead of the installed ones. ba2_common is pip-installed from the
# MAIN checkout, so without this a bench run from a worktree silently measures the code on
# dev rather than the code being benchmarked -- and reports it as the branch's number.
#
# THE BACKEND IS THEN MOVED BACK TO THE FRONT. ``packages/common`` holds its own regular ``tests``
# package; ahead of the backend it shadows ``tests`` for every process SPAWNED after this import
# (a spawn child rebuilds sys.path from the parent's), so a later pytest module whose spawn pool
# pickles ``tests.test_...`` callables dies in the child with ModuleNotFoundError -- seen as
# test_local_pool_stall_recovery failing only in a worktree, where these paths are not already
# on sys.path via the editable installs and so really are inserted.
for _p in (_BACKEND, os.path.join(_REPO, "testplatform"),
           os.path.join(_REPO, "packages", "common"),
           os.path.join(_REPO, "packages", "providers"),
           os.path.join(_REPO, "packages", "experts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

#: The gates the trial phase turns on. Thresholds chosen to PASS most of the time so the gated
#: arm still trades: a gated arm that entered nothing would compare a working backtest against
#: an empty one, and the ratio would measure the absence of trades, not the cost of the gates.
TRIAL_GATES = (("adx", "below", 95.0), ("slope", "above", -5.0), ("rv", "below", 9.0))


def _now() -> float:
    return time.perf_counter()


def _pct(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


def _summary(samples: Sequence[float]) -> Dict[str, float]:
    return {"n": len(samples),
            "p50_us": _pct(samples, 0.50) * 1e6,
            "p95_us": _pct(samples, 0.95) * 1e6,
            "mean_us": (statistics.fmean(samples) * 1e6) if samples else float("nan")}


# --------------------------------------------------------------------------- fixture store
def fabricate_store(root: str, symbols: Sequence[str], sessions: Sequence[date]) -> str:
    """A published snapshot with valid rows -- the ``--quick`` mode's stand-in for the real one.

    Values vary per (symbol, session) so a reader that returned one cached row for everything
    would not pass the checks below unnoticed.
    """
    from ba2_common.core.market_condition_store import MarketConditionStore, month_of
    from ba2_common.core.market_conditions import PROFILES, STATUS_VALID

    profile = PROFILES["ohlcv-v1"]
    n = len(profile.fields)
    store = MarketConditionStore(root)
    objects = []
    for i, symbol in enumerate(symbols):
        by_month: Dict[Any, List[Dict[str, Any]]] = {}
        for j, session in enumerate(sessions):
            by_month.setdefault(month_of(session), []).append({
                "session": session,
                "values": [0.01 * ((i + j) % 17), 10.0 + ((i + j) % 23), 0.5 + 0.01 * ((i + j) % 40)],
                "status": [STATUS_VALID] * n, "reasons": [""] * n,
                "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
                "raw_row_lo": 0, "raw_row_hi": 0})
        objects += [store.write_feature_object(profile, symbol, rows)[0]
                    for _, rows in sorted(by_month.items())]
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1",
        timing_policy="prior_session_v1", objects=objects, raw_objects=[],
        coverage={s: {"rows": len(sessions)} for s in symbols}, universe=list(symbols),
        sessions=list(sessions), window_start=sessions[0], window_end=sessions[-1])
    return store.write_manifest(manifest)


# --------------------------------------------------------------------------- phases
def phase_coverage(reader: Any, universe: Sequence[str], wanted: int) -> Dict[str, Any]:
    covered = set(reader.symbols())
    chosen = [s for s in universe if s in covered][:wanted]
    missing = [s for s in universe if s not in covered]
    return {"universe": len(universe), "covered": len(covered), "chosen": chosen,
            "uncovered": missing}


def _reader_over(reader: Any, *, retain_windows: bool) -> Any:
    from ba2_common.core.market_condition_readers import WindowMarketConditionReader

    return WindowMarketConditionReader(reader.profile, mapped=reader,
                                       retain_windows=retain_windows)


def _observe_samples(warm: Any, symbols: Sequence[str], sessions: Sequence[date],
                     repeats: int) -> Dict[str, Any]:
    misses: List[float] = []
    hits: List[float] = []
    rows = nulls = 0
    for symbol in symbols:
        for session in sessions:
            t0 = _now()
            row = warm.observe(symbol, session)
            misses.append(_now() - t0)
            rows += row is not None
            nulls += row is None
    for _ in range(repeats):
        for symbol in symbols:
            for session in sessions:
                t0 = _now()
                warm.observe(symbol, session)
                hits.append(_now() - t0)
    return {"miss": _summary(misses), "hit": _summary(hits),
            "rows_served": rows, "rows_absent": nulls,
            # THE CONTRACT ITSELF: with a manifest pinned, a trial computes nothing.
            "computed": warm.computed, "mapped_rows": warm.mapped_rows}


def phase_observe(reader: Any, symbols: Sequence[str], sessions: Sequence[date],
                  repeats: int) -> Dict[str, Any]:
    """Per-``observe`` latency on a MISS (the first read of a key) and on a HIT.

    TWO READERS, because they are two different costs and only one of them is on the decision
    path. ``BacktestMarketConditionReader`` passes ``retain_windows=False``; a reader that
    RETAINS windows (the live capture path) additionally re-reads and re-hashes the row's raw
    shard on every miss, which is hundreds of times dearer. Measuring only the retaining one
    would report a trial cost the trial does not pay; measuring only the other would leave the
    capture cost -- which live pays once per symbol per day -- unrecorded.
    """
    return {"trial_path": _observe_samples(_reader_over(reader, retain_windows=False),
                                           symbols, sessions, repeats),
            "capture_path": _observe_samples(_reader_over(reader, retain_windows=True),
                                             symbols, sessions, repeats)}


def phase_gate(reader: Any, symbols: Sequence[str], sessions: Sequence[date]) -> Dict[str, Any]:
    """The full per-evaluation cost through the real condition: resolver -> memo -> compare."""
    import ba2_common.core.TradeConditions as TC
    from ba2_common.core.market_condition_context import MarketConditionContext
    from ba2_common.core.types import ExpertEventType

    warm = _reader_over(reader, retain_windows=False)   # the BACKTEST reader's configuration
    session = sessions[len(sessions) // 2]
    label = date.fromordinal(session.toordinal() + 1)
    ctx = MarketConditionContext(
        decision_time=datetime(label.year, label.month, label.day, 21, tzinfo=timezone.utc),
        session_label=label, prior_session=session,
        source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        calc_version=warm.calc_version, reader=warm)
    saved = TC.get_market_condition_context_resolver()
    TC.set_market_condition_context_resolver(lambda account, symbol, rec: ctx)
    try:
        leaves = [TC.create_condition(ExpertEventType.N_UNDERLYING_ADX, object(), symbol, None,
                                      operator_str="<", value=1e9) for symbol in symbols]
        for leaf in leaves:                     # warm the memo; we are timing the WARM path
            leaf.evaluate()
        cold_calls = TC.market_condition_resolver_calls()
        samples: List[float] = []
        for _ in range(20):
            for leaf in leaves:
                t0 = _now()
                leaf.evaluate()
                samples.append(_now() - t0)
        return {"evaluate": _summary(samples),
                "resolver_calls": TC.market_condition_resolver_calls() - cold_calls,
                "computed": warm.computed}
    finally:
        TC.set_market_condition_context_resolver(saved)


def _decoded_gate(launcher: Any, member: str, short: str, mode: str, threshold: float) -> Dict[str, Any]:
    """The launcher's OWN leaf for ``short``, decoded to one concrete mode -- the same two
    functions the grid uses, so the bench cannot measure a leaf shape nothing emits."""
    from app.services.strategy_param_space import _apply_mode

    saved = getattr(launcher, "_MARKET_CONDITION_PROFILES", ())
    launcher._MARKET_CONDITION_PROFILES = ("ohlcv-v1",)
    try:
        leaves = launcher._market_condition_gates(member)
    finally:
        launcher._MARKET_CONDITION_PROFILES = saved
    leaf = next(dict(x) for x in leaves if x["id"].endswith(f"-market-{short}"))
    leaf["value"] = float(threshold)
    _apply_mode(leaf, leaf["id"], mode)
    return leaf


def _append_gates(rules: Any, launcher: Any, member: str) -> int:
    """Append the three decoded gates to the FIRST entry rule's AND tree. Returns the count."""
    if not rules:
        raise SystemExit("the genome decoded to no entry rules; nothing to gate")
    tree = rules[0].get("conditions")
    if not isinstance(tree, dict) or not isinstance(tree.get("conditions"), list):
        raise SystemExit(f"entry rule {rules[0].get('id')!r} has no AND/OR group to append to")
    for short, mode, threshold in TRIAL_GATES:
        tree["conditions"].append(_decoded_gate(launcher, member, short, mode, threshold))
    return len(TRIAL_GATES)


def _configure_test_db() -> None:
    """Point ba2_common's engine at the TEST database, as ``ba2test_launcher._enter_backend``
    does for every ba2-test command.

    Without it a standalone script reads ba2_common's neutral default DB (``BA2_HOME/db.sqlite``
    -- the LIVE trade database), where the app settings a backtest needs do not exist, and the
    run dies minutes later on "FMP API key not configured".
    """
    from app.models.database import DATABASE_URL

    if DATABASE_URL.startswith("sqlite:///"):
        from ba2_common.core import db as ba2_db

        ba2_db.configure_db(DATABASE_URL.replace("sqlite:///", "", 1))
    if not os.environ.get("FMP_API_KEY"):
        from ba2_common.config import get_app_setting

        key = get_app_setting("FMP_API_KEY")
        if key:
            os.environ["FMP_API_KEY"] = key


def phase_trial(args: Any, symbols: Sequence[str], covered: Sequence[str] = ()) -> Dict[str, Any]:
    """N full backtests of one genome, profile OFF then ON with the three gates active."""
    import importlib.util

    _covered = {str(s).upper() for s in covered}
    _configure_test_db()

    from app.models.database import SessionLocal
    from app.models.strategy import Strategy
    from app.services.strategy_optimization_handler import (
        _build_daily_trial_config, _build_hoisted_state, _persist_trial_worker,
    )
    from app.services.strategy_param_space import decode_params

    sys.path.insert(0, os.path.join(_REPO, "tools"))
    from backtest_parity import resolve_backtest_source, resolve_source  # noqa: E402

    spec = importlib.util.spec_from_file_location(
        "bench_launcher", os.path.join(_REPO, "testplatform", "ba2test_launcher.py"))
    launcher = importlib.util.module_from_spec(spec)
    sys.modules["bench_launcher"] = launcher
    try:
        spec.loader.exec_module(launcher)
    except SystemExit:
        pass

    opt_id, rank = resolve_backtest_source(int(args.bt))
    db = SessionLocal()
    try:
        src = resolve_source(opt_id, rank, db=db)
        strat = db.query(Strategy).filter_by(id=src["strategy_id"]).first()
        if strat is None:
            raise SystemExit(f"opt {opt_id}: strategy {src['strategy_id']} is gone")
        base = dict(src["bt_block"])
        # THE COVERED SUB-UNIVERSE AND A SHORT WINDOW. Both arms get exactly the same override,
        # so the ratio is unaffected by it; the absolute seconds are not this row's own.
        #
        # ``--trial-universe source`` (the default) keeps the GENOME'S OWN universe, minus the
        # symbols the snapshot does not cover -- a gated run over them would be refused, and a
        # strategy benchmarked on twenty symbols it was never tuned for enters differently
        # enough that the two arms' trade counts stop being comparable to the real thing.
        if args.trial_universe == "source":
            covered = set(symbols) | set(_covered)
            universe = [s for s in (base.get("enabled_instruments") or []) if s.upper() in covered]
            dropped = [s for s in (base.get("enabled_instruments") or []) if s.upper() not in covered]
            if dropped:
                print(f"[trial] dropped {len(dropped)} uncovered symbol(s) from the source "
                      f"universe: {', '.join(dropped)}", flush=True)
            if not universe:
                raise SystemExit("the snapshot covers none of the source run's universe")
        else:
            universe = list(symbols)
        base["enabled_instruments"] = universe
        base["start_date"], base["end_date"] = args.start, args.end
        decoded_off = decode_params(strat, src["genome"])
        decoded_on = decode_params(strat, src["genome"])
        hoisted = _build_hoisted_state(base) if base.get("screener_opt") else None
        gates = _append_gates(decoded_on.get("entry_rules"), launcher, args.member)
    finally:
        db.close()

    def _cfg(decoded, profile):
        cfg = _build_daily_trial_config(base, decoded, hoisted,
                                        option_trade_records=False)  # a benchmark
        cfg["market_condition_profile"] = profile
        if profile != "none":
            cfg["market_condition_manifest"] = args.manifest
        else:
            cfg.pop("market_condition_manifest", None)
        return cfg

    out: Dict[str, Any] = {"evaluations": int(args.evaluations), "gates": gates,
                           "universe": universe, "symbols": len(universe),
                           "start": args.start, "end": args.end,
                           "source_backtest": int(args.bt), "optimization": opt_id}
    for arm, decoded, profile in (("off", decoded_off, "none"), ("on", decoded_on, "ohlcv-v1")):
        seconds, trades = [], []
        for i in range(int(args.evaluations)):
            cfg = _cfg(decoded, profile)
            cfg["name"] = f"bench-mc-{arm}-{i}"
            t0 = _now()
            result = _persist_trial_worker(cfg)
            seconds.append(_now() - t0)
            if not result or not result.get("ok"):
                raise SystemExit(f"[{arm}] trial {i} failed: {(result or {}).get('error')}")
            trades.append(result["results"].get("total_trades"))
            print(f"[{arm}] trial {i + 1}/{args.evaluations} {seconds[-1]:.2f}s "
                  f"trades={trades[-1]}", flush=True)
        out[arm] = {"median_s": statistics.median(seconds), "min_s": min(seconds),
                    "max_s": max(seconds), "seconds": seconds, "trades": trades}
    median_off, median_on = out["off"]["median_s"], out["on"]["median_s"]
    out["overhead_pct"] = (median_on - median_off) / median_off * 100.0 if median_off else None
    out["passes_one_percent_bar"] = (out["overhead_pct"] is not None
                                     and out["overhead_pct"] < 1.0)
    # ANTI-VACUITY: an arm that traded nothing is not a trial, and its seconds measure the
    # absence of work rather than the cost of a gate.
    out["vacuous"] = not (any(t for t in out["off"]["trades"])
                          and any(t for t in out["on"]["trades"]))
    return out


def arithmetic_bound(observe: Any, gate: Any, trial: Any, sessions: int,
                     gates: Optional[int] = None) -> Dict[str, Any]:
    """The gate cost as a fraction of a trial, computed from the per-operation measurements.

    WHY THIS AND NOT ONLY THE A/B. The A/B difference is a small number sitting inside the
    rig's own run-to-run spread, so on a good day it comes out NEGATIVE and on a bad day it
    comes out at a percent for reasons that have nothing to do with the gates. This bound does
    not depend on that: it multiplies the measured per-operation costs by the number of
    operations a trial can perform. It is an UPPER bound -- the entry rule short-circuits
    before most leaves ever run -- so a trial's real cost is at most this.

    One memo MISS per (symbol, session) -- the first market leaf on a bar pays it -- plus one
    ``evaluate()`` per leaf per bar, which already includes its memo hit.
    """
    symbols = int(trial.get("symbols") or 0)
    leaves = int(gates if gates is not None else (trial.get("gates") or 0))
    keys = symbols * int(sessions)
    evaluations = keys * leaves
    miss_us = float(observe["trial_path"]["miss"]["p50_us"])
    eval_us = float(gate["evaluate"]["p50_us"])
    seconds = (keys * miss_us + evaluations * eval_us) / 1e6
    baseline = float(trial["off"]["median_s"] or 0.0)
    return {
        "symbols": symbols, "sessions": int(sessions), "leaves_per_bar": leaves,
        "memo_misses": keys, "evaluations": evaluations,
        "observe_miss_p50_us": miss_us, "evaluate_p50_us": eval_us,
        "seconds": seconds, "trial_median_s": baseline,
        "pct": (seconds / baseline * 100.0) if baseline else None,
    }


# --------------------------------------------------------------------------- workers
def _worker(cache_root: str, manifest: str, profile: str, symbol: str, session_ord: int,
            ready, hold) -> None:
    """Open the mapping, read one row, then hold the process open to be measured."""
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader

    reader = MappedMarketConditionReader(cache_root, manifest, profile)
    reader.observe(symbol, date.fromordinal(session_ord))
    ready.put(os.getpid())
    hold.get()


def _reap(procs: Any, hold: Any) -> None:
    """Release, join, then TERMINATE and KILL whatever is still alive. Never raises.

    THE LEAK THIS CLOSES. The children block on ``hold.get()`` so they can be measured; if
    anything between start and release raises -- one child dying on import, a timeout waiting
    for the last ``ready``, psutil refusing a handle -- the release never happens and thirty
    spawned Python processes stay resident for the life of the parent. A benchmark that leaks
    the thing it is measuring is worse than one that fails: the next measurement on that box is
    wrong and nothing says so.

    ``procs`` must be the processes that actually STARTED, not every one constructed. The
    failure that brings us here can be ``start()`` itself -- a fork/spawn refused at the
    thirtieth child, which is exactly the size this phase exists to probe -- and
    ``Process.join`` on a never-started process raises ``AssertionError: can only join a
    started process`` from inside the ``finally``, replacing the real error with one about
    cleanup. The caller appends to its ``started`` list as each ``start()`` returns.
    """
    for _ in procs:
        try:
            hold.put_nowait(1)
        except Exception:  # noqa: BLE001 -- a full/closed queue must not stop the kill below
            pass
    for proc in procs:
        proc.join(timeout=30)
    for proc in procs:
        if proc.is_alive():
            proc.terminate()
    for proc in procs:
        proc.join(timeout=10)
    for proc in procs:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=10)


def phase_workers(cache_root: str, manifest: str, profile: str, symbol: str, session: date,
                  workers: int, ready_timeout: float = 300.0) -> Dict[str, Any]:
    """Peak RSS and open descriptors/handles with ``workers`` children holding the mapping.

    Every exit path goes through :func:`_reap`. A child that never reports raises here (a
    benchmark that cannot measure must refuse, not report the children that did answer as if
    they were all of them) -- and the survivors are still cleaned up on the way out.
    """
    import multiprocessing as mp

    import psutil

    ctx = mp.get_context("spawn")
    ready, hold = ctx.Queue(), ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(cache_root, manifest, profile, symbol,
                                               session.toordinal(), ready, hold))
             for _ in range(workers)]
    started_at = _now()
    started: list = []
    try:
        for proc in procs:
            proc.start()
            started.append(proc)          # AFTER start(), so a refusal leaves it out of the reap
        pids = []
        for i in range(len(procs)):
            try:
                pids.append(ready.get(timeout=ready_timeout))
            except Exception as e:  # noqa: BLE001 -- queue.Empty and anything else alike
                alive = sum(1 for proc in started if proc.is_alive())
                raise RuntimeError(
                    f"only {i} of {len(started)} children opened the mapping within "
                    f"{ready_timeout}s ({alive} still alive): {e!r}. Their stderr is above; a "
                    f"child that dies before reporting is usually an import the spawned "
                    f"process cannot resolve.") from e
        opened = _now() - started_at
        rss, fds = [], []
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                rss.append(proc.memory_info().rss / (1024 * 1024))
                counter = getattr(proc, "num_fds", None) or getattr(proc, "num_handles", None)
                fds.append(counter() if counter else None)
            except psutil.Error as e:
                # RECORDED, never treated as zero: a process we could not measure is an unknown,
                # and an unknown averaged in as 0 MB is how a memory budget passes on paper.
                rss.append(None)
                fds.append(None)
                print(f"[workers] pid {pid} not measurable: {e!r}")
    finally:
        _reap(started, hold)
    measured = [v for v in rss if v is not None]
    handles = [v for v in fds if v is not None]
    return {"workers": workers, "opened_s": opened,
            "rss_mb_max": max(measured) if measured else None,
            "rss_mb_median": statistics.median(measured) if measured else None,
            "rss_mb_total": sum(measured) if measured else None,
            "handles_max": max(handles) if handles else None,
            "handles_median": statistics.median(handles) if handles else None,
            "unmeasurable": sum(1 for v in rss if v is None)}


# --------------------------------------------------------------------------- main
def run(args: Any) -> Dict[str, Any]:
    """Every requested phase, in a temporary store under ``--quick`` and the real one otherwise.

    ``--quick``'s fabricated store lives in a ``TemporaryDirectory``: a failed run used to leave
    a ``bench-mc-*`` tree behind on every attempt, and the one thing worse than a benchmark that
    fails is a benchmark that fails while filling the disk it is measuring.
    """
    import tempfile

    if args.quick:
        with tempfile.TemporaryDirectory(prefix="bench-mc-") as temp:
            universe = ["QAAA", "QBBB", "QCCC"]
            sessions = _sessions_for(date(2024, 6, 28), 40)
            return _run_phases(args, temp, fabricate_store(temp, universe, sessions),
                               universe, sessions)
    if not args.manifest:
        raise SystemExit("--manifest is required without --quick")
    cache_root = args.cache_root
    if cache_root is None:
        from ba2_common.config import CACHE_FOLDER
        cache_root = CACHE_FOLDER
    with open(args.universe_file, encoding="utf-8") as f:
        universe = [line.strip().upper() for line in f
                    if line.strip() and not line.startswith("#")]
    return _run_phases(args, cache_root, args.manifest, universe,
                       _sessions_for(date.fromisoformat(args.end), args.sessions))


def _sessions_for(anchor: date, n: int) -> List[date]:
    """``n`` regular sessions ending at ``anchor``, walking back when it is not one itself.

    ``--end`` is a WINDOW bound (the trial's) and is routinely a weekend or a holiday; the
    observe/gate phases need real sessions.
    """
    from ba2_common.core.market_calendar import prior_regular_session, regular_sessions_ending_at

    try:
        return regular_sessions_ending_at(anchor, n)
    except ValueError:
        return regular_sessions_ending_at(prior_regular_session(anchor), n)


def _run_phases(args: Any, cache_root: str, manifest: str, universe: Sequence[str],
                sessions: Sequence[date]) -> Dict[str, Any]:
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader

    reader = MappedMarketConditionReader(cache_root, manifest, args.profile)
    phases = set(args.phases.split(","))
    out: Dict[str, Any] = {
        "host": {"platform": sys.platform, "python": sys.version.split()[0],
                 "shared_arrays": os.environ.get("BA2_SHARED_ARRAYS", "<unset>")},
        "manifest": reader.manifest_digest, "profile": args.profile,
        "cache_root": str(cache_root), "quick": bool(args.quick),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    coverage = phase_coverage(reader, universe, args.symbols)
    out["coverage"] = coverage
    symbols = coverage["chosen"]
    if not symbols:
        raise SystemExit("the snapshot covers none of the universe file's symbols")
    print(f"[coverage] {len(symbols)} of {coverage['universe']} symbols chosen; "
          f"{len(coverage['uncovered'])} uncovered", flush=True)

    if "observe" in phases:
        out["observe"] = phase_observe(reader, symbols, sessions, args.repeats)
        for path, block in out["observe"].items():
            print(f"[observe/{path}] miss P50 {block['miss']['p50_us']:.1f}us "
                  f"P95 {block['miss']['p95_us']:.1f}us; "
                  f"hit P50 {block['hit']['p50_us']:.2f}us; "
                  f"computed {block['computed']} mapped_rows {block['mapped_rows']}", flush=True)
    if "gate" in phases:
        out["gate"] = phase_gate(reader, symbols, sessions)
        print(f"[gate] evaluate P50 {out['gate']['evaluate']['p50_us']:.2f}us "
              f"P95 {out['gate']['evaluate']['p95_us']:.2f}us", flush=True)
    if "workers" in phases:
        out["workers"] = phase_workers(cache_root, reader.manifest_digest, args.profile,
                                       symbols[0], sessions[-1], args.workers)
        print(f"[workers] {args.workers} children: max RSS "
              f"{out['workers']['rss_mb_max']:.1f} MB, max handles "
              f"{out['workers']['handles_max']}", flush=True)
    if "trial" in phases:
        if not args.bt:
            print("[trial] skipped: --bt <persisted backtest id> names the genome to time")
        else:
            out["trial"] = phase_trial(args, symbols, reader.symbols())
            print(f"[trial] off {out['trial']['off']['median_s']:.2f}s, "
                  f"on {out['trial']['on']['median_s']:.2f}s, "
                  f"overhead {out['trial']['overhead_pct']:.3f}%", flush=True)
    if "observe" in out and "gate" in out and "trial" in out:
        # THE NUMBER THE REPORT QUOTES. The A/B overhead sits inside this rig's own run-to-run
        # spread; this one is arithmetic over the measured per-operation costs and does not.
        window = _trial_sessions(args)
        out["arithmetic_bound"] = arithmetic_bound(out["observe"], out["gate"], out["trial"],
                                                   window)
        print(f"[bound] {out['arithmetic_bound']['memo_misses']} memo misses + "
              f"{out['arithmetic_bound']['evaluations']} evaluations = "
              f"{out['arithmetic_bound']['seconds']:.3f}s = "
              f"{out['arithmetic_bound']['pct']:.3f}% of a trial", flush=True)
    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    return out


def _trial_sessions(args: Any) -> int:
    """Regular sessions in the trial window -- the number of bars a gate can be asked about."""
    from ba2_common.core.market_calendar import nyse_regular_sessions

    return len(nyse_regular_sessions(date.fromisoformat(args.start),
                                     date.fromisoformat(args.end)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="bench_market_conditions.py", description=__doc__.split("\n")[0])
    p.add_argument("--manifest", help="Published manifest digest (required without --quick).")
    p.add_argument("--profile", default="ohlcv-v1")
    p.add_argument("--cache-root", help="Default: ba2_common CACHE_FOLDER.")
    p.add_argument("--universe-file", default=os.path.join(_REPO, "tools",
                                                           "options_universe_top100.txt"))
    p.add_argument("--symbols", type=int, default=20, help="Covered symbols to bench (20).")
    p.add_argument("--sessions", type=int, default=20, help="Sessions per symbol for observe/gate.")
    p.add_argument("--repeats", type=int, default=5, help="Warm re-reads per key (5).")
    p.add_argument("--evaluations", type=int, default=30, help="Backtests per arm (30).")
    p.add_argument("--workers", type=int, default=30, help="Children opening the mapping (30).")
    p.add_argument("--bt", type=int, help="Persisted backtest id naming the trial-phase genome.")
    p.add_argument("--member", default="o_lc", help="Rule prefix the trial gates are built for.")
    p.add_argument("--trial-universe", choices=("source", "file"), default="source",
                   help="source (default): the genome's own universe minus uncovered symbols; "
                        "file: the covered head of --universe-file.")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="2023-12-31")
    p.add_argument("--phases", default="observe,gate,trial,workers")
    p.add_argument("--quick", action="store_true",
                   help="Fabricate a tiny store and shrink every phase (no caches needed).")
    p.add_argument("--out", default="bench_market_conditions.json")
    args = p.parse_args(argv)
    # A standalone script bypasses the GA's own logging suppression, and the conditions log a
    # DEBUG line per unknown observation -- at thousands of evaluations that is slower than the
    # thing being measured and is therefore the reason the measurement would be wrong. Done in
    # main(), not at import: the test that exercises --quick must not disable pytest's logging
    # for the rest of the session.
    logging.disable(logging.INFO)
    if args.quick:
        args.symbols = min(args.symbols, 3)
        args.sessions = min(args.sessions, 5)
        args.repeats = min(args.repeats, 2)
        args.workers = min(args.workers, 4)
        args.phases = ",".join(x for x in args.phases.split(",") if x != "trial") or "observe"
    result = run(args)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True, default=str)
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
