#!/usr/bin/env python
"""Ablate the market master gene (``market:enabled``) on completed goal2027atr TOP-N winners.

Operator decision 2026-09-29: no separate all-off control GA job for the goal2027atr grid --
ONE GA per cell where market conditions (and ATR) are togglable by the GA, plus a CHEAP ablation
of the winners after the fact. This tool is that ablation: for every completed optimization whose
name matches ``--pattern`` (default ``%atr27%``) and whose config carries a market-condition
profile, it takes each persisted TOP-N backtest (``TOP<k>-<opt name>``, linked by
``optimization_id``) whose genome carries the master gene, and re-runs the SAME configuration
TWICE -- ``market:enabled`` as STORED, and forced to 0 -- everything else identical: seed,
window, costs, stress, fitness, robust setting, ATR policy, all read verbatim off the
optimization's own ``optimization_config``.

COMPARABILITY (review fix, 2026-09-29): the report compares two FRESH re-runs of the same
in-process path against each other, not a fresh re-run against the GA's own stored
``ga_fitness`` -- code/data can drift between when the GA scored a genome and when this tool
ablates it, and diffing a fresh number against a stale one would misattribute that drift to the
market gate. The stored ``ga_fitness`` is still reported, as a deviation check on the FRESH
ORIGINAL re-run: if it moves by more than 1%, the pair is flagged rather than silently trusted.

Persists BOTH re-runs as new ``Backtest`` rows -- ``ABL-ORIG-TOP<k>-<opt name>`` (label
``ablation-original``) and ``ABL-MKTOFF-TOP<k>-<opt name>`` (label ``ablation-market-off``) --
so the pair is queryable together later.

Reuses the platform's existing re-run path -- the same reconstruction
``app.services.backtest.rerun_handler._build_optimization_rerun_config`` uses for an
optimization-derived row (``_gene_params(bt.strategy_params)`` -> ``decode_params`` ->
``_build_daily_trial_config``) -- and the same in-process synchronous runner
(``run_daily_backtest``) the GA's own top-N persist phase calls. No new backtest runner.

Runs LOCALLY, IN-PROCESS, ONE BACKTEST AT A TIME (a grid runs on this machine; running two
multi-GB backtests at once here risks OOM alongside it -- see ``tools/run_genome_once.py``).

Usage:
    python tools/strategy_research/atr_grid/ablate_market.py --out reports/ablation.csv
    python tools/strategy_research/atr_grid/ablate_market.py --pattern "%atr27-S1%" --dry-run
    python tools/strategy_research/atr_grid/ablate_market.py --opt-id 512 --out out.md
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# --- path bootstrap: resolve relative to THIS file, so the tool runs correctly from any
# checkout/worktree it is copied into (tools/strategy_research/atr_grid/ -> tools -> repo root).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_BACKEND = os.path.join(_REPO_ROOT, "testplatform", "backend")
for _p in (_BACKEND, os.path.join(_REPO_ROOT, "testplatform")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logger = logging.getLogger(__name__)

#: Default optimization-name filter (SQL LIKE): the goal2027atr grid's job names.
DEFAULT_PATTERN = "%atr27%"

#: Labels the two persisted rows of a pair carry, so they are queryable later
#: (``labels`` is a JSON list, filtered via SQLite json_each -- see app/models/backtest.py).
ABLATION_LABEL = "ablation-market-off"
ORIGINAL_LABEL = "ablation-original"

#: Above this absolute percent, a FRESH re-run of the stored (unmodified) genome disagreeing
#: with the GA's own recorded ``ga_fitness`` is flagged rather than silently trusted -- the same
#: fidelity-gate idea ``ba2test_launcher._persist_top_backtests``/``rerun_fitness_divergence``
#: already applies to an ordinary top-N re-run, reused here at a fixed, reportable threshold.
DEVIATION_THRESHOLD_PCT = 1.0

_TOPN_RE = re.compile(r"^TOP(\d+)-")


# ==================================================================================================
# pure logic -- no DB, no engine. Unit-tested directly.
# ==================================================================================================
def market_condition_profiles(optimization_config: Optional[Dict[str, Any]]) -> List[str]:
    """The profiles an optimization's stored config was gated with (``[]`` if none/absent).

    Read from ``backtest['market_condition_profiles']`` -- the RESOLVED list every stored-config
    consumer already reads (re-runs, robustness variants, top-N persist, tools/backtest_parity.py;
    see ``ba2test_launcher._apply_market_conditions``), not re-derived from anything else.
    """
    bt = (optimization_config or {}).get("backtest") or {}
    return list(bt.get("market_condition_profiles") or [])


def require_market_profile(opt_name: str, optimization_config: Optional[Dict[str, Any]]) -> None:
    """Refuse an optimization with nothing to ablate.

    Loudly, by raising -- a market-off ablation of a config that was never gated would silently
    report "no change" for every row, which reads exactly like a genuine finding.
    """
    if not market_condition_profiles(optimization_config):
        raise ValueError(
            f"optimization {opt_name!r}: optimization_config carries no "
            f"market_condition_profiles -- nothing to ablate. This tool only ablates "
            f"market-condition-gated jobs (the goal2027atr grid's --market-condition-profile "
            f"runs); a plain job needs no market-off control.")


def topn_rank(name: str) -> Optional[int]:
    """The ``k`` of a ``TOP<k>-...`` backtest name, or None if it does not match that shape."""
    m = _TOPN_RE.match(name or "")
    return int(m.group(1)) if m else None


def ablation_name(topn_name: str) -> str:
    """``TOP3-sen-S1-atr27`` -> ``ABL-MKTOFF-TOP3-sen-S1-atr27`` (the market-off re-run)."""
    return f"ABL-MKTOFF-{topn_name}"


def original_name(topn_name: str) -> str:
    """``TOP3-sen-S1-atr27`` -> ``ABL-ORIG-TOP3-sen-S1-atr27`` (the FRESH stored-genome re-run,
    the comparability baseline for the market-off row of the same pair)."""
    return f"ABL-ORIG-{topn_name}"


def force_market_off(genome: Dict[str, Any]) -> Dict[str, Any]:
    """A COPY of ``genome`` with ``market:enabled`` forced to 0 -- and ONLY that key changed.

    Refuses (raises) a genome that does not carry the master gene at all: an optimization can be
    market-condition-gated (pass ``require_market_profile``) yet have run before the master gene
    existed, or under ``--market-condition-mode all-off`` (which never adds it -- see
    ``strategy_param_space._strategy_opts_into_market_master_gene``); either way there is
    nothing to force off, and silently no-op-ing would report a market-off row that is
    byte-identical to the original as though the ablation had run.
    """
    if "market:enabled" not in genome:
        raise ValueError(
            "genome carries no 'market:enabled' gene -- nothing to ablate (the strategy may "
            "carry no market genes, or --market-condition-mode was 'all-off' when this "
            "optimization ran, both of which never emit the master gene)")
    out = dict(genome)
    out["market:enabled"] = 0
    return out


def row_metrics(bt: Any) -> Dict[str, Optional[float]]:
    """The five metrics this tool reports, read off a ``Backtest`` row's columns."""
    return {
        "fitness": bt.ga_fitness,
        "car": bt.annualized_return,
        "max_drawdown": bt.max_drawdown,
        "total_return": bt.total_return,
        "trades": bt.total_trades,
    }


def fitness_deviation_pct(stored: Optional[float], fresh: Optional[float]) -> Optional[float]:
    """``(fresh - stored) / |stored| * 100``, or None when either side is missing/zero (a zero
    stored fitness has no meaningful percent base -- reported as an unmeasurable deviation, not
    a divide-by-zero)."""
    if not isinstance(stored, (int, float)) or not isinstance(fresh, (int, float)):
        return None
    if stored == 0:
        return None
    return (float(fresh) - float(stored)) / abs(float(stored)) * 100.0


def pair_row(source_name: str, rank: Optional[int], stored_fitness: Optional[float],
            original: Dict[str, Optional[float]], ablated: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """One report row: the FRESH original vs the FRESH market-off re-run (and their delta) for
    the 5 tracked metrics, plus the stored GA fitness and how far the fresh original strayed
    from it."""
    out: Dict[str, Any] = {"source": source_name, "rank": rank, "stored_fitness": stored_fitness}
    dev = fitness_deviation_pct(stored_fitness, original.get("fitness"))
    out["fitness_stored_deviation_pct"] = dev
    out["fitness_stored_deviation_flag"] = (dev is not None and abs(dev) > DEVIATION_THRESHOLD_PCT)
    for key in ("fitness", "car", "max_drawdown", "total_return", "trades"):
        o, a = original.get(key), ablated.get(key)
        out[key] = o
        out[f"{key}_off"] = a
        out[f"{key}_delta"] = (a - o) if isinstance(o, (int, float)) and isinstance(a, (int, float)) else None
    return out


REPORT_COLUMNS = ["source", "rank", "stored_fitness", "fitness_stored_deviation_pct",
                  "fitness_stored_deviation_flag",
                  "fitness", "fitness_off", "fitness_delta",
                  "car", "car_off", "car_delta", "max_drawdown", "max_drawdown_off",
                  "max_drawdown_delta", "total_return", "total_return_off", "total_return_delta",
                  "trades", "trades_off", "trades_delta"]


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "YES" if v else ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def render_csv(rows: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REPORT_COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({k: _fmt(r.get(k)) for k in REPORT_COLUMNS})
    return buf.getvalue()


def render_markdown(rows: List[Dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(REPORT_COLUMNS) + " |",
             "|" + "|".join("---" for _ in REPORT_COLUMNS) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(r.get(k)) for k in REPORT_COLUMNS) + " |")
    return "\n".join(lines) + "\n"


def print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("no rows")
        return
    widths = {k: max(len(k), *(len(_fmt(r.get(k))) for r in rows)) for k in REPORT_COLUMNS}
    header = "  ".join(k.ljust(widths[k]) for k in REPORT_COLUMNS)
    print(header)
    print("  ".join("-" * widths[k] for k in REPORT_COLUMNS))
    for r in rows:
        print("  ".join(_fmt(r.get(k)).ljust(widths[k]) for k in REPORT_COLUMNS))


def write_report(rows: List[Dict[str, Any]], out_path: str) -> None:
    text = render_markdown(rows) if out_path.lower().endswith(".md") else render_csv(rows)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# ==================================================================================================
# DB-touching logic -- injectable session/runner, so tests use an isolated sqlite engine and a
# stubbed backtest runner instead of the real multi-GB engine.
# ==================================================================================================
def find_target_optimizations(db: Any, pattern: str, opt_ids: Optional[List[int]] = None) -> List[Any]:
    """Every COMPLETED optimization whose name matches ``pattern`` (SQL LIKE), oldest first.

    ``opt_ids``, when given, additionally restricts to those ids (still requiring completed +
    the name match) -- lets an operator target one job without editing the pattern.
    """
    from app.models.strategy_optimization import StrategyOptimization

    q = db.query(StrategyOptimization).filter(
        StrategyOptimization.status == "completed",
        StrategyOptimization.name.like(pattern),
    )
    if opt_ids:
        q = q.filter(StrategyOptimization.id.in_(opt_ids))
    return q.order_by(StrategyOptimization.id.asc()).all()


def find_topn_rows(db: Any, optimization_id: int) -> List[Any]:
    """Every completed TOP-N backtest of ``optimization_id``, ordered by rank (``TOP<k>-...``)."""
    from app.models.backtest import Backtest

    rows = db.query(Backtest).filter(
        Backtest.optimization_id == optimization_id,
        Backtest.name.like("TOP%"),
        Backtest.status == "completed",
    ).all()
    ranked = [(topn_rank(r.name), r) for r in rows]
    ranked = [(k, r) for k, r in ranked if k is not None]
    ranked.sort(key=lambda kr: kr[0])
    return [r for _k, r in ranked]


def find_existing_row(db: Any, name: str) -> Optional[Any]:
    """The already-persisted row of this name, or None -- resume support (either half of a pair)."""
    from app.models.backtest import Backtest

    return db.query(Backtest).filter(Backtest.name == name).first()


def build_trial_config(db: Any, opt: Any, source_bt: Any, genome: Dict[str, Any],
                       name: str) -> Dict[str, Any]:
    """The ``run_daily_backtest`` config for ``genome`` under ``opt``'s stored run config.

    Mirrors ``rerun_handler._build_optimization_rerun_config`` (the SAME reconstruction an
    optimization-derived row's ordinary re-run uses): window, costs, stress, fitness metric,
    robust setting, ATR/rm-toggle policy all come from ``opt.optimization_config['backtest']``
    verbatim, unchanged. The caller supplies the (already gene-filtered) genome, so this one
    function serves both halves of a pair -- the stored genome unchanged, or with
    :func:`force_market_off` applied.
    """
    from app.models.strategy import Strategy
    from app.services.strategy_optimization_handler import _build_daily_trial_config, _build_hoisted_state
    from app.services.strategy_param_space import decode_params

    cfg = opt.optimization_config or {}
    if "backtest" not in cfg:
        raise ValueError(f"optimization {opt.id}: optimization_config carries no 'backtest' block")
    bt_block = dict(cfg["backtest"])
    strat = db.query(Strategy).filter(Strategy.id == opt.strategy_id).first()
    if strat is None:
        raise ValueError(f"optimization {opt.id}: strategy {opt.strategy_id} not found")

    decoded = decode_params(strat, genome)
    hoisted = _build_hoisted_state(bt_block) if bt_block.get("screener_opt") else None
    trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted,
                                          option_trade_records=True, stored_row=True)  # a persisted row
    trial_cfg["name"] = name
    trial_cfg["persist_trading_db"] = True
    return trial_cfg


def _new_row(db: Any, opt: Any, source_bt: Any, *, name: str, label: str,
            market_enabled_override: Optional[int]) -> Any:
    """A placeholder ``running`` Backtest row (mirrors ``ba2test_launcher._persist_one``): created
    BEFORE the (slow) backtest runs, so it has a real id to hand the trial config as
    ``backtest_id`` and progress is visible even if the process dies mid-run.

    ``market_enabled_override`` is written onto the persisted ``strategy_params['market:enabled']``
    when given (0 for the market-off half); the ORIGINAL half passes None so the stored value --
    whatever the genome actually carried -- rides through unchanged.
    """
    from app.models.backtest import Backtest

    labels = list(source_bt.labels or [])
    for extra in (label, f"ablation-source:{source_bt.id}"):
        if extra not in labels:
            labels.append(extra)
    strategy_params = dict(source_bt.strategy_params or {})
    if market_enabled_override is not None:
        strategy_params["market:enabled"] = market_enabled_override
    strategy_params["ablation_source_backtest_id"] = source_bt.id
    strategy_params["ablation_source_backtest_name"] = source_bt.name
    bt = Backtest(
        name=name, model_id=None, engine_type="daily_expert",
        expert_name=source_bt.expert_name, optimization_id=opt.id,
        labels=labels, strategy_params=strategy_params,
        start_date=source_bt.start_date, end_date=source_bt.end_date,
        initial_capital=source_bt.initial_capital,
        status="running", started_at=datetime.now(),
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


def _run_and_persist(db: Any, opt: Any, source_bt: Any, *, name: str, label: str,
                     genome: Dict[str, Any], market_enabled_override: Optional[int],
                     runner: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Any:
    """Build, run and persist ONE half of a pair (either the original or the market-off
    re-run); returns the completed, persisted ``Backtest`` row."""
    from app.services.backtest.daily_backtest_handler import _persist_results
    from app.services.strategy_fitness import compute_fitness

    trial_cfg = build_trial_config(db, opt, source_bt, genome, name)
    bt = _new_row(db, opt, source_bt, name=name, label=label,
                 market_enabled_override=market_enabled_override)
    trial_cfg["backtest_id"] = bt.id
    print(f"  running {name} (backtest #{bt.id})...", flush=True)
    out = runner(trial_cfg)
    _persist_results(db, bt, out)
    try:
        bt.ga_fitness = float(compute_fitness(opt.fitness_metric, out))
    except (ValueError, TypeError) as e:  # noqa: BLE001 -- fitness annotation must not lose the row
        logger.warning(f"{name}: fitness computation failed: {e}")
    bt.status = "completed"
    bt.completed_at = datetime.now()
    bt.is_saved = True
    db.commit()
    return bt


def run_pair(db: Any, opt: Any, source_bt: Any, *,
            runner: Callable[[Dict[str, Any]], Dict[str, Any]],
            dry_run: bool = False) -> Optional[Dict[str, Any]]:
    """Ablate ONE TOP-N row: a FRESH re-run of the stored genome AND a FRESH market-off re-run,
    each independently resumable. Returns the report row, or None when skipped (dry-run, or
    everything already persisted with nothing new to report from this call -- resume still
    returns the row so a partial re-invocation's output stays complete).

    ``runner`` defaults to ``run_daily_backtest`` in :func:`main`; tests pass a stub so no real
    backtest executes.
    """
    from app.services.backtest.rerun_handler import _gene_params

    orig_name, off_name = original_name(source_bt.name), ablation_name(source_bt.name)
    orig_bt = find_existing_row(db, orig_name)
    off_bt = find_existing_row(db, off_name)

    if dry_run:
        for name, existing in ((orig_name, orig_bt), (off_name, off_bt)):
            if existing is not None:
                print(f"  skip {name} (already persisted, backtest #{existing.id})")
            else:
                print(f"  would run {source_bt.name!r} (#{source_bt.id}) -> {name!r}")
        return None

    base_genome = _gene_params(source_bt.strategy_params)
    if orig_bt is None:
        orig_bt = _run_and_persist(db, opt, source_bt, name=orig_name, label=ORIGINAL_LABEL,
                                   genome=base_genome, market_enabled_override=None, runner=runner)
    else:
        print(f"  skip {orig_name} (already persisted, backtest #{orig_bt.id})")
    if off_bt is None:
        off_genome = force_market_off(base_genome)
        off_bt = _run_and_persist(db, opt, source_bt, name=off_name, label=ABLATION_LABEL,
                                  genome=off_genome, market_enabled_override=0, runner=runner)
    else:
        print(f"  skip {off_name} (already persisted, backtest #{off_bt.id})")

    return pair_row(source_bt.name, topn_rank(source_bt.name), source_bt.ga_fitness,
                    row_metrics(orig_bt), row_metrics(off_bt))


def ablate_optimization(db: Any, opt: Any, *,
                        runner: Callable[[Dict[str, Any]], Dict[str, Any]],
                        dry_run: bool = False) -> List[Dict[str, Any]]:
    """Ablate every TOP-N row of ONE optimization. Refuses (raises) if it is not gated."""
    from app.services.backtest.rerun_handler import _gene_params

    require_market_profile(opt.name or f"#{opt.id}", opt.optimization_config)
    rows = find_topn_rows(db, opt.id)
    if not rows:
        print(f"  {opt.name}: no completed TOP-N rows to ablate")
        return []
    out: List[Dict[str, Any]] = []
    for source_bt in rows:
        try:
            force_market_off(_gene_params(source_bt.strategy_params))  # validate-only; raises if absent
        except ValueError as e:
            print(f"  refusing {source_bt.name!r}: {e}")
            continue
        result = run_pair(db, opt, source_bt, runner=runner, dry_run=dry_run)
        if result is not None:
            out.append(result)
    return out


# ==================================================================================================
# CLI
# ==================================================================================================
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--pattern", default=DEFAULT_PATTERN,
                   help=f"SQL LIKE pattern over the optimization name (default {DEFAULT_PATTERN!r})")
    p.add_argument("--opt-id", type=int, action="append", dest="opt_ids", default=None,
                   help="restrict to this optimization id (repeatable)")
    p.add_argument("--out", default=None, help="write the summary as CSV (or .md for markdown)")
    p.add_argument("--dry-run", action="store_true", help="list what would be ablated; run nothing")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    from app.models.database import DATABASE_URL as _DB_URL
    if _DB_URL.startswith("sqlite:///"):
        from ba2_common.core import db as _ba2_db
        _ba2_db.configure_db(_DB_URL.replace("sqlite:///", "", 1))

    import app.models  # noqa: F401 -- registers every model class
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    # Standalone runs bypass the GA's own logging suppression -> 10x+ slower and a flood of
    # per-bar INFO spam (see tools/run_genome_once.py / ba2test_launcher._persist_top_backtests).
    _prior = logging.root.manager.disable
    logging.disable(logging.INFO)

    db = SessionLocal()
    try:
        optimizations = find_target_optimizations(db, args.pattern, args.opt_ids)
        if not optimizations:
            print(f"no completed optimization matches {args.pattern!r}")
            return 1
        print(f"{len(optimizations)} completed optimization(s) match {args.pattern!r}")
        rows: List[Dict[str, Any]] = []
        for opt in optimizations:
            print(f"{opt.name} (#{opt.id}):")
            try:
                rows += ablate_optimization(db, opt, runner=run_daily_backtest, dry_run=args.dry_run)
            except ValueError as e:
                print(f"  REFUSED: {e}")
                continue
        if args.dry_run:
            return 0
        print_table(rows)
        flagged = [r for r in rows if r.get("fitness_stored_deviation_flag")]
        if flagged:
            print(f"WARNING: {len(flagged)} row(s) had a fresh-original fitness more than "
                  f"{DEVIATION_THRESHOLD_PCT:.0f}% off the stored ga_fitness: "
                  f"{[r['source'] for r in flagged]}")
        if args.out:
            write_report(rows, args.out)
            print(f"wrote {args.out}")
        return 0
    finally:
        db.close()
        logging.disable(_prior)


if __name__ == "__main__":
    sys.exit(main())
