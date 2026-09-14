"""Re-run ONE known genome twice -- private arrays vs host-shared mapped arrays -- and compare
the two persisted rows BYTE FOR BYTE.

THE ACCEPTANCE CRITERION THIS TOOL IS (operator, 2026-09-14): "ensure byte comparison of known
backtest with new shared cache". Task 9a of
docs/plans/2026-09-14-shared-arrays-across-workers.md. Nothing ships to remote227 until the
reference runs listed there print PASS.

WHY TWO PROCESSES, NOT TWO CALLS. The reader caches (``_UNDERLYING_CACHE``, ``_WORKER_BAR_CACHE``,
the memo frames) are process globals, and ``BA2_SHARED_ARRAYS`` is read where arrays are opened.
A single process cannot hold both worlds: whichever mode ran first would serve the second from
its own cache and the comparison would be of one run against itself -- a PASS that means nothing.
So the parent spawns the child twice, private FIRST then shared, SEQUENTIALLY (they share the
machine; overlapping them would make a build transient collide with a run).

WHY A NEW ROW EACH TIME. The source row is evidence. It is never touched, never overwritten, and
neither is an earlier comparison's pair: if ``PARITY-private-<name>`` / ``PARITY-shared-<name>``
already exist the tool REFUSES and tells you to pass ``--label`` for a fresh pair.

WHAT COUNTS AS A DIFFERENCE. Everything except the run's identity and its clock (``_IDENTITY_KEYS``
below, one comment per key). No tolerance on numbers: the shared path maps the SAME bytes the
private path parsed, so "close enough" is a bug in the mapping, not a rounding question. A FAIL
is a blocker -- trace it, fix the cause, re-run; never widen the comparison to make it pass.

Usage
-----
    python tools/backtest_parity.py --opt 487 --rank best
    python tools/backtest_parity.py --opt 512 --rank 1 --label rerun2
    python tools/backtest_parity.py --opt 512 --rank 1 --dry-run

Exit codes: 0 = PASS, 1 = FAIL (the rows differ), 2 = the comparison could not be made (a child
failed, or the parity rows already exist).
"""
from __future__ import annotations

import argparse
import functools
import json
import math
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: repo root: this file is <repo>/tools/backtest_parity.py
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The child's last stdout line. The parent reads the id from it, so it is a PROTOCOL, not a log
#: line: keep it last, keep it exact.
BT_ID_PREFIX = "PARITY_BT_ID="

#: ``0`` restores the private path (today's behaviour); ``1`` is the shared mapped path.
_MODE_FLAG = {"private": "0", "shared": "1"}

#: The JSON blob columns. ``results`` also carries every scalar metric the engine computed, so
#: comparing it covers far more than the mapped columns below.
_BLOB_COLUMNS = ("results", "trades", "equity_curve", "drawdown_curve")

#: Numeric columns that identify the ROW rather than describe the RESULT. Two parity rows are two
#: rows: they always differ here, and that says nothing about the arrays.
_EXCLUDED_NUMERIC = ("id", "optimization_id", "model_id", "strategy_id",
                     "prediction_dataset_id", "execution_dataset_id")

#: Keys stripped from the blobs at ANY depth before comparing. Each one is the run's identity or
#: its clock -- nothing here is a computed result, and NOTHING ELSE is forgiven.
#:   name          -- the parity runs are named PARITY-private-* / PARITY-shared-*, by design.
#:   id            -- the new row's primary key.
#:   backtest_id   -- a child row's back-pointer to that primary key.
#:   created_at    -- row insert time.
#:   started_at    -- when the run began.
#:   completed_at  -- when it finished.
#:   run_seconds   -- wall-clock duration; the shared path is expected to differ here (that is
#:                    the performance question, measured elsewhere, not the parity question).
#:   elapsed       -- same thing under another spelling.
#:   elapsed_s     -- and another.
#:   timestamp     -- a stamp written at run time. NOTE: the curves key their points on "date",
#:                    not "timestamp", so this cannot swallow a curve point (verified 2026-09-14
#:                    in results.py); if a future blob ever uses "timestamp" as a RESULT, remove
#:                    it from this tuple rather than losing the comparison.
_IDENTITY_KEYS = ("name", "id", "backtest_id", "created_at", "started_at", "completed_at",
                  "run_seconds", "elapsed", "elapsed_s", "timestamp")

_BOOTSTRAPPED = False


# =============================================================================================
# Comparison -- pure, importable, unit-tested (tests/test_backtest_parity_tool.py)
# =============================================================================================
@functools.lru_cache(maxsize=1)
def numeric_columns() -> Tuple[str, ...]:
    """Every Float/Integer column of ``Backtest`` that describes the RESULT.

    Read off ``Backtest.__table__`` rather than listed by hand so a migration that adds a metric
    column is compared from the day it lands -- a hand-written list would silently stop covering
    the newest number."""
    from sqlalchemy import Float, Integer

    from app.models.backtest import Backtest

    return tuple(c.name for c in Backtest.__table__.columns
                 if isinstance(c.type, (Float, Integer)) and c.name not in _EXCLUDED_NUMERIC)


def _row_view(bt: Any) -> Dict[str, Any]:
    """An ORM row -> the plain dict ``compare_rows`` consumes.

    The one place that knows about SQLAlchemy, so the comparison itself stays pure and the tests
    can build fakes with nothing but attributes."""
    view: Dict[str, Any] = {c: getattr(bt, c, None) for c in _BLOB_COLUMNS}
    for c in numeric_columns():
        view[c] = getattr(bt, c, None)
    return view


def _loaded(value: Any) -> Any:
    """A blob as Python. The columns are SQLAlchemy ``JSON``, which hands back dicts/lists -- but
    a text-stored row (or an older DB) hands back the string, so parse it when it is one."""
    if isinstance(value, (str, bytes)):
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(value)
    return value


def _strip_identity(obj: Any) -> Any:
    """``_IDENTITY_KEYS`` removed at any depth. Everything else survives to be compared."""
    if isinstance(obj, dict):
        return {k: _strip_identity(v) for k, v in obj.items() if k not in _IDENTITY_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_strip_identity(v) for v in obj]
    return obj


def _canonical(obj: Any) -> str:
    """One spelling per value: sorted keys, no whitespace. Two dicts that differ only in insert
    order are the same bytes here, and two floats that differ in the last bit are not."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _scalars_equal(x: Any, y: Any) -> bool:
    """``==`` with ONE exception: NaN equals NaN. A run with no trades leaves NaN metrics, and
    ``nan != nan`` would report every such run as a parity failure."""
    if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
        return True
    return bool(x == y)


def _diff_paths(a: Any, b: Any, path: str, out: List[str]) -> None:
    """Every differing LEAF, deepest-first-in-order, as ``path: a != b``."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            sub = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append(f"{sub}: <missing> != {b[k]!r}")
            elif k not in b:
                out.append(f"{sub}: {a[k]!r} != <missing>")
            else:
                _diff_paths(a[k], b[k], sub, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i in range(min(len(a), len(b))):
            _diff_paths(a[i], b[i], f"{path}[{i}]", out)
        return
    if not _scalars_equal(a, b):
        out.append(f"{path}: {a!r} != {b!r}")


def compare_rows(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    """The verdict: a list of human-readable differences, EMPTY meaning byte-identical.

    Blobs are compared as canonical strings (the actual byte comparison); when they differ the
    structures are walked so the report names the first differing path and how many leaves moved
    -- the operator needs "trades[17].exit_price" to go look, not "10 MB of JSON differ"."""
    diffs: List[str] = []
    for col in _BLOB_COLUMNS:
        av = _strip_identity(_loaded(a.get(col)))
        bv = _strip_identity(_loaded(b.get(col)))
        if _canonical(av) == _canonical(bv):
            continue
        leaves: List[str] = []
        _diff_paths(av, bv, col, leaves)
        if not leaves:
            # Canonical strings differ but no leaf does: a type/shape difference the walk cannot
            # localise (e.g. list vs dict at the root). Report it rather than swallowing it.
            diffs.append(f"{col}: canonical JSON differs (no differing leaf localised)")
        else:
            diffs.append(f"{col}: {len(leaves)} differing leaf/leaves; first: {leaves[0]}")
    for col in numeric_columns():
        if not _scalars_equal(a.get(col), b.get(col)):
            diffs.append(f"{col}: {a.get(col)!r} != {b.get(col)!r}")
    return diffs


def parse_child_bt_id(stdout: str) -> Optional[int]:
    """The child's persisted Backtest id, read from the LAST ``PARITY_BT_ID=`` line.

    Scanned from the end and tolerant of anything printed before or after it: a child prints
    preload chatter, and a logger that escaped ``logging.disable`` can land between the marker and
    the process exit. ``None`` means the child never got that far -- a failure, never a guess."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith(BT_ID_PREFIX):
            try:
                return int(line[len(BT_ID_PREFIX):].strip())
            except ValueError:
                return None
    return None


# =============================================================================================
# Source resolution (shared by parent and child, so both re-run the SAME genome)
# =============================================================================================
def _bootstrap() -> None:
    """Put the backend on the path and silence logging, once per process.

    ``logging.disable`` comes BEFORE the heavy imports and is skipped when the backend is already
    importable -- that case is pytest, where disabling the root logger would reach out of this
    tool and into the rest of the session. A standalone run is the one that needs it: a direct
    backtest call that keeps logging is 10x+ slower (memory
    ``standalone-backtest-scripts-need-logging-disable``)."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True
    import importlib.util

    try:
        already = importlib.util.find_spec("app.models.database") is not None
    except (ImportError, ValueError):
        already = False
    if already:
        return
    import logging

    logging.disable(logging.WARNING)
    sys.path.insert(0, os.path.join(REPO, "testplatform"))
    import ba2test_launcher as L  # noqa: E402

    L._enter_backend()


def _ranked_genome(opt: Any, rank: Any) -> Tuple[Dict[str, Any], Optional[float], str]:
    """(genome, its GA fitness, the rank's row-name suffix) for ``best`` or an integer rank.

    The ranking is the DISTINCT-FITNESS one from ``ba2test_launcher._persist_top_backtests``
    (mirrored in ``tools/recover_missing_topn.py:build_spec``): a converged GA yields many param
    sets differing only in INERT genes that score identically, so keying on params would rank
    behaviourally-identical individuals as different. ``best`` takes ``best_params`` instead,
    which is the only correct source on a CHECKPOINT-RESUMED run (its ``all_results`` restarted
    empty at the resume while ``best_params`` carries the winner from before it)."""
    if rank == "best":
        if not opt.best_params:
            raise SystemExit(f"opt {opt.id}: no best_params to re-run")
        return dict(opt.best_params), opt.best_fitness, "BEST"
    seen, ranked = set(), []
    for r in sorted(opt.all_results or [],
                    key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9),
                    reverse=True):
        fit = r.get("fitness")
        key = (round(fit, 6) if isinstance(fit, (int, float))
               else json.dumps(r.get("params"), sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        ranked.append((r["params"], fit))
        if len(ranked) >= rank:
            break
    if len(ranked) < rank:
        raise SystemExit(f"opt {opt.id}: only {len(ranked)} distinct-fitness individuals, "
                         f"no rank {rank}")
    params, fit = ranked[rank - 1]
    return dict(params), fit, f"TOP{rank}"


def resolve_source(opt_id: int, rank: Any, db: Any = None) -> Dict[str, Any]:
    """Everything both children need to run the SAME genome, from the optimization row.

    Called by the parent (to name the pair and print the summary) and again by each child (to
    build its config). Resolving it twice from the same immutable row is deliberate: the parent
    never has to hand a genome to a subprocess through a command line."""
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization

    own = db is None
    db = SessionLocal() if own else db
    try:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        if opt is None:
            raise SystemExit(f"no optimization with id {opt_id}")
        cfg = opt.optimization_config or {}
        if "backtest" not in cfg:
            raise SystemExit(f"opt {opt_id}: optimization_config has no 'backtest' block; "
                             f"there is nothing to re-run")
        bt_block = dict(cfg["backtest"])
        expert = next((s["class"] for s in (bt_block.get("experts") or [])
                       if isinstance(s, dict) and s.get("class")), None)
        if not expert:
            raise SystemExit(f"opt {opt_id}: no expert in optimization_config")
        genome, ga_fitness, prefix = _ranked_genome(opt, rank)
        return {
            "opt_id": opt.id,
            "rank": rank,
            # The name the source row carries (or would carry): the parity names are derived
            # from it so a pair is obviously about THAT row.
            "name": f"{prefix}-{opt.name or expert}",
            "expert": expert,
            "genome": genome,
            "ga_fitness": ga_fitness,
            "fitness_metric": opt.fitness_metric or "consistent_annual_return",
            "strategy_id": opt.strategy_id,
            "bt_block": bt_block,
            "start_date": str(bt_block["start_date"]),
            "end_date": str(bt_block["end_date"]),
            "initial_capital": float(bt_block["initial_capital"]),
        }
    finally:
        if own:
            db.close()


def parity_name(source_name: str, mode: str, label: Optional[str] = None) -> str:
    return f"PARITY-{mode}-{source_name}" + (f"-{label}" if label else "")


def existing_parity_names(names: Sequence[str]) -> List[str]:
    """Which of ``names`` already exist as Backtest rows. Non-empty => refuse (never overwrite)."""
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal

    db = SessionLocal()
    try:
        return [b.name for b in db.query(Backtest).filter(Backtest.name.in_(list(names))).all()]
    finally:
        db.close()


# =============================================================================================
# Child -- runs ONE mode in its own process and persists ONE row
# =============================================================================================
def run_child(mode: str, opt_id: int, rank: Any, name: str) -> int:
    """Re-run the genome in THIS process under whatever ``BA2_SHARED_ARRAYS`` the parent set, and
    persist the result as a new Backtest. Prints ``PARITY_BT_ID=<id>`` last.

    Same machinery as a TOP-N persist (``_build_daily_trial_config`` -> ``_persist_trial_worker``
    -> ``_persist_results``), so a parity row is produced exactly the way the rows it is
    validating were."""
    _bootstrap()

    import app.models  # noqa: F401  -- registers the mappers
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.models.strategy import Strategy
    from app.services.backtest.daily_backtest_handler import _persist_results
    from app.services.strategy_optimization_handler import (
        _build_daily_trial_config, _build_hoisted_state, _persist_trial_worker,
    )
    from app.services.strategy_param_space import decode_params

    db = SessionLocal()
    try:
        src = resolve_source(opt_id, rank, db=db)
        strat = db.query(Strategy).filter_by(id=src["strategy_id"]).first()
        if strat is None:
            raise SystemExit(f"opt {opt_id}: strategy {src['strategy_id']} is gone; "
                             f"the genome cannot be decoded")
        bt_block = src["bt_block"]
        # The SAME screener hoisted state the GA scored each individual with. Without it a
        # screener run silently becomes a static-universe run -- and the screener/metric-store
        # path is precisely what one of the reference runs exists to exercise.
        hoisted = _build_hoisted_state(bt_block) if bt_block.get("screener_opt") else None
        decoded = decode_params(strat, src["genome"])
        cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
        cfg["name"] = name
        cfg["persist_trading_db"] = True          # keyed by backtest_id, so the two never collide
        cfg["ga_fitness"] = src["ga_fitness"]

        print(f"[{mode}] BA2_SHARED_ARRAYS={os.environ.get('BA2_SHARED_ARRAYS', '<unset>')} "
              f"running {name}", flush=True)
        t0 = datetime.now()
        out = _persist_trial_worker(cfg)
        if not out or not out.get("ok"):
            print(f"[{mode}] re-run FAILED: {(out or {}).get('error', 'no result')}")
            return 2

        strategy_params = dict(src["genome"])
        fixed = {}
        for spec in (bt_block.get("experts") or []):
            if isinstance(spec, dict) and spec.get("class") == src["expert"]:
                fixed = dict(spec.get("settings") or {})
                break
        if fixed:
            strategy_params["expertFixedSettings"] = fixed
        if decoded.get("entry_rules") is not None:
            strategy_params["entryRules"] = decoded["entry_rules"]
        if decoded.get("exit_rules") is not None:
            strategy_params["exitRules"] = decoded["exit_rules"]

        bt = Backtest(
            name=name, model_id=None, engine_type="daily_expert",
            expert_name=src["expert"], optimization_id=src["opt_id"],
            # "parity" groups the pair; the mode label says which side it is. Deliberately NOT
            # the source run's labels: a parity row must never be picked up by a label filter
            # that was looking for deployable candidates.
            labels=["parity", mode],
            strategy_params=strategy_params,
            start_date=datetime.fromisoformat(src["start_date"]),
            end_date=datetime.fromisoformat(src["end_date"]),
            initial_capital=src["initial_capital"],
            fitness_metric=src["fitness_metric"],
            status="running", started_at=t0,
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        # The fitness decomposition the TOP-N persist writes onto the blob, so a parity row is
        # comparable to the rows it validates. It is a pure function of `results`, so it can only
        # differ between the two modes if the results already did.
        from app.services.strategy_fitness import compute_fitness as _cf

        _cf(src["fitness_metric"], out["results"])
        _persist_results(db, bt, out["results"])
        if cfg.get("ga_fitness") is not None:
            bt.ga_fitness = float(cfg["ga_fitness"])
        bt.status = "completed"
        bt.completed_at = datetime.now()
        bt.is_saved = True
        db.commit()
        bt_id = bt.id
    finally:
        db.close()
    print(f"{BT_ID_PREFIX}{bt_id}", flush=True)     # PROTOCOL: last line, exact spelling
    return 0


# =============================================================================================
# Parent
# =============================================================================================
def _child_command(mode: str, opt_id: int, rank: Any, name: str) -> List[str]:
    return [sys.executable, os.path.abspath(__file__), "--_child", mode,
            "--opt", str(opt_id), "--rank", str(rank), "--name", name]


def _run_one(mode: str, opt_id: int, rank: Any, name: str) -> Tuple[Optional[int], str]:
    """Start the child for one mode and return (backtest id, note). The mode is passed through
    the ENVIRONMENT, not a flag, because that is how every consumer reads it -- a flag would test
    a code path the GA never uses."""
    cmd = _child_command(mode, opt_id, rank, name)
    env = {**os.environ, "BA2_SHARED_ARRAYS": _MODE_FLAG[mode]}
    print(f"  [{mode}] BA2_SHARED_ARRAYS={_MODE_FLAG[mode]} {' '.join(cmd)}", flush=True)
    t0 = datetime.now()
    proc = subprocess.run(cmd, env=env, cwd=REPO, capture_output=True, text=True)
    took = (datetime.now() - t0).total_seconds()
    tail = "\n".join((proc.stdout or "").splitlines()[-15:])
    if proc.returncode != 0:
        return None, (f"child exited {proc.returncode} after {took:.0f}s\n--- stdout tail ---\n"
                      f"{tail}\n--- stderr tail ---\n"
                      f"{chr(10).join((proc.stderr or '').splitlines()[-15:])}")
    bt_id = parse_child_bt_id(proc.stdout or "")
    if bt_id is None:
        return None, (f"child exited 0 after {took:.0f}s but printed no {BT_ID_PREFIX} line\n"
                      f"--- stdout tail ---\n{tail}")
    return bt_id, f"persisted backtest {bt_id} in {took:.0f}s"


def _load_view(bt_id: int) -> Dict[str, Any]:
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        if bt is None:
            raise SystemExit(f"backtest {bt_id} vanished between the child and the comparison")
        return _row_view(bt)
    finally:
        db.close()


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backtest_parity.py",
        description="Re-run one known genome private vs shared and compare the rows byte-for-byte.")
    p.add_argument("--opt", type=int, required=True, help="StrategyOptimization id.")
    p.add_argument("--rank", default="best",
                   help="'best' (best_params) or a 1-based rank of the distinct-fitness ranking.")
    p.add_argument("--label", help="Appended to both row names, so a second comparison of the "
                                   "same source is a NEW pair instead of a refusal.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the source and the two child commands; run nothing.")
    p.add_argument("--keep-going", action="store_true",
                   help="Run the second mode even if the first child failed (the comparison is "
                        "still impossible; this only gets both failures in one pass).")
    p.add_argument("--_child", dest="child", choices=sorted(_MODE_FLAG),
                   help=argparse.SUPPRESS)      # hidden: the child form, spawned by the parent
    p.add_argument("--name", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.rank != "best":
        try:
            args.rank = int(args.rank)
        except ValueError:
            p.error("--rank must be 'best' or a 1-based integer")
        if args.rank < 1:
            p.error("--rank must be >= 1")
    if args.child and not args.name:
        p.error("--_child requires --name")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse(argv)
    if args.child:
        return run_child(args.child, args.opt, args.rank, args.name)

    _bootstrap()
    src = resolve_source(args.opt, args.rank)
    names = {m: parity_name(src["name"], m, args.label) for m in ("private", "shared")}
    print(f"source: opt {src['opt_id']} rank {src['rank']} -> {src['name']} "
          f"[{src['expert']}] ga_fitness={src['ga_fitness']} "
          f"{src['start_date']}..{src['end_date']} capital={src['initial_capital']}")

    clash = existing_parity_names(list(names.values()))
    if clash:
        print(f"REFUSING: parity rows already exist ({', '.join(sorted(clash))}). They are "
              f"evidence and are never overwritten -- pass --label <tag> to run a new pair.")
        return 2

    print("children (private FIRST, then shared, sequentially -- they share this machine):")
    if args.dry_run:
        for mode in ("private", "shared"):
            cmd = _child_command(mode, args.opt, args.rank, names[mode])
            print(f"  [{mode}] BA2_SHARED_ARRAYS={_MODE_FLAG[mode]} {' '.join(cmd)}")
        print("--dry-run: nothing was run.")
        return 0

    ids: Dict[str, Optional[int]] = {}
    for mode in ("private", "shared"):
        bt_id, note = _run_one(mode, args.opt, args.rank, names[mode])
        ids[mode] = bt_id
        print(f"  [{mode}] {note}")
        if bt_id is None and not args.keep_going:
            print("FAILED TO RUN: no comparison was made.")
            return 2
    if any(v is None for v in ids.values()):
        print("FAILED TO RUN: no comparison was made.")
        return 2

    diffs = compare_rows(_load_view(ids["private"]), _load_view(ids["shared"]))
    print(f"compared backtest {ids['private']} (private) vs {ids['shared']} (shared)")
    if not diffs:
        print("PASS: the two rows are byte-identical across every blob and metric column.")
        return 0
    print(f"FAIL: {len(diffs)} difference(s):")
    for d in diffs:
        print(f"  {d}")
    print("A difference is a blocker, not a tolerance discussion: trace it to the cause and fix "
          "it before the shared path ships.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
