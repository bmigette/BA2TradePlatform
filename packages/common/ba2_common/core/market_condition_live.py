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

Certification failure (DECISION 2026-09-16): a cache that fails split certification must NOT stop
the platform -- exits and protective-order handling have to keep running. ``resolver_from_env``
then returns an :class:`UncertifiedSourceResolver`: it resolves NO context for every leaf, its
``no_context_reason`` is the certification summary (carried by ``TradeConditions``' once-per-field
WARNING), and one ERROR naming the failing symbols is logged at install. Every gated entry is
refused loudly; nothing else changes.

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
from typing import Any, Callable, Iterator, Mapping, Optional, Tuple

from ba2_common.core.market_calendar import NY_TZ, prior_regular_session
from ba2_common.core.market_condition_context import (
    TIMING_POLICY_PRIOR_SESSION_V1,
    MarketConditionContext,
    MarketConditionReader,
)
from ba2_common.core.market_condition_reader import coverage_detail, missing_coverage
from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY

__all__ = [
    "PROFILE_ENV",
    "MANIFEST_ENV",
    "DecisionState",
    "LiveMarketConditionResolver",
    "current_decision",
    "market_condition_decision_scope",
    "run_in_decision_context",
    "submit_in_decision_context",
    "resolver_from_env",
    "SourceCertificationError",
    "UncertifiedSourceResolver",
    "NO_DECISION_SCOPE_REASON",
    "UNIVERSE_SENTINELS",
    "gated_expert_instances",
    "gated_live_universe",
]

#: Environment switch for the live profile. Unset, empty or ``none`` -> nothing is installed.
#:
#: SINGLE PROFILE, deliberately. Task 10 widened the BACKTEST seam to a profile LIST (one reader
#: per profile behind ``market_condition_reader_for``); live still serves ONE, because its reader
#: is also the capture/replay tape and the coverage subject, and making those plural is its own
#: piece of work with its own recorded-identity contract. This is not a silent gap: a deployed
#: leaf whose field belongs to a profile this resolver does not serve raises ``LookupError`` in
#: ``MarketConditionCompare.evaluate`` ("the reader was not built for this field's profile"), and
#: a comma list here is refused by the reader as an unregistered profile name. Deploy a
#: multi-profile genome only after the live side is widened too.
PROFILE_ENV = "BA2_MARKET_CONDITION_PROFILE"

#: The prepared manifest digest the live instance serves (design section 4.5: "Scheduled analysis
#: consumes a pinned manifest"). Set -> the reader serves that published snapshot and calculates
#: nothing. Unset -> the reader computes on a miss from the FMP cache, which is research/dev
#: behaviour and is logged ONCE per process by ``warn_research_mode``.
MANIFEST_ENV = "BA2_MARKET_CONDITION_MANIFEST"

#: What ``get_enabled_instruments`` returns INSTEAD of symbols when the instance picks its
#: universe at analysis time. None of these can be coverage-checked ahead of a decision, so they
#: are reported once and left to the reader's honest ``missing_session``.
UNIVERSE_SENTINELS = frozenset({"EXPERT", "DYNAMIC", "SCREENER"})

#: Why a live leaf got no context: read by ``TradeConditions`` for its once-per-field WARNING.
NO_DECISION_SCOPE_REASON = (
    "market-condition leaf evaluated OUTSIDE a market_condition_decision_scope (only the "
    "enter-market pass opens one; open-positions/exit rulesets and the ruleset test page do not): "
    "the gate is unknown and never passes")


class SourceCertificationError(RuntimeError):
    """The OHLCV cache the live profile would read failed split certification."""

    def __init__(self, report: Any):
        bad = [f"{c.symbol}: basis={c.basis} close_ratio={c.close_ratio} {c.reason}".strip()
               for c in report.symbols if not c.consistent]
        super().__init__(f"source profile {report.source_profile} is NOT certified for cache "
                         f"{report.cache_root}: " + "; ".join(bad))
        self.report = report


class UncertifiedSourceResolver:
    """Installed instead of a live resolver when the served cache failed certification: resolves
    no context, ever, and says why. Opening a decision scope with it is a no-op (no clock read)."""

    def __init__(self, profile: str, report: Any, *, source_profile: str = SOURCE_PROFILE_FMP_DAILY):
        self.profile = profile
        self.source_profile = source_profile
        self.report = report
        self.failing_symbols = tuple(c.symbol for c in report.symbols if not c.consistent)
        self.no_context_reason = (
            f"market-condition gates DISABLED: {SourceCertificationError(report)}")

    def __call__(self, account: Any, instrument_name: str, expert_recommendation: Any) -> None:
        return None


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


def gated_expert_instances() -> Tuple[int, ...]:
    """Ids of the ENABLED expert instances whose ENTER-MARKET ruleset carries a market leaf.

    Only the enter-market ruleset is scanned because that is the only ruleset a market leaf may
    live on (``market_condition_rules.assert_no_market_conditions`` refuses one on an exit /
    open-positions ruleset, and the whole point of that refusal is that the live resolver has no
    context outside the entry decision pass).

    Persisted rules are ``EventAction.triggers`` -- a dict of ``{"cond_0": {"event_type": ...}}``
    -- NOT the condition trees ``iter_market_condition_leaves`` walks, which key on ``field``.
    The two vocabularies meet because every market field's ``ExpertEventType`` VALUE is the field
    name (pinned by ``rule_builders.register_market_condition_field_events``), so the membership
    test is the trigger's ``event_type`` against :func:`market_condition_fields`.
    """
    from ba2_common.core.db import get_all_instances, ruleset_event_actions
    from ba2_common.core.market_condition_rules import market_condition_fields
    from ba2_common.core.models import ExpertInstance

    fields = market_condition_fields()
    found: list = []
    for instance in get_all_instances(ExpertInstance):
        if not instance.enabled or not instance.enter_market_ruleset_id:
            continue
        for action in ruleset_event_actions(instance.enter_market_ruleset_id):
            triggers = action.triggers or {}
            if any(isinstance(t, Mapping) and t.get("event_type") in fields
                   for t in triggers.values()):
                found.append(int(instance.id))
                break
    return tuple(found)


def gated_live_universe() -> Tuple[Tuple[str, ...], Tuple[Tuple[int, str], ...]]:
    """``(symbols, deferred)`` for every gated instance: the union of their enabled instruments,
    and the ``(instance_id, sentinel)`` pairs whose universe is only known at analysis time.

    Built from the same accessor ``JobManager._schedule_expert_jobs`` schedules from -- the
    expert's own ``get_enabled_instruments()`` -- not a second reading of the settings rows.

    ONE KNOWN DIVERGENCE from ``JobManager._get_enabled_instruments``, recorded rather than
    silently carried: JobManager resolves ``instrument_selection_method`` through
    ``get_setting_with_interface_default`` while the interface method reads
    ``settings.get(..., "static")``. An instance whose setting row is ABSENT but whose expert
    class defaults to ``screener`` therefore looks static here, and its stale
    ``enabled_instruments`` get coverage-checked instead of being reported as deferred. The
    effect is spurious per-symbol ERRORs, never a missed refusal. The right fix lives in
    ba2_common's own accessor (which JobManager should then use); it is not a coverage change.
    """
    from ba2_common.core.instance_resolver import get_instance_resolver

    resolver = get_instance_resolver()
    symbols: set = set()
    deferred: list = []
    for instance_id in gated_expert_instances():
        expert = resolver.get_expert_instance(instance_id)
        for name in (expert.get_enabled_instruments() or ()):
            upper = str(name).upper()
            if upper in UNIVERSE_SENTINELS:
                deferred.append((instance_id, upper))
            else:
                symbols.add(upper)
    return tuple(sorted(symbols)), tuple(deferred)


class LiveMarketConditionResolver:
    """The ``TradeConditions`` market-condition resolver for the live platform."""

    #: Surfaced by ``TradeConditions`` when this resolver returns no context.
    no_context_reason = NO_DECISION_SCOPE_REASON

    def __init__(self, profile: str, *, reader: Optional[Any] = None,
                 source_profile: str = SOURCE_PROFILE_FMP_DAILY,
                 manifest_digest: Optional[str] = None):
        from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader

        self.reader = reader if reader is not None else FMPCacheMarketConditionReader(
            profile, manifest_digest=manifest_digest)
        self.manifest_digest = manifest_digest
        if self.reader.profile != profile:
            raise ValueError(f"reader serves profile {self.reader.profile!r}, resolver wants {profile!r}")
        self.profile = profile
        self.calc_version = self.reader.calc_version
        self.source_profile = source_profile
        #: clock reads taken by ``begin_decision`` (visibility for tests and diagnostics).
        self.decisions = 0
        #: ``symbol -> reason`` for every live instrument the pinned snapshot does not cover.
        #: Their gates resolve NO context (and say why) instead of reading a row that is not
        #: there -- see :meth:`refresh_coverage`.
        self.uncovered: dict = {}
        self._checked_universe: Optional[tuple] = None
        self._coverage_reported: set = set()
        self._coverage_universe_warned = False
        self._coverage_universe_errors: set = set()
        self._coverage_deferred_warned = False
        self._coverage_lock = threading.Lock()

    def begin_decision(self, *, replay_reader: Optional[Any] = None) -> DecisionState:
        """Read the evaluation clock ONCE (call on the coordinating thread) and bind the reader."""
        from ba2_common.core.replay.context import ReplayMiss, current_capture

        capture = current_capture()
        # The live universe can change between passes (an instance enabled, an instrument added),
        # so the snapshot is re-checked here rather than only at install. A pass whose universe
        # is unchanged pays one tuple compare.
        self.refresh_coverage()
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

    @property
    def mapped_reader(self) -> Optional[Any]:
        """The pinned snapshot behind this resolver's reader, or None (research mode)."""
        return getattr(self.reader, "mapped_reader", None)

    def no_context_reason_for(self, symbol: Any) -> Optional[str]:
        """Why THIS symbol has no context, or None to fall back to the generic reason.

        Read by ``MarketConditionCompare.evaluate``: a snapshot that does not cover a symbol is
        a different failure from "no decision scope is open", and reporting both as the one
        class-level sentence is how an operator would miss it.
        """
        return self.uncovered.get(str(symbol).upper())

    def refresh_coverage(self, universe: Optional[Any] = None, *, force: bool = False,
                         at_install: bool = False) -> list:
        """Check the pinned snapshot against the LIVE universe; return the missing symbols.

        THE FAILURE THIS PREVENTS is the live twin of the backtest seam's: a symbol the warmup
        could not warm has no row, every gate on it reads ``missing_session``, and the sleeve
        simply never enters it -- a strategy silently reduced to a subset of its universe, with
        nothing in the log to say which subset or why. Here each uncovered symbol gets ONE
        ERROR naming the digest, and its gates then report ``no_context`` with that reason
        instead of the generic one, so the refusal names its own cause.

        Called at install (``at_install=True``) and at every decision-scope open. THE PER-PASS
        COST IS THE UNIVERSE READ ITSELF -- one ``get_all_instances`` scan plus the rulesets and
        enabled instruments of the gated instances; the unchanged-universe short-circuit saves
        only the set difference and the logging after it. That is per decision PASS, not per
        evaluation, and it is the price of noticing an instance enabled since the last pass.
        ``universe=None`` derives it; a caller that already has the list (a test, or a host that
        knows its own universe) passes it.

        ``at_install`` exists because ``wire_all_seams`` runs BEFORE ``init_db``, so the install
        call normally cannot read the universe at all. That one failure is expected and is
        reported once. A failure at DECISION time is a different event -- the DB has gone away,
        or one expert instance raises while being built -- and it means coverage is no longer
        being checked at all, so it is reported once per distinct cause rather than swallowed
        into the install flag's budget.
        """
        mapped = self.mapped_reader
        if mapped is None:
            return []                    # no manifest pinned: nothing to check against
        deferred: tuple = ()
        if universe is None:
            try:
                universe, deferred = gated_live_universe()
            except Exception as e:  # noqa: BLE001 -- at install the DB may not be open yet
                from ba2_common.logger import logger

                if at_install:
                    if not self._coverage_universe_warned:
                        self._coverage_universe_warned = True
                        logger.warning(
                            f"market-condition coverage not checked at install: the live "
                            f"universe could not be read ({e}). This is expected when the "
                            f"database is opened after the seams are wired; it is re-checked "
                            f"when the first decision scope opens.")
                elif str(e) not in self._coverage_universe_errors:
                    self._coverage_universe_errors.add(str(e))
                    logger.error(
                        f"market-condition coverage is NOT being checked: the live universe "
                        f"could not be read during a decision pass ({e}). Until this clears, an "
                        f"uncovered symbol's gates report the generic no-context reason and "
                        f"nothing names the snapshot that is missing it.")
                return []
        key = tuple(str(s).upper() for s in universe)
        if deferred and not self._coverage_deferred_warned:
            from ba2_common.logger import logger

            self._coverage_deferred_warned = True
            logger.warning(
                f"market-condition coverage: expert instances "
                f"{sorted({i for i, _ in deferred})} pick their universe at analysis time "
                f"({sorted({s for _, s in deferred})}), so their symbols cannot be checked "
                f"against manifest {mapped.manifest_digest} in advance; an uncovered one reads "
                f"missing_session at the gate.")
        # ONE recompute at a time, and the cache key is published LAST. Two analyses can open a
        # decision scope concurrently (the UI route and the worker queue both call the entry
        # pass); setting the key first would let the second pass see "already checked" and read
        # the PREVIOUS ``uncovered`` -- usually empty -- so its uncovered symbols would report
        # the generic reason instead of the coverage one, intermittently.
        with self._coverage_lock:
            if key == self._checked_universe and not force:
                return [s for s in key if s in self.uncovered]
            missing = missing_coverage(mapped, key)
            self.uncovered = {
                symbol: (f"market-condition manifest {mapped.manifest_digest} has no rows for "
                         f"{symbol}: its gates are unknown and refuse the entry rather than "
                         f"passing unmeasured")
                for symbol in missing}
            self._checked_universe = key
        if missing:
            from ba2_common.logger import logger

            detail = coverage_detail(mapped, missing)
            for symbol in missing:
                if symbol in self._coverage_reported:
                    continue
                self._coverage_reported.add(symbol)
                logger.error(
                    f"market-condition manifest {mapped.manifest_digest} does not cover "
                    f"{symbol} ({len(missing)} of {len(key)} live instruments uncovered"
                    f"{detail}). Every gate on {symbol} is unknown, so this sleeve will not "
                    f"enter it. Warm and re-publish the snapshot for the live universe "
                    f"(tools/warm_market_conditions.py plan/build), or take the gate off that "
                    f"instance.")
        return missing

    def __call__(self, account: Any, instrument_name: str,
                 expert_recommendation: Any) -> Optional[MarketConditionContext]:
        state = _DECISION.get()
        if state is None or state.resolver is not self:
            return None
        if self.uncovered and str(instrument_name).upper() in self.uncovered:
            # Refuse rather than hand the condition a context whose reader will return None:
            # both end as "unknown, never passes", but only this one can say WHICH failure it
            # was (``no_context`` with the coverage reason, not a bare ``missing_session``).
            return None
        return state.context()


def current_decision() -> Optional[DecisionState]:
    return _DECISION.get()


@contextmanager
def market_condition_decision_scope(*, replay_reader: Optional[Any] = None) -> Iterator[Optional[DecisionState]]:
    """Open one decision pass. A no-op (no clock read, yields ``None``) unless a
    ``LiveMarketConditionResolver`` is installed in ``TradeConditions``.

    Nested scopes reuse the OUTER state (same decision time, same context, no second clock read):
    one decision pass has one clock, however many helpers open a scope inside it."""
    from ba2_common.core.TradeConditions import get_market_condition_context_resolver

    resolver = get_market_condition_context_resolver()
    if not isinstance(resolver, LiveMarketConditionResolver):
        yield None
        return
    outer = _DECISION.get()
    if outer is not None and outer.resolver is resolver:
        if replay_reader is not None and outer.reader is not replay_reader:
            raise ValueError("a nested decision scope cannot switch to a different replay reader")
        yield outer
        return
    state = resolver.begin_decision(replay_reader=replay_reader)
    token = _DECISION.set(state)
    try:
        yield state
    finally:
        _DECISION.reset(token)


def run_in_decision_context(fn: Callable) -> Callable:
    """Wrap ``fn`` so a pool thread running it sees the decision state that is current WHEN THIS
    WRAPPER IS CREATED (captured once, at wrap time -- not when ``fn`` later runs). Create the
    wrapper inside the scope, on the coordinating thread. Safe to reuse concurrently: it
    re-installs this one ContextVar rather than sharing a ``contextvars.Context`` object."""
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


def resolver_from_env(environ: Optional[Any] = None,
                      cache_root: Optional[str] = None) -> Optional[Any]:
    """``LiveMarketConditionResolver`` for ``BA2_MARKET_CONDITION_PROFILE``, or ``None`` when the
    variable is unset/empty/``none``.

    Before installing, the cache the reader will read is CERTIFIED (``certify_source_columns``:
    two parquet reads). ``cache_root`` defaults to ``native_cache.CACHE_FOLDER`` -- the root the
    default ``FMPCacheMarketConditionReader`` resolves its files under, so the certified cache is
    the served cache.

    A cache that fails certification does NOT raise: it returns an
    ``UncertifiedSourceResolver`` (gates refuse with the certification reason) and logs ONE
    ERROR naming the failing symbols -- see the module docstring's DECISION.

    Raises:
        ValueError: an unregistered profile name (loud misconfiguration).
    """
    env = os.environ if environ is None else environ
    raw = (env.get(PROFILE_ENV) or "").strip()
    if not raw or raw.lower() == "none":
        return None
    from ba2_common.core.market_conditions import PROFILES

    if raw not in PROFILES:
        raise ValueError(f"{PROFILE_ENV}={raw!r} is not a registered market-condition profile "
                         f"({sorted(PROFILES)!r})")
    from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader
    from ba2_common.core.market_condition_source import certify_source_columns

    if cache_root is None:
        from ba2_common.core import native_cache
        root = native_cache.CACHE_FOLDER
    else:
        root = cache_root
    report = certify_source_columns(root)
    if not report.consistent:
        from ba2_common.logger import logger

        degraded = UncertifiedSourceResolver(raw, report)
        logger.error(
            f"{PROFILE_ENV}={raw}: source {report.source_profile} FAILED certification for "
            f"{', '.join(degraded.failing_symbols)} under {report.cache_root}; market-condition "
            f"gates will refuse every entry. {SourceCertificationError(report)}")
        return degraded
    # The pinned snapshot, if the deployment has one. Certification still runs and still decides:
    # it proves the SOURCE the rows were computed from is the one this instance believes in, and a
    # cache that fails it degrades the gates to "refuse loudly" whether or not a manifest is set.
    digest = (env.get(MANIFEST_ENV) or "").strip() or None
    # ``root``, not ``cache_root``: root is the root that was just CERTIFIED (cache_root is None
    # for the default deployment, and the reader would then resolve its own root again -- so the
    # certified cache and the served cache could differ, which is the one thing certification is
    # supposed to establish).
    resolver = LiveMarketConditionResolver(
        raw, reader=FMPCacheMarketConditionReader(raw, root, manifest_digest=digest),
        manifest_digest=digest)
    # AT INSTALL, best effort: ``wire_all_seams`` runs before ``init_db``, so the expert/ruleset
    # read can legitimately fail here. It is not skipped in that case -- ``begin_decision``
    # re-checks on every pass -- but an operator who HAS a readable DB finds the uncovered
    # symbols in the startup log rather than after a week of a sleeve not entering.
    resolver.refresh_coverage(at_install=True)
    return resolver
