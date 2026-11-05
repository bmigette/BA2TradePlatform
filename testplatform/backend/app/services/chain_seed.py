"""Walk-forward CHAIN SEEDING: fold k+1 (and the refit) start from fold k's FINAL population.

Three jobs, all pure apart from reading the source ``StrategyOptimization`` row they are handed:

* ``final_population_record`` -- what the handler stores for a COMPLETED job in the
  ``strategy_optimization_populations`` table: the genes and fitnesses of the last EVALUATED
  generation plus the gene-space signature they were bred in.
* ``build_chain_seed_population`` -- read that record back for a NEW job: refuse loudly when it is
  missing, when the new job's gene space differs, or when the source saw data beyond the new job's
  window; pad a short population with random individuals (count logged and returned); cut a long
  one to the fittest.
* ``lineage`` -- the ``seededFrom`` block recorded on the new row so reports can show the chain.

Why not ``--warm-start-from``: that seeds from the tail of ``all_results`` (an approximation of the
last generation), does no space check and leaves no lineage. Why not the GA checkpoint: it is
cleared on completion and its fingerprint contains generations/population, which differ per fold.
Design: docs/plans/2026-10-25-goal2027-walk-forward-plan.md.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SEEDED_FROM_KEY = "seededFrom"
CHAIN_SEED_KEY = "chainSeedFromOptimizationId"


class ChainSeedError(Exception):
    """A refusal to seed. The handler fails the job with the message; never swallowed."""


def gene_space_signature(param_ranges: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ordered, JSON-safe identity of a gene space: per gene its name, type and every bound the
    decode reads. Two jobs can exchange chromosomes iff these lists are equal."""
    out = []
    for name, cfg in param_ranges.items():
        out.append({"name": name, "type": cfg.get("type"), "min": cfg.get("min"),
                    "max": cfg.get("max"), "step": cfg.get("step"),
                    "choices": list(cfg["choices"]) if cfg.get("choices") is not None else None})
    return out


def final_population_record(param_ranges: Dict[str, Dict[str, Any]],
                            snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What the handler stores in ``strategy_optimization_populations`` (None when no generation
    was evaluated): ``gene_space`` -> the signature column, ``population`` -> the population column."""
    if not snapshot or not snapshot.get("genes"):
        return None
    return {"gene_space": gene_space_signature(param_ranges),
            "population": {"generation": int(snapshot["generation"]),
                           "gene_names": list(param_ranges.keys()),
                           "genes": [list(g) for g in snapshot["genes"]],
                           "fitnesses": [float(f) for f in snapshot["fitnesses"]]}}


def stored_record(row: Any) -> Optional[Dict[str, Any]]:
    """The flat record ``build_chain_seed_population`` reads, from a population-table row."""
    if row is None:
        return None
    return {**row.population, "gene_space": row.gene_space_signature}


def _diff(stored: List[Dict[str, Any]], current: List[Dict[str, Any]]) -> str:
    s = {g["name"]: g for g in stored}
    c = {g["name"]: g for g in current}
    only_s = [n for n in s if n not in c]
    only_c = [n for n in c if n not in s]
    changed = [n for n in s if n in c and s[n] != c[n]]
    parts = []
    if only_s:
        parts.append(f"only in the source: {only_s[:8]}")
    if only_c:
        parts.append(f"only in this job: {only_c[:8]}")
    if changed:
        parts.append(f"range/type changed: {changed[:8]}")
    if not parts:
        parts.append("same genes in a different ORDER")
    return "; ".join(parts) + f" ({len(stored)} source genes, {len(current)} here)"


def _iso_end(cfg: Dict[str, Any]) -> Optional[date]:
    raw = (cfg.get("backtest") or {}).get("end_date")
    return date.fromisoformat(str(raw)[:10]) if raw else None


def build_chain_seed_population(source_opt: Any, record: Optional[Dict[str, Any]], optimizer: Any,
                                target_size: int, this_end: Optional[date]
                                ) -> Tuple[list, Dict[str, Any]]:
    """(population, info) seeding a new job from ``source_opt`` and its stored final population
    ``record`` (the ``strategy_optimization_populations`` row: ``gene_space`` + population dict).

    ``this_end`` is the NEW job's train-window end: a source that trained past it has seen data this
    job's test window may contain (look-ahead) and is refused; a job with no end date cannot prove
    anything and is refused too. Failed-evaluation individuals and duplicate genomes are dropped
    BEFORE padding, so the random fill is never displaced by junk. Raises ``ChainSeedError``.
    """
    who = f"chain seed source {source_opt.id} ({source_opt.name!r})"
    if source_opt.status != "completed":
        raise ChainSeedError(f"{who} is {source_opt.status!r}, not completed")
    if not record or not record.get("genes"):
        raise ChainSeedError(
            f"{who} has no stored final population (it ran before final populations were "
            f"persisted, or no generation was evaluated). Re-run it under a new name; there is "
            f"deliberately no fallback to all_results.")
    current = gene_space_signature(optimizer.param_ranges)
    if record["gene_space"] != current:
        raise ChainSeedError(f"{who} was bred in a different gene space: "
                             f"{_diff(record['gene_space'], current)}")
    cfg = source_opt.optimization_config or {}
    src_end = _iso_end(cfg)
    if this_end is None:
        raise ChainSeedError(f"this job has no end_date, so {who} cannot be shown to have seen "
                             f"no later data (look-ahead check impossible)")
    if src_end is None:
        raise ChainSeedError(f"{who} records no end_date; cannot prove it saw no data past "
                             f"{this_end}")
    if src_end > this_end:
        raise ChainSeedError(
            f"{who} trained to {src_end}, after this job's window ends ({this_end}): seeding "
            f"would carry knowledge of data this job must not have seen (look-ahead)")
    genes = record["genes"]
    fits = record["fitnesses"]
    if len(genes) != len(fits):
        raise ChainSeedError(f"{who}: {len(genes)} genomes but {len(fits)} fitnesses")
    for i, g in enumerate(genes):
        if len(g) != len(current):
            raise ChainSeedError(f"{who}: genome #{i} has {len(g)} genes, space has {len(current)}")
    from app.services.genetic import FITNESS_EVALUATION_FAILED
    order = sorted(range(len(genes)), key=lambda i: fits[i], reverse=True)
    n_stored = len(genes)
    failed = [i for i in order if fits[i] <= FITNESS_EVALUATION_FAILED]
    seen, kept = set(), []
    for i in order:
        if fits[i] <= FITNESS_EVALUATION_FAILED:
            continue
        key = genome_key(genes[i])
        if key in seen:
            continue
        seen.add(key)
        kept.append(i)
    dupes = n_stored - len(failed) - len(kept)
    seeds = [genes[i] for i in kept[:target_size]]
    filled = max(0, target_size - len(seeds))
    population = optimizer.seeded_population(seeds, target_size)
    logger.warning(f"chain seed from {source_opt.name!r}: stored {n_stored}, dropped {len(failed)} "
                   f"failed + {dupes} duplicate; seeded {len(seeds)}, filled {filled} randomly "
                   f"(target {target_size})")
    from app.services.strategy_optimization_handler import assert_population_screener_genes_in_range
    assert_population_screener_genes_in_range(optimizer, population)
    info = lineage(source_opt, n_stored, len(seeds), filled, src_end)
    info.update({"dropped_failed": len(failed), "dropped_duplicates": dupes})
    return population, info


def genome_key(genes: List[Any]) -> tuple:
    """Canonical identity of a chromosome: the gene values, floats rounded so a JSON round trip or
    a 1-ulp difference cannot make two copies of one genome look distinct."""
    return tuple(round(g, 9) if isinstance(g, float) else g for g in genes)


def lineage(source_opt: Any, stored: int, seeded: int, filled_random: int,
            source_end: Optional[date]) -> Dict[str, Any]:
    return {"kind": "chain", "optimization_id": int(source_opt.id), "name": source_opt.name,
            "stored_population": int(stored), "seeded": int(seeded),
            "filled_random": int(filled_random),
            "source_end": source_end.isoformat() if source_end else None}
