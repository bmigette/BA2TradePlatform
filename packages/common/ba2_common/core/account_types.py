"""Broker-agnostic account value objects (pure dataclasses, no DB/SDK deps).

``get_account_info()`` returns a pydantic ``TradeAccount`` on Alpaca, a dict on
IBKR and TastyTrade, and ``None`` on Alpaca auth failure. These dataclasses are
the single shape every broker adapter maps ONTO, so no call site has to guess.

Every money field is a plain ``float``, and the ADAPTER coerces at the mapping
boundary: Alpaca publishes its balances as strings and TastyTrade's
``BuyingPowerEffect.change_in_buying_power`` is a ``Decimal``. Nothing here
re-coerces -- an uncoerced ``OrderImpact`` would make ``bp_cost`` return a
``Decimal`` in breach of its own ``-> float`` annotation, and that is the
adapter bug surfacing rather than being masked.

stdlib imports only -- this module must stay importable from both
``core/interfaces/*`` and ``core/portfolio_allocation.py`` with no cycle.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


# ``CashTransfer.event_type`` and ``portfolio_income_event.event_type`` are PLAIN
# str (small enums are str columns here -- matching OptionActivity.activity_type,
# and avoiding the SQLModel str-enum-stored-by-NAME migration trap). These are the
# ONLY legal spellings; always use the constant, never a bare literal.
CASH_TRANSFER_DEPOSIT = "DEPOSIT"
CASH_TRANSFER_WITHDRAWAL = "WITHDRAWAL"
CASH_TRANSFER_DIVIDEND = "DIVIDEND"

# Provenance of a MarginInfo.bp_factor, best first (see the design's
# "precheck over estimation" ordering).
MARGIN_SOURCE_PRECHECK = "precheck"    # broker order dry-run (preview_order_impact)
MARGIN_SOURCE_ASSET = "asset"          # per-asset metadata (Alpaca Asset + multiplier)
MARGIN_SOURCE_POSITION = "position"    # derived from a held position's requirement
MARGIN_SOURCE_DEFAULT = "default"      # conservative fallback = account multiplier

# Provenance of a MarketHours answer. PLAIN str (same reasoning as the
# CASH_TRANSFER_* constants); always use the constant, never a bare literal.
# No module outside this one may re-declare these strings.
MARKET_HOURS_SOURCE_BROKER = "broker"            # the broker's own clock/session endpoint
MARKET_HOURS_SOURCE_FALLBACK = "fallback"        # our offline NYSE regular-session calendar
MARKET_HOURS_SOURCE_UNAVAILABLE = "unavailable"  # the lookup FAILED -- caller must fail closed


@dataclass
class AccountSnapshot:
    """Broker-agnostic cash / equity / buying-power state of one account.

    Every numeric field is ``Optional`` and defaults to ``None``: a field the
    broker did not supply stays ``None`` and the CALLER must raise rather than
    substitute a default (platform rule: no fallback values for prices, balances
    or quantities). ``None`` here means "unknown", never "zero".

    ``margin_multiplier`` is Alpaca's ``TradeAccount.multiplier`` (a STRING there:
    "1" / "2" / "4"), i.e. how many dollars of buying power one dollar of equity
    yields. It is the conservative ``default_bp_factor`` fed to the engine.

    ``equity`` is cash plus positions marked to market (Alpaca
    ``TradeAccount.equity``); ``net_liquidation`` is what the account would be
    worth if every position were closed right now (TastyTrade
    ``net-liquidating-value``). They are the same number for a cash/equities
    account and diverge only where liquidation value is not the mark. An adapter
    whose broker publishes only one MUST set BOTH to that value rather than
    leave one ``None``. Neither is the allocation denominator -- the engine's
    base is ``buying_power`` plus the managed position value (see
    ``build_base_snapshot``) -- so report ``net_liquidation`` as the account's
    headline total value.

    ``short_market_value`` is NEGATIVE while shorts are held (the Alpaca
    convention). A broker that publishes a positive magnitude instead
    (TastyTrade's ``short-equity-value``) MUST be negated by its adapter, so
    that gross exposure is one formula for every broker.
    """
    cash: Optional[float] = None
    equity: Optional[float] = None
    net_liquidation: Optional[float] = None
    buying_power: Optional[float] = None
    non_marginable_buying_power: Optional[float] = None
    margin_multiplier: Optional[float] = None
    is_margin_account: bool = False
    long_market_value: Optional[float] = None
    short_market_value: Optional[float] = None
    pending_transfer_in: Optional[float] = None
    supports_fractional: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CashTransfer:
    """One broker cash movement: a deposit, a withdrawal or a dividend.

    ``external_id`` MUST be the broker's own activity id -- it is the
    ``(account_id, external_id)`` idempotency key of ``portfolio_income_event``,
    so re-syncing the same window upserts instead of duplicating.

    ``amount`` is POSITIVE for deposits and dividends and NEGATIVE for
    withdrawals (Alpaca ``CSW`` net_amount). Only ``is_income`` rows are
    persisted to the ledger; withdrawals are not income.
    """
    external_id: str
    event_date: date
    event_type: str                     # CASH_TRANSFER_DEPOSIT | _WITHDRAWAL | _DIVIDEND
    amount: float
    symbol: Optional[str] = None        # payer symbol for DIVIDEND; None for cash transfers
    description: Optional[str] = None

    @property
    def is_income(self) -> bool:
        """True when this event may fund an allocation run."""
        return (
            self.event_type in (CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND)
            and self.amount > 0
        )


@dataclass(frozen=True)
class MarginInfo:
    """Per-symbol margin / fractionability metadata used to size buying power.

    ``bp_factor`` = ``initial_margin_rate * account_multiplier`` -- the dollars of
    buying power one dollar of NOTIONAL consumes. A fully marginable stock in a
    2:1 account is ``0.5 * 2 = 1.0`` (dollar for dollar); a non-marginable one is
    ``1.0 * 2 = 2.0`` (double). Note ``initial_margin_rate`` is a property of the
    ACCOUNT as much as the symbol: where the account cannot borrow at all
    (multiplier 1) it is 1.0 even for a marginable name.

    FROZEN: adapters cache these and hand the SAME object to every caller
    (``AlpacaAccount._margin_info_cache``), so an in-place edit would silently
    poison every later reader. Derive a changed copy with
    ``dataclasses.replace()``.

    ``min_order_size`` / ``min_trade_increment`` mirror Alpaca ``Asset`` field
    names exactly, and BOTH ARE SHARE QUANTITIES. ``min_order_size`` is the
    smallest number of SHARES the broker will accept in one order; the engine
    compares it directly against a rounded share count
    (``portfolio_allocation.round_quantity``, ``_suppress_below_min_order``).
    A MONEY floor must never be stored here -- 5.0 meaning "$5" would be read as
    "5 shares", which on a $34 ETF suppresses every order below $170 instead of
    every order below $5, and looks like nothing at all from the outside.

    ``min_fractional_notional`` is the money floor, in DOLLARS, and applies only
    to an order whose quantity is FRACTIONAL. TastyTrade publishes one (see
    ``TastyTradeAccount.MIN_FRACTIONAL_NOTIONAL_USD``); Alpaca does not, and
    leaves it ``None``. ``None`` means "the broker published no floor", never 0.0.
    It is deliberately NOT applied to whole-share orders: the broker's rule is
    worded "Fractional equities orders ...", so enforcing it on every order would
    refuse a legal 1-share buy of a stock trading under $5.

    ``fractionable`` is TRI-STATE, and it is the ONE canonical per-symbol
    fractional-eligibility flag that crosses adapter/engine boundaries:
      * ``True``  -- the broker SAYS the symbol can be traded in fractions
        (Alpaca ``Asset.fractionable is True``; TastyTrade
        ``Equity.is_fractional_quantity_eligible is True`` AND a quantity step
        is known).
      * ``False`` -- the broker SAYS it cannot (Alpaca ``Asset.fractionable is
        False``; TastyTrade ``is_fractional_quantity_eligible is False``).
      * ``None``  -- the broker DID NOT SAY. This is the default, and it covers
        TastyTrade's ``is_fractional_quantity_eligible is None``, an eligible
        symbol with no published step, and any lookup that failed. A symbol
        OMITTED from ``get_symbol_margin_info()``'s dict is implicitly in this
        state too.
    Never write ``bool(x)`` or ``getattr(obj, 'fractionable', False)`` at an
    adapter boundary: that is the failure-becomes-``False`` antipattern, and it
    reports a broker fact nobody published. Code that must tell the three apart
    tests ``is True`` / ``is False`` / ``is None``, never truthiness. ``None`` is
    falsy, so ``_round_shares`` (portfolio_allocation.py:351-372) keeps taking the
    conservative whole-share branch unchanged -- the widening is
    behaviour-compatible.

    ``min_trade_increment`` has ONE meaning across every adapter: the smallest
    QUANTITY step the broker will accept for this symbol. It is a step in
    SHARES, never in dollars, and never a hint derived from ``fractionable``.
      * ``None`` means the broker did not publish one. It is NOT 1.0 and NOT
        "no limit": the caller must fall back to whole shares rather than
        assume a finer step (platform rule: no fabricated values for live
        broker data). Alpaca leaves it ``None`` when ``Asset.min_trade_increment``
        is absent; TastyTrade leaves it ``None`` when the quantity-precision
        table could not be fetched.
      * ``1.0`` means whole shares. A symbol with ``fractionable = False``
        steps by 1.0 BY DEFINITION -- that is a fact about the symbol, not a
        reading, so an adapter reports it even when its precision lookup failed.
      * anything smaller is the broker's published fractional step (Alpaca
        ``Asset.min_trade_increment``, TastyTrade's equity
        ``QuantityDecimalPrecision.value`` -- ``value``, the quantity decimal
        precision, NOT the sibling ``minimum-increment-precision``, which is 0
        on the live equity row and would report whole shares).
    ``fractionable = True`` with ``min_trade_increment = None`` is therefore a
    legal, meaningful pair: the symbol can be split, but by an unknown step.
    """
    symbol: str
    bp_factor: float
    marginable: bool = True
    fractionable: Optional[bool] = None    # TRI-STATE: True / False / None = "broker did not say"
    #: Will the broker accept an order for this symbol AT ALL. TRI-STATE on the
    #: same terms as ``fractionable``: ``False`` is the broker SAYING no (Alpaca
    #: ``Asset.tradable is False`` -- a delisted, halted or never-supported name),
    #: ``None`` is "nobody said", which is what a symbol omitted from
    #: ``get_symbol_margin_info()`` means and must never be read as a refusal.
    #:
    #: DISTINCT FROM ``marginable``, which is about how much buying power the
    #: order costs; this is about whether there is an order at all. A plan sized
    #: perfectly against a non-tradable symbol is a plan with a guaranteed
    #: rejection in it, and until this field existed nothing in the allocation
    #: path asked the question -- Alpaca publishes it on the same ``Asset`` row
    #: every other field here already comes from.
    tradable: Optional[bool] = None
    min_order_size: Optional[float] = None          # SHARES
    min_trade_increment: Optional[float] = None     # SHARES
    min_fractional_notional: Optional[float] = None  # DOLLARS, fractional orders only
    initial_margin_rate: Optional[float] = None
    maintenance_margin_rate: Optional[float] = None
    source: str = MARGIN_SOURCE_DEFAULT


@dataclass
class OrderImpact:
    """Result of a broker-side order dry-run (precheck).

    ``change_in_buying_power`` is the broker's SIGNED value: TastyTrade's
    ``BuyingPowerEffect.change_in_buying_power`` is NEGATIVE for a buy (see the
    ``set_sign_for`` validator, tastytrade/order.py:366-393). Always consume the
    ``bp_cost`` property rather than the raw signed field.
    """
    symbol: str
    change_in_buying_power: float
    margin_requirement: Optional[float] = None   # isolated_order_margin_requirement
    estimated_fees: Optional[float] = None       # fee_calculation.total_fees
    accepted: bool = True                        # False when the broker returned errors
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def bp_cost(self) -> float:
        """Positive buying power CONSUMED by this order (0.0 when it frees BP)."""
        return -self.change_in_buying_power if self.change_in_buying_power < 0 else 0.0


@dataclass(frozen=True)
class MarketHours:
    """Whether the market is trading right now, and when that next changes.

    Defined ONCE, here. Every broker adapter maps onto this shape; no adapter
    declares its own market-hours type, its own source strings or its own session
    times.

    The field set maps 1:1 onto Alpaca's ``Clock``
    (alpaca/trading/models.py:348 -- ``timestamp``, ``is_open``, ``next_open``,
    ``next_close``), because that adapter has the least freedom. ``Clock``
    publishes no "when did the CURRENT session start", so ``open_at`` stays
    ``None`` there while a session is live; TastyTrade's ``MarketSession``
    (tastytrade/market_sessions.py) publishes ``open_at``/``close_at`` and fills
    everything.

    ``next_open`` and ``next_close`` are both STRICTLY FUTURE instants, so while
    the market is open ``next_open`` is tomorrow's open and ``next_close`` is
    today's. When the market is SHUT, ``open_at``/``close_at`` describe the
    UPCOMING session and therefore equal ``next_open``/``next_close`` -- so a
    caller that just wants "the session in question" can read ``open_at`` in both
    states without branching. A field an adapter cannot supply stays ``None``;
    ``None`` never means "now" and never means "unknown, so guess".

    ``source`` is one of the three ``MARKET_HOURS_SOURCE_*`` constants.
    ``unavailable`` means the lookup FAILED -- the broker could not answer AND the
    offline calendar could not either. In that state ``is_open`` is ``False`` so
    the submit gate fails closed, but ``is_known`` is ``False`` so the UI says
    "unknown" rather than lying that the market is closed. This is the
    ``get_positions() -> None vs []`` rule applied to a clock: failure is a third
    state, not ``False``.

    ``status`` is the BROKER'S OWN raw word -- "Open", "Extended", "Pre-market" --
    unnormalised and DISPLAY-ONLY. Never branch on it. It is what lets the banner
    explain an extended-hours block; there is deliberately no ``close_at_ext``
    field and ``is_open`` stays regular-session-only.

    ``detail`` is a human sentence: the fallback's cause, or the broker's error
    text. Never parsed.

    Every datetime is timezone-aware and ``__post_init__`` REFUSES a naive one, so
    a naive broker datetime cannot escape into the UI. Read as UTC instead of
    Eastern it moves the boundary by four or five hours, which is the difference
    between "submit" and "the broker rejects the batch". Adapters normalise at
    their own boundary (``AlpacaAccount._to_market_utc``, TastyTrade's
    ``now_in_new_york``-derived values); this is the backstop.

    ``is_open`` has no default -- it is required -- and every other field defaults
    to the SHUT / unknown direction on purpose. An adapter that only half-fills
    this must not be able to produce an accidental "open".

    FROZEN: ``ReadOnlyAccountInterface.get_market_hours()`` caches one instance
    and hands the same object to every caller, so an in-place edit would silently
    poison every later reader (the ``MarginInfo`` rule). Derive a changed copy
    with ``dataclasses.replace()`` -- which is exactly what an adapter's fallback
    path does.

    REJECTED spellings, not to be introduced anywhere: ``opens_at``, ``closes_at``,
    ``open``, ``close``, ``open_now``, ``reason``, ``checked_at``.
    """
    is_open: bool
    open_at: Optional[datetime] = None
    close_at: Optional[datetime] = None
    next_open: Optional[datetime] = None
    next_close: Optional[datetime] = None
    source: str = MARKET_HOURS_SOURCE_FALLBACK
    status: Optional[str] = None
    detail: Optional[str] = None
    as_of: Optional[datetime] = None

    def __post_init__(self) -> None:
        """Refuse any naive datetime. The invariant every consumer assumes.

        Raises:
            ValueError: naming the offending field. No guess is made about which
                timezone was meant: assuming UTC shifts the 09:30/16:00 ET
                boundaries by four or five hours, and assuming Eastern hides a
                genuine adapter bug.
        """
        for name in ("open_at", "close_at", "next_open", "next_close", "as_of"):
            value = getattr(self, name)
            if value is None:
                continue
            if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
                raise ValueError(
                    f"MarketHours.{name} must be timezone-aware; got the naive "
                    f"{value!r}. Normalise at the adapter boundary "
                    f"(AlpacaAccount._to_market_utc / TastyTrade's "
                    f"now_in_new_york-derived instants) -- a naive value here would "
                    f"silently shift the 09:30/16:00 ET boundaries")

    @property
    def next_transition(self) -> Optional[datetime]:
        """The next instant at which ``is_open`` flips, or ``None`` if unknown.

        This is the correct cache-expiry key for this object. A plain elapsed-time
        TTL cannot express "valid until 16:00" and will happily serve ``is_open =
        True`` one second after the bell.
        """
        candidates = [t for t in (self.next_open, self.next_close) if t is not None]
        return min(candidates) if candidates else None

    @property
    def is_known(self) -> bool:
        """False only when the lookup FAILED (``source == "unavailable"``).

        ``is_open is False`` alone cannot be distinguished from "we have no idea",
        and the UI needs to say which -- "Market closed, opens Monday 09:30 ET" is
        a very different message from "Could not determine market status". The
        submit gate treats both as not-open; only the copy differs.
        """
        return self.source != MARKET_HOURS_SOURCE_UNAVAILABLE


# What a broker would actually do with a fractional order quantity. PLAIN str, like
# the CASH_TRANSFER_*, MARGIN_SOURCE_* and MARKET_HOURS_SOURCE_* families above;
# always use the constant.
#
# ALPACA-INTERNAL. These and FractionalPreview below live here only because
# account_types.py is where this codebase keeps its cross-layer value objects; no
# non-Alpaca code reads them. The cross-broker per-symbol eligibility carrier is
# MarginInfo.fractionable.
FRACTIONAL_OUTCOME_WHOLE = "whole"        # nothing fractional about it
FRACTIONAL_OUTCOME_KEPT = "kept"          # the fraction reaches the broker as sized
FRACTIONAL_OUTCOME_FLOORED = "floored"    # floored to whole shares BEFORE submission
FRACTIONAL_OUTCOME_SKIPPED = "skipped"    # floors to 0: nothing is sent at all
FRACTIONAL_OUTCOME_REJECTED = "rejected"  # the fraction would reach the broker and be refused


@dataclass(frozen=True)
class FractionalPreview:
    """What a broker would do with one order quantity, computed BEFORE submitting.

    Exists so the allocation dry run can state the truth about rounding instead of
    discovering it from a rejection or a silently CANCELED row. Roughly a quarter of a
    real book is not fractionable, so mixed eligibility is the normal case.

    ``submit_quantity`` is what would actually reach the broker: equal to
    ``requested_quantity`` for WHOLE and KEPT, the floored share count for FLOORED,
    ``None`` for SKIPPED (nothing is sent), and the unchanged fraction for REJECTED
    (it is sent, and refused). It is always a QUANTITY IN SHARES -- there is no
    dollar-denominated order anywhere in this codebase.

    ``fractionable`` is TRI-STATE, exactly like ``MarginInfo.fractionable``. ``None``
    means the broker did not say -- never coerce it to ``False``: a fabricated
    ``False`` makes the dry run promise a whole-share rounding that will not happen,
    and a fabricated ``True`` promises a fraction the broker then refuses. This is a
    LOCAL copy of the flag because a preview is a submission-planning object, not a
    cross-boundary carrier; ``MarginInfo.fractionable`` remains the one flag that
    crosses module boundaries.

    ``constraint`` is the bare broker rule that was hit ("fractional qty 4.25 is not
    accepted by Alpaca on a sell_limit order"); ``reason`` is the full sentence shown to
    the operator and persisted on the order row, and it INCLUDES the constraint.

    FROZEN: previews are handed to the UI and to the submission path; an in-place edit
    would let one reader change what another was promised.
    """
    symbol: str
    requested_quantity: float
    submit_quantity: Optional[float]
    outcome: str
    requires_day_tif: bool = False
    fractionable: Optional[bool] = None
    constraint: Optional[str] = None
    reason: Optional[str] = None

    @property
    def will_submit(self) -> bool:
        """False only when nothing at all reaches the broker (the SKIP case)."""
        return self.outcome != FRACTIONAL_OUTCOME_SKIPPED

    @property
    def is_adjusted(self) -> bool:
        """True when the quantity the caller asked for is not the quantity that trades.

        This is the flag a dry run counts to decide how prominent the rounding warning
        has to be.
        """
        return self.outcome in (FRACTIONAL_OUTCOME_FLOORED, FRACTIONAL_OUTCOME_SKIPPED)
