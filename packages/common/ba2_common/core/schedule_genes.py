"""Schedule genes (``schedule:<day>``): the cadence a stored genome actually ran with.

Moved from testplatform/backend/app/services/strategy_param_space.py (2026-09) so exports
built outside the test app (the public site) reconstruct the same run_schedule_override.
Pure: no DB, no app imports.
"""
from typing import Any, Dict, List, Optional, Sequence

# Fixed order so the gene list (and therefore reproducibility) is stable across runs.
SCHEDULE_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# The days a daily-bar backtest actually has bars for. saturday/sunday stay in SCHEDULE_DAYS
# (their genes are part of stored genomes and of the export/deploy reconstruction below), but
# they can never produce a decision point on their own.
WEEKDAYS = SCHEDULE_DAYS[:5]

#: The DECISION-TIME gene: a categorical choice among the job's explicit list of HH:MM values
#: (exchange-local). Decoded into the ``times`` of BOTH the entry and the manage schedule.
SCHEDULE_TIME_GENE = "schedule:time"

# Regular US session, minutes since midnight, exchange-local. Intraday bars are stamped at their
# START, so with an N-minute interval the first bar is 09:30 and the last starts at 16:00 - N.
_SESSION_OPEN_MIN = 9 * 60 + 30
_SESSION_CLOSE_MIN = 16 * 60

_INTERVAL_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60, "1h": 60}


def interval_minutes(execution_interval: str) -> int:
    """Bar length in minutes for an intraday execution interval; raises for a daily clock or an
    unknown interval (a decision TIME has no meaning on a daily clock)."""
    if execution_interval in _INTERVAL_MINUTES:
        return _INTERVAL_MINUTES[execution_interval]
    raise ValueError(
        f"a decision time needs an intraday execution interval {sorted(_INTERVAL_MINUTES)}, "
        f"got {execution_interval!r} (a daily clock has one bar per session: nothing to time)")


def hhmm_to_minutes(value: Any) -> int:
    if (not isinstance(value, str) or len(value) != 5 or value[2] != ":"
            or not (value[:2] + value[3:]).isdigit()):
        raise ValueError(f"decision time {value!r} must be a zero-padded HH:MM string")
    hh, mm = int(value[:2]), int(value[3:])
    if hh > 23 or mm > 59:
        raise ValueError(f"decision time {value!r} is not a clock time")
    return hh * 60 + mm


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_decision_times(values: Any, execution_interval: str, *, min_values: int = 2
                            ) -> List[str]:
    """The job's decision-time list, validated LOUDLY and returned sorted.

    Every value must be HH:MM, on the bar grid of ``execution_interval`` (minutes since the
    09:30 open divisible by the bar length), STRICTLY AFTER the session's first bar (the price at
    a decision is the close of the latest bar that has ENDED, so a decision on the first bar only
    sees the prior session: the engine merely warns for stored rows, a NEW job refuses), and
    STRICTLY BEFORE the session's last bar (an order fills at the open of the first bar AFTER the
    decision bar; a decision on the last bar would fill at the NEXT session's open, an overnight
    gap live never takes, since live fills within seconds). At least two distinct values (one
    value is not a gene), no duplicates. Sorted so the gene's index order, and therefore the
    job's identity, does not depend on how the list was typed.

    A time that does not exist on a SHORT session (13:00 half days) is not refused here (the
    calendar is not known to this pure function); the engine counts and logs those sessions.
    """
    step = interval_minutes(execution_interval)
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError(f"decision times must be a list of HH:MM strings, got {values!r}")
    mins = [hhmm_to_minutes(v) for v in values]
    if len(set(mins)) != len(mins):
        raise ValueError(f"decision times {list(values)!r} contain a duplicate")
    if len(mins) < min_values:
        raise ValueError(
            f"decision times {list(values)!r}: need at least {min_values} value(s) "
            f"(a gene needs two: a single time is not a search)")
    first_bar, last_bar = _SESSION_OPEN_MIN, _SESSION_CLOSE_MIN - step
    for v, m in zip(values, mins):
        if m < first_bar or m > last_bar or (m - first_bar) % step != 0:
            raise ValueError(
                f"decision time {v!r} is not on the {execution_interval} bar grid of the regular "
                f"session ({_fmt(first_bar)}..{_fmt(last_bar)})")
        if m == first_bar:
            raise ValueError(
                f"decision time {v!r} is the session's first bar: the decision price would be the "
                f"PRIOR session's last close (use >= {_fmt(first_bar + step)})")
        if m == last_bar:
            raise ValueError(
                f"decision time {v!r} is the session's last bar: the order would fill at the NEXT "
                f"session's open (use <= {_fmt(last_bar - step)})")
    return [_fmt(m) for m in sorted(mins)]


def parse_decision_times_arg(raw: Optional[str], execution_interval: str, *,
                             when_unset: str = "fixed") -> Optional[List[str]]:
    """The ONE reading of a ``--decision-times`` flag, shared by the launcher and every driver.

    ``None`` (flag absent) means ``when_unset``: ``"fixed"`` for a plain ``optimize`` (the single
    shared default time, no gene), ``"default"`` for a grid DRIVER. ``"fixed"`` -> None (no gene).
    ``"default"`` -> ``DEFAULT_DECISION_TIME_CHOICES`` on an intraday interval; on a DAILY clock
    an UNSET driver flag resolves to None (daily-clock jobs have no decision time) but an explicit
    ``default`` or list is refused by ``validate_decision_times``. Anything else is a comma list,
    validated and sorted. Returns the validated list or None."""
    from ba2_common.core.knowability import DEFAULT_DECISION_TIME_CHOICES

    explicit = raw is not None
    token = (raw if explicit else when_unset).strip()
    if token == "fixed":
        return None
    if token == "default":
        if not explicit and execution_interval not in _INTERVAL_MINUTES:
            return None
        values: Sequence[str] = list(DEFAULT_DECISION_TIME_CHOICES)
    else:
        values = [t.strip() for t in token.split(",") if t.strip()]
    return validate_decision_times(values, execution_interval)


#: LIVE DEPLOYMENT bound on a decision time (backtests may still explore any valid time).
#: The backtest fills at the open of the bar after the decision; live submits when the analysis
#: pass finishes. Measured on PROD (read-only, September 2026, 09:30 ET passes, trigger = 13:30
#: UTC to the last order created that day, 27 entry passes after dropping two multi-hour
#: manual/deploy-day outliers): median / p95 / max seconds -- FMPEarningsDrift 150/268/268,
#: FMPRating 253/425/425, DeterministicScorer 401/422/422, FMPInsiderClusterBuy 434/506/506.
#: FMPSenateTraderWeight has no usable sample (one 4.7 h manual run), so the bound is the
#: LARGEST measured p95 for every expert, rounded up. A deployment is refused when
#: ``time + LIVE_PASS_P95_SECONDS + LIVE_PASS_MARGIN_SECONDS`` falls after the regular close of a
#: normal day. With the 300 s margin: 15:30 passes (15:43:30), 15:45 passes (15:58:30, the
#: latest default value), 15:50 is refused (16:03:30).
#: The measured MAXIMUM pass was 506 s (FMPInsiderClusterBuy): for a 15:45 decision the orders are
#: out by about 15:53:30. The sample is September passes at 09:30 on a machine that was often
#: loaded. RE-MEASURE this bound if the expert universe or the hardware changes.
LIVE_PASS_P95_SECONDS = 510
LIVE_PASS_MARGIN_SECONDS = 300


def live_deploy_time_refusal(time_hhmm: str) -> Optional[str]:
    """A refusal message when a live schedule at ``time_hhmm`` could not finish its pass before
    the close of a normal day (see ``LIVE_PASS_P95_SECONDS``), else None."""
    finish = hhmm_to_minutes(time_hhmm) * 60 + LIVE_PASS_P95_SECONDS + LIVE_PASS_MARGIN_SECONDS
    if finish > _SESSION_CLOSE_MIN * 60:
        return (f"schedule time {time_hhmm}: an entry pass takes up to {LIVE_PASS_P95_SECONDS}s "
                f"(measured p95) plus a {LIVE_PASS_MARGIN_SECONDS}s margin, ending "
                f"{finish // 3600:02d}:{finish % 3600 // 60:02d} -- after the "
                f"{_fmt(_SESSION_CLOSE_MIN)} close; the order would miss the session the "
                f"backtest filled it in")
    return None


def schedule_time_warnings(times: Sequence[str], time_basis: str = "market",
                           local_tz: Any = None) -> List[str]:
    """UI warnings for a live schedule's ``times`` (a WARNING only, never a refusal).

    * outside the regular session (09:30 <= t < 16:00 New York): "this pass will be skipped: the
      market is closed at that time" -- the live session guard skips it;
    * inside it but so late that ``t + LIVE_PASS_P95_SECONDS + LIVE_PASS_MARGIN_SECONDS`` falls
      after the close: "too close to the close to finish".
    ``time_basis`` "market": the times are New York wall time. "local": converted from
    ``local_tz`` (a tzinfo; required) on a reference weekday. Unparseable times are skipped (the
    form's own validator flags them). A half day's 13:00 close is not modelled here."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    out: List[str] = []
    for t in times:
        try:
            minutes = hhmm_to_minutes(t if len(t) == 5 else t.zfill(5))
        except (ValueError, AttributeError):
            continue
        if time_basis != "market":
            if local_tz is None:
                raise ValueError("a local-basis schedule needs local_tz to be judged")
            today = datetime.now(local_tz)
            local = today.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
            ny_dt = local.astimezone(ny)
            minutes = ny_dt.hour * 60 + ny_dt.minute
            label = f"{t} local = {_fmt(minutes)} NYSE"
        else:
            label = f"{t} NYSE"
        if not (_SESSION_OPEN_MIN <= minutes < _SESSION_CLOSE_MIN):
            out.append(f"{label}: this pass will be skipped: the market is closed at that time "
                       f"(regular session {_fmt(_SESSION_OPEN_MIN)}-{_fmt(_SESSION_CLOSE_MIN)} NYSE)")
            continue
        late = live_deploy_time_refusal(_fmt(minutes))
        if late:
            out.append(f"{label}: too close to the close to finish: {late}")
    return out


def retime_schedules(config: Dict[str, Any], decision_time: str) -> Dict[str, Any]:
    """``config`` with BOTH the entry and the manage schedule retimed to ``decision_time`` (days
    untouched). For tools that re-run a stored row at another time (the same two schedules the
    ``schedule:time`` gene drives). Validated against the config's own execution interval;
    refuses a config that lacks either schedule's days. Returns a NEW dict (shallow copy)."""
    validate_decision_times([decision_time], config["execution_interval"], min_values=1)
    out = dict(config)
    for key in ("run_schedule_override", "manage_schedule_override"):
        sched = config.get(key)
        if not sched or not sched.get("days"):
            raise ValueError(f"cannot retime: config has no {key} with days")
        out[key] = {"days": dict(sched["days"]), "times": [decision_time]}
    return out


def schedule_time_from_genes(strategy_params: Optional[Dict[str, Any]]) -> Optional[str]:
    """The decision time a stored genome chose (its ``schedule:time`` gene), or None."""
    if not isinstance(strategy_params, dict) or SCHEDULE_TIME_GENE not in strategy_params:
        return None
    value = strategy_params[SCHEDULE_TIME_GENE]
    hhmm_to_minutes(value)  # a stored non-time value is corrupt: refuse, never reinterpret
    return value


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
    chosen_time = schedule_time_from_genes(strategy_params)
    by_day = {
        k[len("schedule:"):]: bool(v)
        for k, v in strategy_params.items()
        if isinstance(k, str) and k.startswith("schedule:") and k != SCHEDULE_TIME_GENE
    }
    if not by_day and chosen_time is None:
        return None
    if not by_day:
        # A time gene with no day genes: the days are the run-level override's, untouched.
        base_days = (base_override or {}).get("days")
        if not base_days:
            raise ValueError(
                "a genome carries a schedule:time gene but no schedule:<day> genes and the run "
                "states no days: refusing to guess a cadence")
        days = dict(base_days)
        if weekdays_only:
            days = {day: (bool(value) and day in WEEKDAYS) for day, value in days.items()}
        return {"days": days, "times": [chosen_time]}
    days = {day: by_day.get(day, False) for day in SCHEDULE_DAYS}
    if weekdays_only:
        days = {day: (value and day in WEEKDAYS) for day, value in days.items()}
    # Same repair as decode_params, so a re-run reconstructs the days the trial actually ran
    # with. With weekdays_only the filter above has already cleared the weekend, so an
    # all-weekend genome deploys as Monday rather than as an instance that never scans at all.
    days = repair_no_weekday(days, option_run=option_run)
    # A stored row that states no time ran at the session's first bar (every row before the
    # 2026-10-07 default moved); a NEW run always states its time (DEFAULT_DECISION_TIME).
    from ba2_common.core.knowability import entry_times_for
    if chosen_time is not None:
        # The genome CHOSE its time: it wins over the run-level time (both live schedules).
        return {"days": days, "times": [chosen_time]}
    # a STORED row: an absent time is the legacy first-bar time (one explicit rule, in knowability)
    return {"days": days, "times": entry_times_for((base_override or {}).get("times"),
                                                   stored_row=True, intraday=True)}
