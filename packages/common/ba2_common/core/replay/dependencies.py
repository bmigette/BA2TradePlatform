"""What one expert configuration actually needs on disk to be replayed (spec step 4, section 5).

`docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md` section 5 asks
for ONE settings-aware dependency resolver, consumed by capture coverage, CLI
prewarm, API prewarm and historical replay. This module is that resolver's
contract and registry; the per-expert knowledge lives in
``ba2_experts.replay_dependencies`` (an expert adapter must know its expert, and
``ba2_common`` may not import ``ba2_experts``).

**Three rules this module exists to enforce.**

*An unknown expert is ``unsupported``, never an empty list.* An empty list reads
as "nothing is needed", which a coverage report renders green. A class with no
adapter has undeclared reads, which is the opposite of covered -- so
:func:`required_replay_inputs` answers with exactly one
:class:`Requirement` of kind ``unsupported``.

*Settings are read explicitly.* ``settings.get(key, default)`` inside a resolver
invents the configuration the live run had; the warm plan then warms the wrong
surface and every later comparison is measured against it. Adapters read through
:func:`require_setting`, which raises :class:`MissingDependencySetting` naming the
key and the requirement it was needed for.

*The expert class alone cannot describe a trade.* The active rules and the risk
manager read data too (ATR, the earnings calendar, the cooldown ledger), so
:func:`required_replay_inputs` appends :func:`rule_requirements` to every
supported expert's list. :func:`expert_replay_inputs` is the half WITHOUT those
extras, for the one caller that genuinely asks a narrower question -- the
historical comparison, which rebuilds and diffs an expert's INPUTS and must not
be blocked on an ATR series that comparison never reads.

Nothing here reaches a network, a database or a cache: it maps configuration to
typed declarations. Deciding whether a declaration is already satisfied is the
planner's job (``ba2_providers.warm.planner``), and fetching it is the warm
fetcher's (``ba2_experts.warm_fetchers``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Kinds
# --------------------------------------------------------------------------- #
#: A per-symbol ``fmp_history`` JSON payload (``<root>/fmp_history/<ns>__<SYM>.json``).
KIND_HISTORY = "history"
#: A parquet price series (``<root>/<OhlcvProviderClass>/<SYM>_<interval>.parquet``).
KIND_TIMESERIES = "timeseries"
#: A macro series file (``<root>/fred/<SERIES_ID>.json``).
KIND_SERIES = "series"
#: A DERIVED value (ATR): no artifact of its own, computed from a price series by
#: whichever indicator provider the host wired. Declared so the plan can say the
#: underlying series has to cover the indicator's lookback.
KIND_INDICATOR = "indicator"
#: Platform-local state (the closed-transaction ledger a cooldown rule reads).
#: Nothing to warm -- declared so a replay knows the decision depended on it.
KIND_STATE = "state"
#: "This expert class has no adapter": undeclared reads, never a green result.
KIND_UNSUPPORTED = "unsupported"

KINDS: Tuple[str, ...] = (
    KIND_HISTORY,
    KIND_TIMESERIES,
    KIND_SERIES,
    KIND_INDICATOR,
    KIND_STATE,
    KIND_UNSUPPORTED,
)

#: The registry name of the OHLCV provider ``LiveProviderBundle`` hands every
#: expert ``_gather`` (``backtest_context.LiveProviderBundle.ohlcv``). Requirements
#: name the provider a READ actually goes through; guessing it would point the
#: planner at the wrong parquet directory.
OHLCV_PROVIDER = "fmp"
#: The provider all ``fmp_history`` namespaces belong to.
FMP_PROVIDER = "fmp"
#: The macro store.
FRED_PROVIDER = "fred"
#: An indicator's provider is the registry CATEGORY, not a named implementation:
#: which OHLCV source backs it is host wiring (``seam_helpers``), so the plan
#: searches every provider directory rather than inventing one.
INDICATOR_PROVIDER = "indicators"
#: Platform-local state lives in the trading database, not with a data provider.
PLATFORM_PROVIDER = "platform"


class MissingDependencySetting(KeyError):
    """A setting an adapter needs was not in the settings it was given.

    A ``KeyError`` subclass so an adapter can simply index, but with a message that
    names the requirement the key was needed for -- the caller has to fix the
    configuration it passed, and "KeyError: 'w_analyst'" does not say which
    declaration went missing.
    """

    def __init__(self, key: str, needed_by: str) -> None:
        super().__init__(key)
        self.key = key
        self.needed_by = needed_by

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"setting {self.key!r} is required to declare {self.needed_by}; "
            f"pass the expert's resolved settings (no default is substituted here)"
        )


def require_setting(settings: Mapping[str, Any], key: str, *, needed_by: str) -> Any:
    """Read ``key`` from ``settings`` or raise :class:`MissingDependencySetting`.

    The ONE way an adapter reads configuration. ``settings.get(key, default)`` would
    make the resolver answer for a configuration the live run never had.
    """
    if settings is None or key not in settings:
        raise MissingDependencySetting(key, needed_by)
    return settings[key]


def as_bool(value: Any) -> bool:
    """Coerce a settings value that means a boolean, loudly.

    Settings arrive as strings from the database ("1", "true", "False"), as real
    bools from a test, and occasionally as numbers. Anything that means neither
    raises rather than being read as False -- a bool gene silently reading False is
    exactly the deploy-parity trap this project has already paid for once.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        # NOT False. A setting present-but-null is a broken row, not an "off" switch, and
        # reading it as off silently drops the ATR declaration from the plan -- the same
        # failure shape as a missing key, so it fails the same way.
        raise ValueError(
            "a boolean setting is present but null; set it to true/false rather than "
            "leaving it unset (null is not 'off')")
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"cannot read {value!r} as a boolean setting value")


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    """The span of history a requirement has to cover, in UTC.

    ``start`` is ``None`` for a payload with no range parameter -- FMP serves the
    whole per-symbol history and the consumer filters in Python, so asking for a
    narrower window would not make a smaller request (spec section 6, "An endpoint
    without a range parameter may require a full payload refresh").
    """

    start: Optional[datetime]
    end: datetime

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, datetime):
                raise TypeError(f"Window.{name} must be a datetime, got {type(value).__name__}")
            if value.tzinfo is None:
                raise ValueError(f"Window.{name} must be timezone-aware (UTC)")
        if self.start is not None and self.start > self.end:
            raise ValueError(f"Window.start {self.start} is after Window.end {self.end}")

    def to_mapping(self) -> Dict[str, Optional[str]]:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat(),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Window":
        start = data["start"]
        return cls(
            start=datetime.fromisoformat(start) if start else None,
            end=datetime.fromisoformat(data["end"]),
        )

    def trailing(self, days: int) -> "Window":
        """The same end with a start ``days`` earlier (an explicit lookback)."""
        return Window(start=self.end - timedelta(days=int(days)), end=self.end)


# --------------------------------------------------------------------------- #
# Requirement
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Requirement:
    """One declared input: what it is, for whom, and why.

    ``optional`` marks a dependency the calculation degrades without rather than
    fails on. It is NOT "probably not needed": spec section 5 is explicit that a
    declared-but-unused optional dependency is not a proven live input, so the
    plan counts it separately and the budget spends on it last.

    ``reason`` is free text naming the setting or rule that produced the
    declaration. It is the only thing that tells an operator why 400 statement
    payloads appeared in a plan.
    """

    provider: str
    namespace: str
    symbol: Optional[str]
    window: Optional[Window]
    interval: Optional[str]
    kind: str
    optional: bool
    reason: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown requirement kind {self.kind!r}; expected one of {KINDS}")
        if self.kind in (KIND_TIMESERIES, KIND_INDICATOR) and not self.interval:
            raise ValueError(f"a {self.kind} requirement must name its interval ({self.namespace})")

    @property
    def key(self) -> str:
        """A stable identity for dedupe, budget accounting and manifests.

        The window is deliberately NOT part of it: two callers asking for the same
        per-symbol payload over different spans are one fetch, and keying on the
        window would warm it twice.
        """
        return "|".join((
            self.kind,
            self.provider,
            self.namespace,
            (self.symbol or "").upper(),
            self.interval or "",
        ))

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "namespace": self.namespace,
            "symbol": self.symbol,
            "window": self.window.to_mapping() if self.window else None,
            "interval": self.interval,
            "kind": self.kind,
            "optional": self.optional,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Requirement":
        window = data["window"]
        return cls(
            provider=data["provider"],
            namespace=data["namespace"],
            symbol=data["symbol"],
            window=Window.from_mapping(window) if window else None,
            interval=data["interval"],
            kind=data["kind"],
            optional=bool(data["optional"]),
            reason=data["reason"],
        )


def unsupported_requirement(expert_class: str, detail: str) -> Requirement:
    """The single requirement an expert without an adapter resolves to."""
    return Requirement(
        provider=PLATFORM_PROVIDER,
        namespace=expert_class,
        symbol=None,
        window=None,
        interval=None,
        kind=KIND_UNSUPPORTED,
        optional=False,
        reason=detail,
    )


def dedupe(requirements: Iterable[Requirement]) -> List[Requirement]:
    """Dedupe by :attr:`Requirement.key`, keeping first-seen ORDER.

    Which COPY survives is decided by ``optional``, not by arrival: where the same
    payload is declared both required and optional, the REQUIRED one wins whichever
    came first. A required declaration demoted to optional by a later twin would be
    warmed last and dropped first by the budget -- a real input treated as a nice-to-
    have. The surviving copy keeps the position of the first occurrence, so a plan
    reads in declaration order.
    """
    out: Dict[str, Requirement] = {}
    for req in requirements:
        existing = out.get(req.key)
        if existing is None:
            out[req.key] = req
        elif existing.optional and not req.optional:
            out[req.key] = req
    return list(out.values())


# --------------------------------------------------------------------------- #
# Adapter registry
# --------------------------------------------------------------------------- #
#: expert class name -> ``adapter(settings, universe, window) -> Sequence[Requirement]``.
Adapter = Callable[[Mapping[str, Any], Sequence[str], Window], Sequence[Requirement]]
_ADAPTERS: Dict[str, Adapter] = {}


def register_adapter(expert_class: str, adapter: Adapter) -> None:
    """Register (or replace) the adapter for one expert class.

    Replacing is allowed and silent: the experts package registers at import, and
    an import can legitimately happen twice (a reload, a second test module).
    """
    _ADAPTERS[expert_class] = adapter


def unregister_adapter(expert_class: str) -> None:
    """Drop one adapter (tests, and a host that unloads an expert package)."""
    _ADAPTERS.pop(expert_class, None)


def registered_experts() -> Tuple[str, ...]:
    """Expert classes that currently have an adapter, sorted."""
    return tuple(sorted(_ADAPTERS))


def adapter_for(expert_class: str) -> Optional[Adapter]:
    """The registered adapter for ``expert_class``, or ``None``.

    The ONE way to ask "is this expert declared?". Inferring it from the SHAPE of
    a requirement list (one entry, kind ``unsupported``) reads a data value as a
    control signal: an adapter that legitimately returned such a requirement would
    be mistaken for an unregistered class, and the two mean opposite things.
    """
    return _ADAPTERS.get(expert_class)


def expert_replay_inputs(
    expert_class: str,
    settings: Mapping[str, Any],
    universe: Sequence[str],
    window: Window,
) -> List[Requirement]:
    """What the EXPERT ITSELF reads -- its adapter's declarations and nothing else.

    Separate from :func:`required_replay_inputs` because the two answer different
    questions and one caller genuinely wants only this half. The historical
    comparison (spec step 5) rebuilds an expert's ``_gather``/``_process`` from a
    pinned cache root and compares the INPUTS: the ATR series, the earnings
    calendar and the cooldown ledger a RULE reads belong to the decision
    comparison (step 6), and folding them in here would block an expert-input
    comparison on an artifact that comparison never touches -- and would demand
    risk-manager settings (``use_atr_stop``, ``sizing_mode``, ``atr_period``) the
    caller may not hold.

    An expert class with no registered adapter resolves to exactly one
    ``unsupported`` requirement: its reads are undeclared, which is the opposite
    of "nothing is needed".
    """
    if not isinstance(window, Window):
        raise TypeError(f"window must be a Window, got {type(window).__name__}")
    adapter = adapter_for(expert_class)
    if adapter is None:
        return [unsupported_requirement(
            expert_class,
            f"no replay-dependency adapter is registered for {expert_class}; its provider "
            f"reads are undeclared (registered: {', '.join(registered_experts()) or 'none'})",
        )]
    return dedupe(adapter(settings, _clean_symbols(universe), window))


def required_replay_inputs(
    expert_class: str,
    settings: Mapping[str, Any],
    rules: Any,
    universe: Sequence[str],
    window: Window,
) -> List[Requirement]:
    """Everything a replay of ``expert_class`` over ``universe`` needs on disk.

    The expert's own adapter first (:func:`expert_replay_inputs`), then the
    rule/risk-manager extras derived from ``rules`` and ``settings``
    (:func:`rule_requirements`) -- the expert class alone cannot describe what a
    trade reads.

    An expert class with no registered adapter resolves to exactly one
    ``unsupported`` requirement and NOTHING else: its reads are undeclared, so
    appending the rule extras would present a partial list as a complete one.
    """
    if adapter_for(expert_class) is None:
        return expert_replay_inputs(expert_class, settings, universe, window)
    requirements = expert_replay_inputs(expert_class, settings, universe, window)
    requirements.extend(
        rule_requirements(settings, rules, _clean_symbols(universe), window))
    return dedupe(requirements)


def _clean_symbols(universe: Sequence[str]) -> List[str]:
    """Upper-cased, de-duplicated, order-preserving symbols."""
    seen = set()
    out: List[str] = []
    for raw in universe or ():
        symbol = str(raw).strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


# --------------------------------------------------------------------------- #
# Rule / risk-manager extras
# --------------------------------------------------------------------------- #
#: Rule event types that read the earnings calendar through
#: ``TradeConditions.DaysToEarningsCondition`` (``ba2_common.core.types``
#: ``ExpertEventType.N_DAYS_TO_EARNINGS``).
EARNINGS_EVENT_TYPES = ("days_to_earnings",)
#: Rule event types answered from the platform's own closed-transaction ledger
#: (``DaysSinceLastCloseCondition`` and its profit-sign subclasses).
COOLDOWN_EVENT_TYPES = (
    "days_since_last_close",
    "days_since_last_profitable_close",
    "days_since_last_losing_close",
)
#: ``DaysToEarningsCondition.EARNINGS_CALENDAR_PERIODS`` quarters of calendar, read
#: through ``get_past_earnings`` -- the SAME ``past_earnings_quarterly`` namespace
#: the experts use, so one payload serves both.
EARNINGS_CALENDAR_NAMESPACE = "past_earnings_quarterly"
#: The documented ANNUAL fallback when the calendar holds no scheduled future print.
EARNINGS_ESTIMATES_NAMESPACE = "earnings_estimates_quarterly"
#: The daily bars ``position_sizing.get_latest_atr`` asks the indicator provider
#: for. It never requests an intraday interval (spec section 6: daily bars for
#: indicator lookbacks).
ATR_INTERVAL = "1d"
#: ``get_latest_atr`` pulls ``max(period * 4, 60)`` days so the average is stable.
ATR_LOOKBACK_MULTIPLE = 4
ATR_LOOKBACK_MIN_DAYS = 60


def iter_rule_event_types(rules: Any) -> List[str]:
    """Every event type mentioned by ``rules``, in encounter order.

    Accepts what the callers actually hold: a sequence of ``EventAction`` rows
    (``.triggers`` -> ``{key: {"event_type": ...}}``), the same thing as plain dicts (an
    exported ruleset), a ruleset-shaped mapping with ``event_actions``, or a bare
    sequence of event-type strings. ``None`` is no rules.

    TWO SPELLINGS, both read. ``event_type`` is what ``TradeActionEvaluator`` reads; some
    exported and older rulesets spell the same field ``type``. Reading only the first
    would silently drop every condition in such a ruleset -- the plan would then be
    missing the earnings calendar and the ATR series while every log said the resolver
    ran -- so ``type`` is read as a fallback and a trigger that carries BOTH with
    different values is refused rather than resolved by a coin toss.

    A trigger dict that names NEITHER raises. Yielding zero event types for it is
    indistinguishable, at the plan, from a ruleset with no data conditions.
    """
    if rules is None:
        return []
    if isinstance(rules, Mapping):
        rules = rules.get("event_actions", [rules])
    if isinstance(rules, (str, bytes)):
        raise TypeError("rules must be event actions or event-type strings, not a single string")
    out: List[str] = []
    for rule in rules:
        if isinstance(rule, str):
            out.append(rule)
            continue
        triggers = rule.get("triggers") if isinstance(rule, Mapping) else getattr(rule, "triggers", None)
        if triggers is None:
            raise TypeError(
                f"rule {rule!r} has no 'triggers'; pass EventAction rows, their dict form, "
                f"or plain event-type strings")
        for name, trigger in (triggers or {}).items():
            if not isinstance(trigger, Mapping):
                raise TypeError(
                    f"trigger {name!r} is a {type(trigger).__name__}, not a mapping; a rule "
                    f"this resolver cannot read must not be silently skipped")
            event_type = trigger.get("event_type")
            legacy = trigger.get("type")
            if event_type and legacy and str(event_type) != str(legacy):
                raise ValueError(
                    f"trigger {name!r} carries both event_type={event_type!r} and "
                    f"type={legacy!r}; which condition it declares is ambiguous")
            resolved = event_type or legacy
            if not resolved:
                raise ValueError(
                    f"trigger {name!r} names no event type (neither 'event_type' nor 'type'); "
                    f"a rule whose condition cannot be read would silently drop its data "
                    f"requirements from the warm plan")
            out.append(str(resolved))
    return out


def rule_requirements(
    settings: Mapping[str, Any],
    rules: Any,
    universe: Sequence[str],
    window: Window,
) -> List[Requirement]:
    """What the ACTIVE RULES and the risk manager read, beyond the expert itself.

    Three sources, each of which has silently broken a comparison before:

    * **ATR.** ``TradeRiskManagement._ensure_safeguard_stop`` fetches the latest ATR
      whenever ``use_atr_stop`` is on -- in EVERY sizing mode, not only
      ``risk_atr`` (the safeguard stop exists precisely so a ``notional`` entry is
      not unprotected). So ``use_atr_stop`` is the gate; ``sizing_mode`` is
      recorded in the reason because it decides whether the same ATR also sizes
      the position.
    * **Earnings conditions.** ``days_to_earnings`` reads the quarterly calendar
      for the symbol, with the annual estimates as a documented fallback.
    * **Cooldown.** ``days_since_last_*_close`` reads the platform's own closed
      transactions -- nothing to warm, but a replay that does not know the decision
      depended on it will silently reproduce a different branch.
    """
    event_types = iter_rule_event_types(rules)
    out: List[Requirement] = []

    use_atr_stop = as_bool(require_setting(settings, "use_atr_stop", needed_by="the ATR indicator"))
    if use_atr_stop:
        sizing_mode = str(require_setting(settings, "sizing_mode", needed_by="the ATR indicator"))
        period = int(require_setting(settings, "atr_period", needed_by="the ATR indicator"))
        lookback = max(period * ATR_LOOKBACK_MULTIPLE, ATR_LOOKBACK_MIN_DAYS)
        for symbol in universe:
            out.append(Requirement(
                provider=INDICATOR_PROVIDER,
                namespace=f"atr_{period}",
                symbol=symbol,
                window=window.trailing(lookback),
                interval=ATR_INTERVAL,
                kind=KIND_INDICATOR,
                optional=False,
                reason=(f"use_atr_stop=true: the safeguard stop reads ATR({period}) over "
                        f"{lookback}d of {ATR_INTERVAL} bars (sizing_mode={sizing_mode})"),
            ))

    if any(e in EARNINGS_EVENT_TYPES for e in event_types):
        for symbol in universe:
            out.append(Requirement(
                provider=FMP_PROVIDER,
                namespace=EARNINGS_CALENDAR_NAMESPACE,
                symbol=symbol,
                window=window,
                interval=None,
                kind=KIND_HISTORY,
                optional=False,
                reason="a days_to_earnings rule condition reads the quarterly earnings calendar",
            ))
            out.append(Requirement(
                provider=FMP_PROVIDER,
                namespace=EARNINGS_ESTIMATES_NAMESPACE,
                symbol=symbol,
                window=window,
                interval=None,
                kind=KIND_HISTORY,
                optional=True,
                reason=("days_to_earnings falls back to the ANNUAL analyst-estimate period "
                        "when the calendar holds no scheduled future print"),
            ))

    if any(e in COOLDOWN_EVENT_TYPES for e in event_types):
        out.append(Requirement(
            provider=PLATFORM_PROVIDER,
            namespace="closed_transactions",
            symbol=None,
            window=window,
            interval=None,
            kind=KIND_STATE,
            optional=False,
            reason=("a days_since_last_close cooldown rule reads this expert's closed "
                    "transactions from the trading database; there is nothing to warm, but a "
                    "replay without that state reproduces a different branch"),
        ))

    return out


__all__ = [
    "ATR_INTERVAL",
    "Adapter",
    "COOLDOWN_EVENT_TYPES",
    "EARNINGS_CALENDAR_NAMESPACE",
    "EARNINGS_ESTIMATES_NAMESPACE",
    "EARNINGS_EVENT_TYPES",
    "FMP_PROVIDER",
    "FRED_PROVIDER",
    "INDICATOR_PROVIDER",
    "KINDS",
    "KIND_HISTORY",
    "KIND_INDICATOR",
    "KIND_SERIES",
    "KIND_STATE",
    "KIND_TIMESERIES",
    "KIND_UNSUPPORTED",
    "MissingDependencySetting",
    "OHLCV_PROVIDER",
    "PLATFORM_PROVIDER",
    "Requirement",
    "Window",
    "adapter_for",
    "as_bool",
    "dedupe",
    "expert_replay_inputs",
    "iter_rule_event_types",
    "register_adapter",
    "registered_experts",
    "require_setting",
    "required_replay_inputs",
    "rule_requirements",
    "unregister_adapter",
    "unsupported_requirement",
]
