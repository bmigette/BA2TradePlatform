"""Schedule genes (``schedule:<day>``): the cadence a stored genome actually ran with.

Moved from testplatform/backend/app/services/strategy_param_space.py (2026-09) so exports
built outside the test app (the public site) reconstruct the same run_schedule_override.
Pure: no DB, no app imports.
"""
from typing import Any, Dict, List, Optional

# Fixed order so the gene list (and therefore reproducibility) is stable across runs.
SCHEDULE_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# The days a daily-bar backtest actually has bars for. saturday/sunday stay in SCHEDULE_DAYS
# (their genes are part of stored genomes and of the export/deploy reconstruction below), but
# they can never produce a decision point on their own.
WEEKDAYS = SCHEDULE_DAYS[:5]


#: What an ABSENT weekday key in a schedule's ``days`` dict means: Monday-Friday enabled, Saturday and
#: Sunday disabled. It is the default the settings UI shows for an absent day, and it cannot change a
#: backtest (a backtest clock has no weekend bars). ONE definition, read by the live scheduler
#: (``JobManager._parse_schedule``), the backtest entry gate (``daily_engine._schedule_allows_entry``)
#: and the UI summaries/loaders.
SCHEDULE_DAY_DEFAULTS: Dict[str, bool] = {day: day not in ("saturday", "sunday") for day in SCHEDULE_DAYS}


def schedule_weekday_enabled(days: Dict[str, Any], weekday: str) -> bool:
    """Whether ``weekday`` (lower-case name, ``SCHEDULE_DAYS``) is enabled in a schedule's ``days`` dict.

    An explicit value wins; an absent key takes ``SCHEDULE_DAY_DEFAULTS``. Raises ``KeyError`` for a
    name that is not a weekday (a caller bug, never a data condition). Keep this cheap: the backtest
    calls it per bar.
    """
    return bool(days.get(weekday, SCHEDULE_DAY_DEFAULTS[weekday]))


def unknown_schedule_day_keys(days: Dict[str, Any]) -> List[str]:
    """The keys of a schedule's ``days`` dict that are not exact lower-case weekday names (a typo
    such as "wensday"), in dict order. A caller that cannot run such a schedule refuses it loudly."""
    return [k for k in days if k not in SCHEDULE_DAY_DEFAULTS]


def repair_no_weekday(days: Dict[str, bool], option_run: bool) -> Dict[str, bool]:
    """Force the first weekday ON when the genome is a dead config; the weekend flags are left
    as they are. Shared by ``decode_params`` (what a trial runs) and
    ``schedule_override_from_genes`` (what a re-run/export reconstructs) so the two cannot
    drift apart.

    OPTION runs (``option_run=True``): repaired when no WEEKDAY is on. A daily clock has no
    saturday/sunday bars, so a weekend-only genome never scans for entries -- the same dead
    config as all-OFF, which the fitness cannot tell from "just unlucky" (plan 2026-09-24
    Task 5). Repair, don't reject.

    EQUITY runs (``option_run=False``): repaired only when ALL seven days are off -- the
    historical rule, kept unchanged on purpose. Stock backtests and grids must not change
    behaviour (user rule): an equity weekend-only genome keeps scoring ZERO_TRADE exactly as
    every stored equity result and every in-flight equity checkpoint was scored.

    MUTATES ``days`` in place (and returns it for convenience); callers pass a dict they built
    for this call.
    """
    dead = ((not any(days.get(day) for day in WEEKDAYS)) if option_run
            else (not any(days.values())))
    if dead:
        days[SCHEDULE_DAYS[0]] = True
    return days


def schedule_override_from_genes(
    strategy_params: Optional[Dict[str, Any]],
    base_override: Optional[Dict[str, Any]] = None,
    weekdays_only: bool = False,
    option_run: bool = False,
) -> Optional[Dict[str, Any]]:
    """The run_schedule_override a stored genome ACTUALLY ran with, or None if it has no
    schedule genes.

    ``_build_daily_trial_config`` lets a decoded ``schedule_days`` REPLACE the run-level
    cadence for that individual, keeping only the run-level ``times``. Anything that
    reconstructs a genome's config after the fact -- a re-run, an export, a deploy -- has to
    reproduce that same replacement, or it silently reports/deploys the run-level cadence
    instead of the days the GA selected. That is exactly how five live instances came to fire
    on Mondays when their genomes had chosen Thursday, or Tue/Thu/Fri (2026-09-07).

    Mirrors ``decode_params``' repair rule (``repair_no_weekday``): a dead schedule gets the
    first weekday forced back ON, because a config that never scans for entries is dead rather
    than merely unlucky. ``option_run`` selects which rule, exactly as ``decode_params`` derives
    it from the strategy (``_strategy_is_option_run``): no weekday on (options) vs all seven off
    (equity, the default). Under ``weekdays_only`` the two rules coincide -- the filter has
    already cleared the weekend -- which is why the deploy callers need not pass it.

    ``weekdays_only`` translates the genome into the cadence it EFFECTIVELY ran, for callers
    that drive a real scheduler rather than a bar loop. On a daily clock there are no weekend
    bars, so a saturday/sunday gene is noise the GA was never able to evaluate -- it stays ON
    in perfectly good genomes purely because nothing selected against it. A live deploy that
    copies those bits arms a real Saturday cron and runs an entry pass into a closed market,
    which is behaviour no backtest ever scored. Deploy paths pass True; anything reproducing a
    backtest leaves it False so the reconstruction stays bit-for-bit.

    Returns None when the genome predates the schedule genes, so the caller keeps whatever
    run-level override it already had.
    """
    if not isinstance(strategy_params, dict):
        return None
    by_day = {
        k[len("schedule:"):]: bool(v)
        for k, v in strategy_params.items()
        if isinstance(k, str) and k.startswith("schedule:")
    }
    if not by_day:
        return None
    days = {day: by_day.get(day, False) for day in SCHEDULE_DAYS}
    if weekdays_only:
        days = {day: (value and day in WEEKDAYS) for day, value in days.items()}
    # Same repair as decode_params, so a re-run reconstructs the days the trial actually ran
    # with. With weekdays_only the filter above has already cleared the weekend, so an
    # all-weekend genome deploys as Monday rather than as an instance that never scans at all.
    days = repair_no_weekday(days, option_run=option_run)
    return {"days": days, "times": (base_override or {}).get("times") or ["09:30"]}
