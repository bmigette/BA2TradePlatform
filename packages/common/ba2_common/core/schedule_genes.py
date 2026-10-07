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


_TRUE_WORDS = frozenset({"true", "1"})
_FALSE_WORDS = frozenset({"false", "0"})
_INVALID = object()


def _read_day_value(value: Any) -> Any:
    """A stored day value read by MEANING: ``True`` / ``False``, or ``_INVALID``.

    Real booleans are the canonical form. Tolerated, because this repo has a history of bools stored as
    ``"1"`` / ``1`` (``tools/migrate_bool_settings``): the integers 1 / 0 and the strings
    "true" / "false" / "1" / "0" (case-insensitive, surrounding whitespace ignored). NEVER truthiness
    (``bool("false")`` is True), and never a guess: None, "yes", 2, "", a float, a list ... are invalid.

    Deliberately a small pure parser here and not ``ExtendableSettingsInterface.coerce_bool``: this
    module is pure (the public-site export imports it, ``coerce_bool`` drags in the database layer) and
    ``coerce_bool`` also accepts "yes"/"on", floats and escaped JSON, which a schedule must refuse. The
    tolerated set is a subset of ``coerce_bool``'s (pinned by a test), so no spelling reads differently."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value) if value in (0, 1) else _INVALID
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return _INVALID


def schedule_weekday_enabled(days: Dict[str, Any], weekday: str) -> bool:
    """Whether ``weekday`` (lower-case name, ``SCHEDULE_DAYS``) is enabled in a schedule's ``days`` dict.

    An explicit value is read by meaning (``_read_day_value``); an absent key takes
    ``SCHEDULE_DAY_DEFAULTS``. Raises ``ValueError`` for an explicit value that is not a boolean in any
    tolerated spelling (never a silent guess; callers refuse the whole schedule up front via
    ``schedule_refusal_message``, so this only fires on a path that skipped that check), and
    ``KeyError`` for a name that is not a weekday (a caller bug). Cheap: the backtest calls it per bar.
    """
    if weekday not in days:
        return SCHEDULE_DAY_DEFAULTS[weekday]
    read = _read_day_value(days[weekday])
    if read is _INVALID:
        raise ValueError(f"schedule day {weekday!r} has invalid value {days[weekday]!r} "
                         f"(use true or false)")
    return read


def schedule_weekday_for_display(days: Dict[str, Any], weekday: str) -> bool:
    """``schedule_weekday_enabled`` for a UI that must RENDER a stored schedule: an invalid value shows
    the day's default (the editor also shows the refusal banner) instead of raising."""
    if weekday in days and _read_day_value(days[weekday]) is _INVALID:
        return SCHEDULE_DAY_DEFAULTS[weekday]
    return schedule_weekday_enabled(days, weekday)


def invalid_schedule_day_values(days: Dict[str, Any]) -> List[Any]:
    """``[(day, value), ...]`` for known weekday keys whose stored value is not a boolean in a tolerated
    spelling (None included: "absent" already has a defined default, an explicit null is ambiguous)."""
    return [(d, days[d]) for d in SCHEDULE_DAYS if d in days and _read_day_value(days[d]) is _INVALID]


def unknown_schedule_day_keys(days: Dict[str, Any]) -> List[str]:
    """The keys of a schedule's ``days`` dict that are not exact lower-case weekday names (a typo
    such as "wensday"), in dict order. A caller that cannot run such a schedule refuses it loudly."""
    return [k for k in days if k not in SCHEDULE_DAY_DEFAULTS]


def schedule_refusal_message(schedule: Any) -> Optional[str]:
    """Why a schedule dict cannot be run as written, or None when it can: the ONE sentence the live
    scheduler, the settings page, the Scheduled Jobs views and the backtest setup all show.

    A dict ``days`` is refused for a key that is not an exact lower-case weekday name ("schedule
    refused: unknown day key 'wensday'") and for a day value that is not a boolean in a tolerated
    spelling ("schedule refused: invalid value for day 'monday'=None"); other shapes (monthly, no
    ``days``) are not this check's business.
    """
    if not isinstance(schedule, dict):
        return None
    days = schedule.get("days")
    if not isinstance(days, dict):
        return None
    unknown = unknown_schedule_day_keys(days)
    invalid = invalid_schedule_day_values(days)
    if not unknown and not invalid:
        return None
    parts = []
    if unknown:
        keys = ", ".join(repr(k) for k in unknown)
        parts.append(f"unknown day key{'s' if len(unknown) > 1 else ''} {keys} "
                     f"(valid keys: {', '.join(SCHEDULE_DAYS)})")
    if invalid:
        vals = ", ".join(f"{d!r}={v!r}" for d, v in invalid)
        parts.append(f"invalid value for day {vals} (use true or false)")
    return "schedule refused: " + "; ".join(parts)


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
