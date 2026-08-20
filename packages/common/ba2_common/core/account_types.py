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
from datetime import date
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
    names exactly.
    """
    symbol: str
    bp_factor: float
    marginable: bool = True
    fractionable: bool = False
    min_order_size: Optional[float] = None
    min_trade_increment: Optional[float] = None
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
