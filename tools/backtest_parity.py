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

A PASS ALSO HAS TO PROVE THE SHARED PATH RAN. Two children that both silently took the private
path produce identical rows for a reason that has nothing to do with this work. So each child
reports what its caches actually held (``PARITY_EVIDENCE=``: ``shared_arrays.enabled()`` plus the
private/shared MB split from ``price_source.memory_stats`` and
``parquet_options_provider.memory_stats``) and the parent refuses to call it a PASS when the
shared child mapped nothing the private child held privately.

PREWARM FIRST. The shared child BUILDS the derived ``.npy`` cache if the host is cold, paying the
build (transient ~2.3x the frame) inside the run. Run ``tools/build_shared_arrays.py`` before
this tool if the timings are meant to mean anything.

Usage
-----
    python tools/backtest_parity.py --opt 487 --rank best
    python tools/backtest_parity.py --opt 512 --rank 1 --label rerun2
    python tools/backtest_parity.py --bt 1681            # rank read off the row's TOP<n>- name
    python tools/backtest_parity.py --opt 512 --rank 1 --dry-run

Exit codes: 0 = PASS, 1 = FAIL (the rows differ), 2 = the comparison could not be made (a child
failed or timed out, the parity rows already exist, or the evidence says the two modes did not
actually differ).
"""
from __future__ import annotations

import argparse
import collections
import functools
import json
import math
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: repo root: this file is <repo>/tools/backtest_parity.py
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The child's last stdout line. The parent reads the id from it, so it is a PROTOCOL, not a log
#: line: keep it last, keep it exact.
BT_ID_PREFIX = "PARITY_BT_ID="

#: The child's other protocol line, printed just before the id: what the caches ACTUALLY held
#: when the run finished. A PASS is only evidence if the shared child really mapped shared
#: arrays -- two runs that both silently took the private path are identical for a reason that
#: proves nothing. See ``evidence_problems``.
EVIDENCE_PREFIX = "PARITY_EVIDENCE="

#: How many differing leaves to spell out per column before summarising the rest. One is rarely
#: enough to see a pattern ("every exit_price" vs "one trade"); twenty is a wall.
_MAX_REPORTED_LEAVES = 5
#: Values in a difference line are truncated to this many characters: a differing leaf can be a
#: whole nested dict, and an unbounded repr turns the verdict into a dump.
_MAX_REPR = 120
#: Child output lines kept for the protocol parse (the stream itself is forwarded live).
_TAIL_LINES = 200

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


def _force_utf8_stdout() -> None:
    """Make this process's stdout/stderr able to carry ANYTHING a child logs.

    MEASURED THE HARD WAY (2026-09-14): a reference run died at 40 minutes with
    UnicodeEncodeError forwarding a child line containing '⚡'. Under ``nohup``/a redirect the
    parent's stdout is not a console, so Python picks the locale encoding -- cp1252 on this box
    -- and one non-ASCII character in a log line destroys an hour of work at the very end."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):       # detached / already-wrapped stream
                pass


def _emit(text: str) -> None:
    """Print a line that came from a child. Second line of defence behind ``_force_utf8_stdout``:
    if the stream still cannot encode a character (a stdout this tool does not own, a stricter
    wrapper), the character is replaced -- a mangled glyph is a trivial loss, and losing the run
    over it is not."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)


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


def _r(value: Any) -> str:
    """A value for a difference line: its repr, truncated. A differing leaf can be a whole nested
    dict, and an unbounded repr turns the verdict into a dump nobody reads."""
    text = repr(value)
    return text if len(text) <= _MAX_REPR else text[:_MAX_REPR - 1] + "…"


def _diff_paths(a: Any, b: Any, path: str, out: List[str]) -> None:
    """Every differing LEAF, deepest-first-in-order, as ``path: a != b``."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            sub = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append(f"{sub}: <missing> != {_r(b[k])}")
            elif k not in b:
                out.append(f"{sub}: {_r(a[k])} != <missing>")
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
        out.append(f"{path}: {_r(a)} != {_r(b)}")


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
            shown = "; ".join(leaves[:_MAX_REPORTED_LEAVES])
            more = (f"; (+{len(leaves) - _MAX_REPORTED_LEAVES} more)"
                    if len(leaves) > _MAX_REPORTED_LEAVES else "")
            diffs.append(f"{col}: {len(leaves)} differing leaf/leaves; first: {shown}{more}")
    for col in numeric_columns():
        if not _scalars_equal(a.get(col), b.get(col)):
            diffs.append(f"{col}: {_r(a.get(col))} != {_r(b.get(col))}")
    return diffs


def parse_child_evidence(stdout: str) -> Optional[Dict[str, Any]]:
    """The child's cache evidence, read from the LAST ``PARITY_EVIDENCE=`` line.

    ``None`` when the child never printed one or printed something unparsable -- in both cases
    the parent has no proof of which path ran, which is a refusal, not a warning."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith(EVIDENCE_PREFIX):
            try:
                loaded = json.loads(line[len(EVIDENCE_PREFIX):].strip())
            except ValueError:
                return None
            return loaded if isinstance(loaded, dict) else None
    return None


def is_option_source(bt_block: Dict[str, Any]) -> bool:
    """Does this optimization's backtest block describe an OPTION strategy?

    There is no single field for it. The launcher's option jobs are identified by the entry
    ACTION (``buy_call``/``buy_put``/... plus the ``option_*`` gene keys) and carry an ``O_*``
    label; ``strategy`` is honoured too for blocks that have one. ``options_store`` is NOT a
    signal -- verified 2026-09-14 against the live DB, every equity opt carries
    ``options_store: "sqlite"`` as a default, so keying on it would call every run an option run.
    """
    if str(bt_block.get("strategy") or "").startswith(("O_", "OS")):
        return True
    if any(str(lbl).startswith("O_") for lbl in (bt_block.get("labels") or [])):
        return True
    entry = bt_block.get("entry_action")
    if isinstance(entry, dict):
        if any(str(k).startswith("option_") for k in entry):
            return True
        action = str(entry.get("action_type") or "")
        if "call" in action or "put" in action:
            return True
    return False


def _mb(ev: Optional[Dict[str, Any]], key: str) -> float:
    try:
        return float((ev or {}).get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evidence_problems(private_ev: Optional[Dict[str, Any]],
                      shared_ev: Optional[Dict[str, Any]],
                      option_source: bool = False) -> List[str]:
    """Reasons a PASS from these two children would prove NOTHING.

    Two runs that both took the private path are byte-identical for a reason that has nothing to
    do with shared arrays, and that is exactly the failure mode a green gate must not hide: an
    unset flag, a typo in an env name, a consumer that quietly fell back. So the verdict is
    conditional on the caches actually having held what each mode claims."""
    problems: List[str] = []
    if private_ev is None:
        problems.append(f"the private child printed no {EVIDENCE_PREFIX} line: there is no proof "
                        f"of which path it took")
    if shared_ev is None:
        problems.append(f"the shared child printed no {EVIDENCE_PREFIX} line: there is no proof "
                        f"the shared path was used")
    if private_ev is not None and private_ev.get("shared_enabled"):
        problems.append("the PRIVATE child ran with shared arrays ENABLED (BA2_SHARED_ARRAYS did "
                        "not reach it): the comparison would be shared against shared")
    if shared_ev is not None and not shared_ev.get("shared_enabled"):
        problems.append("the SHARED child ran with shared arrays DISABLED (BA2_SHARED_ARRAYS did "
                        "not reach it): the comparison would be private against private")
    for label, ev in (("private", private_ev), ("shared", shared_ev)):
        if ev is not None and ev.get("options_stats_error"):
            problems.append(f"the {label} child could not read the option cache stats "
                            f"({ev['options_stats_error']}): its 0 MB of options is an unknown, "
                            f"not a measurement")
    if private_ev is not None and shared_ev is not None:
        # The private child is the witness that this run HAS bars/options at all: if it held
        # none, the shared child holding none is silence, not a failure.
        if _mb(private_ev, "bars_private_mb") > 0 and _mb(shared_ev, "bars_shared_mb") <= 0:
            problems.append(
                f"the private child held {_mb(private_ev, 'bars_private_mb')} MB of bars but the "
                f"shared child mapped 0 MB: the OHLCV columns were NOT served from the derived "
                f"cache")
        if _mb(private_ev, "options_private_mb") > 0 and _mb(shared_ev, "options_shared_mb") <= 0:
            problems.append(
                f"the private child held {_mb(private_ev, 'options_private_mb')} MB of option "
                f"arrays but the shared child mapped 0 MB: the option columns were NOT served "
                f"from the derived cache")
        # A run that traded NOTHING compares equal whatever the arrays did: two empty trade
        # lists, two flat curves, two zeroed metric columns. Measured in the field on opt 429
        # (the O_LEAP perf probe), whose stored bt block carries no options_cache_db, so the
        # engine built no options provider, no chain was ever read and both children "agreed"
        # on nothing at all -- and the tool printed PASS.
        if private_ev.get("total_trades") == 0 and shared_ev.get("total_trades") == 0:
            problems.append("VACUOUS: 0 trades on both sides -- the gate proves nothing. Two "
                            "runs that traded nothing are identical whatever the arrays did. "
                            "Pick a source whose re-run actually trades.")
        if option_source and not (private_ev.get("options_provider_built")
                                  or shared_ev.get("options_provider_built")):
            problems.append("the source is an OPTION strategy but neither child loaded a single "
                            "option underlying (0 cache entries): no chain was read, so this run "
                            "exercises none of the option reader the shared path changed. Check "
                            "the stored backtest block actually builds an options provider "
                            "(options_cache_db / the option entry action).")
    return problems


def _format_evidence(ev: Optional[Dict[str, Any]]) -> str:
    if ev is None:
        return "<none printed>"
    calls = ev.get("market_condition_resolver_calls")
    return (f"shared_enabled={ev.get('shared_enabled')} trades={ev.get('total_trades')} "
            f"bars {_mb(ev, 'bars_shared_mb')} MB shared / {_mb(ev, 'bars_private_mb')} MB private; "
            f"options {_mb(ev, 'options_shared_mb')} MB shared / "
            f"{_mb(ev, 'options_private_mb')} MB private "
            f"({ev.get('options_entries')} underlying(s)); "
            f"market-condition resolver calls "
            f"{'unknown (old build)' if calls is None else calls}")


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
    ``standalone-backtest-scripts-need-logging-disable``). The floor is INFO, matching the TOP-N
    persist path: the per-bar ruleset/RM spam goes, and a WARNING from a failing run still
    reaches the parent's stream."""
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

    logging.disable(logging.INFO)
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
            # Whether a run of this source is SUPPOSED to touch the option reader -- if it is and
            # neither child did, the comparison says nothing about the option path.
            "option_source": is_option_source(bt_block),
        }
    finally:
        if own:
            db.close()


#: ``TOP<n>-<opt name>`` / ``BEST-<opt name>`` -- the names ``_persist_top_backtests`` and
#: ``recover_missing_topn`` give the rows they persist. They ARE the rank, and they are the only
#: record of it on the row (the genome is stored, the rank is not).
_TOP_NAME = re.compile(r"^TOP(\d+)-")
_BEST_NAME = re.compile(r"^BEST-")


def rank_from_backtest_name(name: str) -> Any:
    """The rank a persisted row's NAME encodes, or ``None`` when it encodes none.

    ``None`` is a refusal, not a default: guessing ``best`` for an arbitrarily-named row would
    re-run a DIFFERENT genome than the one the operator pointed at and then compare it against
    that row, which is worse than declining."""
    m = _TOP_NAME.match(name or "")
    if m:
        return int(m.group(1))
    return "best" if _BEST_NAME.match(name or "") else None


def resolve_backtest_source(bt_id: int) -> Tuple[int, Any]:
    """(optimization id, rank) for ``--bt``: which genome that archived row came from."""
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        if bt is None:
            raise SystemExit(f"no backtest with id {bt_id}")
        if not bt.optimization_id:
            raise SystemExit(f"backtest {bt_id} ({bt.name!r}) has no optimization_id: there is no "
                             f"genome to re-run. Pass --opt/--rank instead.")
        rank = rank_from_backtest_name(bt.name or "")
        if rank is None:
            raise SystemExit(
                f"backtest {bt_id} is named {bt.name!r}, which encodes no rank (expected a "
                f"'TOP<n>-' or 'BEST-' prefix). The rank is not stored on the row, so it cannot "
                f"be recovered -- pass --opt {bt.optimization_id} --rank <best|n> explicitly.")
        return int(bt.optimization_id), rank
    finally:
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
def _market_condition_calls() -> Optional[int]:
    """This process's market-condition resolver-call count, or None on a build that predates
    the counter (an old package pinned by a worker, say -- reported, never assumed zero)."""
    try:
        from ba2_common.core.TradeConditions import market_condition_resolver_calls

        return int(market_condition_resolver_calls())
    except ImportError:
        return None


def collect_evidence(results: Dict[str, Any]) -> Dict[str, Any]:
    """What the caches ACTUALLY held when the run finished, and what the run actually DID --
    the proof that goes with the row.

    Called after ``_persist_trial_worker`` returns and BEFORE anything is released:
    ``run_daily_backtest`` clears neither the bar cache nor the option reader cache, so the run's
    own arrays are still resident and their private/shared split is the honest answer to "which
    path served this run".

    ``total_trades`` is here because a run that traded nothing compares equal for free -- see
    ``evidence_problems``."""
    from ba2_common.core import shared_arrays as SA

    from app.services.backtest import price_source as ps

    bars = ps.memory_stats()["bar_cache"]
    trades = results["total_trades"]     # the same key _persist_results maps to bt.total_trades
    # ``mb`` is the PRIVATE half of the bar cache and ``shared_mb`` the mapped half — see
    # price_source.memory_stats (the keys are always private; only the five float columns map).
    ev = {"shared_enabled": bool(SA.enabled()),
          "total_trades": int(trades) if trades is not None else None,
          "bars_shared_mb": float(bars.get("shared_mb") or 0.0),
          "bars_private_mb": float(bars.get("mb") or 0.0),
          "options_shared_mb": 0.0,
          "options_private_mb": 0.0,
          # Entries in the option reader's raw cache: one per underlying whose chain was read.
          # Zero means no options provider was ever built (or never asked for a chain), which
          # for an option source makes the whole comparison beside the point.
          "options_entries": 0,
          "options_provider_built": False,
          # MARKET-CONDITION NO-IMPACT EVIDENCE (plan Task 9). With the profile off the gates
          # must not merely produce the same numbers -- they must never be reached. Identical
          # results with the resolver quietly answering every leaf would be a worse outcome
          # than a diff, because nothing about it would look wrong. This counts every entry
          # into ``TradeConditions.resolve_market_condition_context`` in the child process, so
          # "the resolver was never called" is a measurement rather than a belief.
          "market_condition_resolver_calls": _market_condition_calls()}
    try:
        from app.services.backtest import parquet_options_provider as pq

        opts = pq.memory_stats()
        ev["options_shared_mb"] = float(opts.get("shared_mb") or 0.0)
        ev["options_private_mb"] = float(opts.get("private_mb") or 0.0)
        ev["options_entries"] = int(opts.get("entries") or 0)
        ev["options_provider_built"] = ev["options_entries"] > 0
    except ImportError as e:
        # ONLY an import error is tolerated, and even that is RECORDED rather than reported as
        # "0 MB of options". There is no legitimate exception here: an equity run simply has an
        # empty _WORKER_RAW_CACHE and memory_stats returns zeros without raising. A broad catch
        # would turn a real defect (a renamed key, a changed signature) into 0 MB in BOTH
        # children -- which compares equal, and silently PASSES the option reference runs, the
        # exact runs this evidence exists for. evidence_problems refuses on this key.
        ev["options_stats_error"] = repr(e)
        print(f"[evidence] option cache stats unavailable ({e!r})")
    return ev


def run_child(mode: str, opt_id: int, rank: Any, name: str) -> int:
    """Re-run the genome in THIS process under whatever ``BA2_SHARED_ARRAYS`` the parent set, and
    persist the result as a new Backtest. Prints ``PARITY_EVIDENCE={...}`` then
    ``PARITY_BT_ID=<id>`` last.

    Same machinery as a TOP-N persist (``_build_daily_trial_config`` -> ``_persist_trial_worker``
    -> ``_persist_results``), so a parity row is produced exactly the way the rows it is
    validating were.

    NOTE ON COLD START: the shared child BUILDS the derived ``.npy`` cache if the host was never
    prewarmed, so its first run pays the build (and its transient ~2.3x the frame) on top of the
    backtest. Run ``tools/build_shared_arrays.py`` first for a timing that means anything."""
    _force_utf8_stdout()    # the child's own logs reach the parent through this pipe
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
        # Read the caches BEFORE persisting: nothing has been released yet, and a DB error below
        # must not cost the evidence of what the run actually mapped.
        evidence = collect_evidence(out["results"])

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
        # differ between the two modes if the results already did. Wrapped exactly as
        # _persist_top_backtests wraps it: this is telemetry, and a raise here must not burn an
        # hour-long run AND the PARITY name it just claimed.
        try:
            from app.services.strategy_fitness import compute_fitness as _cf

            _cf(src["fitness_metric"], out["results"])
        except Exception as e:  # noqa: BLE001 -- never lose a persisted row over telemetry
            print(f"[{mode}] fitness annotation failed: {e!r}")
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
    # PROTOCOL: evidence first, the id LAST. Both exact spellings; the parent parses them.
    print(f"{EVIDENCE_PREFIX}{json.dumps(evidence, sort_keys=True)}", flush=True)
    print(f"{BT_ID_PREFIX}{bt_id}", flush=True)
    return 0


# =============================================================================================
# Parent
# =============================================================================================
def _child_command(mode: str, opt_id: int, rank: Any, name: str) -> List[str]:
    return [sys.executable, os.path.abspath(__file__), "--_child", mode,
            "--opt", str(opt_id), "--rank", str(rank), "--name", name]


def _run_one(mode: str, opt_id: int, rank: Any, name: str,
             timeout_min: float = 0.0) -> Tuple[Optional[int], Optional[Dict[str, Any]], str]:
    """Start the child for one mode; return (backtest id, evidence, note).

    The mode is passed through the ENVIRONMENT, not a flag, because that is how every consumer
    reads it -- a flag would test a code path the GA never uses.

    The child's output is STREAMED, line by line, prefixed with its mode: a re-run is an hour of
    work and a silent capture makes a stuck one indistinguishable from a slow one. utf-8 is
    forced on the pipe because Python would otherwise decode it as the console's cp1252 on
    Windows and a single non-ASCII byte in a log line would kill the run with a UnicodeDecodeError
    after that hour. ``timeout_min`` (0 = none) kills a child that outlives its budget, stream or
    no stream -- the timer fires on wall clock, not on output."""
    cmd = _child_command(mode, opt_id, rank, name)
    env = {**os.environ, "BA2_SHARED_ARRAYS": _MODE_FLAG[mode]}
    print(f"  [{mode}] BA2_SHARED_ARRAYS={_MODE_FLAG[mode]} {' '.join(cmd)}", flush=True)
    t0 = datetime.now()
    proc = subprocess.Popen(cmd, env=env, cwd=REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    tail: "collections.deque[str]" = collections.deque(maxlen=_TAIL_LINES)
    killed = threading.Event()

    def _kill():
        killed.set()
        proc.kill()

    timer = threading.Timer(timeout_min * 60.0, _kill) if timeout_min and timeout_min > 0 else None
    if timer is not None:
        timer.daemon = True
        timer.start()
    # The protocol lines are captured AS THEY STREAM, not re-found in the kept tail: the tail is
    # bounded, and a child that logs a few hundred lines after them (a shutdown message, a
    # library's parting warning) would push them out and turn a good run into INCONCLUSIVE.
    evidence: Optional[Dict[str, Any]] = None
    bt_id: Optional[int] = None
    try:
        for line in proc.stdout:                      # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            tail.append(line)
            if line.strip().startswith(EVIDENCE_PREFIX):
                evidence = parse_child_evidence(line) or evidence
            elif line.strip().startswith(BT_ID_PREFIX):
                bt_id = parse_child_bt_id(line) or bt_id
            _emit(f"    [{mode}] {line}")
    finally:
        # BEFORE the wait: the timer must not fire on a process that has already finished (its
        # stdout is closed, so the loop above ended), which would report a clean run as KILLED.
        if timer is not None:
            timer.cancel()
        if proc.stdout is not None:
            proc.stdout.close()
    rc = proc.wait()
    took = (datetime.now() - t0).total_seconds()
    text = "\n".join(tail)
    if killed.is_set() and rc != 0:
        return None, None, (f"child KILLED after {took:.0f}s (--timeout-min {timeout_min:g})\n"
                            f"--- output tail ---\n{text}")
    if rc != 0:
        return None, None, (f"child exited {rc} after {took:.0f}s\n--- output tail ---\n{text}")
    if bt_id is None:
        return None, evidence, (f"child exited 0 after {took:.0f}s but printed no "
                                f"{BT_ID_PREFIX} line\n--- output tail ---\n{text}")
    return bt_id, evidence, (f"persisted backtest {bt_id} in {took:.0f}s; "
                             f"{_format_evidence(evidence)}")


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
    p.add_argument("--opt", type=int, help="StrategyOptimization id.")
    p.add_argument("--bt", type=int,
                   help="An ARCHIVED backtest row to re-run: its optimization_id and its rank "
                        "(from the TOP<n>-/BEST- name prefix) are read off the row, and the two "
                        "re-runs are ALSO compared against it, informationally.")
    p.add_argument("--rank", default="best",
                   help="'best' (best_params) or a 1-based rank of the distinct-fitness ranking.")
    p.add_argument("--label", help="Appended to both row names, so a second comparison of the "
                                   "same source is a NEW pair instead of a refusal.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the source and the two child commands; run nothing.")
    p.add_argument("--keep-going", action="store_true",
                   help="Run the second mode even if the first child failed (the comparison is "
                        "still impossible; this only gets both failures in one pass).")
    p.add_argument("--timeout-min", type=float, default=0.0,
                   help="Kill a child that runs longer than this many minutes (0 = no limit).")
    p.add_argument("--_child", dest="child", choices=sorted(_MODE_FLAG),
                   help=argparse.SUPPRESS)      # hidden: the child form, spawned by the parent
    p.add_argument("--name", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if not args.opt and not args.bt:
        p.error("pass --opt <optimization id> or --bt <archived backtest id>")
    if args.opt and args.bt:
        p.error("--opt and --bt name the source two different ways; pass one")
    if args.child and not args.opt:
        p.error("--_child requires --opt (the parent always resolves --bt first)")
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
    _force_utf8_stdout()
    args = _parse(argv)
    if args.child:
        return run_child(args.child, args.opt, args.rank, args.name)

    _bootstrap()
    opt_id, rank = args.opt, args.rank
    if args.bt:
        opt_id, rank = resolve_backtest_source(args.bt)
        print(f"--bt {args.bt} -> opt {opt_id} rank {rank}")
    src = resolve_source(opt_id, rank)
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
            cmd = _child_command(mode, opt_id, rank, names[mode])
            print(f"  [{mode}] BA2_SHARED_ARRAYS={_MODE_FLAG[mode]} {' '.join(cmd)}")
        print("--dry-run: nothing was run.")
        return 0

    ids: Dict[str, Optional[int]] = {}
    evidence: Dict[str, Optional[Dict[str, Any]]] = {}
    for mode in ("private", "shared"):
        bt_id, ev, note = _run_one(mode, opt_id, rank, names[mode], args.timeout_min)
        ids[mode], evidence[mode] = bt_id, ev
        print(f"  [{mode}] {note}")
        if bt_id is None and not args.keep_going:
            print("FAILED TO RUN: no comparison was made.")
            return 2
    if any(v is None for v in ids.values()):
        print("FAILED TO RUN: no comparison was made.")
        return 2

    print(f"evidence [private] {_format_evidence(evidence['private'])}")
    print(f"evidence [shared]  {_format_evidence(evidence['shared'])}")
    problems = evidence_problems(evidence["private"], evidence["shared"], src["option_source"])
    if problems:
        print("INCONCLUSIVE: the two runs cannot be compared as private vs shared --")
        for p_ in problems:
            print(f"  {p_}")
        print("Fix the setup and re-run (with --label, the rows above are kept). A matching pair "
              "of rows proves nothing unless the shared side actually mapped shared arrays.")
        return 2

    private_view, shared_view = _load_view(ids["private"]), _load_view(ids["shared"])
    diffs = compare_rows(private_view, shared_view)
    print(f"compared backtest {ids['private']} (private) vs {ids['shared']} (shared)")
    if args.bt:
        # INFORMATIONAL ONLY, never part of the verdict. The archived row was persisted by the GA
        # from a config rebuilt differently (screener state re-derived, a later code state); a
        # re-run is allowed to diverge from it by ~0.5% and the platform has a dedicated concept
        # for that (rerun_fitness_divergence). The parity question is private vs shared, and
        # folding the archive into it would fail the gate for a reason that is not about arrays.
        archived = compare_rows(_load_view(args.bt), private_view)
        if archived:
            print(f"archived vs private (informational, NOT part of the verdict): "
                  f"{len(archived)} difference(s); first: {archived[0]}")
        else:
            print("archived vs private (informational): identical.")
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
