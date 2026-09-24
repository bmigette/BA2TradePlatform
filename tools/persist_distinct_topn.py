#!/usr/bin/env python
"""Persist the best N BEHAVIOUR-DISTINCT genomes of one optimization as saved Backtests.

WHY. A converged GA fills its top ranks with genomes that differ only in inert genes: stage-1
option job 1 at gen 34 had 11 distinct parameter sets in its top 20, all producing the identical
backtest (fitness 14.129, +1588%, -34.3% maxDD, 351 trades). The end-of-job TOP-N persist dedupes
on the fitness value, so it would save near-identical strategies. This tool selects the best N
that actually BEHAVE differently (``app.services.distinct_topn.select_behaviour_distinct``:
fingerprint = trades, total return, max drawdown; near-duplicate tolerances below) and persists
them through the SAME re-run + persist path as the end-of-job TOP-N
(``ba2test_launcher._persist_top_backtests`` with an explicit candidate list) -- nothing about
the re-run or the Backtest row is reimplemented here.

Persisted rows are named ``DTOP<rank>-<optimization name>`` (never colliding with the end-of-job
``TOP<rank>-`` rows or with tools/recover_missing_topn.py), carry the optimization's labels plus
``TopNDistinct`` (plus any ``--labels``), are ``is_saved`` and have ``ga_fitness`` set.

USAGE (from a checkout of THIS code, with DATABASE_URL/BA2_HOME pointing at the target DB):

    python tools/persist_distinct_topn.py --opt-id 12 --dry-run          # select + print only
    python tools/persist_distinct_topn.py --name O_LC-FMPRating-st1 --n 10 --skip-already-persisted
    python tools/persist_distinct_topn.py --opt-id 12 --dry-run --allow-running   # live job

  --n N                       picks to select (default 10, >= 1); fewer when fewer behaviours
                              exist
  --min-trade-rows K          exclude genomes with fewer than K round-trip ROWS (default 30; one
                              row per LEG on options -- the engine's total_trades; 0 allows
                              them). A sanity floor only: the option fitnesses' own gate counts
                              completed STRUCTURES and is stricter. Alias: --min-trades
  --min-return-rel-pct X      near-duplicate tolerance on total return, RELATIVE % (default 5)
  --min-dd-pts Y              near-duplicate tolerance on max drawdown, absolute pts (default 2)
  --min-trades-rel-pct Z      near-duplicate tolerance on round-trip rows, RELATIVE % (default 5)
                              A candidate is kept only if it differs from EVERY selected pick on
                              at least one axis by at least its tolerance. 0 ignores an axis;
                              all three 0 = only exact fingerprints collapse. The return axis is
                              relative, so near-zero returns always look distinct on it (+0.5% vs
                              +1.0% is 50% apart) -- see app/services/distinct_topn.py.
  --dry-run                   select and print the table; no re-runs, no DB writes
  --labels L [L ...]          extra labels on the persisted rows (TopNDistinct is always added)
  --skip-already-persisted    do not re-run a pick that already has a completed Backtest for this
                              optimization (any name: TOP*, DTOP*, BEST-*), matched on its genome
                              (``key`` match) or on its behaviour fingerprint (``fingerprint``
                              match -- rows flagged ga_fitness_divergence are NOT used for it:
                              their numbers are not the GA's). The table shows which match hit;
                              a skipped pick is reported from the existing row instead
  --parallel P                re-runs at a time (default 1). NEVER > 1 beside a live grid (an
                              option re-run is ~1-3 GB private on top of the shared arrays). Each
                              batch of P is bounded by BT_LOCAL_STALL_TIMEOUT_S (5400 s)
  --allow-running             permit an optimization whose status is not ``completed`` (meant for
                              --dry-run on a live job)
  --min-free-gb G             refuse to re-run when MemAvailable (/proc/meminfo) is below G GB
                              (default 20); skipped with a warning where /proc/meminfo is absent
  --force-memory              re-run despite MemAvailable < --min-free-gb

MEMORY. The re-runs run in THIS process tree, OUTSIDE the grid's cgroup and memory governor, so
nothing stops them from starving a live grid. Beside a running grid, check ``free -g`` and run
under a scope with its own cap, e.g.

    systemd-run --user --scope -p MemoryMax=16G \\
        python tools/persist_distinct_topn.py --opt-id 12 --skip-already-persisted

and keep ``--parallel 1``.

TWO CARs, labelled apart. The selection table's ``CAR(GA)`` compounds the GA record's total
return over the optimization's configured window. The summary's ``CAR(bt)`` is the persisted
re-run's own ``annualized_return`` (the engine's figure; compounded from the row's total return
over its start/end only when the engine left it empty).

Re-runs are LOCAL only (the optimization's remote workers are not used). After persisting, a
summary prints each pick's fitness, total return, CAR(bt), max DD, round-trip rows,
per-calendar-year returns (from the persisted equity curve,
``strategy_fitness._calendar_year_returns``) and concentration (top-1 / top-5 bets' share of net
P&L from the persisted trades, one bet = one option structure,
``strategy_fitness._convex_telemetry``). Exit codes: 0 ok, 1 some pick not persisted, 2 refused.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LABEL = "TopNDistinct"
NAME_PREFIX = "DTOP"


class Refused(Exception):
    """A precondition failed: ``main`` prints the message to stderr and exits 2."""


def _bootstrap():
    """Import the launcher from THIS checkout and enter its backend (sys.path + DB wiring)."""
    tp = os.path.join(_REPO, "testplatform")
    if tp not in sys.path:
        sys.path.insert(0, tp)
    import ba2test_launcher as L
    L._enter_backend()
    return L


def _positive_int(v: str) -> int:
    i = int(v)
    if i < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {i}")
    return i


def _mem_available_gb() -> Optional[float]:
    """Linux MemAvailable in GB, or None where /proc/meminfo does not exist (Windows/macOS)."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024.0 * 1024.0)   # kB -> GB
    except OSError:
        return None
    return None


def _parse(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--opt-id", type=int)
    who.add_argument("--name", help="StrategyOptimization.name (must be unique)")
    ap.add_argument("--n", type=_positive_int, default=10)
    ap.add_argument("--min-trade-rows", "--min-trades", dest="min_trade_rows", type=int,
                    default=30)
    ap.add_argument("--min-return-rel-pct", type=float, default=5.0)
    ap.add_argument("--min-dd-pts", type=float, default=2.0)
    ap.add_argument("--min-trades-rel-pct", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--labels", nargs="*", default=[])
    ap.add_argument("--skip-already-persisted", action="store_true")
    ap.add_argument("--parallel", type=_positive_int, default=1)
    ap.add_argument("--allow-running", action="store_true")
    ap.add_argument("--min-free-gb", type=float, default=20.0)
    ap.add_argument("--force-memory", action="store_true")
    return ap.parse_args(argv)


def _load_opt(db, opt_id: Optional[int], name: Optional[str]):
    from app.models.strategy_optimization import StrategyOptimization as SO
    if opt_id is not None:
        row = db.query(SO).filter(SO.id == opt_id).first()
        if row is None:
            raise Refused(f"no optimization id={opt_id}")
        return row
    ids = db.query(SO.id, SO.status).filter(SO.name == name).all()
    if not ids:
        raise Refused(f"no optimization named {name!r}")
    if len(ids) > 1:
        raise Refused(f"{len(ids)} optimizations named {name!r} "
                      f"({', '.join(f'id={i} {s}' for i, s in ids)}); pass --opt-id")
    return db.query(SO).filter(SO.id == ids[0][0]).first()


def _window_years(bt_block: Dict[str, Any]) -> float:
    """The optimization's run window in years. A missing, unparseable or empty window is
    REFUSED: every CAR(GA) in the table depends on it."""
    try:
        start = _dt.fromisoformat(str(bt_block["start_date"])[:10])
        end = _dt.fromisoformat(str(bt_block["end_date"])[:10])
    except KeyError as e:
        raise Refused(f"optimization_config.backtest has no {e.args[0]}") from e
    except ValueError as e:
        raise Refused(f"optimization_config.backtest has an unparseable date: {e}") from e
    days = (end - start).days
    if days <= 0:
        raise Refused(f"optimization window {start.date()}..{end.date()} is empty")
    return days / 365.25


def _expert_of(bt_block: Dict[str, Any]) -> str:
    for spec in bt_block.get("experts") or []:
        if isinstance(spec, dict) and spec.get("class"):
            return spec["class"]
    raise Refused("optimization_config.backtest names no expert")


def _existing_rows(db, opt_id: int) -> List[Tuple[int, str, str, Optional[tuple]]]:
    """(id, name, params_key, fingerprint) of every completed Backtest of this optimization.
    Never the curve/trade blobs (see the backtests blob-layout note). ``fingerprint`` is None
    for a row flagged ``ga_fitness_divergence``: its metrics come from a re-run that did NOT
    reproduce the GA's score, so they must not stand in for a GA record's behaviour."""
    from app.models.backtest import Backtest
    from app.services.distinct_topn import behaviour_fingerprint, params_key
    rows = (db.query(Backtest.id, Backtest.name, Backtest.strategy_params, Backtest.total_trades,
                     Backtest.total_return, Backtest.max_drawdown, Backtest.results)
              .filter(Backtest.optimization_id == opt_id, Backtest.status == "completed")
              .order_by(Backtest.id).all())
    out = []
    for bid, name, sp, trades, ret, dd, res in rows:
        diverged = isinstance(res, dict) and res.get("ga_fitness_divergence") is not None
        fp = (behaviour_fingerprint(trades, ret, dd)
              if None not in (trades, ret, dd) and not diverged else None)
        out.append((bid, name, params_key(sp), fp))
    return out


def _match_existing(pick, existing) -> Optional[Tuple[int, str, str]]:
    """(backtest id, name, "key" | "fingerprint") of the first existing row covering ``pick``.
    A genome (key) match wins over a behaviour (fingerprint) match."""
    from app.services.distinct_topn import params_key
    pk = params_key(pick.params)
    for bid, name, key, _fp in existing:
        if key == pk:
            return bid, name, "key"
    for bid, name, _key, fp in existing:
        if fp is not None and fp == pick.fingerprint:
            return bid, name, "fingerprint"
    return None


def _fmt(v, spec=".1f", suffix="%"):
    return "n/a" if v is None else f"{format(v, spec)}{suffix}"


def _print_selection(picks, existing_by_rank, stats, tol, min_trade_rows):
    print(f"eligible genomes: {stats['eligible']} | distinct behaviours: "
          f"{stats['distinct_behaviours']} | excluded: {stats['excluded_unmeasured']} "
          f"sentinel/unmeasured, {stats['excluded_low_trades']} below {min_trade_rows} "
          f"round-trip rows, {stats['excluded_no_metrics']} without metrics")
    print(f"near-duplicate tolerances: return {tol.return_rel_pct:g}% rel, DD "
          f"{tol.dd_pts:g} pts, trade rows {tol.trades_rel_pct:g}% rel")
    hdr = (f"{'#':>3} {'fitness':>9} {'return':>10} {'CAR(GA)':>8} {'maxDD':>8} {'trades':>7} "
           f"{'clones':>6} {'near':>5}  persisted")
    print(hdr)
    print("-" * len(hdr))
    for p in picks:
        ex = existing_by_rank.get(p.rank)
        print(f"{p.rank:>3} {p.fitness:>9.4f} {_fmt(p.total_return):>10} {_fmt(p.car, '.2f'):>8} "
              f"{_fmt(p.max_drawdown):>8} {p.trades:>7} {p.clones:>6} {p.near_duplicates:>5}  "
              f"{f'bt#{ex[0]} {ex[1]} ({ex[2]} match)' if ex else '-'}")
    print("clones = genomes with the IDENTICAL behaviour (inert-gene variants); near = distinct "
          "behaviours dropped as within tolerance of this pick; trades = round-trip rows (one "
          "per leg on options).")
    print("CAR(GA) = the GA record's total return compounded over the optimization window. "
          "persisted: key match = same genome; fingerprint match = same behaviour (rows "
          "flagged ga_fitness_divergence are never fingerprint-matched).")


def _summarise(db, rows: List[Tuple[int, int]]):
    """Per-pick economics from the PERSISTED rows (re-run numbers, not the GA's)."""
    from app.models.backtest import Backtest
    from app.services.distinct_topn import annualise, labelled_calendar_year_returns
    from app.services.strategy_fitness import _convex_telemetry
    table = []
    years_seen: List[str] = []
    for rank, bid in rows:
        bt = db.query(Backtest).filter(Backtest.id == bid).first()
        if bt is None:
            continue
        yrs = labelled_calendar_year_returns(bt.equity_curve or [])
        for y, _ in yrs:
            if y not in years_seen:
                years_seen.append(y)
        conc = _convex_telemetry(bt.trades or [], 0.0, 0)
        span = ((bt.end_date - bt.start_date).days / 365.25
                if bt.start_date and bt.end_date else None)
        car = bt.annualized_return if bt.annualized_return is not None else annualise(
            bt.total_return, span)
        diverged = (bt.results or {}).get("ga_fitness_divergence")
        table.append((rank, bt, car, dict(yrs), conc, diverged))
    ycols = "".join(f"{y:>8}" for y in years_seen)
    hdr = (f"{'#':>3} {'bt':>6} {'fitness':>9} {'return':>10} {'CAR(bt)':>8} {'maxDD':>8} "
           f"{'trades':>7}{ycols} {'top1':>7} {'top5':>7}")
    print("\nSUMMARY (persisted re-runs)")
    print(hdr)
    print("-" * len(hdr))
    for rank, bt, car, yrs, conc, diverged in table:
        ycells = "".join(f"{_fmt(yrs.get(y)):>8}" for y in years_seen)
        flag = "  !! re-run diverged from GA fitness" if diverged is not None else ""
        print(f"{rank:>3} {bt.id:>6} {_fmt(bt.ga_fitness, '.4f', ''):>9} "
              f"{_fmt(bt.total_return):>10} {_fmt(car, '.2f'):>8} {_fmt(bt.max_drawdown):>8} "
              f"{bt.total_trades if bt.total_trades is not None else 'n/a':>7}{ycells} "
              f"{_fmt(conc['top1_pct'], '.0f'):>7} {_fmt(conc['top5_pct'], '.0f'):>7}{flag}")
    print("CAR(bt) = the persisted re-run's annualized_return; years = calendar-year returns from "
          "its equity curve; top1/top5 = share of net P&L from the best 1/5 bets (one option "
          "structure = one bet; n/a when net P&L <= 0).")


def _check_memory(args) -> None:
    """The re-runs run OUTSIDE the grid's cgroup / memory governor (see MEMORY above)."""
    avail = _mem_available_gb()
    if avail is None:
        print("MemAvailable unknown (no /proc/meminfo) -- check free RAM yourself before a "
              "re-run beside a live grid.")
        return
    print(f"MemAvailable {avail:.1f} GB (floor --min-free-gb {args.min_free_gb:g})")
    if avail < args.min_free_gb:
        if not args.force_memory:
            raise Refused(f"MemAvailable {avail:.1f} GB < --min-free-gb {args.min_free_gb:g}: a "
                          f"re-run beside the grid could starve it. Free memory, lower the "
                          f"floor, or pass --force-memory.")
        print("  !! below the floor; continuing because --force-memory was given")


def main(argv=None) -> int:
    args = _parse(argv)
    try:
        return _main(args)
    except Refused as e:
        print(f"persist_distinct_topn: {e}", file=sys.stderr)
        return 2


def _main(args) -> int:
    L = _bootstrap()
    from app.models.database import SessionLocal
    from app.services.distinct_topn import Tolerances, select_behaviour_distinct

    tol = Tolerances(return_rel_pct=args.min_return_rel_pct, dd_pts=args.min_dd_pts,
                     trades_rel_pct=args.min_trades_rel_pct)
    db = SessionLocal()
    try:
        opt = _load_opt(db, args.opt_id, args.name)
        if opt.status != "completed" and not args.allow_running:
            raise Refused(f"optimization {opt.id} ({opt.name}) is {opt.status!r}, not "
                          f"'completed'. Pass --allow-running (meant for --dry-run on a live "
                          f"job) to proceed anyway.")
        if not isinstance((opt.optimization_config or {}).get("backtest"), dict):
            raise Refused(f"optimization {opt.id} has no optimization_config.backtest block")
        bt_block = dict(opt.optimization_config["backtest"])
        expert = _expert_of(bt_block)
        years = _window_years(bt_block)
        stats: Dict[str, int] = {}
        picks = select_behaviour_distinct(opt.all_results, args.n,
                                          min_trades=args.min_trade_rows,
                                          tolerances=tol, years=years, stats=stats)
        existing = _existing_rows(db, opt.id)
        existing_by_rank = {p.rank: m for p in picks
                            if (m := _match_existing(p, existing)) is not None}
        opt_id, opt_name, opt_status = opt.id, opt.name, opt.status
    finally:
        db.close()

    print(f"optimization {opt_id} {opt_name!r} [{opt_status}] expert={expert} "
          f"window={years:.2f}y")
    _print_selection(picks, existing_by_rank, stats, tol, args.min_trade_rows)
    if not picks:
        print("nothing to persist: no eligible genome.")
        return 0
    if args.dry_run:
        print("\n--dry-run: nothing re-run, nothing written.")
        return 0

    todo = [p for p in picks
            if not (args.skip_already_persisted and p.rank in existing_by_rank)]
    done: List[Tuple[int, int]] = [(r, m[0]) for r, m in existing_by_rank.items()
                                   if args.skip_already_persisted]
    if todo:
        _check_memory(args)
    labels = [DEFAULT_LABEL] + [x for x in args.labels if x != DEFAULT_LABEL]
    step = args.parallel
    for i in range(0, len(todo), step):
        chunk = todo[i:i + step]
        print(f"\nre-running + persisting ranks {[p.rank for p in chunk]} "
              f"({i + len(chunk)}/{len(todo)})...", flush=True)
        ids: List[Tuple[int, int]] = []
        L._persist_top_backtests(
            opt_id, expert, n=len(chunk), parallel=step,
            candidates=[(p.params, p.key, p.fitness) for p in chunk],
            ranks=[p.rank for p in chunk], name_prefix=NAME_PREFIX, extra_labels=labels,
            use_remote_workers=False, persisted_ids=ids)
        done.extend(ids)
    missing = sorted(set(p.rank for p in picks) - {r for r, _ in done})
    if missing:
        print(f"\n!! ranks {missing} were NOT persisted (re-run failed or timed out; see above).")

    db = SessionLocal()
    try:
        _summarise(db, sorted(done))
    finally:
        db.close()
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
