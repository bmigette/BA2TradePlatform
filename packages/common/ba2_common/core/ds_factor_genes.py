"""The DeterministicScorer factor genes (goal2027 item 2.8) and the ONE canonicalisation of a genome over them.

Used by the GA decode (``testplatform strategy_param_space.decode_params`` / the trial memo key) AND by the deploy
export (``export.backtest_export``), so a backtest and the instance deployed from its row run the same phenotype.
Pure; no IO.
"""
from __future__ import annotations

from typing import Any, Dict

#: goal2027 item 2.8 -- the DeterministicScorer factor genes (``--search-ds-factor-weights``, name token ``dsfw``).
#: DS normalises positive weights inside each section (``technical.technical_score``, ``fundamental.fundamental_score``)
#: and across sections (``combine.normalize_weights``), so only RATIOS matter. Two combinations have no honest
#: reading and are therefore CANONICALISED here (never left to score a silent 0):
#:   * a section whose sub-weights are ALL zero while its section weight is positive: the technical section would
#:     score a literal 0.0 that still carries ``w_technical`` (a hidden dilution), the fundamental section would be
#:     dropped as "missing". Both decode to the section being OFF: section weight -> 0.0, sub-weights -> absent.
#:   * a section weight of 0 makes that section's sub-weights dead: they are REMOVED (absent == the expert default,
#:     which is never read).
#: ``mom_lookback_days`` is dead when the technical section is off or ``tw_mom`` is 0; the analyst window / minimum
#: targets are dead when ``w_analyst`` is 0. One pure function, called from ``decode_params`` (GA, top-N persist,
#: re-run) and ``canonical_trial_params`` (memo key), and by the deploy export.
DS_TECH_WEIGHT_GENES = ("model:tw_mom", "model:tw_d200", "model:tw_rsi", "model:tw_don")
DS_FUND_WEIGHT_GENES = ("model:fw_quality", "model:fw_piotroski", "model:fw_value", "model:fw_growth")
DS_MOM_LOOKBACK_GENE = "model:mom_lookback_days"
DS_ANALYST_DETAIL_GENES = ("model:analyst_target_window_days", "model:analyst_min_targets")
#: The two analyst-section legs (``analyst.analyst_score`` divides by their sum; both 0 drops the section as missing).
DS_ANALYST_LEG_GENES = ("model:aw_targets", "model:aw_grades")
DS_FACTOR_GENES = (DS_TECH_WEIGHT_GENES + DS_FUND_WEIGHT_GENES + (DS_MOM_LOOKBACK_GENE,)
                   + DS_ANALYST_LEG_GENES + DS_ANALYST_DETAIL_GENES)


def neutralise_ds_factor_genes(flat_params: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalise a decoded genome's DeterministicScorer factor genes (see the block comment above).

    Returns ``flat_params`` ITSELF when there is no factor gene (every older job) or nothing changes. Never mutates
    the input. A section weight that is absent from the genome is treated as unknown (nothing is decided on it)."""
    if not any(k in flat_params for k in DS_FACTOR_GENES):
        return flat_params
    out = dict(flat_params)

    def _section(weight_key: str, subs: tuple) -> bool:
        """Apply the two rules to one section; returns True when the section is ON afterwards."""
        w = out.get(weight_key)
        if w is None:
            return True
        if float(w) <= 0.0:
            for k in subs:
                out.pop(k, None)
            return False
        present = [out[k] for k in subs if k in out]
        if present and len(present) == len(subs) and sum(float(v) for v in present) <= 0.0:
            out[weight_key] = 0.0
            for k in subs:
                out.pop(k, None)
            return False
        return True

    tech_on = _section("model:w_technical", DS_TECH_WEIGHT_GENES)
    _section("model:w_fundamental", DS_FUND_WEIGHT_GENES)
    tw_mom = out.get("model:tw_mom")
    if (not tech_on) or (tw_mom is not None and float(tw_mom) <= 0.0):
        out.pop(DS_MOM_LOOKBACK_GENE, None)
    # Analyst section: legs (aw_*) are normalised like the sub-weights above. w_analyst 0 -> legs and details dead;
    # both legs 0 with w_analyst > 0 -> the section would be dropped as "missing", so it is switched OFF explicitly;
    # a zero targets leg makes the price-target window / minimum dead.
    wa = out.get("model:w_analyst")
    legs = [out[k] for k in DS_ANALYST_LEG_GENES if k in out]
    if wa is not None and float(wa) <= 0.0:
        for k in DS_ANALYST_LEG_GENES + DS_ANALYST_DETAIL_GENES:
            out.pop(k, None)
    elif wa is not None and len(legs) == len(DS_ANALYST_LEG_GENES) and sum(float(v) for v in legs) <= 0.0:
        out["model:w_analyst"] = 0.0
        for k in DS_ANALYST_LEG_GENES + DS_ANALYST_DETAIL_GENES:
            out.pop(k, None)
    elif out.get("model:aw_targets") is not None and float(out["model:aw_targets"]) <= 0.0:
        for k in DS_ANALYST_DETAIL_GENES:
            out.pop(k, None)
    return flat_params if out == flat_params else out
