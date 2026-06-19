"""Portfolio construction for FactorRanker.

Pure functions that turn a ranking + composite scores into long-only target
weights. v1 ships ``long_only_top_n``; a typed seam for a future long/short mode
is intentionally left out (YAGNI).
"""

from typing import Dict, List


def long_only_top_n(ranked: List[str], scores: Dict[str, float], top_n: int,
                    weighting: str = "equal", max_weight_per_name: float = 1.0,
                    gross_exposure: float = 1.0) -> Dict[str, float]:
    """Long-only target weights for the top-N ranked names.

    Args:
        ranked: symbols best-first (output of ``rank_symbols``).
        scores: composite score per symbol (used by score weighting).
        top_n: number of names to hold.
        weighting: ``"equal"`` (1/N each) or ``"score"`` (proportional to the
            non-negative composite score).
        max_weight_per_name: per-name weight cap.
        gross_exposure: total weight to deploy across the book.

    Caps are enforced by water-filling: any name whose proportional share would
    exceed the cap is fixed at the cap and its freed budget is redistributed among
    the remaining names. When every held name sits at the cap, the leftover stays
    in cash (total deployed < gross_exposure) — concentration limits win over full
    deployment.
    """
    picks = ranked[:top_n]
    if not picks:
        return {}

    # Base proportions across picks (sum to 1).
    if weighting == "score":
        raw = {s: max(scores.get(s, 0.0), 0.0) for s in picks}
        total_raw = sum(raw.values())
        base = ({s: raw[s] / total_raw for s in picks} if total_raw > 0
                else {s: 1.0 / len(picks) for s in picks})
    else:  # equal
        base = {s: 1.0 / len(picks) for s in picks}

    weights: Dict[str, float] = {}
    uncapped = list(picks)
    budget = float(gross_exposure)

    while uncapped:
        base_sum = sum(base[s] for s in uncapped)
        if base_sum <= 0 or budget <= 0:
            break
        newly_capped = [s for s in uncapped
                        if budget * base[s] / base_sum > max_weight_per_name + 1e-12]
        if not newly_capped:
            for s in uncapped:
                weights[s] = budget * base[s] / base_sum
            break
        for s in newly_capped:
            weights[s] = max_weight_per_name
            uncapped.remove(s)
        budget = gross_exposure - sum(weights.values())

    return weights
