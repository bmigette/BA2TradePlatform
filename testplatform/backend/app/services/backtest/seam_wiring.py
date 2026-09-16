"""Host-side wiring of the ba2_common / ba2_experts seams for the backtest engine.

Phase 0 defined the seams (instance resolver, LLM service, TradeConditions provider
resolver, ATR indicator injection) but left them unconfigured; the live
BA2TradePlatform wires them in Phase 6. BA2TestPlatform wires its OWN
(backtest-flavoured) versions here so the inherited AccountInterface / expert /
TradeConditions / TradeRiskManagement code can resolve instances, a (loud, unused)
LLM service, and providers, all against the backtest cache.

Confirmed against the installed Phase-0 packages (NOT the plan's draft guesses):
  * ba2_common.core.instance_resolver.set_instance_resolver / get_instance_resolver,
    InstanceResolver Protocol = {get_expert_instance, get_account_instance,
    get_account_instance_from_transaction}.
  * ba2_common.core.interfaces.LLMServiceInterface.set_llm_service, LLMServiceInterface
    (ABC with create_llm + do_llm_call_with_websearch), LLMServiceNotConfigured.
  * ba2_common.core.TradeConditions.set_provider_resolver(fn) with fn(category, name, **kw).
  * ba2_providers.get_provider(category, name, **kwargs); the indicators/"pandas"
    provider (PandasIndicatorCalc) REQUIRES an ohlcv_provider in its constructor, so
    make_indicator_provider() builds it with the ohlcv/"fmp" provider (the no-arg call
    would raise TypeError -> get_provider silently falls back to a broken default).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.instance_resolver import (
    set_instance_resolver,
    InstanceResolver,  # noqa: F401  (exported for typing / Protocol checks)
)
from ba2_common.core.interfaces.LLMServiceInterface import (
    set_llm_service,
    LLMServiceInterface,
    LLMServiceNotConfigured,
)


class BacktestInstanceResolver:
    """Resolves expert / account ids to live instances for the backtest run.

    Satisfies the ba2_common ``InstanceResolver`` Protocol. The backtest engine
    constructs the ``BacktestAccount`` + expert instances and registers them here so
    the inherited AccountInterface / TradeManager-equivalent code (which calls
    ``get_instance_resolver().get_account_instance(...)`` etc.) finds them.

    Registrations are THREAD-LOCAL: the resolver is a process-wide singleton (registered
    once into ba2_common), but every backtest registers the SAME ids (account_id=1 /
    expert_id=1). When the serve runs re-runs CONCURRENTLY in worker threads, a process-global
    registry would let run B's BacktestAccount overwrite run A's under id=1 — so run A's
    inherited code resolves run B's account (its balance/positions) and the two runs corrupt
    each other (garbage / negative-equity results). Keying the registry by thread isolates
    concurrent runs; sequential trials in one thread are unaffected (each re-registers id=1).
    """

    def __init__(self) -> None:
        self._tl = threading.local()

    def _accounts_map(self) -> Dict[int, Any]:
        m = getattr(self._tl, "accounts", None)
        if m is None:
            m = self._tl.accounts = {}
        return m

    def _experts_map(self) -> Dict[int, Any]:
        m = getattr(self._tl, "experts", None)
        if m is None:
            m = self._tl.experts = {}
        return m

    @property
    def _accounts(self) -> Dict[int, Any]:
        return self._accounts_map()

    @property
    def _experts(self) -> Dict[int, Any]:
        return self._experts_map()

    # -- registration (host fills these in before driving the loop) -------------
    def register_account(self, account_id: int, instance: Any) -> None:
        self._accounts_map()[int(account_id)] = instance

    def register_expert(self, expert_id: int, instance: Any) -> None:
        self._experts_map()[int(expert_id)] = instance

    def unregister_account(self, account_id: int) -> None:
        """Drop a registration. A run/test that is finished must not leave its account
        resolvable: the registry is per THREAD, not per run, so a later run on the same
        thread would otherwise still find a dead object under an id it never registered.
        Silent on an id that is not registered -- teardown must be idempotent."""
        self._accounts_map().pop(int(account_id), None)

    def unregister_expert(self, expert_id: int) -> None:
        """The expert twin of ``unregister_account``; same reason, same idempotence."""
        self._experts_map().pop(int(expert_id), None)

    # -- InstanceResolver Protocol ----------------------------------------------
    def get_account_instance(self, account_id: int) -> Any:
        try:
            return self._accounts[int(account_id)]
        except KeyError:
            raise KeyError(
                f"BacktestInstanceResolver: no account registered for id={account_id}. "
                f"Registered accounts: {sorted(self._accounts)}"
            )

    def get_expert_instance(self, expert_id: int) -> Any:
        try:
            return self._experts[int(expert_id)]
        except KeyError:
            raise KeyError(
                f"BacktestInstanceResolver: no expert registered for id={expert_id}. "
                f"Registered experts: {sorted(self._experts)}"
            )

    def get_account_instance_from_transaction(self, transaction: Any) -> Any:
        return self.get_account_instance(transaction.account_id)


class _NoLLMService(LLMServiceInterface):
    """The clean experts (FMPEarningsDrift / FMPInsiderClusterBuy) never call an LLM.

    Configure a loud-failing service so any *accidental* LLM call during a backtest is
    caught immediately rather than silently producing a (lookahead-prone) response.
    """

    def create_llm(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured(
            "Backtest engine does not provide an LLM service "
            "(clean experts must not call LLMs)."
        )

    def do_llm_call_with_websearch(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured(
            "Backtest engine does not provide an LLM service "
            "(clean experts must not call LLMs)."
        )


_resolver: Optional[BacktestInstanceResolver] = None


def get_backtest_resolver() -> BacktestInstanceResolver:
    """Return the process-wide backtest instance resolver.

    Raises if ``wire_backtest_seams()`` has not been called yet (loud, not silent).
    """
    if _resolver is None:
        raise RuntimeError(
            "seam wiring not initialised; call wire_backtest_seams() first"
        )
    return _resolver


def wire_backtest_seams() -> BacktestInstanceResolver:
    """Install the resolver + LLM service + TradeConditions provider resolver once.

    Idempotent per process: repeated calls return the SAME resolver and do NOT
    re-register the seams. Returns the resolver so the caller can register the
    BacktestAccount / expert instances on it.
    """
    global _resolver
    if _resolver is None:
        _resolver = BacktestInstanceResolver()
        set_instance_resolver(_resolver)  # ba2_common instance-resolution seam
        set_llm_service(_NoLLMService())  # ba2_common LLM-service seam
        _wire_provider_resolver()         # TradeConditions data-access seam
    return _resolver


# Per-run OHLCV provider override (THREAD-LOCAL; set by run_daily_backtest at the start of
# each trial and cleared at the end). When set, the TradeConditions provider resolver returns
# THIS provider for any ("ohlcv", *) request — so the expert's price_at_date / data-condition
# OHLCV fetches go through the run's MemoizedOHLCVProvider (one in-memory load per worker,
# shared across the whole GA population) instead of re-reading the disk cache every bar.
#
# Thread-local, NOT a process-global: the serve runs re-runs CONCURRENTLY in worker threads, and
# a single global override let run B's MemoizedOHLCVProvider clobber run A's mid-run — so run A's
# expert read run B's prices (wrong fills, negative-equity garbage). Per-thread isolates them;
# sequential trials in one worker thread are unaffected (set at start, cleared at end).
_ohlcv_override_tl = threading.local()


def _current_ohlcv_override() -> Optional[Any]:
    return getattr(_ohlcv_override_tl, "provider", None)


def set_backtest_ohlcv_override(provider: Optional[Any]) -> None:
    """Install (or clear, with None) the per-run OHLCV provider the resolver hands experts.
    Thread-local — only affects the calling thread's backtest."""
    _ohlcv_override_tl.provider = provider


# Market-condition entry gates (design 2026-09-15 section 4.1). OPT-IN per run: only a config
# that pins at least one registered profile installs anything, and only then is the adapter
# module imported. A run may pin MORE THAN ONE (Task 10) -- one reader each, joined by
# ``market_condition_reader_for``. The TradeConditions seam is process-global while backtests run
# CONCURRENTLY in worker threads (see the OHLCV override above), so the process gets ONE
# dispatching resolver and each run's resolver lives in THIS thread's slot; a thread without a
# run resolver resolves None (the gate reports ``no_context``).
MARKET_CONDITION_PROFILE_NONE = "none"
_market_condition_tl = threading.local()
_log = logging.getLogger(__name__)


def _current_market_condition_resolver() -> Optional[Any]:
    return getattr(_market_condition_tl, "resolver", None)


def _dispatch_market_condition_context(account: Any, instrument_name: str,
                                       expert_recommendation: Any) -> Optional[Any]:
    resolver = _current_market_condition_resolver()
    if resolver is None:
        return None
    return resolver(account, instrument_name, expert_recommendation)


def market_condition_profile_setting(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """``(the setting is present, the profiles it names)`` from the run's EXPERT SETTINGS.

    THE AUTHORITY since Task 12: the profile is the expert setting ``market_condition_profile``
    (``market_condition_rules.PROFILE_SETTING``), the same setting the LIVE resolver reads, so a
    deployed genome and the backtest that scored it cannot end up gated on different data. In a
    run config the expert settings live at ``config["experts"][i]["settings"]`` -- the dict
    ``_build_daily_trial_config`` copies wholesale into every trial, which is why the setting
    survives that whitelist while the run-level keys each have to be listed by hand.

    A run may carry more than one expert. Their profiles are UNIONED (first appearance wins the
    order): the run installs ONE reader set for the process-wide condition seam, a condition
    knows only its own field name, and every field is served by exactly one profile -- so the
    union serves each expert exactly what it asked for and nothing else changes hands.

    "Present" means at least one expert spec HAS the key, whatever its value: an explicit empty
    string is an expert that says the gates are off, which is a statement to be checked against
    the legacy keys, not an absence.
    """
    from ba2_common.core.market_condition_rules import PROFILE_SETTING, parse_profile_setting

    present = False
    profiles: List[str] = []
    for spec in (config.get("experts") or ()):
        if not isinstance(spec, dict):
            continue                      # a bare class name carries no settings
        settings = spec.get("settings")
        if not isinstance(settings, dict) or PROFILE_SETTING not in settings:
            continue
        present = True
        for name in parse_profile_setting(settings[PROFILE_SETTING]):
            if name not in profiles:
                profiles.append(name)
    return present, profiles


def market_condition_pins(config: Dict[str, Any], *, required: bool = True
                          ) -> Tuple[List[str], Dict[str, Optional[str]]]:
    """``(profiles, digest per profile)`` for one run config, in any of its shapes.

    CANONICAL (Task 12): the profiles come from the EXPERT SETTING
    ``experts[i].settings["market_condition_profile"]`` -- see
    :func:`market_condition_profile_setting`. ``market_condition_manifests`` stays a RUN-LEVEL
    pin mapping each profile to ITS OWN published digest, because a manifest is data identity
    (which warmed snapshot this search reads), not a property of a strategy. One manifest per
    profile is not a convention but a fact about the format: a manifest names the single profile
    it was warmed for, so two profiles are two snapshots.

    CONFIG KEYS (Task 10 plural ``market_condition_profiles``; pre-Task-10 singular
    ``market_condition_profile`` + ``market_condition_manifest``): still READ, never refused.
    Every optimization_config persisted before Task 12 carries one of them and no setting, and
    re-running one of those genomes -- the parity tool, a re-run, a robustness variant, a top-N
    persist -- has to keep working. ``_build_daily_trial_config`` also writes the derived plural
    key into every trial config, so the two shapes normally travel together and AGREE.

    A config whose setting and whose legacy key DISAGREE raises. That pair is exactly the shape
    of the failure this whole task exists to prevent: one of them says the run is gated and the
    other says it is not, and the quiet reading of it is a run that trades UNGATED under the name
    of a gated one.

    Refuses an unregistered profile, a repeated one, ``"none"`` mixed with a real profile, and
    more than one profile pinned by a single unattributed digest.

    ``required`` (the seam's default) raises ``KeyError`` when NO shape is present at all: the
    seam is handed a config ``run_daily_backtest`` has already normalised, so a missing pin there
    means the normalisation did not run, not that the gates are off. Callers that read a RAW
    stored config -- the trial-config builder, the master prepare -- pass ``required=False``,
    where "no key" legitimately means "this run predates the feature".
    """
    from ba2_common.core.market_conditions import PROFILES

    has_setting, setting_profiles = market_condition_profile_setting(config)
    raw = config.get("market_condition_profiles")
    has_legacy_key = "market_condition_profile" in config
    legacy_profile = config.get("market_condition_profile")
    #: The legacy key NORMALISED to the plural shape, so the two are compared like for like.
    #: ``None`` and ``"none"`` both mean "no profile", which is a value, not an absence.
    legacy_profiles = ([] if legacy_profile in (None, MARKET_CONDITION_PROFILE_NONE)
                       else [str(legacy_profile)])
    given = config.get("market_condition_manifests")
    legacy_digest = config.get("market_condition_manifest")

    no_shape = raw is None and not has_legacy_key and not has_setting
    if raw is None and not has_legacy_key:
        # The setting alone (Task 12's canonical shape), or nothing at all.
        profiles: List[str] = list(setting_profiles)
    else:
        if raw is None:
            raw = legacy_profiles
        elif isinstance(raw, str):
            raw = [t for t in (x.strip() for x in raw.split(",")) if t]
        profiles = [str(p) for p in raw if p]
    if MARKET_CONDITION_PROFILE_NONE in profiles:
        if len(profiles) > 1:
            raise ValueError(f"market_condition_profiles {profiles!r} mixes "
                             f"{MARKET_CONDITION_PROFILE_NONE!r} with a real profile")
        profiles = []
    if len(set(profiles)) != len(profiles):
        raise ValueError(f"market_condition_profiles {profiles!r} repeats a profile")
    unknown = [p for p in profiles if p not in PROFILES]
    if unknown:
        raise ValueError(f"market-condition profile(s) {unknown!r} are not registered "
                         f"(known: {sorted(PROFILES)!r} or {MARKET_CONDITION_PROFILE_NONE!r})")
    # COMPARED WHENEVER THE SINGULAR KEY IS PRESENT, including when the plural list is EMPTY:
    # ``{"market_condition_profiles": [], "market_condition_profile": "ohlcv-v1"}`` is a config
    # that says the gates are on in one key and off in the other, and the quiet reading of it is
    # a run that trades UNGATED under the name of a gated one.
    if has_legacy_key and legacy_profiles != profiles:
        raise ValueError(
            f"config pins market_condition_profiles {profiles!r} AND market_condition_profile "
            f"{legacy_profile!r}: they disagree. Carry one shape, not two.")
    # SAME CHECK against the expert SETTING, which is the authority since Task 12 and the one
    # thing that also reaches LIVE. A config key saying "ohlcv-v1" over a setting saying nothing
    # would install the readers for a run whose deployed twin is ungated (and the reverse would
    # score a gated strategy on data the trial never pinned) -- so the two must agree exactly,
    # including the empty-vs-named case the config-key check above already refuses.
    if has_setting and list(setting_profiles) != profiles:
        from ba2_common.core.market_condition_rules import PROFILE_SETTING

        raise ValueError(
            f"config pins market_condition_profiles {profiles!r} but the run's expert "
            f"{PROFILE_SETTING} setting names {list(setting_profiles)!r}: they disagree. The "
            f"setting is what LIVE reads, so a run whose config key and setting differ scores a "
            f"different strategy from the one it would deploy.")
    # A MANIFEST WITHOUT A PROFILE is not a harmless leftover: the digest is what a driver folds
    # into the job identity, so the run reads as gated everywhere afterwards while nothing ever
    # reads the snapshot. Checked BEFORE the "predates the feature" return, which would otherwise
    # drop the pin on exactly the configs that carry one key and not the other.
    if not profiles and (given or legacy_digest):
        raise ValueError(
            f"a market-condition manifest is pinned ({given or legacy_digest!r}) but no profile "
            f"is: nothing would read it, and the run would be UNGATED while its digest says "
            f"otherwise. Pin the profile(s) the rules were built for, or drop the manifest.")
    if no_shape:
        if required:
            raise KeyError("market_condition_profiles")
        return [], {}

    manifests: Dict[str, Optional[str]] = {}
    if given:
        extra = [p for p in given if p not in profiles]
        if extra:
            raise ValueError(f"market_condition_manifests pins profile(s) {sorted(extra)!r} the "
                             f"run does not use (profiles: {profiles!r})")
        manifests = {p: (given.get(p) or None) for p in profiles}
    if legacy_digest:
        if len(profiles) > 1 and not given:
            raise ValueError(
                f"market_condition_manifest {legacy_digest!r} is a single digest but the run pins "
                f"{len(profiles)} profiles {profiles!r}. A manifest names the ONE profile it was "
                f"warmed for: pin market_condition_manifests as {{profile: digest}}.")
        clash = [p for p in profiles if manifests.get(p) not in (None, legacy_digest)]
        if clash:
            raise ValueError(
                f"config pins market_condition_manifests for {clash!r} AND a different "
                f"market_condition_manifest {legacy_digest!r}: they disagree.")
        if not given:
            manifests = {p: legacy_digest for p in profiles}
    return profiles, {p: manifests.get(p) for p in profiles}


def normalize_market_condition_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    """The canonical plural pins, for a caller that rebuilds a run config (``run_daily_backtest``
    normalises once so every later reader of that config sees one shape). A config carrying
    neither shape predates the feature and normalises to "no profile"."""
    profiles, manifests = market_condition_pins(config, required=False)
    return {"market_condition_profiles": profiles, "market_condition_manifests": manifests}


def install_backtest_market_conditions(config: Dict[str, Any], price_source: Any) -> Optional[Any]:
    """Install this thread's market-condition resolver for one run, or nothing.

    The run's profiles come from :func:`market_condition_pins` (the plural keys, or the legacy
    singular pair). No profile returns None without importing the adapter or touching the seam;
    one or more registered profiles build ONE reader EACH over ``price_source`` -- every profile
    is a separate snapshot with its own coverage -- joined by ``market_condition_reader_for``
    into the single reader a context carries (a one-profile run gets that reader itself,
    unwrapped, and is unchanged by this).

    A profile's manifest digest pins its published snapshot: present, that profile's reader is a
    mapped-store reader (no calculation anywhere in the run); absent, it computes on a miss and
    warns once -- unless the config is an optimizer trial, which is refused (see below).
    """
    profiles, manifests = market_condition_pins(config)
    # THE UNGATED FAST PATH. No profile pinned AND no registered field name anywhere in the
    # config: neither walk below can find anything, and this function runs once per TRIAL for
    # every backtest in the system. One substring scan of the config JSON replaces two recursive
    # walks. Deliberately AFTER the pins, so a gated-but-leafless config still reaches the
    # refusals that are about config SHAPE (a manifest with no profile, a profile with no
    # manifest on a GA trial, a setting contradicting a config key).
    if not profiles and _has_no_market_condition_field(config):
        _market_condition_tl.resolver = None
        return None
    # PER EXPERT, before the union is used for anything: a leaf only one expert's profile serves
    # would otherwise ride the shared reader set here and find nothing live. No-op for a run with
    # no market leaves, which is every existing backtest.
    assert_each_expert_serves_its_gates(config)
    if not profiles:
        # A run with market leaves but no profile would evaluate every gate as no_context and
        # place ZERO entries without a word (e.g. a trial config that dropped the key after an
        # earlier gated run installed the dispatcher in this process). Refuse it instead.
        leaves = market_condition_leaves_in(config)
        if leaves:
            raise ValueError(
                f"no market-condition profile is pinned but the run's rules contain "
                f"market-condition leaves {leaves!r}: every such gate would be unknown and never "
                f"pass. Set the profile(s) the rules were built for.")
        _market_condition_tl.resolver = None
        return None
    from ba2_common.core import TradeConditions
    from ba2_common.core.market_condition_readers import (
        market_condition_reader_for,
        warn_research_mode,
    )

    from app.services.backtest.market_condition_bt import (
        BacktestMarketConditionReader,
        BacktestMarketConditionResolver,
    )

    # THE PINNED SNAPSHOT (design section 4.5). A search prepares ONE manifest before dispatch and
    # carries its digest in every trial config; the reader then serves that snapshot's published
    # rows and calculates nothing. A config WITHOUT the digest can only compute on a miss, which
    # is fine for a one-off research run and is not fine inside an optimization: the numbers would
    # come from whatever each worker's own cache happened to hold, no two hosts provably agreeing,
    # and nothing anywhere would say so. So an optimizer-assembled config (``_ga_trial``: GA trial,
    # re-run, robustness variant and top-N persist alike -- see
    # strategy_optimization_handler._build_daily_trial_config) is REFUSED here instead.
    readers = []
    for profile in profiles:
        digest = manifests.get(profile)
        if not digest:
            if config.get("_ga_trial"):
                raise ValueError(
                    f"market-condition profile {profile!r} is on but the trial config pins no "
                    f"manifest for it. An optimization must prepare one snapshot PER PROFILE "
                    f"(tools/warm_market_conditions.py plan/build/verify/prepare-host) and carry "
                    f"every digest into every trial; computing 128-session indicators per trial "
                    f"is not a fallback this path takes.")
            warn_research_mode(profile, "backtest reader")
        reader = BacktestMarketConditionReader(price_source, profile, manifest_digest=digest)
        # PER PROFILE: each snapshot was warmed separately and can cover a different set of
        # symbols (and a different window), so "is this run served" is a question with one answer
        # per profile -- both halves of it.
        check_market_condition_coverage(config, reader)
        check_market_condition_window(config, reader)
        readers.append(reader)
    resolver = BacktestMarketConditionResolver(market_condition_reader_for(readers))
    if TradeConditions.get_market_condition_context_resolver() is not _dispatch_market_condition_context:
        TradeConditions.set_market_condition_context_resolver(_dispatch_market_condition_context)
    _market_condition_tl.resolver = resolver
    return resolver


#: How many missing symbols a message names before it says "and N more".
_COVERAGE_NAMES = 20


def market_condition_universe(config: Any) -> List[str]:
    """The symbols this run can ever evaluate a market-condition gate for.

    ``enabled_instruments`` is the right list even for a screener run: the screener gates ENTRIES
    to a per-day subset of it (and the optimizer's ``screener_candidate`` has already narrowed it
    to what this trial's screen can ever select), so it is the superset a gate can be asked about.
    """
    return sorted({str(s).upper() for s in (config.get("enabled_instruments") or ())})


def check_market_condition_coverage(config: Dict[str, Any], reader: Any) -> List[str]:
    """Refuse (or, in research mode, report) a run whose universe the snapshot does not cover.

    THE FAILURE THIS PREVENTS. A pinned manifest covers exactly the symbols the warmup could warm
    -- the first real one covers 85 of the 98-symbol option universe, because 13 carry a split
    whose basis the prices cannot settle and need a full provider re-fetch first. For a symbol the
    manifest omits, every gate reads ``missing_session`` for the whole run: it never enters, and
    the genome that would have traded it scores as though its strategy simply did not fire there.
    A feature-cache miss must not become a property of the fitness landscape.

    A GA trial RAISES (the job fails, which is what an environment fault deserves); research mode
    logs one ERROR naming the symbols and continues, because a one-off run over a wider universe
    than the snapshot is a legitimate thing to do deliberately. Returns the missing symbols.

    THE SECOND QUESTION (review 2026-09-16, finding F1). A snapshot that carries every symbol can
    still carry none of the ROWS this run will ask for -- one warmed over 2024-03 is "complete"
    for a 2025 universe and serves ``missing_session`` on every decision date of it. So when the
    symbols are all present, the pin is also checked against the run's decision window; see
    :func:`check_market_condition_window`.
    """
    from ba2_common.core.market_condition_reader import coverage_detail, missing_coverage

    mapped = getattr(reader, "mapped_reader", None)
    if mapped is None:
        return []                      # research mode without a manifest: nothing to compare to
    universe = market_condition_universe(config)
    missing = missing_coverage(mapped, universe)
    if not missing:
        return []
    shown = ", ".join(missing[:_COVERAGE_NAMES])
    if len(missing) > _COVERAGE_NAMES:
        shown += f", and {len(missing) - _COVERAGE_NAMES} more"
    detail = coverage_detail(mapped, missing)
    message = (
        f"market-condition manifest {mapped.manifest_digest} does not cover "
        f"{len(missing)} of this run's {len(universe)} instruments: {shown}{detail}. "
        f"Every gate on those symbols would read missing_session for the whole run, so they "
        f"would silently never enter. Warm and re-publish the snapshot for the full universe "
        f"(tools/warm_market_conditions.py plan/build), or run the narrower universe.")
    if config.get("_ga_trial"):
        raise ValueError(message)
    _log.error(message)
    return missing


def check_market_condition_window(config: Dict[str, Any], reader: Any) -> List[str]:
    """Refuse (or, in research mode, report) a pin that does not serve this run's SESSIONS.

    THE FAILURE THIS PREVENTS is the one symbol presence cannot see. A snapshot warmed over
    2024-03-01..2024-03-29 contains every symbol of a 2025 universe and not one row the run will
    read: ``observe()`` returns None for every decision date, every gate evaluates
    ``missing_session``, every gated entry is refused, and the GA scores that suppression as
    strategy behaviour. Reproduced against the real preflight on 2026-09-16.

    The work is in :func:`ba2_common.core.market_condition_reader.window_coverage_problems`
    (memoised per digest/universe/window, manifest JSON only -- no object is read and no array is
    mapped). It refuses an out-of-range pin and an unexplained hole, and deliberately does NOT
    refuse a legitimately undefined observation: the warm-up prefix, a symbol that listed
    part-way through the window, or a structure field with no confirmed pivot yet.

    A config with no ``start_date``/``end_date`` cannot be checked. That config shape does not
    reach a backtest (the engine requires both), and the LAUNCHER refuses to dispatch a run whose
    block carries no window, so this reports one WARNING rather than inventing a window.
    """
    from ba2_common.core.market_condition_reader import window_coverage_problems

    mapped = getattr(reader, "mapped_reader", None)
    if mapped is None:
        return []                      # research mode without a manifest: nothing to compare to
    start, end = config.get("start_date"), config.get("end_date")
    if start in (None, "") or end in (None, ""):
        _log.warning(
            f"market-condition manifest {mapped.manifest_digest} could not be checked against "
            f"this run's decision window: the config carries no start_date/end_date. Symbol "
            f"coverage was checked; the sessions were not.")
        return []
    universe = market_condition_universe(config)
    problems = window_coverage_problems(mapped, universe, start, end)
    if not problems:
        return []
    message = (
        f"market-condition manifest {mapped.manifest_digest} does not serve the feature rows "
        f"this run's {start}..{end} decision window needs: " + "; ".join(problems) + ". "
        f"Each of those decision dates would read missing_session, so the gated entries would "
        f"be refused for a cache reason and the search would score that as strategy behaviour. "
        f"This is a job configuration fault, not a result.")
    if config.get("_ga_trial"):
        raise ValueError(message)
    _log.error(message)
    return problems


def market_condition_leaf_fields_in(config: Any, path: str = "config"
                                   ) -> List[Tuple[str, str]]:
    """``(label, field)`` for every condition leaf anywhere in ``config`` whose ``field`` is a
    registered market-condition field. The label is the leaf's id, or its config path when it
    has none. Walks dicts and lists, and JSON-encoded rule trees held as strings (expert
    settings store trees that way).

    A string that NAMES a market field but does not parse as a tree yields ``(path, "")`` -- an
    unservable field name, so every caller refuses it rather than walking past something it
    could not read."""
    import json

    from ba2_common.core.market_conditions import PROFILES

    fields = {f.name for prof in PROFILES.values() for f in prof.fields}
    hits: List[Tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            field = node.get("field")
            if isinstance(field, str) and field in fields:
                hits.append(((str(node["id"]) if node.get("id") else path), field))
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str) and node[:1] in ("{", "[") and any(f in node for f in fields):
            try:
                decoded = json.loads(node)
            except ValueError:
                hits.append((path, ""))  # names a market field, unparseable: refuse it too
                return
            walk(decoded, path)

    walk(config, path)
    return hits


def market_condition_leaves_in(config: Any) -> List[str]:
    """Ids (or config paths) of every market-condition leaf in ``config``; the labels of
    :func:`market_condition_leaf_fields_in`."""
    return [label for label, _ in market_condition_leaf_fields_in(config)]


def _has_no_market_condition_field(config: Any) -> bool:
    """True when NO registered market-condition field name appears anywhere in ``config``.

    A cheap, conservative pre-filter for ``install_backtest_market_conditions``, which runs once
    per trial for every backtest in the system. False negatives are harmless (the real walks run
    and find nothing); a false POSITIVE would skip the gates of a gated run, so the test is the
    weakest possible one: the name as a substring of the config's JSON text.

    Used only together with "no profile is pinned", so a gated run never takes the fast path
    however its leaves are spelled.
    """
    import json

    from ba2_common.core.market_conditions import PROFILES

    names = {f.name for prof in PROFILES.values() for f in prof.fields}
    try:
        text = json.dumps(config, default=str)
    except Exception:  # noqa: BLE001 -- an unserialisable config is not proof of absence
        return False
    return not any(name in text for name in names)


def assert_each_expert_serves_its_gates(config: Dict[str, Any]) -> None:
    """Every expert's OWN ``market_condition_profile`` must serve every market leaf it evaluates.

    THE BT/LIVE ASYMMETRY THIS CLOSES. A run installs ONE reader set, built from the UNION of
    its experts' settings (``market_condition_profile_setting``), and a condition only knows its
    own field name -- so in a backtest an ``ohlcv-v1`` expert happily reads a ``ta-structure-v1``
    field that some OTHER expert's setting brought into the union. Live there is no union: each
    expert gets a resolver built from its own setting alone, so the same leaf finds no reader for
    its profile and the sleeve never enters. Identical inputs, different trades -- which this
    repo treats as a code bug, not a documented limitation.

    Checking per expert makes the union an OPTIMISATION (one reader set instead of N) rather than
    a semantic. Unreachable today -- the launcher writes the same setting onto every spec and the
    option jobs are single-expert -- so this is the rail, not a repair.

    A backtest's rules are RUN-LEVEL (``entry_rules``/``exit_rules``, seeded into every expert's
    ruleset by ``daily_backtest_handler._build_experts``), so every expert evaluates them all;
    a leaf inside one spec's own settings counts only against that spec. Both are covered.
    """
    from ba2_common.core.market_condition_rules import (
        PROFILE_SETTING, assert_fields_served, parse_profile_setting,
    )

    specs = [spec for spec in (config.get("experts") or ()) if isinstance(spec, dict)]
    if not specs:
        return
    shared = market_condition_leaf_fields_in(
        {k: v for k, v in config.items() if k != "experts"})
    for i, spec in enumerate(specs):
        settings = spec.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        used = shared + market_condition_leaf_fields_in(spec, f"config.experts[{i}]")
        if not used:
            continue
        assert_fields_served(used, parse_profile_setting(settings.get(PROFILE_SETTING)),
                             where=f"expert {spec.get('class')!r} rules")


def clear_backtest_market_conditions() -> None:
    """Drop this thread's run resolver (idempotent). The dispatcher stays installed: another
    thread's run may still be using it, and with no slot set it resolves None."""
    _market_condition_tl.resolver = None


def _wire_provider_resolver() -> None:
    """Route ``TradeConditions`` data fetches through ba2_providers.get_provider.

    Phase 0 severed the ba2_common -> ba2_providers edge; data-driven conditions now
    resolve a provider through this host-injected resolver. The signature matches
    ba2_providers.get_provider exactly: fn(category, name, **kwargs). When a per-run OHLCV
    override is set, ("ohlcv", *) resolves to it (the memoized in-memory provider).
    """
    from ba2_common.core import TradeConditions
    from ba2_providers import get_provider  # ba2_providers is allowed here (host side)

    def _resolve(category: str, name: str, **kwargs: Any) -> Any:
        override = _current_ohlcv_override()
        if category == "ohlcv" and override is not None:
            return override
        return get_provider(category, name, **kwargs)

    TradeConditions.set_provider_resolver(_resolve)


def make_indicator_provider(ohlcv_provider: Any = None) -> Any:
    """Build the indicator provider injected into ``TradeRiskManagement`` / ATR sizing.

    Phase 0 made ``position_sizing.get_latest_atr(symbol, indicator_provider, ...)`` and
    ``TradeRiskManagement(indicator_provider=...)`` take an *injected* provider so
    ba2_common never imports ba2_providers. The indicators/"pandas" provider
    (PandasIndicatorCalc) REQUIRES an OHLCV provider in its constructor, so we build it
    from the ohlcv/"fmp" provider here.

    Args:
        ohlcv_provider: the OHLCV provider to back the indicator calc. If omitted, the
            default ohlcv/"fmp" provider is constructed. The backtest engine passes its
            as_of-aware OHLCV provider so ATR is computed against the backtest cache.
    """
    from ba2_providers import get_provider

    if ohlcv_provider is None:
        ohlcv_provider = get_provider("ohlcv", "fmp")
    return get_provider("indicators", "pandas", ohlcv_provider=ohlcv_provider)


class MetricStoreATRProvider:
    """ATR-only indicator provider that reads PRECOMPUTED ``atr_<period>`` columns from the
    screener metric store — fully offline (no network, no live OHLCV fetch), so it is hermetic
    and safe for the GA/optimize trial-worker path, which has no route to a live/as-of-clamped
    indicator provider (unlike the single-backtest handler's ``AsOfClampedOHLCVProvider`` path).

    Only understands ``indicator="atr"`` with a ``period`` in the store's precomputed
    ``metric_store.ATR_PERIODS``; anything else returns no values (the caller,
    ``position_sizing.get_latest_atr``, then logs "no ATR value returned" and the safeguard falls
    back to its risk%-only floor — the SAME safe behaviour as today, just narrower in scope).

    ``end_date`` MUST be the backtest's SIMULATED as-of date (threaded from
    ``TradeRiskManagement(as_of=...)`` -> ``get_latest_atr(end_date=...)``) — using wall-clock
    would resolve to the store's MOST RECENT row regardless of the historical bar being sized, a
    lookahead bug. The store is loaded via ``metric_store.load_store`` (memoised per worker), so
    repeated construction is cheap.
    """

    def __init__(self, store_dir: str):
        self._store_dir = store_dir

    def get_indicator(self, symbol: str, indicator: str, start_date: Any = None,
                      end_date: Any = None, lookback_days: Any = None, interval: str = "1d",
                      format_type: str = "dict", period: Any = None) -> Dict[str, Any]:
        from ba2_providers.screener import metric_store as ms

        empty = {"values": [], "dates": [], "symbol": symbol.upper(), "indicator": indicator}
        if indicator != "atr" or not period or int(period) not in ms.ATR_PERIODS:
            return empty
        if end_date is None:
            return empty  # never fall back to wall-clock here — see class docstring
        col = f"atr_{int(period)}"
        try:
            df = ms.load_store(self._store_dir)
            day = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date)[:10]
            rows = ms.metrics_as_of(df, day, [col])
        except Exception:  # noqa: BLE001 — any store issue -> safe empty (caller's no-ATR fallback)
            return empty
        row = rows.get(symbol.upper()) or rows.get(symbol)
        if not row:
            return empty
        val = row.get(col)
        try:
            import math
            if val is None or (isinstance(val, float) and math.isnan(val)):
                return empty
            return {"values": [float(val)], "dates": [day], "symbol": symbol.upper(),
                   "indicator": indicator}
        except (TypeError, ValueError):
            return empty


def make_atr_cache_indicator_provider(screener_store: Optional[str]) -> Optional[Any]:
    """``MetricStoreATRProvider`` bound to ``screener_store``, or ``None`` when no store is
    configured for this run (caller falls back to ``make_indicator_provider()``, the existing
    — currently non-hermetic in the GA path — live provider)."""
    if not screener_store:
        return None
    return MetricStoreATRProvider(screener_store)
