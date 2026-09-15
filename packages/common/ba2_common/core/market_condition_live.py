"""Live market-condition context: one decision time per analysis, carried by a ContextVar.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` section 4.1 -- "The
live adapter reads ``replay_now()`` once on the coordinating thread, then passes that value into
any worker fan-out. Different analyses cannot share a mutable global clock."

* The host installs a :class:`LiveMarketConditionResolver` into the ``TradeConditions`` seam
  (only when a profile is configured; see ``resolver_from_env``).
* The analysis coordinator wraps one decision pass in :func:`market_condition_decision_scope`.
  Entering it reads ``replay_now()`` ONCE on that thread -- and only when the resolver is
  installed, so gates-off costs no clock read -- and stores a :class:`DecisionState` in a
  ContextVar. Two concurrent analyses each have their own state.
* The resolver returns the state's ONE frozen ``MarketConditionContext`` (built lazily on first
  use, then the same object for every leaf of the pass). Outside a scope it returns ``None``
  (the condition reports ``no_context``).
* A ContextVar does not follow work into ``ThreadPoolExecutor`` threads on its own: submit
  through :func:`submit_in_decision_context` / wrap with :func:`run_in_decision_context`.

Capture/replay: if a replay capture context is active when the scope opens, the reader records
every served window (``CapturingMarketConditionReader``); in replay mode the scope must be given
the recorded ``replay_reader`` and otherwise raises ``ReplayMiss`` -- a replay never reads the
provider cache.
"""
from __future__ import annotations

import contextvars
import functools
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterator, Optional

from ba2_common.core.market_calendar import NY_TZ, prior_regular_session
from ba2_common.core.market_condition_context import (
    TIMING_POLICY_PRIOR_SESSION_V1,
    MarketConditionContext,
    MarketConditionReader,
)
from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY

__all__ = [
    "PROFILE_ENV",
    "DecisionState",
    "LiveMarketConditionResolver",
    "current_decision",
    "market_condition_decision_scope",
    "run_in_decision_context",
    "submit_in_decision_context",
    "resolver_from_env",
]

#: Environment switch for the live profile. Unset, empty or ``none`` -> nothing is installed.
PROFILE_ENV = "BA2_MARKET_CONDITION_PROFILE"

_DECISION: contextvars.ContextVar[Optional["DecisionState"]] = contextvars.ContextVar(
    "ba2_market_condition_decision", default=None)


def _replay_now() -> datetime:
    # Indirection so tests can count clock reads without patching the replay package.
    from ba2_common.core.replay.clock import replay_now
    return replay_now()


@dataclass
class DecisionState:
    """One analysis's decision time and its lazily-built, then fixed, context."""

    resolver: "LiveMarketConditionResolver"
    decision_time: datetime
    reader: MarketConditionReader
    recorder: Optional[Callable[..., None]] = None
    _context: Optional[MarketConditionContext] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def session_label(self) -> date:
        return self.decision_time.astimezone(NY_TZ).date()

    def context(self) -> MarketConditionContext:
        ctx = self._context
        if ctx is not None:
            return ctx
        with self._lock:
            if self._context is None:
                self._context = MarketConditionContext(
                    decision_time=self.decision_time,
                    session_label=self.session_label,
                    prior_session=prior_regular_session(self.decision_time),
                    source_profile=self.resolver.source_profile,
                    timing_policy=TIMING_POLICY_PRIOR_SESSION_V1,
                    calc_version=self.resolver.calc_version,
                    reader=self.reader,
                    recorder=self.recorder,
                )
            return self._context


class LiveMarketConditionResolver:
    """The ``TradeConditions`` market-condition resolver for the live platform."""

    def __init__(self, profile: str, *, reader: Optional[Any] = None,
                 source_profile: str = SOURCE_PROFILE_FMP_DAILY):
        from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader

        self.reader = reader if reader is not None else FMPCacheMarketConditionReader(profile)
        if self.reader.profile != profile:
            raise ValueError(f"reader serves profile {self.reader.profile!r}, resolver wants {profile!r}")
        self.profile = profile
        self.calc_version = self.reader.calc_version
        self.source_profile = source_profile
        #: clock reads taken by ``begin_decision`` (visibility for tests and diagnostics).
        self.decisions = 0

    def begin_decision(self, *, replay_reader: Optional[Any] = None) -> DecisionState:
        """Read the evaluation clock ONCE (call on the coordinating thread) and bind the reader."""
        from ba2_common.core.replay.context import ReplayMiss, current_capture

        capture = current_capture()
        decision_time = _replay_now()
        self.decisions += 1
        if capture is not None and capture.is_replay:
            if replay_reader is None:
                raise ReplayMiss("market_condition_window", analysis_id=capture.analysis_id,
                                 detail="replaying a decision needs the recorded market-condition "
                                        "windows; the provider cache is never read in replay")
            return DecisionState(resolver=self, decision_time=decision_time, reader=replay_reader)
        if replay_reader is not None:
            raise ValueError("replay_reader given outside a replay capture context")
        if capture is not None:
            from ba2_common.core.market_condition_readers import CapturingMarketConditionReader

            capturing = CapturingMarketConditionReader(
                self.reader, capture, source_profile=self.source_profile,
                timing_policy=TIMING_POLICY_PRIOR_SESSION_V1)
            return DecisionState(resolver=self, decision_time=decision_time, reader=capturing,
                                 recorder=capturing.record)
        return DecisionState(resolver=self, decision_time=decision_time, reader=self.reader)

    def __call__(self, account: Any, instrument_name: str,
                 expert_recommendation: Any) -> Optional[MarketConditionContext]:
        state = _DECISION.get()
        if state is None or state.resolver is not self:
            return None
        return state.context()


def current_decision() -> Optional[DecisionState]:
    return _DECISION.get()


@contextmanager
def market_condition_decision_scope(*, replay_reader: Optional[Any] = None) -> Iterator[Optional[DecisionState]]:
    """Open one decision pass. A no-op (no clock read, yields ``None``) unless a
    ``LiveMarketConditionResolver`` is installed in ``TradeConditions``."""
    from ba2_common.core.TradeConditions import get_market_condition_context_resolver

    resolver = get_market_condition_context_resolver()
    if not isinstance(resolver, LiveMarketConditionResolver):
        yield None
        return
    state = resolver.begin_decision(replay_reader=replay_reader)
    token = _DECISION.set(state)
    try:
        yield state
    finally:
        _DECISION.reset(token)


def run_in_decision_context(fn: Callable) -> Callable:
    """Wrap ``fn`` so a pool thread running it sees the CURRENT decision state. Safe to reuse
    concurrently (it re-installs this one ContextVar rather than sharing a Context object)."""
    state = _DECISION.get()

    @functools.wraps(fn)
    def _runner(*args, **kwargs):
        if state is None:
            return fn(*args, **kwargs)
        token = _DECISION.set(state)
        try:
            return fn(*args, **kwargs)
        finally:
            _DECISION.reset(token)

    return _runner


def submit_in_decision_context(executor: Any, fn: Callable, *args, **kwargs):
    """``executor.submit`` with the caller's whole context copied into the task
    (``contextvars.copy_context().run``), so the decision state AND any capture context follow."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, functools.partial(fn, *args, **kwargs))


def resolver_from_env(environ: Optional[Any] = None) -> Optional[LiveMarketConditionResolver]:
    """``LiveMarketConditionResolver`` for ``BA2_MARKET_CONDITION_PROFILE``, or ``None`` when the
    variable is unset/empty/``none``. An unregistered profile name raises (loud misconfiguration)."""
    env = os.environ if environ is None else environ
    raw = (env.get(PROFILE_ENV) or "").strip()
    if not raw or raw.lower() == "none":
        return None
    from ba2_common.core.market_conditions import PROFILES

    if raw not in PROFILES:
        raise ValueError(f"{PROFILE_ENV}={raw!r} is not a registered market-condition profile "
                         f"({sorted(PROFILES)!r})")
    return LiveMarketConditionResolver(raw)
