"""FactorRanker ranking-inertness repair (``--fr-top-n-below-pool``).

FactorRanker (a BYPASS expert -- ``_EXPERT_OPT["FactorRanker"]["bypass"] = True`` in
``testplatform/ba2test_launcher.py``) holds ``settings["top_n"]`` names out of the screener's
``settings["screener_max_stocks"]``-sized pool (``long_only_top_n``, ``packages/experts/
ba2_experts/FactorRanker/construction.py``). When ``top_n >= screener_max_stocks`` the slice
``ranked[:top_n]`` discards nothing: the expert holds the WHOLE screened pool, equal-weighted
(well, whatever ``weighting`` says -- but every name is held either way), and the factor weights
(momentum/value/quality/pead) that the GA searched have NO effect on which names are picked. See
``FactorRanker.validate_deployed_settings`` for the same check applied to a live deploy.

The goal2027atr FactorRanker mid band converged exactly there (all TOP-5 rows: top_n 25,
max_stocks 20) -- operator decision 2026-09-29: add a run-level constraint
(``--fr-top-n-below-pool``) that REPAIRS a genome landing in the inert region instead of merely
diagnosing it after the fact, and re-run under it. A same-day follow-up operator decision made
the launcher flag DEFAULT ON for every bypass-expert (FactorRanker) job (``--no-fr-top-n-below-
pool`` opts out) -- this module's own contract is unaffected either way: it only ever repairs
when the CALLER passes it settings carrying both ``top_n`` and ``screener_max_stocks`` AND the
caller's own ``fr_top_n_below_pool`` flag is on; a run's STORED config with the key simply absent
(every run before this flag existed, or a run launched with ``--no-fr-top-n-below-pool``) is
always read as off, independent of any CLI default.

Pure: no DB, no app imports. Shared by ``strategy_optimization_handler._build_daily_trial_config``
(what a trial -- GA, top-N persist, re-run, robustness variant -- actually runs with) and
``ba2_common.export.backtest_export.derive_export_payload`` (what a deploy export reproduces), so
the two can never disagree about which repaired ``top_n`` a genome ran with.
"""
from typing import Any, Dict, Tuple

#: FactorRanker's top_n gene step (``_EXPERT_OPT["FactorRanker"]["expert_params"]["top_n"]``,
#: min 10 max 40 step 5) and the screener_max_stocks gene step
#: (``_SCREENER_OPT["screener_max_stocks"]``, min 10 max 50 step 10) both land on multiples of 5.
_STEP = 5
#: Floor so a tiny pool (e.g. screener_max_stocks=10) never repairs to a non-positive top_n.
_FLOOR = 5


def repair_fr_top_n_below_pool(settings: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Repair ``settings["top_n"]`` so it stays BELOW ``settings["screener_max_stocks"]``.

    When both keys are present and ``top_n >= screener_max_stocks``, returns a NEW dict (the
    input is never mutated) with ``top_n`` set to the next 5-step below the pool:
    ``max(5, screener_max_stocks - 5)``. E.g. max_stocks 20 -> top_n 15; max_stocks 10 -> top_n 5.

    A no-op (returns ``settings`` unchanged, ``repaired=False``) when either key is absent (not a
    screener-mode FactorRanker trial), not coercible to int, or ``top_n`` is already strictly
    below ``screener_max_stocks`` (nothing to repair -- e.g. 15/20 stays 15).

    Returns ``(settings_or_repaired_copy, repaired)``. Callers gate the CALL on their own
    ``fr_top_n_below_pool`` opt-in flag -- this function itself has no opinion on whether the
    repair should apply, only on what the repaired value is once asked for.
    """
    top_n = settings.get("top_n")
    max_stocks = settings.get("screener_max_stocks")
    if top_n is None or max_stocks is None:
        return settings, False
    try:
        top_n_i = int(top_n)
        max_stocks_i = int(max_stocks)
    except (TypeError, ValueError):
        return settings, False
    if top_n_i < max_stocks_i:
        return settings, False
    repaired = dict(settings)
    repaired["top_n"] = max(_FLOOR, max_stocks_i - _STEP)
    return repaired, True
