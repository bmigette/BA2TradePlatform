"""The scoped evaluation-clock seam (spec step 1, section 4).

"Introduce a narrow injectable evaluation-clock seam only where necessary to
record/replay time-dependent calculations. Production must retain the same
time-read semantics. Replay supplies the recorded reads in order. Do not turn
live calls into ``analyze_as_of(now)`` merely to get a timestamp [...]. Do not
freeze the process-wide clock across concurrent live workers."

Hence: ``as_of`` still wins (the historical branch selector is untouched), a live
process with no capture context reads the wall clock exactly as before, and the
context is per-analysis (a ContextVar), never process-wide.

**Reads are an ORDERED LIST, so never read the clock from a pooled worker.**
Replay hands the recorded reads back in the order they were recorded; two workers
racing for the next read record (and would replay) in whatever order they happen
to win, which is not reproducible. Provider taps are safe inside a pool -- they
are matched by request identity -- but a ``replay_now()`` call is not. Read the
evaluation time once, on the analysis thread, and pass it into the fan-out.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ba2_common.core.replay.context import ReplayMiss, current_capture

__all__ = ["replay_now", "host_timezone", "wall_today_in_recorded_timezone", "ReplayMiss"]


def replay_now(as_of: Optional[datetime] = None) -> datetime:
    """The evaluation time for a calculation.

    * ``as_of`` given -> returned unchanged (historical path, unchanged semantics).
    * replay mode -> the next recorded read; exhausted raises :class:`ReplayMiss`.
    * capture mode -> the wall clock, recorded in order.
    * no context -> the wall clock (production behaviour, nothing recorded).
    """
    if as_of is not None:
        return as_of
    context = current_capture()
    if context is not None and context.is_replay:
        return context.next_clock_read()
    now = datetime.now(timezone.utc)
    if context is not None:
        context.record_clock_read(now)
    return now


def host_timezone(now: Optional[datetime] = None) -> dict:
    """The RECORDING host's timezone: ``{"name": <IANA name or None>, "utc_offset_seconds": int}``.

    ``name`` comes from ``tzlocal`` and is ``None`` when the host cannot name its zone (then the
    offset is the only fact, and a replay uses that fixed offset -- exact for the recorded instant,
    which is the only instant a decision pass reads "today" at). ``utc_offset_seconds`` is the offset
    in force at ``now`` (default: the wall clock). Never raises: a host that cannot answer returns
    ``name=None`` and the offset python derives from the local clock.
    """
    now = datetime.now(timezone.utc) if now is None else now
    offset = int(now.astimezone().utcoffset().total_seconds())
    try:
        from tzlocal import get_localzone_name

        name = get_localzone_name()
    except Exception:  # noqa: BLE001 -- recording the zone must never stop a decision
        name = None
    return {"name": name or None, "utc_offset_seconds": offset}


def wall_today_in_recorded_timezone(instant: datetime):
    """The calendar day of ``instant`` in the zone the pass was RECORDED in (replay only).

    Raises :class:`ReplayMiss` when the replay context carries no recorded timezone: naming the
    replaying host's day instead would be a silent wrong answer that differs by host.
    """
    context = current_capture()
    zone = None if context is None else context.recorded_timezone()
    if zone is None:
        raise ReplayMiss(
            "host_timezone", analysis_id=None if context is None else context.analysis_id,
            detail="the recording host's timezone is not in the pass metadata "
                   "(branch_flags['host_timezone']); a replay will not use its own host's zone")
    return instant.astimezone(zone).date()
