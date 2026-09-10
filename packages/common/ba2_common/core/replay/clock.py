"""The scoped evaluation-clock seam (spec step 1, section 4).

"Introduce a narrow injectable evaluation-clock seam only where necessary to
record/replay time-dependent calculations. Production must retain the same
time-read semantics. Replay supplies the recorded reads in order. Do not turn
live calls into ``analyze_as_of(now)`` merely to get a timestamp [...]. Do not
freeze the process-wide clock across concurrent live workers."

Hence: ``as_of`` still wins (the historical branch selector is untouched), a live
process with no capture context reads the wall clock exactly as before, and the
context is per-analysis (a ContextVar), never process-wide.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ba2_common.core.replay.context import ReplayMiss, current_capture

__all__ = ["replay_now", "ReplayMiss"]


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
