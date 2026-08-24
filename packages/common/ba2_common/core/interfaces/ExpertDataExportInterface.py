"""ExpertDataExportInterface — read-only per-symbol metric export for research
tools (SYMBOL360), decoupled from producing a live trading Recommendation or
requiring a DB-backed ExpertInstance.

Reuses each expert's REAL analyze_as_of (the same _gather/_process path
backtests and live analysis run) via a bypass-constructed instance — the exact
`Cls.__new__(Cls); e.id = 1` pattern already used throughout
packages/experts/tests/test_*_gather_process.py, just wrapped as a reusable
classmethod instead of copy-pasted per test file.

Settings note: some experts read settings from context.settings inside
_gather/_process; others (FactorRanker._resolve_universe) read
self.settings/get_setting_with_interface_default directly. The bypass factory
sets self._settings_cache to the SAME merged dict passed as context.settings
so both paths agree.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation
from ba2_common.logger import logger as _module_logger


@dataclass
class ExpertMetric:
    """One row in a SYMBOL360 metric card.

    ``detail`` is free text explaining the number: the raw inputs, what they
    were compared against, and the arithmetic that produced ``value``. It may
    be a one-liner or a multi-step derivation -- see ``plan_metric_detail``
    for which of those gets a hover tooltip and which gets an expandable
    panel. ``detail_table`` carries the same kind of evidence in the shape it
    is actually easiest to read: (label, value) pairs rendered as a small
    two-column table (analyst-rating buckets, price targets, the components
    that sum to a section score).
    """
    label: str
    value: Any
    display: str
    signal: Optional[str] = None   # "buy" | "sell" | "neutral" | None (n/a)
    detail: Optional[str] = None
    detail_table: Optional[List[Tuple[str, str]]] = None


#: A detail longer than this (or containing ANY newline) cannot be read in a
#: hover tooltip and goes to an expandable panel instead. Tooltips are HTML:
#: newlines collapse to spaces, so a multi-step derivation renders as one
#: continuous line wider than the viewport, clipped at both ends and
#: impossible to scroll, select or copy. That is the FMPRating card's actual
#: defect, and it is shared by every card that puts structured text in
#: ``detail``.
DETAIL_INLINE_MAX_CHARS = 90

#: Applied to whatever legitimately STAYS a tooltip, so even a short detail
#: can never overflow the viewport again.
DETAIL_TOOLTIP_STYLE = "white-space: pre-line; max-width: 28rem;"


@dataclass
class MetricDetailLayout:
    """Where one metric's evidence should be drawn. Pure: the renderer only
    draws what this decides, so the rule is unit-testable without a browser."""
    mode: str                      # "none" | "tooltip" | "panel"
    text: str = ""                 # normalized detail text ("" when absent)
    lines: List[str] = field(default_factory=list)
    table: List[Tuple[str, str]] = field(default_factory=list)
    title: str = ""                # panel heading


def _normalize_detail(detail: Optional[str]) -> str:
    """CRLF -> LF, strip trailing/leading blank lines and per-line trailing
    whitespace. A whitespace-only detail is 'no detail'."""
    if not detail:
        return ""
    lines = [ln.rstrip() for ln in str(detail).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def plan_metric_detail(metric: ExpertMetric) -> MetricDetailLayout:
    """Decide how ``metric``'s detail is presented.

    Long derivations belong in an expandable panel, not a hover bubble: a
    calculation you cannot scroll, select or copy is not readable. Only a
    genuinely short one-liner (no newlines, <= DETAIL_INLINE_MAX_CHARS) stays
    a tooltip, and even that gets a wrapping, width-capped style.
    """
    text = _normalize_detail(getattr(metric, "detail", None))
    table = [(str(k), str(v)) for k, v in (getattr(metric, "detail_table", None) or [])]
    if not text and not table:
        return MetricDetailLayout(mode="none")
    lines = text.split("\n") if text else []
    if table or len(lines) > 1 or len(text) > DETAIL_INLINE_MAX_CHARS:
        return MetricDetailLayout(mode="panel", text=text, lines=lines, table=table,
                                  title=f"{metric.label} — details")
    return MetricDetailLayout(mode="tooltip", text=text, lines=lines, table=table,
                              title=f"{metric.label} — details")


@dataclass
class ExpertDataExport:
    """Adapted result of one expert's export_symbol_data call."""
    expert_name: str
    symbol: str
    overall_signal: Optional[str] = None
    confidence: Optional[float] = None
    metrics: List[ExpertMetric] = field(default_factory=list)
    settings_used: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    skipped: bool = False   # True when the expert's Recommendation had skip=True
                             # (e.g. no analyst coverage, empty universe) — distinct
                             # from a normal HOLD; see rec.skip_reason on the metric row
    signal_unavailable_reason: Optional[str] = None
    # Set when the expert declared (via EXPORT_SIGNAL_UNAVAILABLE_REASON) that
    # its Recommendation.signal is not a per-symbol verdict. ``overall_signal``
    # is then None so a UI draws no badge: a constant badge is worse than no
    # badge, since it looks like a per-symbol call and is identical for every
    # symbol ever searched.
    confidence_unavailable_reason: Optional[str] = None
    # Set when the expert declared (via EXPORT_CONFIDENCE_UNAVAILABLE_REASON)
    # that it does not compute a per-symbol confidence at all, so the 0.0 its
    # Recommendation carries is a placeholder, not a measurement. ``confidence``
    # is then None and a UI must say "n/a" plus this reason -- printing
    # "Confidence: 0.0%" states zero conviction, which is a different and false
    # claim. A genuinely computed 0.0 (expert with no such declaration) is
    # untouched and still reported as 0.0.
    relevant_settings: Tuple[str, ...] = ()
    # Curated allowlist of settings_used keys worth showing/editing in a
    # SYMBOL360-style UI settings panel -- see
    # ExpertDataExportInterface.export_relevant_settings(). settings_used
    # itself is untouched (still the FULL merged dict, needed for
    # correctness elsewhere); this only tells a UI which keys are worth a
    # user's attention for THIS expert's displayed metrics.


_SIGNAL_MAP = {
    OrderRecommendation.BUY: "buy",
    OrderRecommendation.OVERWEIGHT: "buy",
    OrderRecommendation.SELL: "sell",
    OrderRecommendation.UNDERWEIGHT: "sell",
    OrderRecommendation.HOLD: "hold",
}


#: Sentinel ``self.id`` given to every bypass-constructed export instance —
#: never a real ExpertInstance DB row. Exported so a basket-level expert's
#: own live-only helpers (e.g. FactorRanker._gather_holdings, which otherwise
#: instantiates a portfolio manager against this id) can detect "this is a
#: read-only export, not a real deployed instance" without a magic literal.
EXPORT_BYPASS_ID = -1


class ExpertDataExportInterface:
    """Mixin: add alongside MarketExpertInterface to opt an expert class into
    SYMBOL360 (or any other read-only research tool). Requires the class to
    implement _gather/_process/analyze_as_of (the Phase-1 BacktestInterface
    contract) — export_symbol_data raises AttributeError, caught into
    ExpertDataExport.error, for anything that isn't wired that way."""

    #: Optional curated allowlist of setting keys worth exposing in a
    #: SYMBOL360-style UI settings panel: knobs that change what THIS
    #: expert's export_symbol_data metrics/signal actually display, not
    #: execution/sizing knobs (position sizing, profit targets, universe-
    #: selection plumbing, live-only risk-manager settings, ...) that never
    #: surface in _build_export_metrics and would just be confusing clutter.
    #: None (the default) means "no curation declared" -- see
    #: export_relevant_settings() for the fallback. Override per expert,
    #: e.g. FMPEarningsDrift = ("surprise_min_pct", "max_days_since_report")
    #: (excluding its own expected_profit_percent/_mode/dynamic_scale, which
    #: only affect a Recommendation field this expert's card never renders).
    EXPORT_RELEVANT_SETTINGS: Optional[Tuple[str, ...]] = None

    #: Set to a short sentence by an expert that never computes a per-symbol
    #: confidence (a basket/ranking expert whose Recommendation carries a
    #: literal 0.0 placeholder). export_symbol_data then reports
    #: confidence=None + this reason instead of a bare 0.0, which a card
    #: renders as "0.0%" and a reader takes for zero conviction. Leave at
    #: None for every expert whose confidence is a real measurement --
    #: including one that legitimately measures 0.0.
    EXPORT_CONFIDENCE_UNAVAILABLE_REASON: Optional[str] = None

    #: Same opt-in for the header BADGE: set by an expert whose
    #: Recommendation.signal is a fixed constant rather than a per-symbol
    #: verdict (a basket/ranking expert says "here is the book", so its card
    #: would show the identical badge for every symbol ever searched). None
    #: for every expert whose signal really is about the requested symbol.
    EXPORT_SIGNAL_UNAVAILABLE_REASON: Optional[str] = None

    @classmethod
    def export_default_settings(cls) -> Dict[str, Any]:
        """Class-default settings, INCLUDING builtin keys (enabled_instruments,
        instrument_selection_method, ...) — needed by FactorRanker's
        single-symbol override (a later plan task)."""
        return {k: d.get("default")
                for k, d in cls.get_merged_settings_definitions().items()}

    @classmethod
    def export_relevant_settings(cls) -> Tuple[str, ...]:
        """Keys from settings_used worth showing in a UI settings panel.

        Falls back to this expert's OWN declared settings only
        (get_settings_definitions(), never get_merged_settings_definitions())
        when EXPORT_RELEVANT_SETTINGS isn't set — i.e. every generic
        base-class setting (enable_buy, risk_manager_model, screener_*,
        sizing_mode, ...) is excluded by default regardless of which expert,
        since none of those are ever reflected in an export card's displayed
        metrics. Override EXPORT_RELEVANT_SETTINGS to narrow further when an
        expert's OWN settings still include execution-only knobs."""
        if cls.EXPORT_RELEVANT_SETTINGS is not None:
            return cls.EXPORT_RELEVANT_SETTINGS
        return tuple(cls.get_settings_definitions().keys())

    @classmethod
    def _bypass_instance(cls, settings: Dict[str, Any], providers=None, symbol: Optional[str] = None):
        """Construct without __init__/_load_expert_instance. Sentinel id=-1
        (never a real DB row); _settings_cache pre-populated so self.settings
        and context.settings agree (see module docstring).

        The base MarketExpertInterface._get_current_price(symbol) resolves the
        price via a DB-backed ExpertInstance -> Account lookup, which the
        sentinel id=-1 instance has no row for; its own broad except-Exception
        silently returns None, and any expert whose live _gather branch
        (as_of is None) calls self._get_current_price(symbol) and then
        formats it (e.g. FMPRating's f"${current_price:.2f}") crashes with a
        TypeError on NoneType.__format__ instead. When ``providers`` and
        ``symbol`` are given, bind an instance-level override that resolves
        the price via providers.price_at_date(symbol, now) instead -- no DB
        needed.

        Only bind it when NOTHING has already customized _get_current_price
        on this class (a real subclass override, or a test's
        monkeypatch.setattr(SomeExpert, "_get_current_price", ...) applied
        before calling export_symbol_data): an instance attribute always wins
        over a class attribute in Python's lookup, so binding unconditionally
        would silently shadow that customization regardless of call order.
        Detected via identity against the base class's own method object.

        ``self._export_symbol`` is always stashed (independent of the
        providers/price-override branch below) so a BASKET-level expert's
        ``_build_export_metrics`` override can recover the symbol that was
        requested -- it has no other way to see it, since ``rec`` and
        ``settings`` (its only two parameters) carry no per-symbol identity
        for an expert whose Recommendation spans a whole universe. See
        FactorRanker._build_export_metrics for the consumer.
        """
        self = cls.__new__(cls)
        self.id = EXPORT_BYPASS_ID
        self._settings_cache = dict(settings)
        self._export_symbol = symbol
        self.instance = None
        cls._ensure_builtin_settings()
        self.logger = _module_logger
        if (providers is not None and symbol is not None
                and getattr(cls, "_get_current_price", None)
                    is MarketExpertInterface._get_current_price):
            def _price_override(sym, *a, **kw):
                return providers.price_at_date(sym, datetime.now(timezone.utc))
            self._get_current_price = _price_override
        return self

    @classmethod
    def export_symbol_data(cls, symbol: str,
                            overrides: Optional[Dict[str, Any]] = None,
                            as_of=None,
                            providers_resolver: Optional[Callable[..., Any]] = None
                            ) -> "ExpertDataExport":
        """Bypass-construct, merge settings, call analyze_as_of, adapt the
        Recommendation into an ExpertDataExport. Never raises: any exception
        (bad symbol, missing API key, thin history, ...) is captured into
        ExpertDataExport.error so one card's failure never breaks the page.

        providers_resolver: injection point for tests (mirrors LiveProviderBundle's
        get_provider callable). Production callers omit it and get the real
        live provider registry via ba2_common.core.TradeConditions._get_provider.
        """
        settings: Dict[str, Any] = {}
        try:
            settings = {**cls.export_default_settings(), **(overrides or {})}
            if providers_resolver is None:
                from ba2_common.core.TradeConditions import _get_provider
                providers_resolver = _get_provider
            providers = LiveProviderBundle(providers_resolver)
            self = cls._bypass_instance(settings, providers=providers, symbol=symbol)
            context = BacktestContext(providers=providers, settings=settings,
                                      as_of=as_of, extra={"symbol": symbol})
            rec = self.analyze_as_of(as_of, context)
            if isinstance(rec, list):   # basket experts (e.g. Copy-trade style) — not exported
                raise TypeError(f"{cls.__name__}.analyze_as_of returned a list; "
                                f"per-symbol export is not defined for basket experts")
            metrics = self._build_export_metrics(rec, settings)
            no_conf = cls.EXPORT_CONFIDENCE_UNAVAILABLE_REASON
            no_sig = cls.EXPORT_SIGNAL_UNAVAILABLE_REASON
            return ExpertDataExport(
                expert_name=cls.__name__, symbol=symbol,
                overall_signal=None if no_sig else _SIGNAL_MAP.get(rec.signal),
                signal_unavailable_reason=no_sig,
                confidence=None if no_conf else rec.confidence,
                confidence_unavailable_reason=no_conf,
                metrics=metrics,
                settings_used=settings, raw=dict(rec.raw_outputs or {}),
                skipped=rec.skip, relevant_settings=cls.export_relevant_settings())
        except Exception as e:  # noqa: BLE001 — intentional: this IS the isolation boundary
            _module_logger.warning(f"{cls.__name__}.export_symbol_data({symbol}) failed: {e}",
                                   exc_info=True)
            return ExpertDataExport(expert_name=cls.__name__, symbol=symbol,
                                    settings_used=settings, error=str(e))

    def _build_export_metrics(self, rec: Recommendation,
                              settings: Dict[str, Any]) -> List[ExpertMetric]:
        """Default: one row summarizing the Recommendation. Override for
        richer per-section rows (DeterministicScorer, FMPInsiderClusterBuy).

        A skipped Recommendation (rec.skip=True — e.g. no analyst coverage,
        empty universe) is "nothing to evaluate", not a real HOLD, so it gets
        its own row carrying rec.skip_reason rather than being rendered as a
        normal signal card."""
        if rec.skip:
            return [ExpertMetric(
                label="Skipped", value=rec.skip_reason,
                display=f"Skipped — {rec.skip_reason}" if rec.skip_reason else "Skipped",
                signal=None, detail=rec.skip_reason)]
        sig = _SIGNAL_MAP.get(rec.signal)
        return [ExpertMetric(
            label="Recommendation", value=rec.signal.value,
            display=f"{rec.signal.value} ({rec.confidence:.0f}%)",
            signal=sig, detail=rec.details)]
