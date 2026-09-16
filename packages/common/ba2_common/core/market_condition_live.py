"""Live market-condition context: one decision time per analysis, carried by a ContextVar.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` section 4.1 -- "The
live adapter reads ``replay_now()`` once on the coordinating thread, then passes that value into
any worker fan-out. Different analyses cannot share a mutable global clock."

* The host installs a :class:`PerInstanceMarketConditionResolver` into the ``TradeConditions``
  seam. The profile is an EXPERT SETTING (``market_condition_profile``, plan Task 12), so the
  dispatcher builds and caches one :class:`LiveMarketConditionResolver` per expert instance that
  names one; an expert with an empty setting gets none and its market leaves read ``no_context``
  exactly as they do on a platform with the feature off.
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
the platform -- exits and protective-order handling have to keep running. The dispatcher
then serves an :class:`UncertifiedSourceResolver` (a failed VERDICT) or an
:class:`UnreadableSourceResolver` (certification that could not run at all -- a corrupt parquet,
a non-midnight ``Date`` label: those RAISE rather than reporting ``unavailable``): it resolves
NO context for every leaf, its
``no_context_reason`` is the certification summary (carried by ``TradeConditions``' once-per-field
WARNING), and one ERROR naming the failing symbols is logged when that expert's resolver is first
built (certification is paid lazily, on the first expert whose setting names a profile, so a
platform with the gates off everywhere never opens the cache at all). Every gated entry is
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
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from ba2_common.core.market_calendar import NY_TZ, prior_regular_session
from ba2_common.core.market_condition_context import (
    TIMING_POLICY_PRIOR_SESSION_V1,
    MarketConditionContext,
    MarketConditionReader,
)
from ba2_common.core.failure_modes import absorb_if_benign
from ba2_common.core.instance_resolver import InstanceResolverNotConfigured
from ba2_common.core.market_condition_reader import coverage_detail, missing_coverage
from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY

__all__ = [
    "PROFILE_ENV_RETIRED",
    "MANIFEST_ENV",
    "DecisionState",
    "LiveMarketConditionResolver",
    "PerInstanceMarketConditionResolver",
    "current_decision",
    "market_condition_decision_scope",
    "run_in_decision_context",
    "submit_in_decision_context",
    "assert_profile_env_retired",
    "manifest_digests_from_env",
    "resolver_for_profiles",
    "resolver_for_expert_instance",
    "SourceCertificationError",
    "UncertifiedSourceResolver",
    "UnreadableSourceResolver",
    "NO_DECISION_SCOPE_REASON",
    "UNIVERSE_SENTINELS",
    "gated_expert_instances",
    "market_condition_fields_in_ruleset",
    "gated_live_universe",
]

#: RETIRED (plan Task 12, operator decision 2026-09-16). The live profile used to be this
#: process-wide environment variable; it is now the expert setting ``market_condition_profile``
#: (``market_condition_rules.PROFILE_SETTING``), read by live and by backtests from the same
#: place, so an expert carries its gates' data supply the way it carries its ruleset.
#:
#: The name is kept ONLY so a set value can FAIL. A deploy script that still exports it would
#: otherwise say nothing at all: the variable is read nowhere, every expert's setting decides,
#: and an operator who believed the export was doing something would be running whatever the
#: settings rows happen to hold. See :func:`assert_profile_env_retired`.
PROFILE_ENV_RETIRED = "BA2_MARKET_CONDITION_PROFILE"

#: The prepared manifest digest(s) the live instance serves (design section 4.5: "Scheduled
#: analysis consumes a pinned manifest"). Set -> the reader serves that published snapshot and
#: calculates nothing. Unset -> the reader computes on a miss from the FMP cache, which is
#: research/dev behaviour and is logged ONCE per process by ``warn_research_mode``.
#:
#: STILL AN ENVIRONMENT VARIABLE, unlike the profile, and deliberately: a manifest is DATA
#: IDENTITY (which warmed snapshot this host serves), not a property of a strategy -- the same
#: split the backtest keeps between the per-expert profile setting and the job-level
#: ``market_condition_manifests`` pin. Two shapes, both parsed by
#: :func:`manifest_digests_from_env`: a bare digest (only meaningful when the expert names ONE
#: profile) or ``profile=digest`` pairs, comma-separated.
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


def _profile_tuple(profile: Any) -> Tuple[str, ...]:
    """``profile`` as a tuple of names: one string stays one name, a sequence is taken in order.

    A plain string is NOT split on commas here. The comma list is the SETTING's spelling and it
    has exactly one parser (``market_condition_rules.parse_profile_setting``, which refuses an
    unregistered or repeated name); splitting it a second time in this module is how the two
    would eventually disagree.
    """
    if isinstance(profile, str):
        return (profile,)
    names = tuple(str(p) for p in profile)
    if len(set(names)) != len(names):
        raise ValueError(f"market-condition profiles repeat a name: {list(names)!r}")
    return names


def _reader_profiles(reader: Any) -> Tuple[str, ...]:
    """The profiles a reader serves: a composite's ``profiles``, else its single ``profile``."""
    plural = getattr(reader, "profiles", None)
    if plural is not None:
        return tuple(plural)
    return (reader.profile,)


def _fmp_cache_reader(profile: str, cache_root: Optional[str] = None, *,
                      manifest_digest: Optional[str] = None) -> Any:
    """One profile's live reader. Indirection so a test can build resolvers without a cache."""
    from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader

    if cache_root is None:
        return FMPCacheMarketConditionReader(profile, manifest_digest=manifest_digest)
    return FMPCacheMarketConditionReader(profile, cache_root, manifest_digest=manifest_digest)


def assert_profile_env_retired(environ: Optional[Any] = None) -> None:
    """FAIL if the retired ``BA2_MARKET_CONDITION_PROFILE`` is set (plan Task 12).

    Nothing reads the variable any more: the profile is the expert setting
    ``market_condition_profile``. Ignoring a stale export would leave an operator believing the
    platform is gated the way the environment says while every expert's setting decides on its
    own -- so the process refuses to finish wiring instead.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(PROFILE_ENV_RETIRED) or "").strip()
    if not raw:
        return
    from ba2_common.core.market_condition_rules import PROFILE_SETTING

    raise RuntimeError(
        f"{PROFILE_ENV_RETIRED}={raw!r} is set, but the market-condition profile is an expert "
        f"setting now ({PROFILE_SETTING} on each ExpertInstance, chosen in the Settings page and "
        f"carried by the deploy payload). Nothing reads this variable: leaving it set would say "
        f"the platform is gated when the settings rows are what decide. Unset it, and set "
        f"{PROFILE_SETTING} on the experts that should be gated.")


def manifest_digests_from_env(profiles: Sequence[str],
                              environ: Optional[Any] = None) -> Dict[str, str]:
    """``{profile: digest}`` from :data:`MANIFEST_ENV`, for the profiles an expert names.

    Two accepted shapes, mirroring the backtest seam's singular/plural manifest pins:

    * ``<digest>`` -- a bare digest, valid only when exactly one profile is served (a manifest
      names the ONE profile it was warmed for, so a bare digest for two profiles is refused
      rather than silently applied to both);
    * ``<profile>=<digest>,<profile>=<digest>`` -- explicit pairs. A pair for a profile the
      expert does not serve is refused: nothing would read it, while the digest suggests the
      snapshot is in use.

    Unset/empty -> ``{}`` (research mode; the reader computes on a miss and warns once).
    """
    env = os.environ if environ is None else environ
    raw = (env.get(MANIFEST_ENV) or "").strip()
    profiles = tuple(profiles)
    if not raw:
        return {}
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if any("=" in t for t in tokens):
        pins: Dict[str, str] = {}
        for token in tokens:
            if "=" not in token:
                raise ValueError(f"{MANIFEST_ENV}={raw!r} mixes bare digests with "
                                 f"profile=digest pairs; use one shape")
            name, _, digest = token.partition("=")
            name, digest = name.strip(), digest.strip()
            if name not in profiles:
                raise ValueError(f"{MANIFEST_ENV} pins profile {name!r}, which this expert does "
                                 f"not serve (profiles: {list(profiles)!r}): nothing would read "
                                 f"that snapshot.")
            if not digest:
                raise ValueError(f"{MANIFEST_ENV}={raw!r} has no digest for {name!r}")
            pins[name] = digest
        return pins
    if len(tokens) != 1 or len(profiles) != 1:
        raise ValueError(
            f"{MANIFEST_ENV}={raw!r} is a bare digest but the expert serves "
            f"{len(profiles)} profile(s) {list(profiles)!r}. A manifest names the ONE profile it "
            f"was warmed for: pin it as profile=digest pairs.")
    return {profiles[0]: tokens[0]}


class UncertifiedSourceResolver:
    """Installed instead of a live resolver when the served cache failed certification: resolves
    no context, ever, and says why. Opening a decision scope with it is a no-op (no clock read)."""

    def __init__(self, profile: Any, report: Any, *, source_profile: str = SOURCE_PROFILE_FMP_DAILY):
        self.profiles = _profile_tuple(profile)
        self.profile = ",".join(self.profiles)
        self.source_profile = source_profile
        self.report = report
        self.failing_symbols = tuple(c.symbol for c in report.symbols if not c.consistent)
        self.no_context_reason = (
            f"market-condition gates DISABLED: {SourceCertificationError(report)}")

    def __call__(self, account: Any, instrument_name: str, expert_recommendation: Any) -> None:
        return None

    def no_context_reason_for(self, symbol: Any) -> Optional[str]:
        """No per-SYMBOL reason: certification is a property of the whole cache, so every symbol
        gets the same class-level :attr:`no_context_reason`. Defined rather than left absent so
        the dispatcher can ask any resolver it holds the same question."""
        return None


class UnreadableSourceResolver:
    """Installed when split certification could not RUN at all: resolves no context, ever.

    ``certify_source_columns`` returns an ``unavailable`` verdict for a cache file that is merely
    MISSING, but it RAISES for one it cannot make sense of -- an unreadable/corrupt parquet, a
    ``Date`` column that is not midnight-aligned (``market_condition_source.read_fmp_daily_cache``).
    Before this class that exception escaped :meth:`PerInstanceMarketConditionResolver._build`,
    and since Task 12 moved certification out of startup and into the live entry pass, it would
    have aborted ``process_expert_recommendations_after_analysis`` for that expert on EVERY pass,
    with nothing cached and no one line saying why.

    So a certification that cannot run degrades exactly like one that fails: every gated entry is
    refused with the reason, exits and protective-order handling are untouched, and the answer is
    cached so the fault is reported once rather than re-raised per leaf.
    """

    def __init__(self, profile: Any, error: BaseException, *, cache_root: Any = None,
                 source_profile: str = SOURCE_PROFILE_FMP_DAILY):
        self.profiles = _profile_tuple(profile)
        self.profile = ",".join(self.profiles)
        self.source_profile = source_profile
        self.error = error
        self.cache_root = cache_root
        self.no_context_reason = (
            f"market-condition gates DISABLED: the OHLCV cache under {cache_root!r} could not be "
            f"certified for source profile {source_profile} -- {type(error).__name__}: {error}")

    def __call__(self, account: Any, instrument_name: str, expert_recommendation: Any) -> None:
        return None

    def no_context_reason_for(self, symbol: Any) -> Optional[str]:
        """No per-SYMBOL reason: an unreadable cache is a property of the cache, not of a symbol."""
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


def market_condition_fields_in_ruleset(ruleset_id: Any) -> Tuple[Tuple[str, str], ...]:
    """``(label, field)`` for every market-condition leaf in a PERSISTED live ruleset.

    Persisted rules are ``EventAction.triggers`` -- ``{"cond_0": {"event_type": ...}}`` -- NOT
    the condition trees ``iter_market_condition_leaves`` walks, which key on ``field``. The two
    vocabularies meet because every market field's ``ExpertEventType`` VALUE is the field name
    (pinned by ``rule_builders.register_market_condition_field_events``), so the membership test
    is the trigger's ``event_type`` against ``market_condition_fields()``.

    The label is ``<action name>.<trigger key>`` so a refusal points at something the operator
    can find on the rules page. ``ruleset_id`` of ``None`` yields nothing (no ruleset assigned).
    """
    from ba2_common.core.db import ruleset_event_actions
    from ba2_common.core.market_condition_rules import market_condition_fields

    if ruleset_id is None:
        return ()
    fields = market_condition_fields()
    found: list = []
    for action in ruleset_event_actions(ruleset_id):
        for key, trigger in (action.triggers or {}).items():
            if isinstance(trigger, Mapping) and trigger.get("event_type") in fields:
                found.append((f"{action.name}.{key}", str(trigger["event_type"])))
    return tuple(found)


def gated_expert_instances() -> Tuple[int, ...]:
    """Ids of the ENABLED expert instances whose ENTER-MARKET ruleset carries a market leaf.

    Only the enter-market ruleset is scanned because that is the only ruleset a market leaf may
    live on (``market_condition_rules.assert_no_market_conditions`` refuses one on an exit /
    open-positions ruleset, and the whole point of that refusal is that the live resolver has no
    context outside the entry decision pass).

    The leaf walk itself is :func:`market_condition_fields_in_ruleset` (persisted rules speak
    ``EventAction.triggers``, not condition trees).
    """
    from ba2_common.core.db import get_all_instances
    from ba2_common.core.models import ExpertInstance

    found: list = []
    for instance in get_all_instances(ExpertInstance):
        if not instance.enabled or not instance.enter_market_ruleset_id:
            continue
        if market_condition_fields_in_ruleset(instance.enter_market_ruleset_id):
            found.append(int(instance.id))
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

    def __init__(self, profile: Any, *, reader: Optional[Any] = None,
                 source_profile: str = SOURCE_PROFILE_FMP_DAILY,
                 manifest_digest: Optional[str] = None):
        """``profile`` is ONE registered name or a sequence of them (Task 12: an expert's
        ``market_condition_profile`` setting may list more than one). ``reader`` must serve
        exactly those profiles -- a single reader for one, a
        ``CompositeMarketConditionReader`` for several; build it with
        :func:`resolver_for_profiles` rather than by hand."""
        from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader

        profiles = _profile_tuple(profile)
        if len(profiles) != 1 and reader is None:
            raise ValueError(f"a default FMP-cache reader serves ONE profile; {list(profiles)!r} "
                             f"needs resolver_for_profiles to build one reader per profile")
        self.reader = reader if reader is not None else FMPCacheMarketConditionReader(
            profiles[0], manifest_digest=manifest_digest)
        self.manifest_digest = manifest_digest
        served = _reader_profiles(self.reader)
        if served != profiles:
            raise ValueError(f"reader serves profile(s) {list(served)!r}, resolver wants "
                             f"{list(profiles)!r}")
        self.profiles = profiles
        #: The comma-joined name, for logs and for a single-profile resolver's ``.profile``
        #: (unchanged for every existing caller and test).
        self.profile = ",".join(profiles)
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
    def mapped_readers(self) -> Tuple[Optional[Any], ...]:
        """Each served profile's pinned snapshot, in profile order; ``None`` where that profile
        is in research mode (no manifest pinned, the reader computes on a miss).

        PLURAL, and there is no singular counterpart on purpose. A composite reader's
        ``mapped_reader`` raises ``TypeError`` (Task 10 review finding): every host-side coverage
        check in this codebase used to be written ``getattr(reader, "mapped_reader", None)``
        followed by "None means research mode, nothing to check", which for a two-profile
        resolver would report a clean bill of health for snapshots it never opened. So the
        composite is asked its own plural question, and a single reader is wrapped into a
        one-tuple here. The ``getattr`` below is the SINGLE-reader case only -- a reader that
        does not define ``mapped_reader`` at all (a test double, a capture wrapper) is genuinely
        in research mode, which is the same thing a ``None`` attribute means.
        """
        plural = getattr(self.reader, "mapped_readers", None)
        if plural is not None:
            return tuple(plural)
        return (getattr(self.reader, "mapped_reader", None),)

    def no_context_reason_for(self, symbol: Any) -> Optional[str]:
        """Why THIS symbol has no context, or None to fall back to the generic reason.

        Read by ``MarketConditionCompare.evaluate``: a snapshot that does not cover a symbol is
        a different failure from "no decision scope is open", and reporting both as the one
        class-level sentence is how an operator would miss it.
        """
        return self.uncovered.get(str(symbol).upper())

    def refresh_coverage(self, universe: Optional[Any] = None, *, force: bool = False,
                         at_install: bool = False) -> list:
        """Check every pinned snapshot against the LIVE universe; return the missing symbols.

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

        ``at_install`` downgrades the "universe unreadable" report from ERROR to a single
        WARNING. It existed for the startup install, which ran before ``init_db`` and so could
        not read the universe at all; since Task 12 the resolver is built lazily on the first
        decision pass, when the DB is open, and NO PRODUCTION CALLER passes it -- only the test
        harness does. It is kept because the distinction it encodes is real (an expected
        can't-read-yet versus coverage silently no longer being checked), and because a future
        eager-warm path would want it back; nothing in the live flow reaches that branch today.
        """
        mapped_by_profile = [(profile, mapped)
                             for profile, mapped in zip(self.profiles, self.mapped_readers)
                             if mapped is not None]
        if not mapped_by_profile:
            return []                    # no manifest pinned anywhere: nothing to check against
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
                f"against manifest(s) "
                f"{ {p: m.manifest_digest for p, m in mapped_by_profile} } in advance; an "
                f"uncovered one reads missing_session at the gate.")
        # ONE recompute at a time, and the cache key is published LAST. Two analyses can open a
        # decision scope concurrently (the UI route and the worker queue both call the entry
        # pass); setting the key first would let the second pass see "already checked" and read
        # the PREVIOUS ``uncovered`` -- usually empty -- so its uncovered symbols would report
        # the generic reason instead of the coverage one, intermittently.
        # PER PROFILE. Each profile is its own warmed snapshot with its own symbol set, so
        # "is this universe covered" has one answer per profile and a symbol that ANY pinned
        # profile misses is uncovered: the leaves of that profile would read nothing for it,
        # which is the silent subset this check exists to break.
        with self._coverage_lock:
            if key == self._checked_universe and not force:
                return [s for s in key if s in self.uncovered]
            reasons: Dict[str, list] = {}
            missing_by_profile: list = []
            for profile, mapped in mapped_by_profile:
                missing_here = missing_coverage(mapped, key)
                if missing_here:
                    missing_by_profile.append((profile, mapped, missing_here))
                for symbol in missing_here:
                    reasons.setdefault(symbol, []).append(
                        f"market-condition manifest {mapped.manifest_digest} has no rows for "
                        f"{symbol}: its gates are unknown and refuse the entry rather than "
                        f"passing unmeasured")
            missing = [s for s in key if s in reasons]
            self.uncovered = {symbol: "; ".join(why) for symbol, why in reasons.items()}
            self._checked_universe = key
        if missing:
            from ba2_common.logger import logger

            for profile, mapped, missing_here in missing_by_profile:
                detail = coverage_detail(mapped, missing_here)
                for symbol in missing_here:
                    if (profile, symbol) in self._coverage_reported:
                        continue
                    self._coverage_reported.add((profile, symbol))
                    logger.error(
                        f"market-condition manifest {mapped.manifest_digest} does not cover "
                        f"{symbol} ({len(missing_here)} of {len(key)} live instruments uncovered"
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


def resolver_for_expert_instance(expert_instance_id: Optional[Any]
                                 ) -> Optional["LiveMarketConditionResolver"]:
    """The live resolver that serves ``expert_instance_id``'s gates, or ``None``.

    ONE reader of the seam for the two live callers (the decision scope and TradeManager's
    replay-capture scope), so "is this expert gated" is answered the same way in both:

    * a :class:`PerInstanceMarketConditionResolver` (what ``wire_all_seams`` installs) is asked
      for THIS expert -- the profile is its setting, so there is no process-wide answer, and an
      id of ``None`` is a wiring defect rather than a reason to pick some other expert's
      resolver;
    * a :class:`LiveMarketConditionResolver` installed directly (a test, a benchmark) is used
      as-is;
    * anything else -- nothing installed, an ``UncertifiedSourceResolver``, a bare callable --
      is ``None``: no clock is read and no capture bundle is opened, which is exactly the
      behaviour an expert with no usable market-condition data must have.
    """
    from ba2_common.core.TradeConditions import get_market_condition_context_resolver

    resolver = get_market_condition_context_resolver()
    if isinstance(resolver, PerInstanceMarketConditionResolver):
        if expert_instance_id is None:
            raise ValueError(
                "the market-condition profile is an expert setting, so resolving one needs the "
                "expert_instance_id whose decision pass this is; there is no process-wide "
                "resolver to fall back on.")
        resolver = resolver.resolver_for(expert_instance_id)
    if isinstance(resolver, LiveMarketConditionResolver):
        return resolver
    return None


@contextmanager
def market_condition_decision_scope(*, expert_instance_id: Optional[Any] = None,
                                    replay_reader: Optional[Any] = None
                                    ) -> Iterator[Optional[DecisionState]]:
    """Open one decision pass for ONE expert instance. A no-op (no clock read, yields ``None``)
    unless that expert has a live market-condition resolver.

    ``expert_instance_id`` is what selects the resolver, because the profile is that expert's
    setting (plan Task 12): the caller is the entry pass, which already knows whose
    recommendations it is about to evaluate. It is REQUIRED when the installed seam is a
    :class:`PerInstanceMarketConditionResolver` -- guessing would mean opening a pass under some
    other expert's profile, and silently serving one strategy's gates from another's snapshot is
    worse than refusing. A directly installed :class:`LiveMarketConditionResolver` (a test, a
    benchmark) is used as-is and ignores the id.

    Nested scopes reuse the OUTER state (same decision time, same context, no second clock read):
    one decision pass has one clock, however many helpers open a scope inside it."""
    resolver = resolver_for_expert_instance(expert_instance_id)
    if resolver is None:
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


def certify_cache_root(cache_root: Optional[str] = None) -> Tuple[str, Any]:
    """``(root, report)`` for the FMP cache the live readers will read, MEMOISED per root.

    Certification is two parquet reads (``certify_source_columns``) and it proves the SOURCE the
    rows are computed from is the one this installation believes in. It is paid ONCE per cache
    root per process, on the first expert whose ``market_condition_profile`` is non-empty -- so a
    platform with the gates off everywhere pays nothing, which is the same "off costs nothing"
    contract the rest of this module keeps.

    ``cache_root=None`` means ``native_cache.CACHE_FOLDER``, the root the default
    ``FMPCacheMarketConditionReader`` resolves its files under; the RESOLVED root is returned and
    is what the readers are then built on, so the certified cache and the served cache cannot
    differ (which is the one thing certification is supposed to establish).
    """
    from ba2_common.core.market_condition_source import certify_source_columns

    if cache_root is None:
        from ba2_common.core import native_cache
        root = native_cache.CACHE_FOLDER
    else:
        root = cache_root
    with _CERTIFICATION_LOCK:
        cached = _CERTIFICATIONS.get(root)
    if cached is not None:
        return root, cached
    report = certify_source_columns(root)
    with _CERTIFICATION_LOCK:
        _CERTIFICATIONS[root] = report
    return root, report


def clear_certification_cache() -> None:
    """Forget every memoised certification, so the next gated expert re-reads the cache.

    Wired into the whole-process ``/api/reload`` branch
    (``ba2_trade_platform.core.instance_registry.drop_market_condition_resolver``): a failed or
    unreadable cache is answered ONCE and then cached with the refusing resolver, so without this
    an operator who repaired the cache would have to restart the platform to have it re-read.
    Also used by tests that write a new cache under the same root.
    """
    with _CERTIFICATION_LOCK:
        _CERTIFICATIONS.clear()


def resolver_for_profiles(profiles: Sequence[str], *,
                          source_profile: str = SOURCE_PROFILE_FMP_DAILY,
                          manifest_digests: Optional[Mapping[str, str]] = None,
                          cache_root: Optional[str] = None) -> "LiveMarketConditionResolver":
    """A live resolver serving ``profiles``: ONE reader per profile, joined by
    ``market_condition_reader_for`` (the reader itself for a single profile, a
    ``CompositeMarketConditionReader`` for several -- the same join the backtest seam makes, so
    the two sides read a multi-profile genome identically).

    ``manifest_digests`` pins each profile's published snapshot; a profile without one computes
    on a miss and warns once (research/dev). Certification is NOT done here -- the caller
    (:class:`PerInstanceMarketConditionResolver`) certifies the root once and degrades to an
    :class:`UncertifiedSourceResolver` when it fails, so a failure refuses every gate loudly
    instead of being re-decided per expert.

    Raises:
        ValueError: no profile at all, or a digest for a profile that is not served.
    """
    from ba2_common.core.market_condition_readers import market_condition_reader_for

    names = _profile_tuple(profiles)
    if not names:
        raise ValueError("resolver_for_profiles needs at least one profile; an expert with an "
                         "empty market_condition_profile setting gets NO resolver, not an empty "
                         "one (its market leaves then read no_context, as on a platform with the "
                         "feature off)")
    digests = dict(manifest_digests or {})
    extra = sorted(set(digests) - set(names))
    if extra:
        raise ValueError(f"manifest digest(s) pinned for profile(s) {extra!r} this resolver does "
                         f"not serve (profiles: {list(names)!r}): nothing would read them.")
    readers = [_fmp_cache_reader(name, cache_root, manifest_digest=digests.get(name))
               for name in names]
    return LiveMarketConditionResolver(
        names, reader=market_condition_reader_for(readers), source_profile=source_profile,
        manifest_digest=digests.get(names[0]) if len(names) == 1 else None)


#: ``cache root -> certification report``, with its lock. Certification is two parquet reads and
#: its answer is a property of the cache, not of the expert asking.
_CERTIFICATIONS: Dict[str, Any] = {}
_CERTIFICATION_LOCK = threading.Lock()

#: The ``(instance id, resolver)`` the LAST dispatch on THIS thread selected (resolver ``None``
#: when the expert has no profile). Read ONLY by the dispatcher's reason accessors, which
#: ``TradeConditions`` calls immediately after ``__call__`` returned None on the same thread: the
#: reason for "no context" depends on WHICH expert was evaluated, and the seam's
#: ``no_context_reason_for(symbol)`` is handed a symbol only. A ContextVar rather than a global
#: so two concurrent decision passes cannot read each other's reason.
_LAST_DISPATCH: contextvars.ContextVar[Optional[Tuple[Any, Any]]] = contextvars.ContextVar(
    "ba2_market_condition_last_dispatch", default=None)


class PerInstanceMarketConditionResolver:
    """The live ``TradeConditions`` resolver: one :class:`LiveMarketConditionResolver` per EXPERT
    INSTANCE that names a ``market_condition_profile`` (plan Task 12).

    Installed unconditionally by ``wire_all_seams``; building it touches no cache, reads no
    settings and certifies nothing. The first expert whose setting is non-empty pays the
    certification (once per cache root) and gets its own resolver, cached under
    ``(instance_id, profiles)``; an expert whose setting is empty gets ``None``, and its market
    leaves read ``no_context`` exactly as on a platform with the feature off. A ruleset carrying
    such a leaf is refused at settings-save / deploy-import time by
    ``market_condition_rules.assert_market_fields_served`` rather than deployed unable to enter.

    The cache key carries the PROFILES as well as the id, so a settings change cannot be served
    from a stale resolver even if an invalidation is missed. The invalidations
    (:meth:`clear_cache`) are wired to ``/api/reload`` and to the live instance invalidation a
    settings save triggers all the same, because a changed MANIFEST or cache root does not change
    the key.
    """

    def __init__(self, *, source_profile: str = SOURCE_PROFILE_FMP_DAILY,
                 cache_root: Optional[str] = None,
                 environ: Optional[Any] = None):
        self.source_profile = source_profile
        self.cache_root = cache_root
        self._environ = environ
        self._resolvers: Dict[Any, Any] = {}
        self._lock = threading.Lock()
        self._settings_errors: set = set()

    # -- settings ---------------------------------------------------------------------------
    def profiles_for(self, expert_instance_id: Any) -> Tuple[str, ...]:
        """The profiles this expert's setting names (``()`` when it is empty).

        Reads the expert through the instance-resolver seam -- the same accessor
        :func:`gated_live_universe` uses, which the live host backs with
        ``get_expert_instance_from_id`` and its instance/settings caches. An unreadable instance
        or an unparseable setting is reported ONCE per distinct cause and treated as "no
        profile": a settings fault must not stop exits and protective-order handling, and the
        gates it would have fed then refuse every entry loudly on their own.
        """
        from ba2_common.core.instance_resolver import get_instance_resolver
        from ba2_common.core.market_condition_rules import PROFILE_SETTING, parse_profile_setting

        try:
            expert = get_instance_resolver().get_expert_instance(int(expert_instance_id))
            return parse_profile_setting(expert.settings.get(PROFILE_SETTING))
        except Exception as e:  # noqa: BLE001 -- named below; a settings fault never stops the pass
            from ba2_common.logger import logger

            # The house convention (failure_modes: deny by default under BA2_ERROR_MODE=enforce).
            # What this path LEGITIMATELY sees: ValueError from parse_profile_setting (an
            # unregistered or repeated profile name -- the whole point of reporting and refusing
            # here), InstanceNotFound/LookupError for an instance deleted between the
            # recommendation and the pass, and InstanceResolverNotConfigured (a RuntimeError) in a
            # package-only process with no host wired. A TypeError or AttributeError is a defect
            # in the resolver seam and must NOT be downgraded to "this expert has no profile".
            absorb_if_benign(e, ValueError, LookupError, InstanceResolverNotConfigured)
            key = (expert_instance_id, str(e))
            if key not in self._settings_errors:
                self._settings_errors.add(key)
                logger.error(
                    f"market-condition profile for expert instance {expert_instance_id} could "
                    f"not be read ({e}): its market-condition gates are unknown and will refuse "
                    f"every entry until this clears. Every other rule is unaffected.")
            return ()

    # -- dispatch ---------------------------------------------------------------------------
    def resolver_for(self, expert_instance_id: Any) -> Optional[Any]:
        """This expert's resolver, built and cached on first use; ``None`` for an empty setting.

        May return an :class:`UncertifiedSourceResolver` (the cache failed split certification)
        or an :class:`UnreadableSourceResolver` (certification could not run -- a corrupt parquet
        or a non-midnight ``Date`` label raises instead of reporting a verdict). Both are cached
        like any other answer, so the fault is one ERROR rather than one per leaf, and both
        refuse every gated entry with their reason while exits keep running (module DECISION
        2026-09-16).

        RAISES on a malformed :data:`MANIFEST_ENV` -- deliberately, and unlike a settings fault.
        A manifest is the host's ops configuration, not a strategy's preference, and the quiet
        readings of a broken one are both unacceptable: ignoring it computes the indicators live
        per decision (design 4.5 forbids that silently) and guessing a mapping serves one
        profile's snapshot for another. Only the ENTRY pass reaches here, so exits and
        protective-order handling keep running either way.
        """
        profiles = self.profiles_for(expert_instance_id)
        if not profiles:
            return None
        key = (int(expert_instance_id), profiles)
        with self._lock:
            hit = self._resolvers.get(key)
        # ``_build`` never returns None, so a plain ``.get`` is unambiguous. The empty-setting
        # answer is not cached at all: ``profiles_for`` returned above, and re-reading a settings
        # dict the host already caches is cheaper than a second cache to invalidate.
        if hit is not None:
            return hit
        resolver = self._build(profiles)
        with self._lock:
            # Another thread may have built it first; either object is correct, and keeping the
            # one already published means concurrent passes share a reader (and its memo).
            resolver = self._resolvers.setdefault(key, resolver)
        return resolver

    def _build(self, profiles: Tuple[str, ...]) -> Any:
        from ba2_common.logger import logger

        try:
            root, report = certify_cache_root(self.cache_root)
        except Exception as e:  # noqa: BLE001 -- see UnreadableSourceResolver
            # CERTIFICATION COULD NOT RUN (a corrupt parquet, a Date column that is not
            # midnight-aligned). An ``unavailable`` VERDICT comes back as a report; these RAISE.
            # Letting that escape would abort the whole enter-market pass for this expert on
            # every schedule, uncached and unsummarised -- so it degrades exactly like a failed
            # verdict, and the cached answer means one ERROR rather than one per leaf.
            absorb_if_benign(e, ValueError, LookupError)
            degraded = UnreadableSourceResolver(profiles, e, cache_root=self.cache_root,
                                                source_profile=self.source_profile)
            logger.error(
                f"market-condition profile(s) {list(profiles)}: the OHLCV cache could not be "
                f"certified at all ({type(e).__name__}: {e}). Every gated entry for this expert "
                f"is refused with that reason; exits and protective-order handling are "
                f"unaffected. Repair the cache and restart (or POST /api/reload) to re-certify.",
                exc_info=True)
            return degraded
        if not report.consistent:
            degraded = UncertifiedSourceResolver(profiles, report,
                                                 source_profile=self.source_profile)
            logger.error(
                f"market-condition profile(s) {list(profiles)}: source {report.source_profile} "
                f"FAILED certification for {', '.join(degraded.failing_symbols)} under "
                f"{report.cache_root}; market-condition gates will refuse every entry. "
                f"{SourceCertificationError(report)}")
            return degraded
        digests = manifest_digests_from_env(profiles, self._environ)
        resolver = resolver_for_profiles(profiles, source_profile=self.source_profile,
                                         manifest_digests=digests, cache_root=root)
        # Best effort, and only now: this runs on the first decision pass for the expert, when
        # the DB is open, so an operator finds the uncovered symbols in the log before the pass
        # decides rather than after a week of a sleeve not entering.
        resolver.refresh_coverage()
        logger.info(
            f"market-condition resolver built for profile(s) {list(profiles)} (source "
            f"{resolver.source_profile}, manifests {digests or 'none -- research mode'})")
        return resolver

    def clear_cache(self, expert_instance_id: Optional[Any] = None) -> None:
        """Drop the cached resolver(s) so the next decision re-reads the setting.

        Called from ``/api/reload`` (which drops the instance + settings caches this reads
        through) and from the live instance invalidation a settings save triggers. Without it a
        profile changed in the UI would take effect only at the next process restart, while the
        page said otherwise.
        """
        with self._lock:
            if expert_instance_id is None:
                self._resolvers.clear()
                self._settings_errors.clear()
                return
            wanted = int(expert_instance_id)
            for key in [k for k in self._resolvers if k[0] == wanted]:
                del self._resolvers[key]
            self._settings_errors = {k for k in self._settings_errors
                                     if k[0] != expert_instance_id}

    # -- the TradeConditions seam ------------------------------------------------------------
    #: Surfaced by ``TradeConditions`` when the evaluation carried no expert recommendation, so
    #: there is no instance whose setting could decide.
    no_context_reason = (
        "no expert recommendation reached this market-condition leaf, so the expert instance "
        "whose market_condition_profile decides the gate is unknown: the gate is unknown and "
        "never passes")

    def __call__(self, account: Any, instrument_name: str,
                 expert_recommendation: Any) -> Optional[MarketConditionContext]:
        instance_id = getattr(expert_recommendation, "instance_id", None)
        if instance_id is None:
            _LAST_DISPATCH.set((None, None))
            return None
        resolver = self.resolver_for(instance_id)
        _LAST_DISPATCH.set((instance_id, resolver))
        if resolver is None:
            return None
        return resolver(account, instrument_name, expert_recommendation)

    def no_context_reason_for(self, symbol: Any) -> Optional[str]:
        """Why THIS symbol got no context, for the expert the last dispatch selected.

        ``None`` falls back to :data:`no_context_reason`, which is the honest answer only for
        the "no recommendation" case; every other cause names the expert.
        """
        from ba2_common.core.market_condition_rules import PROFILE_SETTING

        instance_id, resolver = _LAST_DISPATCH.get() or (None, None)
        if instance_id is None:
            return None
        if resolver is None:
            return (f"expert instance {instance_id} has an empty {PROFILE_SETTING} setting: no "
                    f"market-condition data is served for it, so this gate is unknown and never "
                    f"passes")
        per_symbol = resolver.no_context_reason_for(symbol)
        if per_symbol:
            return per_symbol
        return resolver.no_context_reason
