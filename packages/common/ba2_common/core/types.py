from enum import Enum
from dataclasses import dataclass
from datetime import datetime


class TimeInterval(str, Enum):
    """
    Standard timeframe intervals for market data.
    
    Maps user-friendly names to market data provider API formats.
    """
    # Minutes
    M1 = "1m"   # 1 minute
    M5 = "5m"   # 5 minutes
    M15 = "15m" # 15 minutes
    M30 = "30m" # 30 minutes
    
    # Hours
    H1 = "1h"   # 1 hour
    H4 = "4h"   # 4 hours
    
    # Days/Weeks/Months
    D1 = "1d"   # 1 day (daily)
    W1 = "1wk"  # 1 week (weekly)
    MO1 = "1mo" # 1 month (monthly)
    
    @classmethod
    def get_all_intervals(cls) -> list:
        """Get list of all supported intervals."""
        return [member.value for member in cls]


@dataclass
class MarketDataPoint:
    """
    Represents a single market data point with OHLC data.
    
    Attributes:
        symbol: The ticker symbol (e.g., 'AAPL', 'MSFT')
        timestamp: The datetime of the data point
        open: Opening price
        high: Highest price
        low: Lowest price
        close: Closing price
        volume: Trading volume
        interval: The timeframe interval (e.g., '1d', '1h', '5m')
    """
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: str = '1d'
    
    def __repr__(self):
        return (f"MarketDataPoint(symbol={self.symbol}, "
                f"timestamp={self.timestamp.strftime('%Y-%m-%d %H:%M')}, "
                f"O={self.open:.2f}, H={self.high:.2f}, L={self.low:.2f}, "
                f"C={self.close:.2f}, V={self.volume:.0f})")


class OrderStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"
    NEW = "new"
    UNKNOWN = "unknown"
    CANCELED = "canceled"
    PENDING = "pending"
    WAITING_TRIGGER = "waiting_trigger"
    WASHTRADE_LOCKED = "washtrade_locked"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING_NEW = "pending_new"
    # Additional Alpaca order statuses
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    DONE_FOR_DAY = "done_for_day"
    EXPIRED = "expired"
    REPLACED = "replaced"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    PENDING_REVIEW = "pending_review"
    ACCEPTED_FOR_BIDDING = "accepted_for_bidding"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    CALCULATED = "calculated"
    HELD = "held"
    ERROR = "ERROR"
    
    @classmethod
    def get_terminal_statuses(cls):
        """
        Return a set of order statuses that indicate the order is in a terminal/closed state.
        These are statuses where the order will not change anymore.
        
        Terminal statuses include:
        - CLOSED: Order is closed
        - REJECTED: Order was rejected by the broker
        - CANCELED: Order was canceled
        - EXPIRED: Order expired
        - STOPPED: Order was stopped
        - ERROR: Order encountered an error
        - REPLACED: Order was replaced by another order
        
        Returns:
            set: Set of OrderStatus values representing terminal states
        """
        return {
            cls.CLOSED,
            cls.REJECTED,
            cls.CANCELED,
            cls.EXPIRED,
            cls.STOPPED,
            cls.ERROR,
            cls.REPLACED,
        }

    @classmethod
    def resolve_pending_cancel(cls, broker_status):
        """Next status for an order we marked PENDING_CANCEL, given the broker's
        reported status on refresh.

        Returns the broker status once the order reaches a FINAL state — CANCELED
        (the cancel was confirmed) or FILLED / EXPIRED / REJECTED / ... (the order
        completed before the cancel landed). Returns ``None`` while it is still
        working or still cancelling, so the order stays PENDING_CANCEL and a
        dependent replacement keeps waiting until the broker truly releases it.
        """
        if broker_status is None:
            return None
        if broker_status == cls.FILLED or broker_status in cls.get_terminal_statuses():
            return broker_status
        return None

    @classmethod
    def get_executed_statuses(cls):
        """
        Return the set of order statuses that indicate the order was executed (position opened).

        Returns a module-level ``frozenset`` constant (``_EXECUTED_STATUSES``) rather than
        rebuilding the set on every call: this method is invoked ~1.5M times per backtest run
        (per-order in get_current_open_qty / get_pending_open_qty). A frozenset is safe here —
        verified no caller mutates the result; membership tests, SQLAlchemy ``.in_()`` and
        ``set(...)`` copies all work identically on a frozenset.

        Returns:
            frozenset: OrderStatus values representing executed states
        """
        return _EXECUTED_STATUSES
    
    @classmethod
    def get_unfilled_statuses(cls):
        """
        Return a set of order statuses that indicate the order is not yet filled.

        Semantics: "sent to the broker, but not yet filled". This set deliberately
        EXCLUDES the unsent WAITING_TRIGGER state (an order that exists only in our DB and
        has not been submitted). Use get_active_statuses() when you mean "any in-flight
        order, including not-yet-submitted triggers".

        Unfilled statuses include:
        - PENDING: Order is pending
        - NEW: Order is new
        - OPEN: Order is open
        - PENDING_NEW: Order is pending creation
        - WAITING_TRIGGER: Order is waiting for a trigger condition
        - ACCEPTED: Order was accepted but not filled
        - PENDING_CANCEL: Order is pending cancellation (special: waiting to be cancelled before replacement)
        - PENDING_REPLACE: Order is pending replacement
        - PENDING_REVIEW: Order is pending review
        - ACCEPTED_FOR_BIDDING: Order accepted for bidding
        - HELD: Order is held
        
        Returns:
            set: Set of OrderStatus values representing unfilled states
        """
        return {
            cls.PENDING,
            cls.NEW,
            cls.OPEN,
            cls.PENDING_NEW,
            cls.ACCEPTED,
            cls.PENDING_CANCEL,
            cls.PENDING_REPLACE,
            cls.PENDING_REVIEW,
            cls.ACCEPTED_FOR_BIDDING,
            cls.HELD,
        }
    
    @classmethod
    def get_unsent_statuses(cls):
        """
        Return a set of order statuses that indicate the order was never sent to the broker.
        These orders only exist in the database and can be safely closed without broker communication.
        
        Unsent statuses include:
        - PENDING: Order is pending submission to broker
        - WAITING_TRIGGER: Order is waiting for trigger condition (legacy)
        - WASHTRADE_LOCKED: Order is held back because an opposite-side order is
          working at the broker (would be rejected as a wash trade); exists only
          in the DB until the symbol clears.

        Returns:
            set: Set of OrderStatus values representing unsent states
        """
        return {
            cls.PENDING,
            cls.WAITING_TRIGGER,
            cls.WASHTRADE_LOCKED,
        }

    @classmethod
    def get_active_statuses(cls):
        """
        Return a set of order statuses that indicate the order is still "in flight":
        not terminal and not fully filled.

        This is the union of get_unfilled_statuses() (sent-but-not-filled) plus the two
        states that set excludes: WAITING_TRIGGER (staged, not yet submitted) and
        PARTIALLY_FILLED (working at the broker with shares already filled). Use this when
        the question is "is this order still active?" rather than "was it sent to the
        broker?".

        Returns:
            set: Set of OrderStatus values representing in-flight orders
        """
        return cls.get_unfilled_statuses() | {
            cls.WAITING_TRIGGER,
            cls.WASHTRADE_LOCKED,
            cls.PARTIALLY_FILLED,
        }


# Module-level immutable constant returned by OrderStatus.get_executed_statuses(). Defined
# once at import time so the ~1.5M per-run calls don't each build a fresh set. frozenset is
# safe: callers only do membership / .in_() / set() copies, never mutate it in place.
_EXECUTED_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.PARTIALLY_FILLED,
})


class InstrumentType(str, Enum):
    STOCK = "stock"
    ETF = "etf"
    CRYPTO = "crypto"

class AssetClass(str, Enum):
    EQUITY = "equity"
    OPTION = "option"


class OptionRight(str, Enum):
    CALL = "call"
    PUT = "put"

class OrderType(str, Enum):
    MARKET = "market"
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"
    BUY_STOP_LIMIT = "buy_stop_limit"
    SELL_STOP_LIMIT = "sell_stop_limit"
    TRAILING_STOP = "trailing_stop"
    # Triggered order types for TP/SL management
    OCO = "oco"  # One-Cancels-Other: TP and SL both defined, if one executes the other cancels
    OTO = "oto"  # One-Triggers-Other: Only TP or SL defined, triggers when parent order executes

class OrderDirection(str, Enum):
    SELL = "SELL"
    BUY = "BUY"

class BrokerOrderErrorReason(str, Enum):
    """Broker-agnostic classification of an order-submission rejection.

    Each broker's AccountInterface subclass maps its OWN native error shape (an Alpaca
    APIError's numeric code + message, an IBKR error code, ...) onto this shared taxonomy via
    ``_classify_order_error``. Generic recovery (e.g. resubmitting a breached stop as a market
    order) lives once in AccountInterface, keyed off these reasons — so a new reason handled for
    one broker benefits every broker without touching the retry logic itself.
    """
    INSUFFICIENT_FUNDS = "insufficient_funds"        # not enough buying power / cash
    INSUFFICIENT_QTY = "insufficient_qty"            # not enough shares/qty held to sell/close
    WASH_TRADE = "wash_trade"                        # opposing order already working this symbol
    STOP_THROUGH_MARKET = "stop_through_market"      # stop/trigger price already breached by market
    INVALID_SYMBOL = "invalid_symbol"                # broker doesn't recognize/support the symbol
    # The CREDENTIAL was refused, not the order: a 401/403, an expired token, or an OAuth
    # token missing the scope this endpoint needs. Distinct from UNKNOWN because it is
    # PERMANENT until a human re-authorizes -- nothing about the order can be adjusted to
    # make it succeed, and every retry is guaranteed to fail the same way. Recorded live on
    # 2026-08-21: a read-only TastyTrade token 403s every write endpoint (submit, cancel,
    # preview) and the SDK surfaced it as TastytradeError('') -- see
    # TastyTradeAccount._describe_broker_error.
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"                              # unmapped — broker message kept verbatim

class OrderOpenType(str, Enum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    EXTERNAL = "external"
    NOTOPENED = "notopened"


# ``Transaction.meta_data["origin"]`` values for share positions the platform did NOT
# buy — the stock leg of an option assignment. Written by the broker account's option-
# activity reconciler (AlpacaAccount._apply_option_activity) and read by
# HasAssignedSharesCondition; shared here so writer and reader cannot drift apart.
TXN_ORIGIN_CSP_ASSIGNMENT = "csp_assignment"   # long stock put to us by an assigned short put

class OrderRecommendation(str, Enum):
    SELL = "SELL"
    UNDERWEIGHT = "UNDERWEIGHT"
    HOLD = "HOLD"
    OVERWEIGHT = "OVERWEIGHT"
    BUY = "BUY"
    ERROR = "ERROR"

class TransactionStatus(str, Enum):
    WAITING = "WAITING"
    OPENED = "OPENED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"  # Transaction creation succeeded but order submission failed

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

class TimeHorizon(str, Enum):
    SHORT_TERM = "SHORT_TERM"
    MEDIUM_TERM = "MEDIUM_TERM"
    LONG_TERM = "LONG_TERM"

class ExpertEventRuleType(str, Enum):
    TRADING_RECOMMENDATION_RULE = "trading_recommendation_rule"

class AnalysisUseCase(str, Enum):
    ENTER_MARKET = "enter_market"
    OPEN_POSITIONS = "open_positions"
    
class ExpertEventType(str, Enum):
    # F = Flag/Boolean
    F_BEARISH = "bearish"
    F_BULLISH = "bullish"
    F_HAS_NO_POSITION = "has_no_position"
    F_HAS_POSITION = "has_position"
    F_HAS_BUY_POSITION = "has_buy_position"    # Expert has an open BUY (long) position
    F_HAS_SELL_POSITION = "has_sell_position"   # Expert has an open SELL (short) position
    F_HAS_NO_POSITION_ACCOUNT = "has_no_position_account"
    F_HAS_POSITION_ACCOUNT = "has_position_account"
    F_RATING_NEGATIVE_TO_NEUTRAL = "rating_negative_to_neutral"
    F_RATING_NEGATIVE_TO_POSITIVE = "rating_negative_to_positive"
    F_RATING_NEUTRAL_TO_NEGATIVE = "rating_neutral_to_negative"
    F_RATING_NEUTRAL_TO_POSITIVE = "rating_neutral_to_positive"
    F_RATING_POSITIVE_TO_NEGATIVE = "rating_positive_to_negative"
    F_RATING_POSITIVE_TO_NEUTRAL = "rating_positive_to_neutral"
    # Ordinal rating moves across the full 5-grade scale
    # (SELL < UNDERWEIGHT < HOLD < OVERWEIGHT < BUY): fire whenever the latest
    # recommendation's grade rank rose (upgraded) or fell (downgraded) vs the
    # previous one. Covers OVERWEIGHT/UNDERWEIGHT transitions that the 3-bucket
    # rating_*_to_* events cannot express.
    F_RATING_UPGRADED = "rating_upgraded"
    F_RATING_DOWNGRADED = "rating_downgraded"
    F_CURRENT_RATING_POSITIVE = "current_rating_positive"
    F_CURRENT_RATING_OVERWEIGHT = "current_rating_overweight"
    F_CURRENT_RATING_NEUTRAL = "current_rating_neutral"
    F_CURRENT_RATING_UNDERWEIGHT = "current_rating_underweight"
    F_CURRENT_RATING_NEGATIVE = "current_rating_negative"
    F_SHORT_TERM = "short_term"
    F_MEDIUM_TERM = "medium_term"
    F_LONG_TERM = "long_term"
    F_HIGHRISK = "highrisk"
    F_MEDIUMRISK = "mediumrisk"
    F_LOWRISK = "lowrisk"
    F_NEW_TARGET_HIGHER = "new_target_higher"  # New expert target is higher than current TP (with 2% tolerance)
    F_NEW_TARGET_LOWER = "new_target_lower"    # New expert target is lower than current TP (with 2% tolerance)
    # Option-related flags
    F_HAS_OPTION_POSITION = "has_option_position"  # Expert has an open option position
    F_HAS_COVERED_CALL = "has_covered_call"        # Expert has an open covered call
    F_HAS_PROTECTIVE_PUT = "has_protective_put"    # Expert has an open protective put
    # Shares the expert did not buy: the stock leg of an assigned short put. The wheel's
    # covered-call rule needs this INSTEAD of has_buy_position, which cannot tell assigned
    # stock apart from stock the same expert bought outright.
    F_HAS_ASSIGNED_SHARES = "has_assigned_shares"  # Expert holds stock from an option assignment

    # N = Number/Count
    N_EXPECTED_PROFIT_TARGET_PERCENT = "expected_profit_target_percent"
    N_PERCENT_TO_CURRENT_TARGET = "percent_to_current_target"  # Distance from current price to current TP
    N_PERCENT_TO_NEW_TARGET = "percent_to_new_target"          # Distance from current price to new expert target
    N_NEW_TARGET_PERCENT = "new_target_percent"                # Percent change from current TP to new target (positive if higher, negative if lower)
    N_PROFIT_LOSS_AMOUNT = "profit_loss_amount"
    N_PROFIT_LOSS_PERCENT = "profit_loss_percent"
    # Current price vs. FMPRating's analyst price-target lines (target_low/target_high/
    # target_consensus, already persisted on ExpertRecommendation.data["FMPRating"] by
    # FMPRating.run_analysis) - lets an entry rule gate on WHERE price sits relative to the
    # analyst range, decoupled from the expert's BUY/SELL/HOLD rating. Positive % = price is
    # ABOVE that target line.
    N_PRICE_VS_TARGET_LOW_PERCENT = "price_vs_target_low_percent"
    N_PRICE_VS_TARGET_HIGH_PERCENT = "price_vs_target_high_percent"
    N_PRICE_VS_TARGET_CONSENSUS_PERCENT = "price_vs_target_consensus_percent"
    N_DAYS_OPENED = "days_opened"
    # Cooldown gates: calendar days since this expert last CLOSED a transaction on the symbol.
    # ANY close, only a PROFITABLE close, or only a LOSING close. Used to stop churning the
    # same symbol immediately after exiting it (e.g. require N days between a close and a new
    # entry, optionally only after a loss). When the expert never closed the symbol the value
    # is a large sentinel so a ">" cooldown gate passes (no prior trade -> entry allowed).
    N_DAYS_SINCE_LAST_CLOSE = "days_since_last_close"
    N_DAYS_SINCE_LAST_PROFITABLE_CLOSE = "days_since_last_profitable_close"
    N_DAYS_SINCE_LAST_LOSING_CLOSE = "days_since_last_losing_close"
    N_CONFIDENCE = "confidence"
    N_INSTRUMENT_ACCOUNT_SHARE = "instrument_account_share"    # Current instrument value as % of expert virtual equity
    N_PERCENT_OPEN_TO_NEW_TARGET = "percent_open_to_new_target"  # Distance from open price to new expert target as %
    # Option-related numeric events
    N_PERCENT_BELOW_RECENT_HIGH = "percent_below_recent_high"  # Percent the current price is below the recent high
    N_PERCENT_ABOVE_RECENT_LOW = "percent_above_recent_low"    # Percent the current price is above the recent low
    N_IV_RANK = "iv_rank"                                      # Implied volatility rank (0-100)
    # The UNDERLYING's current-bar volume as a multiple of its own trailing average
    # (1.0 == normal). The underlying, deliberately, not a contract: most individual contracts
    # print zero on most days, so a contract-level ratio is undefined far more often than it
    # is informative. UNEVALUABLE (never 1.0) on insufficient history or a zero-volume
    # average -- "normal" is the one wrong default that looks right.
    N_RELATIVE_VOLUME = "relative_volume"
    # ATM implied volatility divided by the underlying's REALISED volatility. The variance
    # risk premium made explicit, and the actual edge in premium selling: you are paid implied
    # and you pay out realised. Distinct from iv_rank, which compares a symbol's IV to its OWN
    # history and says nothing about whether the stock is earning that IV.
    N_IV_TO_REALIZED_VOL = "iv_to_realized_vol"
    N_DAYS_TO_EARNINGS = "days_to_earnings"                    # Calendar days until the next earnings announcement
    # THE SAME QUANTITY, DIFFERENT SOURCE (design 2026-08-31 leaps-grid S9, the TIMING SPLIT).
    # Days-to-earnings AS THE RANKING EXPERT MEASURED IT, read back off
    # ExpertRecommendation.data["FMPEarningsEvent"]["days_to_earnings"] -- never a second
    # calendar fetch. O_ERN chains behind FMPEarningsEvent: the expert owns the RANKING and
    # already resolved the event date to score the symbol, so the strategy's entry gene must
    # gate on THAT number or the two sides can disagree about when the event is. Point-in-time
    # consistent with the rank, and free (no provider call per bar per symbol).
    #
    # It does NOT replace N_DAYS_TO_EARNINGS, which fetches the calendar itself and stays the
    # answer for UNCHAINED uses (any expert, no stamp required). The two differ where it
    # matters: this one is UNEVALUABLE -- fires in NEITHER direction -- when there is no stamp,
    # which is every recommendation from every other expert. Reading an absent stamp as 0 would
    # satisfy "<= X" for the whole universe.
    N_REC_DAYS_TO_EARNINGS = "rec_days_to_earnings"
    # Calendar days SINCE the stamped event, for an open position: the exit half of the same
    # timing split ("exit Y days after the print", Y searched 0-2). The reference is the event
    # date the ENTRY order carried forward (TradingOrder.data["earnings_event_date"], stamped at
    # submit beside max_loss_per_contract), NOT the current recommendation -- by exit time that
    # is a different, later one. 0 ON the event day, 1 the next calendar day. UNEVALUABLE when
    # the entry order carries no event date, which is every order not opened off an
    # earnings-event recommendation.
    N_DAYS_AFTER_EVENT = "days_after_event"
    # Calendar days of option life REMAINING (expiry - the evaluation date). The complement of
    # N_DAYS_OPENED, which counts days ELAPSED: with the entry DTE window itself a tuned gene,
    # "21 days after opening" and "21 days before expiry" are different quantities. This is what
    # makes roll-at-DTE expressible as a RULE (it previously existed only inside
    # OptionPortfolioManager, so no other expert could roll and the GA could not optimise the
    # roll point) and it is the only exit criterion a 0DTE structure can have. Negative past
    # expiry; UNEVALUABLE (never 0, never infinity) when the expiry cannot be determined.
    N_DAYS_TO_EXPIRY = "days_to_expiry"
    # Unrealized LOSS as a % of the structure's own DEFINED maximum loss (positive while
    # losing, +100 when the whole defined risk is gone). The denominator is the
    # max_loss_per_contract the submit path persisted on the parent order's data (design
    # 2026-08-29 S8.2) times the contract count -- read back, never reconstructed from legs.
    # Scale-free where a %-of-credit stop drifts with however much credit a trial collected.
    # Structures whose max loss was not MEASURED at submit (short calls) have no persisted
    # value, and the condition is then UNEVALUABLE: it never fires in either direction.
    N_LOSS_PCT_OF_MAX_LOSS = "loss_pct_of_max_loss"
    # Current structure value as a MULTIPLE of the entry premium paid on a LONG (debit)
    # option position or spread: value = current_value / entry_premium = 1 + (profit_loss
    # _percent / 100), riding the same _get_pnl_for_condition plumbing as
    # loss_pct_of_max_loss. Scale-free: 1 and 5 contracts read the identical multiple.
    # Meaningless for a credit (SELL) entry -- there is no "multiple of premium paid" when
    # no premium was paid -- so it is UNEVALUABLE (never fires, in EITHER direction) for a
    # credit structure, an order with no resolvable transaction, or a P&L the option-quote
    # machinery cannot price. A PROFIT-side gate (like profit_loss_percent's ">" reading),
    # never a stop -- see TradeActionEvaluator._LOSS_SIDE_STOP_OPERATORS, which
    # deliberately omits it.
    N_PROFIT_MULTIPLE_OF_PREMIUM = "profit_multiple_of_premium"
    # THE THREE TWO-EXPIRY READERS (plan Task 6). A diagonal's legs answer different
    # questions, so "the DTE" and "the delta" stop being quantities until somebody says WHICH
    # LEG -- and these three say so in their names, which is the whole discipline
    # ``option_expiry`` exists to enforce.
    #
    # Calendar days of life left on the SHORT leg -- the roll WINDOW
    # (``option_expiry.EXPIRY_RULE_ROLL_WINDOW``). Its sibling ``days_to_expiry`` reads the
    # LONG leg, because that one asks the structure-exit question. Unevaluable (never fires,
    # in EITHER direction) on a structure with no held short leg.
    N_SHORT_LEG_DAYS_TO_EXPIRY = "short_leg_days_to_expiry"
    # Calendar days of life left on the covered CALL an equity-entry overlay key wrote --
    # resolved through the trade REPOSITORY (expert + underlying), not through the evaluated
    # transaction. That is the whole reason it is a separate event type rather than a reuse of
    # ``days_to_expiry``: on ``O_CC``/``O_WHEEL`` the manage pass is anchored to the STOCK
    # position, so a transaction-anchored DTE reader finds no option legs and is INERT (never
    # fires, in either direction, in either runtime). Same source ``has_covered_call`` uses.
    # Unevaluable when no covered call is held, when one is held whose expiry is not recorded,
    # or when two different expiries are held at once (a contradiction, never a min()).
    N_COVERED_CALL_DAYS_TO_EXPIRY = "covered_call_days_to_expiry"
    # How much of the short overlay's own collected credit has decayed, as a percent: 0 the
    # day it was sold, 100 when it can be bought back for nothing, NEGATIVE when it has gone
    # against us. The buyback trigger of design 2026-08-31 leaps-grid §4. Unevaluable when the
    # overlay cannot be priced or was sold for nothing -- an undefined percentage, never 100.
    N_CREDIT_DECAYED_PCT = "credit_decayed_pct"
    # The |delta| of the LONG leg -- the LEAPS. Design §4's third structure exit ("PMCC: LEAPS
    # delta < ~0.50, searched on/off"): a stock replacement that has stopped tracking the
    # underlying is no longer the position that was opened. Unevaluable when no quote carries
    # a delta, which is every account whose data source publishes none.
    N_LONG_LEG_DELTA = "long_leg_delta"


class ExpertActionType(str, Enum):
    SELL = "sell"
    BUY = "buy"
    CLOSE = "close"
    ADJUST_TAKE_PROFIT = "adjust_take_profit"
    ADJUST_STOP_LOSS = "adjust_stop_loss"
    INCREASE_INSTRUMENT_SHARE = "increase_instrument_share"
    DECREASE_INSTRUMENT_SHARE = "decrease_instrument_share"
    STOP_PROCESSING = "stop_processing"
    # Option-related actions
    BUY_CALL = "buy_call"
    OPEN_BULL_CALL_SPREAD = "open_bull_call_spread"
    SELL_COVERED_CALL = "sell_covered_call"
    BUY_PUT = "buy_put"
    OPEN_BEAR_PUT_SPREAD = "open_bear_put_spread"
    BUY_PROTECTIVE_PUT = "buy_protective_put"
    SELL_CASH_SECURED_PUT = "sell_cash_secured_put"
    OPEN_BEAR_CALL_SPREAD = "open_bear_call_spread"
    OPEN_BULL_PUT_SPREAD = "open_bull_put_spread"
    OPEN_STRADDLE = "open_straddle"
    OPEN_STRANGLE = "open_strangle"
    OPEN_SHORT_STRADDLE = "open_short_straddle"
    OPEN_SHORT_STRANGLE = "open_short_strangle"
    OPEN_IRON_CONDOR = "open_iron_condor"
    OPEN_JADE_LIZARD = "open_jade_lizard"
    OPEN_CALL_BUTTERFLY = "open_call_butterfly"
    OPEN_PUT_RATIO_SPREAD = "open_put_ratio_spread"
    # RATIO BACKSPREADS (1x2), the convexity-financed pair -- SELL 1 nearer leg, BUY 2
    # further-out legs of the SAME right and expiry. The mirror image of
    # OPEN_PUT_RATIO_SPREAD above, which is a FRONTspread (BUY 1, SELL 2) and therefore
    # net short; these are net LONG options and are covered by construction.
    OPEN_CALL_BACKSPREAD = "open_call_backspread"
    OPEN_PUT_BACKSPREAD = "open_put_backspread"
    # THE POOR MAN'S COVERED CALL (design 2026-08-31 leaps-grid §3-§4): a LEAPS call
    # (DTE >= 365) covered by a nearer-dated short call at a HIGHER strike. The first
    # structure in this platform whose legs sit on TWO expiries -- see
    # ``option_expiry.MULTI_EXPIRY_OPTION_STRATEGIES``, which declares the ``"pmcc"``
    # strategy tag the submit guard and both DTE readers consult.
    OPEN_PMCC = "open_pmcc"
    # ROLL THE OVERLAY, KEEP THE LONG. Buys back the PMCC's expiring short call and sells the
    # next one, as ONE order on the same transaction. Not an entry (it opens no structure) and
    # not a close (the position survives it) -- the wheel-pattern maintenance step design
    # 2026-08-31 leaps-grid §4 calls the roll loop.
    ROLL_PMCC_SHORT = "roll_pmcc_short"
    CLOSE_OPTION = "close_option"

class MarketAnalysisStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING = "running"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
class ReferenceValue(str, Enum):
    ORDER_OPEN_PRICE = "order_open_price"
    CURRENT_PRICE = "current_price"
    EXPERT_TARGET_PRICE = "expert_target_price"

class WorkerTaskStatus(Enum):
    """Status of a worker task."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class ActivityLogSeverity(str, Enum):
    """Severity level for activity log entries."""
    SUCCESS = "success"
    INFO = "info"
    WARNING = "warning"
    FAILURE = "failure"
    DEBUG = "debug"

class ActivityLogType(str, Enum):
    """Type of activity being logged."""
    APPLICATION_STATUS_CHANGE = "application_status_change"
    TRANSACTION_CREATED = "transaction_created"
    TRANSACTION_TP_CHANGED = "transaction_tp_changed"
    TRANSACTION_SL_CHANGED = "transaction_sl_changed"
    TRANSACTION_CLOSED = "transaction_closed"
    RISK_MANAGER_RAN = "risk_manager_ran"
    ANALYSIS_STARTED = "analysis_started"
    ANALYSIS_COMPLETED = "analysis_completed"
    ANALYSIS_FAILED = "analysis_failed"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    EXPERT_RECOMMENDATION = "expert_recommendation"
    RULE_EXECUTED = "rule_executed"
    TP_SL_ADJUSTED = "tp_sl_adjusted"
    TRADE_ACTION_OPEN = "trade_action_open"
    TRADE_ACTION_NEW = "trade_action_new"

def get_reference_value_options():
    """Return dictionary of reference value options with user-friendly labels."""
    return {
        ReferenceValue.ORDER_OPEN_PRICE.value: 'Order Open Price',
        ReferenceValue.CURRENT_PRICE.value: 'Current Market Price',
        ReferenceValue.EXPERT_TARGET_PRICE.value: 'Expert Target Price'
    }


# Comparison operators the rule engine actually evaluates for numeric (N_*) event
# conditions. SINGLE source of truth: this list mirrors TradeConditions.operator_map
# (the consumer that maps each string to a Python operator and rejects anything else).
# Both the live trade-platform rule editor and the backtest test platform source their
# numeric-operator picker from here so the offered operators always equal the ones the
# engine accepts. (Note: '==' — not '=' — is the equality operator the engine recognises.)
NUMERIC_OPERATORS = [">", ">=", "<", "<=", "==", "!="]


def get_operator_options():
    """Return the list of numeric comparison operators the rule engine accepts."""
    return list(NUMERIC_OPERATORS)


# Helper functions for UI logic
def get_numeric_event_values():
    """Return list of numeric event type values (N_ prefixed enums)."""
    return [
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
        ExpertEventType.N_PERCENT_TO_CURRENT_TARGET.value,
        ExpertEventType.N_PERCENT_TO_NEW_TARGET.value,
        ExpertEventType.N_PROFIT_LOSS_AMOUNT.value,
        ExpertEventType.N_PROFIT_LOSS_PERCENT.value,
        ExpertEventType.N_DAYS_OPENED.value,
        ExpertEventType.N_DAYS_SINCE_LAST_CLOSE.value,
        ExpertEventType.N_DAYS_SINCE_LAST_PROFITABLE_CLOSE.value,
        ExpertEventType.N_DAYS_SINCE_LAST_LOSING_CLOSE.value,
        ExpertEventType.N_CONFIDENCE.value,
        ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE.value,
        ExpertEventType.N_PERCENT_OPEN_TO_NEW_TARGET.value,
        ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH.value,
        ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW.value,
        ExpertEventType.N_IV_RANK.value,
        ExpertEventType.N_RELATIVE_VOLUME.value,
        ExpertEventType.N_IV_TO_REALIZED_VOL.value,
        ExpertEventType.N_DAYS_TO_EARNINGS.value,
        ExpertEventType.N_REC_DAYS_TO_EARNINGS.value,
        ExpertEventType.N_DAYS_AFTER_EVENT.value,
        ExpertEventType.N_DAYS_TO_EXPIRY.value,
        ExpertEventType.N_LOSS_PCT_OF_MAX_LOSS.value,
        ExpertEventType.N_PROFIT_MULTIPLE_OF_PREMIUM.value,
        ExpertEventType.N_SHORT_LEG_DAYS_TO_EXPIRY.value,
        ExpertEventType.N_COVERED_CALL_DAYS_TO_EXPIRY.value,
        ExpertEventType.N_CREDIT_DECAYED_PCT.value,
        ExpertEventType.N_LONG_LEG_DELTA.value,
    ]


def get_adjustment_action_values():
    """Return list of adjustment action type values (ADJUST_ prefixed enums)."""
    return [
        ExpertActionType.ADJUST_TAKE_PROFIT.value,
        ExpertActionType.ADJUST_STOP_LOSS.value
    ]


def get_share_adjustment_action_values():
    """Return list of share adjustment action type values (INCREASE/DECREASE_INSTRUMENT_SHARE)."""
    return [
        ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
        ExpertActionType.DECREASE_INSTRUMENT_SHARE.value
    ]


def get_option_action_values():
    """Return list of option action type values (CALL/PUT entries + COVERED_CALL/CLOSE_OPTION)."""
    return [
        ExpertActionType.BUY_CALL.value,
        ExpertActionType.OPEN_BULL_CALL_SPREAD.value,
        ExpertActionType.SELL_COVERED_CALL.value,
        ExpertActionType.BUY_PUT.value,
        ExpertActionType.OPEN_BEAR_PUT_SPREAD.value,
        ExpertActionType.BUY_PROTECTIVE_PUT.value,
        ExpertActionType.SELL_CASH_SECURED_PUT.value,
        ExpertActionType.OPEN_BEAR_CALL_SPREAD.value,
        ExpertActionType.OPEN_BULL_PUT_SPREAD.value,
        ExpertActionType.OPEN_STRADDLE.value,
        ExpertActionType.OPEN_STRANGLE.value,
        ExpertActionType.OPEN_SHORT_STRADDLE.value,
        ExpertActionType.OPEN_SHORT_STRANGLE.value,
        ExpertActionType.OPEN_IRON_CONDOR.value,
        ExpertActionType.OPEN_JADE_LIZARD.value,
        ExpertActionType.OPEN_CALL_BUTTERFLY.value,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD.value,
        ExpertActionType.OPEN_CALL_BACKSPREAD.value,
        ExpertActionType.OPEN_PUT_BACKSPREAD.value,
        ExpertActionType.OPEN_PMCC.value,
        ExpertActionType.ROLL_PMCC_SHORT.value,
        ExpertActionType.CLOSE_OPTION.value,
    ]


def get_option_entry_action_values():
    """Option action types that OPEN a structure by SELECTING contracts from a chain.

    The option actions minus the two that act on a position that already exists:

    * ``close_option`` resolves its contracts from the held position and takes no selection
      parameters at all;
    * ``roll_pmcc_short`` re-selects ONE leg, but from the box the ENTRY recorded on its own
      order row rather than from parameters of its own -- so a producer that offers it strike
      or DTE fields is offering knobs nothing reads, and a harness that runs it from a bare
      set of entry kwargs is asking it to build a structure it is not there to build.

    Every "for each option ENTRY action" audit derives from here, so a future action that
    manages rather than opens is excluded once, in one place, instead of being subtracted by
    hand in each of them.
    """
    return [v for v in get_option_action_values()
            if v not in (ExpertActionType.CLOSE_OPTION.value,
                         ExpertActionType.ROLL_PMCC_SHORT.value)]


def is_option_entry_action(action_value):
    """Check whether an option action type OPENS a structure by selecting from a chain."""
    return action_value in get_option_entry_action_values()


def is_numeric_event(event_value):
    """Check if an event value corresponds to a numeric event type."""
    return event_value in get_numeric_event_values()


def is_adjustment_action(action_value):
    """Check if an action value corresponds to an adjustment action type."""
    return action_value in get_adjustment_action_values()


def is_share_adjustment_action(action_value):
    """Check if an action value corresponds to a share adjustment action type."""
    return action_value in get_share_adjustment_action_values()


def is_option_action(action_value):
    """Check if an action value corresponds to an option action type."""
    return action_value in get_option_action_values()


def get_wing_width_action_values():
    """Option action types whose builder reads ``wing_width_pct``.

    These four place a protective/second leg at a distance MEASURED FROM another leg's
    strike, so the width is a real strategy parameter rather than a byproduct of the strike
    method; every other structure ignores it. Kept next to the action enum (not in the UI)
    so a producer can render the field for exactly the actions that consume it -- offering
    it everywhere would be a decoy, and offering it nowhere is what left live iron condors /
    jade lizards / butterflies / ratio spreads silently pinned to their class constants."""
    return [
        ExpertActionType.OPEN_IRON_CONDOR.value,
        ExpertActionType.OPEN_JADE_LIZARD.value,
        ExpertActionType.OPEN_CALL_BUTTERFLY.value,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD.value,
    ]


def uses_wing_width(action_value):
    """Check whether an option action type reads ``wing_width_pct``."""
    return action_value in get_wing_width_action_values()


def get_short_dte_window_action_values():
    """Option action types that select a leg from a SECOND, nearer expiry window.

    Exactly one today: ``open_pmcc``. Its LEAPS leg is picked from ``dte_min``/``dte_max``
    (365+) and its short overlay from ``short_dte_min``/``short_dte_max`` (30-45) — two
    windows, because two expiries is what a diagonal IS. Every other structure puts all its
    legs on one expiry and would read the second window as a decoy.

    Kept beside the action enum for the same reason as ``get_wing_width_action_values``: a
    producer (the rule editor, the GA's action table) must render exactly the fields its
    consumer reads. Offering it everywhere is the OPT-S2 trap in a new place; offering it
    nowhere is what left ``wing_width_pct`` unreachable from a live rule for a year, so that
    a live 4-leg structure ran on a class constant with nothing on screen saying so.
    """
    return [
        ExpertActionType.OPEN_PMCC.value,
    ]


def uses_short_dte_window(action_value):
    """Check whether an option action type reads ``short_dte_min``/``short_dte_max``."""
    return action_value in get_short_dte_window_action_values()


def get_arc_floor_action_values():
    """Option action types whose builder reads ``min_arc`` (the premium-richness floor).

    These EIGHT are the CREDIT structures -- the ones that post collateral, so that
    "annualised return on collateral" has a denominator at all. Each calls
    ``_refuse_if_arc_below_floor`` beside its ``net_credit <= 0`` check.

    "EXACTLY THE RESERVING STRATEGIES WITH A BUILDER" WAS THE RULE UNTIL 2026-09-01 and is
    no longer, so do not re-derive this list from ``RESERVING_STRATEGIES`` and a
    no-builder test. Two exemptions now exist, for two different reasons, and BOTH are
    enumerated in ``option_economics.ARC_FLOOR_EXEMPT_STRATEGIES`` (the one list the two
    drift guards read): ``credit_spread`` / ``naked_put`` / ``debit_spread`` are pricing
    aliases with no action at all, and the two 1x2 BACKSPREADS reserve and have builders
    but deliberately never consult the gate -- their net is near zero by design and may be
    a debit, so a floor would refuse exactly the structures design 2026-08-31 SS2 searches.

    Every other option action EXCEPT THOSE TWO is in ``ZERO_RESERVE_STRATEGIES`` and
    reserves nothing -- a long call, a butterfly, and notably a COVERED CALL, which
    collects a credit but is collateralised by SHARES. For those the ratio is undefined,
    ``option_economics`` returns None, and a configured floor turns None into a REFUSAL.
    So offering the field there would not merely be a decoy (the ``wing_width_pct``
    failure mode) -- it would be a field whose every value silently deletes the structure.
    Kept next to the action enum, not in the UI, so a producer renders it for exactly the
    actions that consume it.
    """
    return [
        ExpertActionType.SELL_CASH_SECURED_PUT.value,
        ExpertActionType.OPEN_BEAR_CALL_SPREAD.value,
        ExpertActionType.OPEN_BULL_PUT_SPREAD.value,
        ExpertActionType.OPEN_SHORT_STRADDLE.value,
        ExpertActionType.OPEN_SHORT_STRANGLE.value,
        ExpertActionType.OPEN_IRON_CONDOR.value,
        ExpertActionType.OPEN_JADE_LIZARD.value,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD.value,
    ]


def uses_arc_floor(action_value):
    """Check whether an option action type reads ``min_arc``."""
    return action_value in get_arc_floor_action_values()


def get_strike_method_action_values():
    """Option action types whose builder actually READS ``strike_method``.

    These THIRTEEN of the twenty ``_OptionEntryAction`` subclasses pass
    ``method=self.strike_method`` into the selector. The other seven -- straddle, short
    straddle, short strangle, iron condor, jade lizard, call butterfly, put ratio spread --
    hard-code ``method="percent_otm"`` at every selection site, so ``strike_method`` is set on
    the shared base and then never read: a dead attribute. (The long STRANGLE left that list on
    2026-09-02; the long STRADDLE is on it for a different reason from the rest -- its strike
    is not a parameter at all, see ``OpenStraddleAction``.)

    THIS IS A LIVE TRAP, not a curiosity (OPT-S2). The rule editor rendered the Strike
    Method select for every non-close option action, DEFAULTED IT TO ``delta``,
    placeholdered Strike Param as ``0.30``, and persisted the choice unconditionally. A user
    configuring an iron condor saw "delta", typed ``0.30`` expecting a 30-delta short, and
    got a strike 0.30 PERCENT out of the money -- effectively at the money, on the leg that
    carries the risk. Nothing anywhere warned.

    So a producer -- the rule editor, or the GA's strike-method gene -- must offer the choice
    for exactly the actions that honour it. Offering it everywhere is that trap; offering it
    nowhere is what left ``percent_otm`` the only strike gene in all 16 option grids
    (OPT-C3). Refusal, NOT fallback: teaching those eight builders to honour delta depends on
    whether delta is computable from the chain data, which is separate work. Until then the
    honest interface is "this structure selects by % OTM", said out loud.

    Kept next to the action enum rather than in the UI for the same reason as
    ``get_wing_width_action_values``: a producer must render exactly the fields its consumer
    reads. Two independent drift guards, written against different consumers and arriving by
    different routes, agree on this same set of thirteen --
    ``packages/common/tests/test_option_strike_method_honoured.py`` runs each builder and
    watches which method the selector was actually handed, and
    ``packages/common/tests/test_strike_method_registry.py`` derives the set from the action
    classes' own source. Either alone would catch a builder being fixed or broken; both
    existing is a cross-check, not duplication, and they were written without knowledge of
    each other.
    """
    return [
        ExpertActionType.BUY_CALL.value,
        ExpertActionType.OPEN_BULL_CALL_SPREAD.value,
        ExpertActionType.BUY_PUT.value,
        ExpertActionType.OPEN_BEAR_PUT_SPREAD.value,
        ExpertActionType.SELL_COVERED_CALL.value,
        ExpertActionType.BUY_PROTECTIVE_PUT.value,
        ExpertActionType.SELL_CASH_SECURED_PUT.value,
        ExpertActionType.OPEN_BEAR_CALL_SPREAD.value,
        ExpertActionType.OPEN_BULL_PUT_SPREAD.value,
        # The two 1x2 BACKSPREADS (2026-09-01). Both legs are box picks routed through
        # ``select_vertical_spread`` with ``method=self.strike_method`` -- the same call the
        # four verticals above make -- so the delta method reaches BOTH the short leg and the
        # long pair. They are the first structures DESIGNED around ``delta`` (design
        # 2026-08-31 SS2 searches the short leg at 0.35-0.50 and the longs at 0.15-0.30), and
        # a per-leg target is expressed the way every vertical already expresses one: a
        # 2-element ``strike_param`` read by ``_spread_params`` as ``(long, short)``.
        ExpertActionType.OPEN_CALL_BACKSPREAD.value,
        ExpertActionType.OPEN_PUT_BACKSPREAD.value,
        # The long STRANGLE (2026-09-02). Both legs go through ``select_single`` with
        # ``method=self.strike_method`` and ONE shared target, which is what makes a delta
        # target a symmetric strangle: the selector ranks on ABSOLUTE delta, so a single
        # |delta| picks a call above spot and a put below it. This is design 2026-08-31
        # section 2's "strangle width delta 0.25-0.45" becoming expressible -- until now
        # O_ERN's width had to be searched in percent because the builder read nothing else.
        # Its sibling ``open_straddle`` is deliberately still absent: a straddle's strike is
        # not a parameter (both legs sit on the ATM strike by definition), so a method there
        # could never move a strike. See ``OpenStraddleAction``'s docstring.
        ExpertActionType.OPEN_STRANGLE.value,
        # The PMCC (2026-09-02, plan Task 6). BOTH legs are ``select_single`` picks made with
        # ``method=self.strike_method``, one per expiry window, and design 2026-08-31 §2
        # states both targets in DELTA (LEAPS 0.75-0.85, overlay 0.15-0.30) -- a per-leg pair
        # read by ``_spread_params`` as ``(long, short)``, the same shape the verticals and
        # the backspreads already use. ``roll_pmcc_short`` is deliberately NOT here: it
        # re-selects the overlay from the SPEC THE ENTRY STAMPED on its order row, never from
        # ``self.strike_method``, so offering it a method knob would be a decoy.
        ExpertActionType.OPEN_PMCC.value,
    ]


def honours_strike_method(action_value):
    """Check whether an option action type's builder reads ``strike_method`` at all."""
    return action_value in get_strike_method_action_values()


def get_action_type_display_label(action_value):
    """
    Get user-friendly display label for an ExpertActionType value.
    
    Maps enum values to more descriptive labels:
    - 'buy' -> 'bullish (buy)'
    - 'sell' -> 'bearish (sell)'
    - Others are capitalized with underscores replaced by spaces
    
    Args:
        action_value: The ExpertActionType enum value (e.g., 'buy', 'sell')
        
    Returns:
        User-friendly display label string
    """
    if action_value == ExpertActionType.BUY.value:
        return "bullish (buy)"
    elif action_value == ExpertActionType.SELL.value:
        return "bearish (sell)"
    else:
        # Capitalize and replace underscores with spaces for other actions
        return action_value.replace("_", " ").title()


from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Recommendation:
    """Value object returned by every expert's pure ``_process``.

    NOT the SQLModel ``ExpertRecommendation`` row — live ``run_analysis`` maps this
    to an ``ExpertRecommendation`` + ``AnalysisOutput`` rows; the backtest engine maps
    it to an enter/exit/hold/skip signal with no DB persistence. SKIP is first-class
    (FMPRating no-coverage / FactorRanker empty-universe).
    """
    signal: OrderRecommendation          # BUY/SELL/HOLD/OVERWEIGHT/UNDERWEIGHT/ERROR
    confidence: float                    # 1-100 scale (platform convention)
    current_price: float                 # the as_of close, resolved in _gather
    details: str = ""
    expected_profit_percent: Optional[float] = None
    target_price: Optional[float] = None   # expert's recommended TP price (None -> backtest derives from expected_profit_percent)
    raw_outputs: Dict[str, Any] = field(default_factory=dict)   # -> AnalysisOutput rows
    skip: bool = False
    skip_reason: Optional[str] = None

    def almost_equals(self, other: "Recommendation", tol: float = 1e-6) -> bool:
        """Golden-test equality: identical signal/skip + float-tolerant numerics + details."""
        if not isinstance(other, Recommendation):
            return False
        if self.signal != other.signal or self.skip != other.skip:
            return False
        if (self.skip_reason or "") != (other.skip_reason or ""):
            return False
        if self.details != other.details:
            return False

        def _close(a, b):
            if a is None or b is None:
                return a is None and b is None
            return abs(float(a) - float(b)) <= tol
        return _close(self.confidence, other.confidence) and \
               _close(self.expected_profit_percent, other.expected_profit_percent)