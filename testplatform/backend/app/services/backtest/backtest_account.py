"""``BacktestAccount`` — a simulated broker implementing the live ``AccountInterface``.

The whole point of the design (§5) is to reuse the live decision/sizing/order code
unchanged: the real expert -> ``Recommendation`` -> ``TradeConditions`` -> classic
``TradeRiskManagement`` -> ``position_sizing`` -> ``account.submit_order()`` path runs
against THIS simulated broker. ``BacktestAccount`` therefore inherits ALL of
``AccountInterface``'s concrete orchestration (``submit_order`` validation/persistence,
``refresh_transactions`` lifecycle, ``close_transaction*``, the ``_validate_*`` helpers,
wash-trade locks) and only implements the broker-specific abstracts.

Equities-only v1: it inherits ``AccountInterface`` (NOT ``OptionsAccountInterface``).

This module (Phase 2 Task 2) implements:
  * the in-memory ledger (cash / signed positions / equity snapshots),
  * the 12 ``ReadOnlyAccountInterface`` abstracts + ``get_settings_definitions``,
  * the price-cache override (the critical gotcha — see ``get_instrument_current_price``),
  * ``snapshot_equity`` (the engine calls it per bar to build the equity curve).

The 6 trading abstracts (``_submit_order_impl``, ``cancel_order``, ``modify_order``,
``adjust_tp``/``adjust_sl``/``adjust_tp_sl``) plus the ``refresh_orders`` FILL ENGINE
implement the full per-bar fill / TP-SL / OCO engine (Phase 2 Task 3). All 18 abstracts
are concrete so the class instantiates (``__abstractmethods__`` is empty).

The fill engine (``refresh_orders``) is the heart of the simulator. Each invocation:
  1. ACTIVATES dependent WAITING_TRIGGER legs whose parent order has reached its trigger
     status (the inherited ``submit_order`` stages TP/SL/OCO legs as WAITING_TRIGGER with
     ``depends_on_order``/``depends_order_status_trigger`` exactly like AlpacaAccount);
  2. EVALUATES every working order against the chosen bar — MARKET fills at next-bar
     open (±slippage), LIMIT fills only when the bar's range crosses the limit, STOP
     triggers when the bar's range crosses the stop (then fills at stop ±slippage);
  3. APPLIES fills to the cash/position ledger (commission charged per fill);
  4. CANCELS the OCO sibling when one OCO leg fills (so the transaction closes on the
     first leg and the other does not also execute).

Transaction lifecycle (WAITING->OPENED->CLOSED) is NOT re-implemented here: the inherited
``refresh_transactions`` derives it from order states. The engine calls
``refresh_orders()`` then ``refresh_transactions()`` per bar. ``refresh_transactions``
recognises a TP/SL close via ``"OCO-" in comment`` or ``order_type == OrderType.OCO`` on a
filled dependent leg, so our legs MUST carry that marker.

Field/enum names verified against the installed ba2_common:
  * TradingOrder cols: id, account_id, symbol, quantity, side (OrderDirection),
    order_type (OrderType), status (OrderStatus), filled_qty, open_price, limit_price,
    stop_price, broker_order_id, depends_on_order, depends_order_status_trigger,
    transaction_id, comment, created_at, ...
  * OrderStatus has classmethods get_terminal_statuses()/get_executed_statuses()/
    get_active_statuses()/get_unfilled_statuses() (NOT get_open_order_statuses — that one
    does not exist). WAITING_TRIGGER is in get_active_statuses() but NOT get_unfilled_statuses().
  * AccountDefinition cols: id, name, provider, description.
  * Transaction has NO entry_order_id column; the market-entry order is the TradingOrder
    with transaction_id == txn.id AND depends_on_order IS NULL.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import (
    OrderStatus,
    OrderType,
    OrderDirection,
    OrderOpenType,
    TransactionStatus,
    AssetClass,
    OptionRight,
)
from ba2_common.core.option_types import OptionPosition
from ba2_common.core.db import get_db, get_instance, add_instance, update_instance
from ba2_common.core.trade_store import orders_where, transactions_where

from .price_source import AsOfPriceSource
from .options_provider import HistoricalOptionsProvider

import logging

logger = logging.getLogger(__name__)


class _AttrDict(dict):
    """A dict whose keys are also attribute-accessible.

    Needed because the inherited ``_validate_position_size_limits`` reads
    ``account_info.equity`` (attribute access) while other callers use
    ``account_info["equity"]``. Supporting both keeps the inherited code working.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


@dataclass
class _Position:
    """In-memory ledger position. ``qty`` is signed: positive long, negative short."""

    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pl: float = 0.0


@dataclass
class _OptionLot:
    """In-memory option ledger lot. ``qty`` is signed CONTRACTS (long +, short -).

    ``multiplier`` (typically 100) and ``avg_price`` (premium per share) let the per-bar
    marking value the lot at premium-close x qty x multiplier and let the fall-back use
    the entry premium when no bar exists for the marking day.
    """

    contract_symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    multiplier: float = 100.0


@dataclass(slots=True)
class _FillProbe:
    """Cheap, non-ORM stand-in for a ``TradingOrder`` used ONLY to test whether a bracket
    TP/SL price crossed on a bar. ``_evaluate_fill``/``_bar_for_fill`` read exactly
    ``symbol``/``order_type``/``side``/``limit_price``/``stop_price`` off the order they're
    given and nothing else, so this plain dataclass is a drop-in for the crossing test.

    Profiled ``_apply_bracket_exits`` on a 3-month backtest: constructing a real
    ``TradingOrder`` (full SQLModel/Pydantic field validation) for EVERY open position
    carrying a TP/SL on EVERY bar -- even though the overwhelming majority never cross --
    was ~29% of total wall time (24.6s of 85.3s, from 19,916 constructed objects). Swapping
    the probe to this dataclass skips that validation chain entirely; the real
    ``TradingOrder`` is only built on the rare bar where a leg actually fills (see below).
    """

    symbol: str
    side: OrderDirection
    order_type: OrderType
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None


# Per-share no-arbitrage tolerance for option-fill validation. The sparse options cache
# occasionally carries junk indicative prints (e.g. a call at $0.01 while spot is $159.85
# against a $105 strike — $54.85 of intrinsic); filling at such a premium and later
# settling/marking against the REAL underlying fabricates P&L (the OS1 $20k -> $7-18M
# blow-up). A fill premium violating a no-arbitrage bound by MORE than this tolerance is
# rejected as untradable (see BacktestAccount._arb_fill_reject_reason).
_ARB_FILL_TOLERANCE = 0.05

# Maximum share of a premium bar's traded volume an option order may absorb and still
# fill on that bar — the standard backtest participation assumption. Without it an
# option order of ANY size fills at the bar's price regardless of liquidity (the
# options audit found 2,100 contracts of a CVX call filled at a bar whose total volume
# was 1 contract — a real order that size would move the market or not fill at all).
# An order needing more contracts than participation x bar volume does NOT fill that
# bar; it stays pending and retries the next (see BacktestAccount._option_fill_price).
_OPTION_FILL_MAX_VOLUME_PARTICIPATION = 0.10

# ---------------------------------------------------------------------------
# OPTION BID-ASK SPREAD MODEL (2026-07-25)
# ---------------------------------------------------------------------------
# WHY A SEPARATE MODEL FROM ``spread_bps``: the equity ``spread_bps`` knob is basis points OF
# PRICE, which is the right SHAPE for a stock and the wrong shape for an option. On a $100
# stock, 5 bps = $0.05 — realistic. On a $1.00 option premium, 5 bps = $0.0005 — about two
# orders of magnitude too small, since a real $1.00 contract quotes something like $0.95/$1.05
# (~10% of premium). Expressing a 5% option spread through ``spread_bps`` would require 500
# bps, which in the same run would also charge stocks 5% — catastrophically wrong. One shared
# knob cannot serve both asset classes, so options get their own PERCENT-OF-PREMIUM model.
#
# WHY IT MATTERS: the historical options cache carries NO usable quote. Verified over the
# 958,024 cached chain rows, every single one is either bid==ask (701,849) or has both NULL
# (256,175) — not one real spread exists. ``_option_fill_price`` reads the bar's open/close
# directly, so before this model an option round trip cost only ``slippage_bps`` of premium,
# i.e. essentially nothing. Multi-leg credit structures are the most exposed: an iron condor
# crosses the spread on 4 legs entering and 4 exiting, so charging ~0 is a large, systematic,
# one-directional overstatement of exactly the strategies the options grid searches.
#
# THE MODEL: full spread = max(min_tick, premium x pct), charged HALF per fill in the adverse
# direction (buys up, sells down), then widened for thin contracts. The absolute floor matters
# because percent-of-premium alone under-charges cheap contracts, which is where fabricated
# edge concentrates. The thin-volume multiplier reflects that quoted width scales inversely
# with activity; the threshold sits near the p75-p80 of the measured cache distribution
# (p10=1, p25=3, p50=14, p75=71, p90=319 contracts/day).
#
# WHERE IT APPLIES (corrected 2026-08-24): every option fill, LIMIT ones included. As first
# shipped the model only reached ``_option_fill_price``'s market-style branch, which multi-leg
# combo CHILDREN take (they carry no limit_price — the parent holds the net limit) but which
# NO single-leg order takes: TradeActions and PremiumSeller submit single legs as
# ``order_type="limit"``, always with a limit_price. So the wheel / 0DTE / long-option branch
# crossed no spread on either end while credit structures paid on all 8 leg-crossings — a
# systematic tilt in favour of exactly the single-leg premium sellers the grid evaluates. A
# limit now crosses the quote (``_option_cross``) and is re-tested against its limit.
#
# All of this is a MODELING ASSUMPTION, not observed data — it is a defensible estimate
# replacing an indefensible zero. Real quotes (e.g. ThetaData EOD bid/ask) should supersede it.
_OPTION_SPREAD_LIQUID_VOLUME = 100.0   # at/above this daily volume, no thin-widening
_OPTION_SPREAD_THIN_MULT = 2.0         # multiplier applied below that volume

#: Slack, in premium dollars per share, when comparing a multi-leg combo's achieved net
#: against the order's net limit (OPT-S7). The net is a sum of per-leg products, so a limit
#: set FROM those same quotes can miss itself by a float ulp; a broker would fill that. Well
#: below the $0.01 minimum option tick, so it can never let a genuinely worse net through.
_NET_LIMIT_TOLERANCE = 1e-9


class BacktestAccount(AccountInterface, OptionsAccountInterface):
    """Simulated broker for daily multi-asset backtests.

    Inherits BOTH ``AccountInterface`` (the equity/orchestration contract) and
    ``OptionsAccountInterface`` (the options-capability mixin). The options READ methods
    delegate to an OPTIONAL injected ``HistoricalOptionsProvider`` clamped to the simulated
    as-of clock; when no provider is injected (the equity-only path) they degrade to
    empty/None so existing equity callers are unaffected.
    """

    # Class-level capability flags (mirror the live account contract).
    supports_trading = True
    supports_options = True

    def __init__(
        self,
        id: int,
        price_source: AsOfPriceSource,
        settings: Dict[str, Any],
        options_provider: Optional[HistoricalOptionsProvider] = None,
    ):
        # ReadOnlyAccountInterface.__init__ registers self.id in the _GLOBAL_PRICE_CACHE.
        super().__init__(id)
        self._price = price_source
        # OPTIONAL as-of-clamped options reader. None on the equity-only path (existing
        # equity callers pass no provider, so options reads degrade to empty/None).
        self._options = options_provider
        # Resolved config dict (validated fail-early by the engine before the run):
        #   starting_cash, commission_per_trade, slippage_bps, fill_model.
        self._cfg = settings
        self._cash: float = float(settings["starting_cash"])
        # Optional fixed-notional cap. None = off; every path is then byte-identical to before.
        # Validated at config time (daily_backtest_handler), so a bad value never reaches here.
        # ``.get`` is correct rather than a hidden default: absence IS the off state, and 0.0
        # would be a very different (and catastrophic) instruction — see equity_cap.py.
        self._equity_cap: Optional[float] = settings.get("equity_cap")
        # symbol -> signed-position ledger.
        self._positions: Dict[str, _Position] = {}
        # The equity curve: one snapshot per simulated bar (engine appends via snapshot_equity).
        self._equity_snapshots: List[Dict[str, Any]] = []
        # Parallel ascending list of snapshot dates (snapshots are appended in clock order) so
        # _bars_between can bisect the count in a window instead of scanning every snapshot per
        # round-trip trade (was O(trades x snapshots) at results time).
        self._snapshot_dates: List[Any] = []
        # Monotonic synthetic broker-order-id counter.
        self._broker_seq = 0
        # Set True the first time snapshot_equity() sees net_liquidating_value <= 0 -- a real
        # (non-margin) account cannot go below zero equity, so a trial that gets here is
        # producing meaningless further simulation (unbounded, un-recoverable "equity" swings
        # that show up as e.g. a -1900% drawdown -- impossible for real capital). The engine's
        # main loop checks this flag and stops the run early; results.py/strategy_fitness.py
        # mark the trial as invalid rather than scoring it on the (nonsensical) numbers a
        # continued simulation would produce.
        self._wiped_out: bool = False
        # contract_symbol -> signed option lot (qty in CONTRACTS, multiplier 100). Kept
        # SEPARATE from ``self._positions`` (which is the equity ledger keyed by the plain
        # underlying symbol and multiplier-unaware) so option marking can value at
        # premium-close x qty x multiplier without disturbing equity fills/marking.
        self._option_positions: Dict[str, _OptionLot] = {}
        # order-id -> SIMULATED fill date (the virtual bar an order filled on). The
        # TradingOrder row's ``created_at`` is stamped by the DB with wall-clock
        # ``datetime.now()`` at row creation, which is NON-deterministic across runs; the
        # filled-trade history must use the SIMULATED clock instead so two identical runs
        # produce a byte-identical trade list (the reproducibility gate). Populated in
        # ``_apply_fill`` and read by ``_order_to_trade``.
        self._fill_dates: Dict[int, datetime] = {}
        # Transaction ids whose close_date/open_date have already been re-stamped to sim time
        # (so refresh_transactions only touches freshly-closed transactions, not all closed ones).
        self._stamped_closed_ids: set = set()
        # Transaction ids whose open_date has been re-stamped to its entry's SIM fill date.
        # The inherited lifecycle stamps open_date with WALL clock on WAITING->OPENED; we
        # overwrite it once with the simulated fill bar so days-opened math is sim-correct.
        self._stamped_open_ids: set = set()
        # In-memory cache of THIS account's TradingOrder rows (the per-bar fill engine reads
        # working orders on EVERY bar; on a 5-minute clock the DB round-trip dominated). None
        # means "reload on next read"; see _all_orders / invalidate_order_cache.
        self._order_cache: Optional[List[TradingOrder]] = None
        # Working-orders sublist: ONLY the active-status orders (the per-bar fill engine's working
        # set), as references to the SAME objects in _order_cache (so in-place fills/cancels are
        # visible in both — no divergence). The fill loop must iterate only these, not the
        # thousands of dead (filled/cancelled) orders a long churning run accumulates. Rebuilt
        # lazily from _order_cache and invalidated together with it.
        self._active_order_cache: Optional[List[TradingOrder]] = None
        self._active_set: Optional[frozenset] = None  # cached frozenset(OrderStatus.get_active_statuses())

        # Per-expert snapshot of OPENED transactions (expert_id -> {symbol: [(txn_id, open_price,
        # open_qty)]}), read by per-bar position managers. The OPENED set only changes when an
        # order fills, so this is cached here and dropped in _update_position (the universal ledger
        # fill path) — the same "cache + invalidate on mutation" discipline as _order_cache. See
        # opened_position_snapshot. Empty dict means "nothing cached yet".
        self._opened_txn_snapshot: Dict[int, Dict[str, List[tuple]]] = {}

        # Generation counter for the OPTIONS memoizations below (_option_group_bounds and the
        # _lot_order index) — the per-bar equity mark + margin check re-ran full get_orders()
        # scans O(orders x lots) on options runs. Bumped by invalidate_order_cache() (every
        # order-CREATION site already calls it) AND at the in-place mutation points the order
        # cache deliberately skips: a NEW option lot appearing at fill time
        # (_update_option_position) and a fill-time quantity cap/rescale
        # (_cap_single_leg_option_entry / _fill_multi_leg_parent) — those change what the
        # memos must report without any new order row.
        self._option_memo_gen: int = 0
        # (generation, contract_group, group_bounds) memo for _option_group_bounds.
        self._group_bounds_memo: Optional[tuple] = None
        # contract_symbol -> the order carrying the contract's terms (first row with a strike,
        # same first-match rule the un-memoized scan used). Rebuilt when the generation moves.
        self._lot_order_index: Optional[Dict[str, TradingOrder]] = None
        self._lot_order_index_gen: int = -1
        # F8 (no-orphaned-stock expiry): symbol -> SIGNED share count created by a short-option
        # physical assignment (+ = long stock from an assigned short put, - = short stock
        # from an assigned naked short call). Option strategies never manage the resulting
        # stock, so process_pending_assignment_liquidations closes ALL of it at the NEXT
        # bar's open (broker post-assignment liquidation; no orphaned stock in backtests).
        # Stays EMPTY when the run sets ``hold_assigned_stock`` — read from ``self._cfg`` at
        # each assignment in _book_assignment_share_leg (assignments are rare; nothing is
        # gained by caching it, and a cached copy would silently ignore a config change).
        self._pending_assignment_sells: Dict[str, float] = {}
        # OPT-L1 exit half: (symbol, reason) pairs the PLEDGED-COVER lock has already
        # explained at ERROR in this run. The lock fires REPEATEDLY by design (a staged
        # TP/SL re-arms on every bar its level is crossed — the measured BAC case stood
        # for 40 consecutive bars), so the full explanation is logged once per symbol per
        # reason and the recurrences drop to DEBUG. See _pledged_share_lock.
        self._pledged_lock_logged: set = set()
        # OPT-B4 (option TIF DAY): order id -> the SIMULATED calendar date the option order
        # was staged on. ``TradingOrder.created_at`` is stamped with the WALL clock by the ORM
        # and is therefore useless for ageing in a backtest. Read only by
        # ``_expire_stale_option_limits``; entries are dropped as they expire.
        self._option_order_day: Dict[int, Any] = {}
        # Count of option fills REJECTED by the no-arbitrage guard (_arb_fill_reject_reason)
        # — junk indicative premium prints the run skipped instead of filling at.
        self.rejected_arb_fills: int = 0
        # Count of option fills REJECTED by the volume-participation cap
        # (_OPTION_FILL_MAX_VOLUME_PARTICIPATION) — bars too thinly traded to absorb the
        # order's size, so the order stays pending instead of filling at a price the
        # market could not have absorbed.
        self.rejected_illiquid_fills: int = 0

    # ======================================================================
    # Settings
    # ======================================================================
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Account settings schema. No defaults: the engine validates fail-early.

        (``get_settings_definitions`` is resolved-non-abstract on the MRO so it does not
        block instantiation, but we implement it for a proper settings surface.)
        """
        return {
            "starting_cash": {
                "type": "float",
                "required": True,
                "description": "Initial simulated cash",
            },
            "commission_per_trade": {
                "type": "float",
                "required": True,
                "description": "Flat $ commission applied per fill",
            },
            "slippage_bps": {
                "type": "float",
                "required": True,
                "description": "Slippage in basis points applied to market/stop fills (worsening)",
            },
            "assume_stop_fills_at_price": {
                "type": "bool",
                "required": False,
                "description": "When True, a triggered STOP is assumed to fill at exactly its "
                               "stop price even if the bar GAPPED through it. Only true for a "
                               "stop-LIMIT; a plain stop is a market order and fills at the "
                               "open on a gap. Setting this hides gap risk -- losses on "
                               "overnight/earnings gaps are understated. Default False "
                               "(accurate). LIMIT orders are unaffected: they always fill at "
                               "their price or better, gap or not.",
            },
            "option_spread_pct": {
                "type": "float",
                "required": False,
                "description": "Modeled option bid-ask spread as a PERCENT OF PREMIUM (full "
                               "width; half is charged per fill in the adverse direction). "
                               "Options need this instead of spread_bps, which is bps of "
                               "price and ~2 orders of magnitude too small for a premium. "
                               "Widened for thin contracts and floored at "
                               "option_spread_min_tick. Defaults to 0.0 (exact no-op, "
                               "pre-2026-07-25 behaviour); the grid passes a real value.",
            },
            "hold_assigned_stock": {
                "type": "bool",
                "required": False,
                "description": "When True, stock created by a short-option assignment is "
                               "KEPT instead of being liquidated at the next bar's open. "
                               "Exists for the WHEEL, which is the only strategy whose "
                               "rules manage the assigned shares (its covered-call overlay "
                               "is gated on has_assigned_shares): the manage pass runs "
                               "BEFORE the liquidation on the following bar, so with this "
                               "off every wheel position is written as a covered call and "
                               "sold naked on the same bar. What it COSTS: the "
                               "no-orphaned-stock policy stops applying, so assigned shares "
                               "no option rule manages will ride to the end of the run "
                               "(marked to market, reported open_at_end) — exercised ITM "
                               "longs riding to 67-85% of final equity was the OS1 blow-up. "
                               "Only turn it on for a strategy that actually manages stock. "
                               "Defaults to False (exact no-op, pre-2026-08-27 behaviour).",
            },
            "option_spread_min_tick": {
                "type": "float",
                "required": False,
                "description": "Absolute floor on the modeled option spread, in premium "
                               "dollars (full width). Percent-of-premium alone under-charges "
                               "cheap contracts, which is where fabricated edge concentrates.",
            },
            "spread_bps": {
                "type": "float",
                "required": False,
                "description": "Round-trip bid-ask spread in basis points, modeled properly "
                               "(not just a pnl haircut): MARKET/STOP fills get an EXTRA "
                               "half-spread price degradation on top of slippage; LIMIT fills "
                               "(the TP leg) get their TRIGGER threshold widened by half-spread "
                               "-- the underlying market must move further to realistically "
                               "cross the spread, so a marginal TP can miss entirely and the "
                               "trade resolves via SL/timeout instead, not just at a worse "
                               "price. Default 0.0 (exact no-op, existing behaviour unchanged).",
            },
            "fill_model": {
                "type": "str",
                "required": True,
                "description": "Fill model: 'next_bar_open' (default) | 'same_bar_close'",
            },
        }

    # ======================================================================
    # Ledger internals
    # ======================================================================
    def _open_positions_mtm(self) -> float:
        """Mark-to-market value of all open positions at the current bar's close.

        Signed value (long positions positive, short positions negative). A held symbol
        with no EXACT bar at the current clock tick is valued at its last-known close
        (forward-fill) — NOT $0 — because the clock is the union of every symbol's
        timestamps, so a held symbol routinely lacks a bar on ticks driven by other symbols
        (and on gaps / half-days / split days). Dropping it to $0 made positions vanish from
        the equity curve and produced spurious 90%+ drawdowns (corrupting max_drawdown /
        Calmar / Sharpe). Final fallback is the entry price for a never-yet-priced symbol.
        Equity positions are valued at the equity bar's close; OPTION positions are
        valued separately at the current premium close x qty x multiplier (with a
        fall-back to the entry premium when there is no premium bar for the day).
        """
        total = 0.0
        for p in self._positions.values():
            if p.qty == 0:
                continue
            px = self._price.close_at(p.symbol)
            if px is None:
                px = self._price.close_asof(p.symbol)  # forward-fill: last known close
            if px is None:
                px = getattr(p, "avg_price", None)  # never-priced held symbol -> entry
            if px is not None:
                total += p.qty * px
        return total + self._option_positions_mtm()

    #: Option strategies whose leg combination is DEFINED-RISK: the structure can only ever be
    #: worth a bounded amount, so its mid-life mark-to-market has a theoretical no-arbitrage
    #: range. Marking each leg independently off the sparse/noisy options cache lets a single
    #: outlier premium print (x contracts x 100) blow the recorded equity/drawdown far outside
    #: that range (the O_BF -473% max_drawdown from a body-leg outlier); we clamp the GROUP's net
    #: MTM contribution to that range. NET-LONG (debit) combos are worth [0, width]; NET-SHORT
    #: (credit) combos are worth [-width, 0]. Undefined-risk structures (short strangle/straddle,
    #: put ratio, jade lizard — an uncovered short leg) are NOT clamped (the margin-liquidation
    #: path bounds those); single-leg and equity marks are unchanged.
    # Only strategies an action actually emits belong here — "debit_spread"/"credit_spread"
    # were dead entries (no builder emits them) implying coverage that didn't exist.
    DEFINED_RISK_LONG_STRATEGIES = frozenset(
        {"bull_call_spread", "bear_put_spread", "call_butterfly"}
    )
    DEFINED_RISK_SHORT_STRATEGIES = frozenset(
        {"bear_call_spread", "bull_put_spread", "iron_condor"}
    )

    def _option_positions_mtm(self) -> float:
        """Mark-to-market value of open OPTION lots at the current bar's premium close.

        Each lot contributes ``premium_close x signed_qty x multiplier`` (mirroring how an
        equity position contributes ``close x qty``, scaled by the contract multiplier).
        When no premium bar exists for the lot's contract on the current bar, the lot is
        valued at its entry premium (``avg_price``) so a held option is never silently
        dropped to zero on a day the cache lacks a bar.

        DEFINED-RISK multi-leg groups (butterflies / vertical spreads / iron condors) are
        marked as a GROUP and their net contribution is CLAMPED to the structure's theoretical
        no-arbitrage range (``[0, width]`` for a debit combo, ``[-width, 0]`` for a credit combo,
        ``width = strategy-aware defined-risk width x 100 x structures`` — see
        ``_defined_risk_width_per_structure``). This stops a single outlier
        premium print in the sparse cache from swinging recorded equity/drawdown outside what the
        structure can actually be worth. The clamp is a MARK-TO-MARKET display bound ONLY — it
        never moves cash, so realized P&L at expiry is unchanged.
        """
        if self._options is None:
            return 0.0
        # Per-GROUP structure bounds (strategy, ALL opening-leg strikes, structure count) resolved
        # once from the order set. The width MUST come from the ORIGINAL structure, not the
        # currently-held lots: once a leg is assigned/settled (e.g. the butterfly body converts to
        # short stock at expiry) the surviving lots alone give a wrong/loose width. Contract->group
        # mapping ties each held lot to its structure.
        contract_group, group_bounds = self._option_group_bounds()

        total = 0.0
        group_mtm: Dict[Any, float] = {}
        # Track, per defined-risk group, whether any SHORT leg is CURRENTLY held. The clamp
        # DIRECTION depends on the live composition, NOT the static strategy label: an iron condor
        # is a credit combo ([-width, 0]) only while its short legs are held; once the shorts are
        # closed/settled the surviving LONG legs are worth [0, +width] and their positive residual
        # must NOT be clamped to 0 (the O_IC id=449 1-bar transient: leftover long value erased ->
        # equity dipped negative for one bar).
        group_has_short: Dict[Any, bool] = {}
        for lot in self._option_positions.values():
            if lot.qty == 0:
                continue
            gkey = contract_group.get(lot.contract_symbol)
            gb = group_bounds.get(gkey) if gkey is not None else None
            is_defined_risk = gb is not None and (
                gb["strategy"] in self.DEFINED_RISK_LONG_STRATEGIES
                or gb["strategy"] in self.DEFINED_RISK_SHORT_STRATEGIES
            )

            bar = self._options.get_bar(lot.contract_symbol, self._as_of_date())
            if bar and bar.get("close") is not None:
                # A bar EXISTS but the sparse cache carries junk prints (the arb guard's
                # own documented class: a $0.01 call against $50+ of intrinsic). Clamp the
                # mark into the same no-arb bounds the guard enforces on fills — kept
                # as-is only when the bounds are unresolvable (no spot: fail-open, like
                # the guard). Sane prints are inside the bounds -> byte-identical.
                # (Review 2026-08-30 F1, the :537 mark twin.)
                px = bar["close"]
                bounds = self._lot_no_arb_bounds(lot.contract_symbol)
                if bounds is not None:
                    px = min(max(float(px), bounds[0]), bounds[1])
            elif is_defined_risk:
                # (2a) NO premium bar for a defined-risk leg on this bar -> mark at INTRINSIC (not
                # the stale entry premium / 0) so an open combo whose sparse cache lacks a bar this
                # tick is not understated (the offsetting leftover-long value is preserved).
                px = self._leg_intrinsic(lot.contract_symbol, gb)
                if px is None:
                    px = lot.avg_price
            else:
                # (2c) NO premium bar for a NON-defined-risk lot: the intrinsic fallback
                # extends to ALL option lots (review 2026-08-30 F2) as a FLOOR on the
                # entry-premium mark — for a SHORT the liability is max(intrinsic, entry)
                # (a deep-ITM naked short stops printing bars precisely when it matters;
                # frozen at its entry credit it was the only found path around the
                # dd>=100 wipeout sentinel), for a LONG the asset is likewise floored at
                # intrinsic. While intrinsic is below entry (e.g. still OTM) the entry
                # premium keeps the mark, exactly as before. When SPOT itself is
                # unresolvable this tick, the entry-premium mark is KEPT — the fix is for
                # the no-OPTION-bar case, not the no-equity-bar case.
                px = lot.avg_price
                bounds = self._lot_no_arb_bounds(lot.contract_symbol)
                if bounds is not None:
                    px = bounds[0] if px is None else max(float(px), bounds[0])
            if px is None:
                continue
            contribution = lot.qty * px * lot.multiplier

            if is_defined_risk:
                group_mtm[gkey] = group_mtm.get(gkey, 0.0) + contribution
                if lot.qty < 0:
                    group_has_short[gkey] = True
            else:
                total += contribution

        # Clamp each defined-risk group's net contribution to its no-arb range. (2b) The bound is
        # composition-aware: while a SHORT leg is held the combo carries credit downside so it is
        # bounded [-width, 0]; once only LONG legs remain (shorts closed/settled) the residual is a
        # net-long asset bounded [0, +width] — never floored below its true minimum, never erased.
        for gkey, mtm in group_mtm.items():
            gb = group_bounds[gkey]
            width = gb["width"]
            if width is not None:
                is_credit = gb["strategy"] in self.DEFINED_RISK_SHORT_STRATEGIES
                # A DEBIT/long structure (butterfly, verticals) is a net-long asset worth
                # [0, width] regardless of its internal short legs. A CREDIT structure (iron
                # condor, credit/bear-call spread) is worth [-width, 0] ONLY WHILE its short legs
                # are still held; once the shorts are closed/settled the surviving LONG legs are a
                # net-long asset worth [0, width] — so their positive residual is preserved, not
                # erased (the O_IC id=449 1-bar transient).
                if is_credit and group_has_short.get(gkey, False):
                    mtm = max(min(mtm, 0.0), -width)          # credit exposure live: [-width, 0]
                else:
                    mtm = min(max(mtm, 0.0), width)           # long / long-only remainder: [0, width]
            total += mtm
        return total

    def _leg_intrinsic(self, contract_symbol: str, group_bound: Dict[str, Any]) -> Optional[float]:
        """Per-share INTRINSIC value of an option leg at the current underlying close.

        Used to mark a held DEFINED-RISK leg when the sparse cache has no premium bar this tick:
        ``max(0, spot-strike)`` for a call, ``max(0, strike-spot)`` for a put. Resolves the leg's
        strike / option_type / underlying from its order (via the ``_lot_order`` index — no
        per-bar full-order scan); returns None if unresolvable (caller then falls back to the
        entry premium).
        """
        o = self._lot_order(contract_symbol)
        if o is None or o.option_type is None:
            return None
        underlying = getattr(o, "underlying_symbol", None) or o.symbol
        spot = self._price.close_at(underlying)
        if spot is None:
            spot = self._price.close_asof(underlying)
        if spot is None:
            return None
        if o.option_type == OptionRight.CALL:
            return max(0.0, float(spot) - float(o.strike))
        return max(0.0, float(o.strike) - float(spot))

    @staticmethod
    def _defined_risk_width_per_structure(strategy: Optional[str], strikes) -> Optional[float]:
        """Defined-risk width PER STRUCTURE, PER SHARE, for a combo's strike set.

        This is the span both the mid-life MTM clamp and the expiry safety clamp scale by
        ``multiplier x structures``, so it must be the structure's TRUE defined risk:

          * ``iron_condor`` (4 strikes k1<k2<k3<k4): ``max(k2-k1, k4-k3)`` — the wider WING.
            The widest adjacent gap is usually the BODY ``k3-k2``, which is not risk (both
            short strikes sit inside it) and made the bound ~2x too loose.
          * 2-strike verticals (bull_call/bear_put/bear_call/bull_put spread): the single gap.
          * ``call_butterfly`` (3 strikes k1<k2<k3): ``k2-k1`` — the LOWER gap. A long 1-2-1
            fly reaches its maximum expiry payoff at spot == k2, where the lower long is
            worth k2-k1 and the other two legs are worthless; the upper wing does not cap it.
            This was ``min(gaps)``, which is strictly BELOW the attainable payoff whenever
            the upper wing is the narrower one — and the clamp runs immediately before
            ``self._cash += net_payoff``, so it destroyed real simulated cash rather than
            merely mis-marking (OPT-S13). That orientation is produced deterministically, not
            by chance: the lower-wing picker tie-breaks to the FARTHER strike while
            ``select_wing`` breaks to the NEARER one, so on a $5 grid with a $100 body the
            GA's 7.5% wing yields 90/100/105 (a 50% truncation) and 12.5% yields 85/100/110
            (33%) — both widths are in the searched grid. Widening it lets nothing impossible
            through: above k3 the fly pays ``(k2-k1) - (k3-k2)``, whose magnitude can exceed
            k2-k1 only when the upper wing is more than TWICE the lower, and in that
            orientation ``min(gaps)`` and ``gaps[0]`` are the same number. Equal wings
            unchanged.
          * any other shape/strategy: the widest adjacent gap (defensive fallback — the
            pre-strategy-aware rule, looser but never tighter than a known shape's risk).

        Returns None when fewer than 2 distinct strikes (the structure cannot be bounded).
        """
        uniq = sorted({float(s) for s in strikes})
        if len(uniq) < 2:
            return None
        gaps = [b - a for a, b in zip(uniq, uniq[1:])]
        if strategy == "iron_condor" and len(uniq) == 4:
            return max(gaps[0], gaps[2])
        if strategy == "call_butterfly" and len(uniq) == 3:
            return gaps[0]
        if len(uniq) == 2:
            return gaps[0]
        return max(gaps)

    def _option_group_bounds(self):
        """Resolve, from the order set, the defined-risk structure bounds for held option lots.

        Returns ``(contract_group, group_bounds)`` where:
          * ``contract_group``: ``contract_symbol -> group_key`` for every held lot (group_key is
            the parent order id for a multi-leg spread, else the contract itself for single-leg).
          * ``group_bounds``: ``group_key -> {strategy, width}`` where ``width`` is the structure's
            theoretical max value = ``strategy-aware width x 100 x structures`` (see
            ``_defined_risk_width_per_structure``; None when it cannot be bounded, e.g. <2
            distinct strikes). ``structures`` is the parent order's ``quantity`` (number of
            structures), NOT a leg's contract count — a butterfly's body leg carries 2x the
            structure count, so using a leg qty would over-loosen the clamp.

        Width is derived from the FULL set of the structure's OPENING legs (all strikes), so it is
        stable even after a leg has been assigned/settled and dropped out of the held lots.

        MEMOIZED on ``_option_memo_gen`` (the equity mark + margin check call this every bar;
        the full order scans dominated options-run profiles). Every input change moves the
        generation: order rows via invalidate_order_cache(), new held lots / fill-time quantity
        caps via their own bumps (see __init__). Pure caching — results are byte-identical.
        """
        memo = self._group_bounds_memo
        if memo is not None and memo[0] == self._option_memo_gen:
            return memo[1], memo[2]

        held = self._option_positions
        orders = self.get_orders()  # ONE fetch reused by all three passes below
        # parent order id -> (strategy, structure quantity, [opening strikes], multiplier)
        parent_info: Dict[int, Dict[str, Any]] = {}
        single_info: Dict[str, Dict[str, Any]] = {}
        # collect opening legs' strikes per parent + parent strategy/qty
        for o in orders:
            if getattr(o, "asset_class", None) != AssetClass.OPTION:
                continue
            if o.parent_order_id is None:
                # multi-leg PARENT (no contract) or a single-leg option order.
                if o.id is not None and getattr(o, "option_strategy", None):
                    if not getattr(o, "contract_symbol", None):
                        parent_info.setdefault(
                            o.id,
                            {"strategy": o.option_strategy,
                             "qty": abs(float(o.quantity or 0.0)) or 1.0,
                             "strikes": [], "multiplier": float(o.multiplier or 100)},
                        )
                    else:  # single-leg option (its own group)
                        single_info[o.contract_symbol] = {
                            "strategy": o.option_strategy,
                            "qty": abs(float(o.quantity or 0.0)) or 1.0,
                            "strikes": [float(o.strike)] if o.strike is not None else [],
                            "multiplier": float(o.multiplier or 100),
                        }
        # opening child legs contribute their strikes to the parent group
        for o in orders:
            if getattr(o, "asset_class", None) != AssetClass.OPTION:
                continue
            if o.parent_order_id is not None and o.parent_order_id in parent_info and o.strike is not None:
                parent_info[o.parent_order_id]["strikes"].append(float(o.strike))

        def _width(info):
            if info["qty"] <= 0:
                return None
            per = self._defined_risk_width_per_structure(info["strategy"], info["strikes"])
            if per is None:
                return None
            return per * info["multiplier"] * info["qty"]

        contract_group: Dict[str, Any] = {}
        group_bounds: Dict[Any, Dict[str, Any]] = {}
        for cs in held:
            # find the order that owns this contract to route it to its group
            owner = None
            for o in orders:
                if getattr(o, "contract_symbol", None) == cs and getattr(o, "asset_class", None) == AssetClass.OPTION:
                    owner = o
                    break
            if owner is None:
                continue
            if owner.parent_order_id is not None and owner.parent_order_id in parent_info:
                gkey = owner.parent_order_id
                info = parent_info[gkey]
            elif cs in single_info:
                gkey = cs
                info = single_info[cs]
            else:
                continue
            contract_group[cs] = gkey
            if gkey not in group_bounds:
                group_bounds[gkey] = {"strategy": info["strategy"], "width": _width(info)}
        self._group_bounds_memo = (self._option_memo_gen, contract_group, group_bounds)
        return contract_group, group_bounds

    def equity(self) -> float:
        """Net liquidating value = cash + mark-to-market of open positions."""
        return self._cash + self._open_positions_mtm()

    def deployed_equity(self) -> float:
        """Equity the SIZER may see: ``min(cap, equity())``. Uncapped when no cap is set.

        Every money accessor routes through here so the cap is enforced at ONE seam. Capping
        inside the risk manager instead would leave buying power and margin reading the real
        balance, letting a margin account deploy twice the cap while appearing capped.
        """
        from app.services.backtest.equity_cap import deployed_equity as _deployed
        return _deployed(self.equity(), cap=self._equity_cap)

    # ======================================================================
    # Maintenance margin + forced liquidation (broker-style, bounds equity)
    # ======================================================================
    #: Reg-T maintenance margin fraction for a SHORT stock position (~30% of notional).
    SHORT_STOCK_MAINTENANCE_FRACTION = 0.30

    def maintenance_margin_requirement(self) -> float:
        """Total maintenance-margin dollars this book must hold against its SHORT risk.

        The requirement is the sum of the (unbounded-risk) short positions' broker
        maintenance margins:

          * short OPTION legs -> ``naked_margin_per_contract(strike, option_type=right, spot)``
            x contracts (Reg-T naked ~20% of notional less the DIRECTION-AWARE OTM amount —
            0 for an ITM short — floored 10%) — the SAME model the entry
            reserve uses, so the maintenance check is consistent with sizing.
          * short STOCK       -> ``SHORT_STOCK_MAINTENANCE_FRACTION`` (30%) x |qty| x price.

        LONG stock/options require NO extra maintenance here: their value is already funded and
        marked into equity (their downside is bounded by going to zero, which the mark reflects).
        This deliberately targets the naked-short blow-up (the -256% drawdown) rather than
        reproducing a full Reg-T long-side schedule.
        """
        req = 0.0
        # Contracts that belong to a DEFINED-RISK combo — their short legs are COVERED by the
        # combo's long legs (defined risk), so they carry NO naked-margin requirement and must be
        # excluded here (otherwise a butterfly's short body / an iron condor's short legs inflate
        # the requirement and trigger a false margin call that breaks the combo).
        defined_risk = self._defined_risk_contracts()
        # Short CALLs fully covered by long underlying shares (covered calls) carry ~zero
        # classic maintenance — exempt them like defined-risk legs.
        covered_calls = self._covered_short_call_contracts()
        # Short option legs.
        for lot in self._option_positions.values():
            if lot.qty >= 0:
                continue  # only SHORT legs carry naked-margin risk here
            if lot.contract_symbol in defined_risk:
                continue  # covered short leg of a defined-risk combo -> no naked margin
            if lot.contract_symbol in covered_calls:
                continue  # short call covered by long shares -> no naked margin
            strike, spot, right = self._lot_strike_spot_right(lot.contract_symbol)
            if strike is None:
                # An unresolvable strike UNDERSTATES the requirement (this short leg
                # contributes nothing) — never silently.
                logger.warning(
                    "[backtest] maintenance margin: no order resolves a strike for held "
                    "short option lot %s — its margin contribution is SKIPPED "
                    "(requirement understated).",
                    lot.contract_symbol,
                )
                continue
            if right is None:
                # An unresolvable RIGHT must not understate the requirement either: charge
                # the worst case over both rights (and log it — the leg's order is malformed).
                logger.warning(
                    "[backtest] maintenance margin: no option_type resolves for held short "
                    "option lot %s — charging the WORST-CASE margin over both rights.",
                    lot.contract_symbol,
                )
                req += max(
                    self.naked_margin_per_contract(strike, option_type=OptionRight.CALL, spot=spot),
                    self.naked_margin_per_contract(strike, option_type=OptionRight.PUT, spot=spot),
                ) * abs(lot.qty)
                continue
            req += self.naked_margin_per_contract(strike, option_type=right, spot=spot) * abs(lot.qty)
        # Short stock.
        for p in self._positions.values():
            if p.qty >= 0:
                continue
            px = self._price.close_at(p.symbol)
            if px is None:
                px = self._price.close_asof(p.symbol)
            if px is None:
                px = p.avg_price
            if px:
                req += self.SHORT_STOCK_MAINTENANCE_FRACTION * abs(p.qty) * float(px)
        return req

    def _defined_risk_contracts(self) -> set:
        """Set of currently-held contract_symbols that belong to a DEFINED-RISK combo.

        Uses the same ``_option_group_bounds`` grouping as the MTM clamp. A leg is defined-risk
        when its group's ``option_strategy`` is one of the defined-risk structures (debit or
        credit) — its short exposure is covered by the combo's long legs, so it carries no naked
        margin and must never be liquidated in isolation (that would break the combo and leave a
        permanent cash imbalance beyond the defined risk).
        """
        contract_group, group_bounds = self._option_group_bounds()
        out: set = set()
        for cs, gkey in contract_group.items():
            gb = group_bounds.get(gkey)
            if gb and (
                gb["strategy"] in self.DEFINED_RISK_LONG_STRATEGIES
                or gb["strategy"] in self.DEFINED_RISK_SHORT_STRATEGIES
            ):
                out.add(cs)
        return out

    def _lot_order(self, contract_symbol: str) -> Optional[TradingOrder]:
        """The option ``TradingOrder`` carrying a held lot's contract terms.

        The lot ledger keeps only qty/premium; the strike / option_type / underlying live on
        the option ``TradingOrder``. Returns None when no order with a strike resolves (callers
        then skip or fall back rather than guessing).

        Served from a ``contract_symbol -> order`` index built ONCE per ``_option_memo_gen``
        (the per-lot full-order scan was O(orders x lots) per bar on options runs). Same
        first-row-with-a-strike rule as the un-indexed scan, so results are byte-identical.
        """
        if self._lot_order_index is None or self._lot_order_index_gen != self._option_memo_gen:
            idx: Dict[str, TradingOrder] = {}
            for o in self.get_orders():
                cs = o.contract_symbol
                if cs and o.strike is not None and cs not in idx:
                    idx[cs] = o
            self._lot_order_index = idx
            self._lot_order_index_gen = self._option_memo_gen
        return self._lot_order_index.get(contract_symbol)

    def _lot_strike_spot_right(self, contract_symbol: str):
        """(strike, underlying_spot, option_right) for a held option lot, from its FILLED order.

        Returns (None, None, None) when the order/strike cannot be resolved (the caller then
        skips that lot's margin contribution rather than guessing). The right may independently
        be None (malformed order row) — the caller then charges the worst case over both rights.
        """
        o = self._lot_order(contract_symbol)
        if o is None:
            return None, None, None
        spot = None
        if o.underlying_symbol:
            spot = self._price.close_at(o.underlying_symbol)
            if spot is None:
                spot = self._price.close_asof(o.underlying_symbol)
        return float(o.strike), (float(spot) if spot is not None else None), o.option_type

    @staticmethod
    def _no_arb_premium_bounds(strike, is_call: bool, spot):
        """Per-share no-arbitrage premium bounds ``(intrinsic, upper)`` for one contract.

        THE single definition of the bounds the arb fill guard
        (``_arb_fill_reject_reason``) rejects junk prints against — floor at intrinsic
        (``max(0, spot-strike)`` call / ``max(0, strike-spot)`` put: nobody sells below
        immediate-exercise value), cap at the upper bound (a call can never cost more than
        the stock, a put never more than its strike). Every NON-fill consumer of the
        sparse premium cache (per-tick marks, expiry settlement, margin-liquidation
        buybacks) clamps into these same bounds via ``_clamp_premium_to_no_arb`` so a junk
        print is never realised into cash or equity (review 2026-08-30 F1) — one
        implementation, no drift.

        ``spot=None`` returns ``(None, upper-or-None)``: a put's upper bound (its strike)
        is spot-independent; nothing else is derivable without spot.
        """
        strike = float(strike)
        if spot is None:
            return None, (None if is_call else strike)
        spot = float(spot)
        intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
        return intrinsic, (spot if is_call else strike)

    @classmethod
    def _clamp_premium_to_no_arb(cls, premium: float, strike, is_call: bool, spot) -> float:
        """A cache premium clamped into ``_no_arb_premium_bounds`` (identity when spot is
        None — without spot no bound is derivable and the print is kept as-is)."""
        lo, hi = cls._no_arb_premium_bounds(strike, is_call, spot)
        if lo is not None:
            premium = max(float(premium), lo)
        if hi is not None:
            premium = min(float(premium), hi)
        return premium

    def _lot_no_arb_bounds(self, contract_symbol: str):
        """``(intrinsic, upper)`` no-arb premium bounds for a HELD lot's contract at the
        current underlying close, or None when strike / option right / spot cannot be
        resolved (callers then keep their existing unbounded behaviour — the fix is for
        the junk/no-OPTION-bar case, not the no-equity-bar case)."""
        strike, spot, right = self._lot_strike_spot_right(contract_symbol)
        if strike is None or spot is None or right is None:
            return None
        return self._no_arb_premium_bounds(strike, right == OptionRight.CALL, spot)

    def _covered_short_call_contracts(self) -> set:
        """Contract symbols of held SHORT CALL lots fully covered by LONG underlying shares.

        A short call covered share-for-share (long shares >= contracts x multiplier) has ~zero
        classic maintenance requirement — charging it naked margin overstates the requirement
        (false breach) and lets the margin call buy back a fully covered call. Covered lots are
        exempt from BOTH the requirement sum and the liquidation candidate set. When several
        short-call lots share one underlying, the shares are allocated GREEDILY (largest lot
        first) so the same shares never cover two lots; a lot is exempt only when FULLY covered.
        """
        by_underlying: Dict[str, List[_OptionLot]] = {}
        for lot in self._option_positions.values():
            if lot.qty >= 0:
                continue
            o = self._lot_order(lot.contract_symbol)
            if o is None or o.option_type != OptionRight.CALL or not o.underlying_symbol:
                continue
            by_underlying.setdefault(o.underlying_symbol, []).append(lot)
        covered: set = set()
        for underlying, lots in by_underlying.items():
            pos = self._positions.get(underlying)
            available = float(pos.qty) if (pos is not None and pos.qty > 0) else 0.0
            for lot in sorted(lots, key=lambda l: abs(l.qty), reverse=True):
                needed = abs(lot.qty) * float(lot.multiplier or 100)
                if needed <= available:
                    covered.add(lot.contract_symbol)
                    available -= needed
        return covered

    def _ledger_shares_pledged_to_short_calls(self, underlying: str) -> Optional[int]:
        """Shares of ``underlying`` PLEDGED as cover by open SHORT CALL lots — or None.

        OPT-L1, THE SIMULATOR'S EXIT HALF. A broker LOCKS the shares collateralising a
        written call: while the covered call is open you cannot sell the stock, because
        that would leave a naked call with unbounded upside risk. This is the number that
        lock is measured against (see ``_pledged_share_lock``).

        TRI-STATE, and the whole point of the method. An int (INCLUDING 0) is MEASURED and
        the caller may sell against it; ``None`` is UNMEASURABLE and the caller must
        REFUSE. "We could not find out" is not "nothing is pledged" — that conflation is
        exactly how a covered call goes naked, and it is the house's dominant defect class.

        WHY THIS IS NOT ``_covered_short_call_contracts`` AND NOT THE INHERITED
        ``shares_pledged_to_short_calls``:

          * ``_covered_short_call_contracts`` answers a DIFFERENT question — "which lots
            are FULLY covered", allocated GREEDILY largest-lot-first, all-or-nothing per
            lot. It feeds the maintenance-margin sum and the margin-call candidate set, so
            its "unresolvable lot -> skip" behaviour is pinned by tests and must not move;
            and reusing it here would reproduce its greedy artifact (two lots on one
            underlying where the first claims the shares) as a phantom shortfall. The lock
            needs the per-underlying SUM, which is allocation-free.
          * ``OptionsAccountInterface.shares_pledged_to_short_calls`` (inherited, and
            correct — it is what refuses the LIVE close path, and it works on this class)
            reads the ORDER BOOK. That is the right source live, where an unfilled
            sell-to-open is a real in-flight obligation. Here it means a DB/transaction
            scan on a per-fill hot path, and the fill-time question is about the ledger
            AFTER this bar's earlier fills. This reads ``_option_positions`` directly:
            O(lots), hermetic, and the more accurate of the two at fill time. Both are
            conservative, so double-guarding the close path costs nothing.

        WHAT PLEDGES AND WHAT DOES NOT. Only SHORT CALL lots pledge SHARES. A short PUT
        obliges CASH (cash-secured) and places no claim on share inventory — counting it
        would refuse a sale nothing has a claim on, which silently disables exits and is
        the mirror error of the defect. LONG options pledge nothing: only the short side
        can be called away. Lots netted to ZERO are left in ``_option_positions`` rather
        than removed, so ``qty >= 0`` skips both the longs and the closed shorts.

        THE MULTIPLIER IS READ PER LOT AND NEVER ASSUMED (OPT-L7). An adjusted contract —
        post-split, post-merger — can deliver a different number of shares, so
        ``float(lot.multiplier or 100)`` (the shape used a few lines above, where a guess
        only perturbs a margin estimate) would under-report the pledge on precisely the
        contract whose oddity nobody remembers. Unreadable here means UNKNOWN.

        EVERY WAY THE ANSWER CAN FAIL, and ONE failure poisons the WHOLE answer rather
        than shrinking it — a partial sum is a smaller number that looks exactly like a
        measured one, and the caller would free the difference:

          * ``_lot_order`` returns None: no order row carries this contract's terms (or
            every row for it has a NULL strike, which the index refuses). The lot might be
            a short call on this ticker and we cannot rule it out.
          * the right is neither CALL nor PUT (a malformed/absent ``option_type``): it
            might be the call.
          * a short CALL with no ``underlying_symbol``: it might be on THIS ticker, so it
            has to poison every ticker's answer. There is deliberately no fall-back to
            ``o.symbol`` — on an option leg row that field can hold the OCC string, which
            would never match and would report the shares as free.
          * a short CALL on THIS ticker with no ``expiry``: whether it is still alive
            cannot be decided, and a lot that might be alive still pledges (see below).
          * a multiplier that is absent or <= 0: a missing field, not a contract that
            delivers no shares.

        AN EXPIRED CONTRACT PLEDGES NOTHING, AND SAYS SO LOUDLY. A call that has passed
        its expiry can no longer be assigned, so it holds no claim on any share — the
        broker released that cover at settlement. Counting it is not "conservative", it is
        WRONG in the disabling direction: measured on the O_CC reference run
        (GOOG,BAC,INTC,F,T / 2023-01-10..2023-03-28), BAC's pledge on 2023-03-08 came out
        at 600 shares against ONE live 200-share call, because two lots that had expired on
        2023-02-10 and 2023-03-03 were still being counted. 297 held minus a phantom 600
        left ZERO free, so the 293-share stop-loss was refused IN FULL on three consecutive
        bars instead of clamping to the 97 shares the design promises — the shares then rode
        to run end with no exit. On the same bar the platform's own order-book measure
        (``OptionsAccountInterface.shares_pledged_to_short_calls``) answered 200: two guards
        in one run disagreeing by 400 shares, with the ledger the wrong one.

        WHY AN EXPIRED LOT IS STILL IN THE LEDGER AT ALL is a separate, PRE-EXISTING
        bookkeeping fault, and this method is where it becomes visible, so it is named at
        ERROR (once per contract per run). ``_apply_option_expiry`` settles what
        ``get_option_positions()`` reports, and that view is derived from OPENED
        transactions; a lot whose option transaction was closed by some other route is
        therefore never settled and its ``_OptionLot.qty`` stays non-zero forever. Treating
        it as live cover would make the pledge PERMANENT — the contract can never expire
        again to release it — which is a frozen exit, not a safeguard. This does NOT repair
        the ledger (that is not this method's job and a silent repair here would hide the
        fault); it reports it and answers the question that was actually asked.

        STRICTLY ``expiry < today``. The engine settles a contract on the bar where
        ``expiry <= as_of`` (``daily_engine._apply_option_expiry``), and the bracket/fill
        paths that consult this lock run EARLIER in that same bar, so on the expiry day
        itself the call is still live and still pledges.

        ROUNDED UP, and paired with a round-DOWN on the holding at the call site: a
        fractional share cannot cover a contract, so both roundings must point at
        "less free inventory than the raw arithmetic suggests".
        """
        wanted = (underlying or "").strip().upper()
        if not wanted:
            logger.error(
                "[backtest] PLEDGED COVER: _ledger_shares_pledged_to_short_calls(%r) has no "
                "underlying to measure — reporting UNKNOWN rather than 'nothing is pledged', "
                "which is what a 0 would be read as.", underlying)
            return None

        total = 0.0
        for lot in self._option_positions.values():
            if lot.qty >= 0:
                continue  # long lots and bought-back (zeroed) shorts pledge nothing
            contract = lot.contract_symbol
            o = self._lot_order(contract)
            if o is None:
                logger.error(
                    "[backtest] PLEDGED COVER UNMEASURABLE for %s: the held SHORT lot %s "
                    "(%g contract(s)) has no resolvable option order — no row carries the "
                    "contract, or every row for it has a NULL strike. It may be a short CALL "
                    "on %s, so how many shares are pledged CANNOT be measured and must not be "
                    "read as zero. Repair the option order book; this is NOT a share shortfall.",
                    wanted, contract, lot.qty, wanted)
                return None
            right = o.option_type
            if right == OptionRight.PUT:
                continue  # a short put pledges CASH (cash-secured), never share inventory
            if right != OptionRight.CALL:
                logger.error(
                    "[backtest] PLEDGED COVER UNMEASURABLE for %s: the held SHORT lot %s "
                    "(%g contract(s)) reports option_type=%r — neither CALL nor PUT. It might "
                    "be the call whose cover is about to be sold, so the pledge is UNKNOWN.",
                    wanted, contract, lot.qty, right)
                return None
            under = (o.underlying_symbol or "").strip().upper()
            if not under:
                logger.error(
                    "[backtest] PLEDGED COVER UNMEASURABLE for %s: the held SHORT CALL lot %s "
                    "(%g contract(s)) names no underlying_symbol, so its pledge cannot be "
                    "attributed to a ticker — it might be on %s. (There is no fall-back to "
                    "order.symbol: on an option leg row that field holds the OCC string and "
                    "would never match, reporting the shares as free.)",
                    wanted, contract, lot.qty, wanted)
                return None
            if under != wanted:
                continue
            # ALIVE OR NOT? Only a call that can still be assigned has a claim on shares.
            expiry = o.expiry
            if expiry is None:
                logger.error(
                    "[backtest] PLEDGED COVER UNMEASURABLE for %s: the held SHORT CALL lot %s "
                    "(%g contract(s)) carries no expiry, so whether it is still alive — and "
                    "therefore whether it still holds a claim on %s shares — cannot be "
                    "decided. An undated contract is not an expired one; repair the option "
                    "order book. This is NOT a share shortfall.",
                    wanted, contract, lot.qty, wanted)
                return None
            if expiry < self._as_of_date():
                # PRE-EXISTING LEDGER FAULT, surfaced here rather than silently obeyed.
                # An expired call cannot be assigned, so it pledges nothing; but a lot that
                # is still on the ledger days after its expiry was never settled (see the
                # docstring — get_option_positions() only reports OPENED transactions), and
                # counting it would freeze this ticker's exits for the rest of the run.
                self._log_pledged_lock(
                    contract, "stale-expired-lot",
                    "[backtest] PLEDGED COVER: the option lot ledger still carries %s "
                    "(%g contract(s), a SHORT CALL on %s) even though it EXPIRED on %s and "
                    "today is %s. An expired call can no longer be assigned, so it pledges "
                    "NO shares and is excluded — counting it would lock %s's shares forever, "
                    "because the contract can never expire again to release them. The lot "
                    "should have been settled by the expiry pass: its option transaction is "
                    "no longer OPENED, so get_option_positions() never showed it to "
                    "_apply_option_expiry. Repair the option transaction bookkeeping; the "
                    "pledge itself is unaffected.",
                    (contract, lot.qty, wanted, expiry, self._as_of_date(), wanted))
                continue
            multiplier = lot.multiplier
            if multiplier is None or multiplier <= 0:
                logger.error(
                    "[backtest] PLEDGED COVER UNMEASURABLE for %s: the held SHORT CALL lot %s "
                    "(%g contract(s)) has multiplier=%r. A multiplier is a MISSING FIELD when "
                    "it is absent or <= 0, not a contract that delivers no shares — guessing "
                    "100 would under-report an adjusted (post-split) contract's pledge.",
                    wanted, contract, lot.qty, multiplier)
                return None
            total += abs(float(lot.qty)) * float(multiplier)
        # Round UP (the -1e-9 keeps an exact 100.0 at 100 rather than pushing it to 101):
        # a partial contract still calls away a whole block of shares.
        return int(math.ceil(total - 1e-9)) if total > 0 else 0

    def _pledged_share_lock(self, symbol: str, qty: float, *, context: str) -> float:
        """How many of ``qty`` shares of ``symbol`` may ACTUALLY be sold — the broker's lock.

        THE SELL-SIDE MIRROR of the CASH-SECURED safeguard in ``_apply_fill``. That one
        stops a BUY the account cannot fund; this one stops a SELL the account is not
        free to make, because the shares are pledged as cover for an open short call.

        MEASURED DEFECT (O_CC / GOOG,BAC,INTC,F,T / 2023-01-10..2023-03-28 / $100k /
        gates-off / 1d): four short calls carried for up to 40 bars with 4 shares held
        against 200 needed — genuinely naked calls with unbounded upside risk, produced
        with no error and no log line, because the equity exit rules (a staged trailing
        stop plus a time exit) sold the collateral out from under them. The GA arms that
        sell premium can therefore book premium against a risk profile no broker would
        permit, which in a benign window reads as free money.

        CLAMP, DO NOT BLANKET-REFUSE. A broker lets you sell the UNPLEDGED EXCESS: 297
        held with 200 pledged sells 97; 200 held with 200 pledged sells nothing. Refusing
        wholesale would disable legitimate exits on any ticker that happens to carry a
        covered call, which is its own (quieter) way of producing wrong numbers.

        THE ALTERNATIVE THAT WAS REJECTED: allow the sale and buy the call back on the
        next bar. That leaves real OVERNIGHT naked exposure — precisely the risk being
        modelled — so the lock is modelled instead.

        THREE SHORT-CIRCUITS, in cost order, and each one keeps a class of run
        bit-identical to before this method existed:

          1. no option lots at all -> equity-only runs pay one dict truth test;
          2. the account is not LONG the symbol -> a sell from flat or from a short OPENS
             or ADDS TO a short. There are no long shares to pledge and nothing to strip,
             so short selling is never touched;
          3. a MEASURED pledge of 0 -> return ``qty`` unchanged. In particular a
             long-to-short FLIP is left alone here, and only clamped when something is
             actually pledged (in which case forgoing the short leg is the conservative
             reading; the simulator's exits do not flip in practice).

        UNKNOWN IS A REFUSAL, NOT A ZERO: an unmeasurable pledge returns 0 (sell nothing)
        and says so, naming the lot that could not be measured, so an operator repairs the
        order book instead of hunting a phantom shortfall.

        THE LOG IS DEDUPED PER (symbol, reason) PER RUN, unlike the cash-secured block
        above. That one is documented as "never fires in a correct run"; this one fires
        REPEATEDLY BY DESIGN — a staged TP/SL re-arms on every bar the level is crossed,
        and the measured BAC case stood for 40 consecutive bars. One loud ERROR carrying
        the full explanation, then DEBUG, keeps a GA's logs readable without hiding the
        event.

        NOT EVERY SHARE-MUTATING PATH GOES THROUGH HERE, deliberately.
        ``_book_assignment_share_leg`` (the broker DELIVERING the stock on assignment) is
        exempt: it removes the shares AND the call together, and blocking it would
        deadlock the wheel, whose only exit IS being called away. ``_liquidate_stock_
        position`` is exempt by construction — its only caller iterates ``qty < 0``, so it
        is always a BUY-to-cover that RAISES the share count. This is also why the lock
        lives at the order/fill boundary and never in the shared ``_update_position``.
        """
        if qty <= 0 or not self._option_positions:
            return qty
        pos = self._positions.get(symbol)
        held = float(pos.qty) if pos is not None else 0.0
        if held <= 0:
            return qty  # selling from flat/short opens a short; nothing is pledged

        pledged = self._ledger_shares_pledged_to_short_calls(symbol)
        if pledged is None:
            self._log_pledged_lock(
                symbol, "unmeasurable",
                "[backtest] PLEDGED COVER: REFUSING to sell %g %s share(s) (%s) — how many "
                "are pledged as cover for open short calls could not be measured (the ERROR "
                "above names the unreadable lot). The account holds %g; selling any of them "
                "could strip an open short call of its cover, and an unmeasurable pledge must "
                "not be read as 'nothing is pledged'. Repair the option order book — this is "
                "NOT a shortfall and closing a position will not clear it.",
                (qty, symbol, context, held))
            return 0.0
        if pledged <= 0:
            return qty  # MEASURED zero: nothing has a claim, path unchanged

        # Round the holding DOWN against a pledge rounded UP: a fractional share cannot
        # cover a contract, so both roundings point at "less free inventory".
        free = max(0.0, float(math.floor(held)) - float(pledged))
        if qty <= free:
            return qty
        self._log_pledged_lock(
            symbol, "clamped" if free > 0 else "blocked",
            "[backtest] PLEDGED-COVER lock TRIPPED on %s (%s): a SELL of %g share(s) would "
            "leave %g of the %g this account holds, but %d are pledged as cover for open "
            "short calls — short by %g, which would leave a written call NAKED (unbounded "
            "upside risk a broker would not permit). %s Buy back or let the short call "
            "expire to release its cover. NOTE this can recur on every bar while the pledge "
            "stands (a staged TP/SL re-arms each bar it is crossed); further occurrences "
            "for %s are logged at DEBUG.",
            (symbol, context, qty, held - qty, held, pledged, pledged - (held - qty),
             (f"Clamping the sale to the {free:g} unpledged share(s)." if free > 0 else
              "NOTHING may be sold; the sale is refused in full."), symbol))
        return free

    def _log_pledged_lock(self, subject: str, reason: str, fmt: str, args: tuple) -> None:
        """LOUD once per (subject, reason) per run, DEBUG thereafter. See ``_pledged_share_lock``.

        ``subject`` is whatever the recurrence is keyed on: the TICKER for the lock's own
        refusal/clamp (one explanation per symbol, however many bars it stands for) and the
        CONTRACT for the stale-expired-lot report in
        ``_ledger_shares_pledged_to_short_calls`` (one per dead contract, not one per bar
        per ticker that happens to consult it).
        """
        key = (subject, reason)
        if key in self._pledged_lock_logged:
            logger.debug(fmt, *args)
            return
        self._pledged_lock_logged.add(key)
        logger.error(fmt, *args)

    def maybe_margin_call_liquidation(self) -> bool:
        """Force-liquidate SHORT positions when equity breaches maintenance margin.

        Mirrors a broker margin call: if net-liquidating-value (equity) is below the total
        maintenance requirement (or below zero), close the highest-margin SHORT positions at the
        current bar's premium/close — booking the realised loss to cash — until the requirement is
        satisfied or the book is flat. Returns True if ANY position was liquidated.

        Deterministic and cheap: it runs only after the (rare) breach check trips, and reuses the
        in-memory ledger close paths (no per-bar DB churn on healthy bars). Long positions are
        left untouched (their risk is bounded and already funded); only the unbounded SHORT risk
        is unwound. Logs a ``margin_call_liquidation`` line per closed position.

        OPTIONS-ONLY: this is a naked short-PREMIUM defense. Equity-only backtests (no options
        provider) never had a margin-call path — short-circuit here so their behaviour stays
        byte-identical and they pay zero per-bar cost (one attribute check).
        """
        if self._options is None:
            return False
        # Compute equity + requirement ONCE per breach check (each re-runs the option-book
        # scans; the old inline check computed equity twice and both run EVERY bar).
        eq = self.equity()
        req = self.maintenance_margin_requirement()
        if eq >= req and eq >= 0:
            return False

        liquidated = False
        # DEFINED-RISK combo legs are covered (defined risk) — never liquidate them in isolation
        # (that orphans the combo's long legs and leaves a permanent cash imbalance). Only unwind
        # genuinely NAKED short legs (short strangle/straddle/jade_lizard/put_ratio, single-leg
        # naked short).
        defined_risk = self._defined_risk_contracts()
        # Covered short CALLs are not naked either — never buy back a fully covered call to fix
        # a breach. The cover set is stable during the option loop (only option lots change
        # there, and long shares are never unwound — the stock loop below touches SHORT stock).
        covered_calls = self._covered_short_call_contracts()
        # Unwind naked short OPTION legs first (the unbounded-risk exposure), largest lot first,
        # re-checking the breach after each close so we stop as soon as margin is satisfied.
        # eq/req are recomputed ONCE per liquidation (each close changes both), not per
        # comparison.
        while True:
            if eq >= req and eq >= 0:
                break
            short_lots = [
                l for l in self._option_positions.values()
                if l.qty < 0
                and l.contract_symbol not in defined_risk
                and l.contract_symbol not in covered_calls
            ]
            if not short_lots:
                break
            lot = max(short_lots, key=lambda l: abs(l.qty))
            if not self._liquidate_option_lot(lot):
                break
            liquidated = True
            eq = self.equity()
            req = self.maintenance_margin_requirement()

        # Then unwind short STOCK if still breaching.
        while True:
            if eq >= req and eq >= 0:
                break
            shorts = [p for p in self._positions.values() if p.qty < 0]
            if not shorts:
                break
            pos = max(shorts, key=lambda p: abs(p.qty))
            if not self._liquidate_stock_position(pos):
                break
            liquidated = True
            eq = self.equity()
            req = self.maintenance_margin_requirement()

        return liquidated

    def _liquidate_option_lot(self, lot: "_OptionLot") -> bool:
        """Buy back a SHORT option lot at the current premium close; book cash + close the txn."""
        bar = self._options.get_bar(lot.contract_symbol, self._as_of_date()) if self._options else None
        bounds = self._lot_no_arb_bounds(lot.contract_symbol)  # (intrinsic, upper) or None
        if bar and bar.get("close") is not None:
            # The blow-up bar's print can be junk (the arb guard's documented class):
            # a $0.01 buyback against $20 of intrinsic understates the blow-up, an
            # impossible above-upper print overstates it. Clamp into the same no-arb
            # bounds fills are guarded by; kept raw only when the bounds are
            # unresolvable (no spot: fail-open, like the guard). (Review 2026-08-30 F1.)
            #
            # A margin liquidation is a FORCED close: it crosses the modelled bid-ask
            # spread fully (a buyback lifts the ask, ``close + half``) exactly like every
            # other risk exit, THEN clamps into the no-arb bounds (review 2026-08-30 F7).
            # With no spread model configured ``_option_cross`` is the identity.
            premium = self._option_cross(float(bar["close"]), True, bar)
            if bounds is not None:
                premium = min(max(premium, bounds[0]), bounds[1])
        else:
            # No premium bar on the liquidation bar. The entry premium books the buyback at
            # break-even — understating the loss at exactly the moment a breach implies the
            # premium moved against the short. Use INTRINSIC, floored at the entry premium
            # (a forced buyback is never booked BELOW entry mid-blow-up); the entry premium
            # remains the last resort when strike/spot/right are unresolvable.
            premium = lot.avg_price
            if bounds is not None and premium is not None:
                premium = max(bounds[0], premium)
        if premium is None:
            return False
        txn = self._option_transaction_for_contract(lot.contract_symbol)
        contracts = abs(lot.qty)
        multiplier = lot.multiplier
        # Buying back a short lot DEBITS cash (premium x contracts x multiplier).
        self._cash -= contracts * float(premium) * multiplier
        if txn is not None:
            # Build the OptionPosition view for this leg so the close is recorded like an expiry
            # settlement (synthetic FILLED closing order for round-trip pairing).
            pos = self._option_position_for_lot(lot, txn)
            if pos is not None:
                self._record_option_expiry_close(txn, pos, float(premium))
        lot.qty = 0.0
        lot.avg_price = 0.0
        if txn is not None and self._all_legs_resolved(txn):
            from ba2_common.core.utils import close_transaction_with_logging

            txn.close_price = float(premium)
            if not txn.close_date:
                txn.close_date = self._price.now()
            close_transaction_with_logging(
                txn, account_id=self.id, close_reason="margin_call_liquidation",
                additional_data={"contract_symbol": lot.contract_symbol},
            )
            update_instance(txn)
        logger.warning(
            "[backtest] margin_call_liquidation: bought back SHORT %g x %s @ %.4f (premium) "
            "to satisfy maintenance margin.", contracts, lot.contract_symbol, float(premium),
        )
        return True

    def _option_position_for_lot(self, lot: "_OptionLot", txn) -> Optional[OptionPosition]:
        """An OptionPosition describing a held lot (for recording its liquidation close)."""
        o = self._lot_order(lot.contract_symbol)
        if o is None:
            return None
        return OptionPosition(
            contract_symbol=lot.contract_symbol,
            underlying=o.underlying_symbol,
            option_type=o.option_type,
            strike=o.strike,
            expiry=o.expiry,
            side=(OrderDirection.BUY if lot.qty > 0 else OrderDirection.SELL),
            quantity=abs(lot.qty),
            avg_entry_price=lot.avg_price,
            multiplier=lot.multiplier,
        )

    def _liquidate_stock_position(self, pos: "_Position") -> bool:
        """Close a stock position at the current close; book cash + realise P&L via the ledger."""
        px = self._price.close_at(pos.symbol)
        if px is None:
            px = self._price.close_asof(pos.symbol)
        if px is None:
            return False
        closed_qty = abs(pos.qty)
        was_long = pos.qty > 0
        signed = -pos.qty  # opposite sign closes the position
        # Selling (signed<0) credits cash; buying-to-cover (signed>0) debits cash.
        self._cash -= signed * float(px)
        self._update_position(pos.symbol, signed, float(px))
        # Persist a synthetic FILLED closing order (the option-lot path already records one via
        # _record_option_expiry_close) so the equity jump shows up as a trade in
        # get_round_trip_trades/reports instead of an unexplained cash move.
        self._record_stock_liquidation_close(pos.symbol, closed_qty, was_long, float(px))
        logger.warning(
            "[backtest] margin_call_liquidation: closed STOCK %g x %s @ %.4f to satisfy "
            "maintenance margin.", closed_qty, pos.symbol, float(px),
        )
        return True

    def _record_stock_liquidation_close(
        self, symbol: str, qty: float, was_long: bool, px: float,
        comment: str = "margin_call_liquidation",
    ) -> None:
        """Persist a synthetic FILLED closing order for a forced STOCK liquidation
        (margin call, or the post-assignment next-bar liquidation — ``comment`` names which).

        BOOK-KEEPING only (the caller already moved cash + ledger). Linked to the symbol's
        OPENED equity transaction when one resolves — the entry order's side must match the
        liquidated direction — carrying ``depends_on_order`` so the sim-dated close is never
        mistaken for the entry by ``_entry_order_for_transaction`` (same guard as
        ``_record_option_expiry_close``). When no transaction resolves the order is persisted
        unlinked; a transaction is never invented.
        """
        want_side = OrderDirection.BUY if was_long else OrderDirection.SELL
        txn_id = None
        entry_id = None
        txns = transactions_where(status=TransactionStatus.OPENED, symbol=symbol)
        for t in txns:
            entry = self._entry_order_for_transaction(t)  # account-scoped lookup
            if (
                entry is not None
                and getattr(entry, "asset_class", None) != AssetClass.OPTION
                and entry.side == want_side
            ):
                txn_id = t.id
                entry_id = entry.id
                break
        as_of = self._price.now()
        order = TradingOrder(
            account_id=self.id,
            symbol=symbol,
            quantity=abs(float(qty)),
            filled_qty=abs(float(qty)),
            side=(OrderDirection.SELL if was_long else OrderDirection.BUY),
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            open_price=float(px),
            transaction_id=txn_id,
            depends_on_order=entry_id,
            open_type=OrderOpenType.AUTOMATIC,
            broker_order_id=self._next_broker_id(),
            comment=comment,
            created_at=as_of,
        )
        new_id = add_instance(order)
        if new_id is not None:
            self._fill_dates[new_id] = as_of
        self.invalidate_order_cache()

    def snapshot_equity(self, as_of: datetime) -> Dict[str, Any]:
        """Append an equity-curve snapshot for ``as_of`` and return it.

        The engine calls this once per bar (after fills/transactions are rolled). Keys
        match ``ReadOnlyAccountInterface.get_balance_history``'s documented contract
        (date / net_liquidating_value / cash_balance / equity_value).

        Clamps a non-positive net_liquidating_value to 0.0 in the RECORDED point and sets
        ``self._wiped_out`` -- a real (non-margin) account cannot hold negative equity, so
        anything the sim computes past that point is unbounded and meaningless (this is what
        produces impossible drawdowns like -1900%). The engine's main loop stops the run as
        soon as this flag is set; see its docstring for the full story.

        A NON-FINITE (NaN/Inf) net_liquidating_value is a different animal and is REJECTED, not
        clamped: zero and negative equity are real, meaningful states, but NaN is arithmetic
        that went wrong (a NaN price/premium reaching the mark). Recording it let the run finish
        and score, because ``results._safe_float`` swapped every NaN equity point for the
        initial capital -- turning a broken run into a flat, zero-drawdown one whose calmar_ratio
        equals its annualised return. A run that produced nonsense must fail, not look flawless.
        """
        equity_value = self._open_positions_mtm()
        nlv = self._cash + equity_value
        if not math.isfinite(nlv):
            raise ValueError(
                f"[backtest] non-finite net_liquidating_value at {as_of}: cash={self._cash!r} "
                f"open-position MTM={equity_value!r}. A NaN/Inf equity point cannot be scored; "
                f"the run is rejected rather than silently coerced to a flat curve."
            )
        if nlv <= 0:
            self._wiped_out = True
            nlv = 0.0
        snap = {
            "date": as_of,
            "net_liquidating_value": nlv,
            "cash_balance": self._cash,
            "equity_value": equity_value,
        }
        self._equity_snapshots.append(snap)
        self._snapshot_dates.append(as_of)
        return snap

    def _update_position(self, symbol: str, signed_qty: float, fill_px: float) -> None:
        """Apply a signed fill to the ledger.

        Increasing (same-sign) exposure updates the weighted-average price; reducing or
        flipping realises P&L on the closed portion. ``signed_qty`` is +buy / -sell.
        """
        # A fill changes this account's OPENED-transaction set, so drop the per-expert snapshot
        # (rebuilt lazily on the next read). This is the universal equity ledger fill path
        # (order fills + option assignment), mirroring invalidate_order_cache's discipline.
        if self._opened_txn_snapshot:
            self._opened_txn_snapshot = {}

        pos = self._positions.get(symbol)
        if pos is None:
            pos = _Position(symbol=symbol)
            self._positions[symbol] = pos

        old_qty = pos.qty
        new_qty = old_qty + signed_qty

        if old_qty == 0 or (old_qty > 0) == (signed_qty > 0):
            # Opening or increasing in the same direction -> weighted-average price.
            total_cost = pos.avg_price * abs(old_qty) + fill_px * abs(signed_qty)
            denom = abs(new_qty)
            pos.avg_price = (total_cost / denom) if denom > 0 else 0.0
        else:
            # Reducing / closing / flipping -> realise P&L on the closed quantity.
            closing_qty = min(abs(signed_qty), abs(old_qty))
            direction = 1.0 if old_qty > 0 else -1.0
            pos.realized_pl += (fill_px - pos.avg_price) * closing_qty * direction
            if abs(signed_qty) > abs(old_qty):
                # Flipped through zero -> the remainder opens a new position at fill price.
                pos.avg_price = fill_px
            # If fully or partially closed without flipping, avg_price is unchanged.

        pos.qty = new_qty
        if pos.qty == 0:
            pos.avg_price = 0.0

    # ======================================================================
    # ReadOnlyAccountInterface abstracts (12)
    # ======================================================================
    def get_balance(self) -> Optional[float]:
        """Spendable cash, never more than the deployed equity allows.

        (The simulated cash ledger when no equity cap is configured — the historical
        behaviour, byte-identical.)

        ``min`` in both directions matters: the cap must not RAISE cash above what is actually
        held (money in open positions is not spendable), and cash must not exceed the cap when
        the account is sitting flat above it.
        """
        if self._equity_cap is None:
            return self._cash
        return min(self._cash, self.deployed_equity())

    def get_account_info(self) -> Dict[str, Any]:
        """Account info dict; exposes ``.equity`` (read by _validate_position_size_limits)."""
        eq = self.deployed_equity()
        cash = self.get_balance()
        return _AttrDict(
            {
                "balance": cash,
                "cash": cash,
                "equity": eq,
                "buying_power": max(cash, 0.0),
            }
        )

    def get_positions(self) -> Any:
        """List of open ledger positions (non-zero qty)."""
        out: List[_AttrDict] = []
        for p in self._positions.values():
            if p.qty == 0:
                continue
            cur = self._price.close_at(p.symbol)
            if cur is None:  # no exact bar this tick -> last-known close (not None/stale)
                cur = self._price.close_asof(p.symbol)
            out.append(
                _AttrDict(
                    {
                        "symbol": p.symbol,
                        "qty": p.qty,
                        "quantity": p.qty,
                        "avg_price": p.avg_price,
                        "average_price": p.avg_price,
                        "current_price": cur,
                        "unrealized_pl": (None if cur is None else (cur - p.avg_price) * p.qty),
                        "realized_pl": p.realized_pl,
                    }
                )
            )
        return out

    def get_orders(self, status: Optional[Any] = None) -> Any:
        """Query ``TradingOrder`` rows for this account from the backtest DB.

        ``status`` filters by OrderStatus when provided (ALL / None returns everything).
        """
        statuses = ([status] if (status is not None and status != OrderStatus.ALL) else None)
        return orders_where(account_id=self.id, statuses=statuses)

    def invalidate_order_cache(self) -> None:
        """Drop the in-memory order cache so the next read reloads from the DB.

        The per-bar fill engine reads working orders on EVERY bar; querying them from the DB
        each time dominated the cost of a fine (5-minute) fill clock (profiled). We cache the
        account's orders and serve ``_orders_filtered`` from memory. The account's OWN per-bar
        mutations (fills / cancels / activation) happen IN PLACE on the cached objects, so the
        cache stays valid without a reload. It only goes stale when NEW orders may have been
        created — analysis / bypass passes, bracket attach, option settlement — and the engine
        calls this at exactly those points. Those bars are rare on a fine clock, so the hot
        no-event bars do ZERO order DB reads.
        """
        self._order_cache = None
        self._active_order_cache = None
        # Any order-set change also invalidates the options memos (_option_group_bounds /
        # the _lot_order index) — they are keyed on this generation, not recomputed per bar.
        self._option_memo_gen += 1

    def opened_position_snapshot(self, expert_id: int) -> Dict[str, List[tuple]]:
        """Expert-scoped snapshot of this account's OPENED transactions, cached + invalidated on
        every ledger fill (see ``_update_position``).

        Returns ``{symbol: [(transaction_id, open_price, open_qty), ...]}`` where ``open_qty`` is
        the transaction's net filled quantity (``Transaction.get_current_open_qty``). This is
        GENERAL account infrastructure (keyed by ``expert_id``, no expert-specific logic): a per-bar
        position manager — any expert's, classic or bypass — can read the OPENED set + cost basis
        without re-querying the DB on every bar. The set only changes when an order FILLS, which is
        exactly when ``_update_position`` drops the cache (same discipline as
        ``invalidate_order_cache``). On a 5-minute clock holding positions across thousands of
        bars this turns ~one OPENED ``SELECT`` + one ``get_current_open_qty`` query PER OPENED
        transaction PER BAR into one rebuild per fill.

        Built with the SAME query (no ``order_by``) + the SAME per-transaction qty computation the
        direct DB path used, so any consumer's results stay byte-identical to the un-cached path.
        """
        cached = self._opened_txn_snapshot.get(expert_id)
        if cached is not None:
            return cached

        snapshot: Dict[str, List[tuple]] = {}
        txns = transactions_where(expert_id=expert_id, status=TransactionStatus.OPENED)
        # Attributes are read immediately into plain tuples (safe for both the store objects and
        # the flag-off session-loaded rows); get_current_open_qty is computed ONCE here, not per bar.
        for t in txns:
            snapshot.setdefault(t.symbol, []).append(
                (t.id, t.open_price, t.get_current_open_qty())
            )

        self._opened_txn_snapshot[expert_id] = snapshot
        return snapshot

    def _all_orders(self) -> List[TradingOrder]:
        """This account's FULL TradingOrder set (incl. terminal), loaded once and cached.

        Kept ONLY for terminal-needing callers (``get_orders``/results/round-trip P&L) — it is
        NOT used in the per-bar fill path anymore (that goes through the O(active)
        ``_active_orders`` query). Because the fill engine mutates+persists the SEPARATE active
        instances, instances in THIS cache may be stale for orders that filled this run; callers
        that need current state must read fresh (see ``_active_orders``' instance note)."""
        if self._order_cache is None:
            self._order_cache = orders_where(account_id=self.id)
        return self._order_cache

    def _active_orders(self) -> List[TradingOrder]:
        """The working set: this account's ACTIVE-status orders, loaded by an ACTIVE-STATUS
        SQL query — O(active), independent of ``_all_orders`` (which materialises EVERY order
        ever created).

        This is what the per-bar fill loop iterates. A long churning run accumulates thousands
        of terminal (filled/cancelled) orders; the old design re-scanned ALL of them every bar
        (and reloaded the full set on each invalidation). Querying only the active statuses keeps
        the per-bar working set proportional to the (small) number of live orders, not the
        ever-growing total.

        INSTANCE NOTE (critical): these are SEPARATE instances from ``_all_orders`` — active-only.
        The fill engine mutates THESE instances in place and persists them. ``FILLED`` is NOT an
        active status, so once an order fills it drops OUT of this query's next reload; the full
        ``_all_orders`` cache may still hold a STALE pre-fill instance of it. Any per-bar caller
        that needs the CURRENT persisted state of a (possibly now-terminal) order must therefore
        read FRESH (``get_instance`` / a direct query) or via the active cache for active orders —
        never via a stale ``_all_orders`` instance. Orders that go terminal in place between
        invalidations stay referenced here but are excluded by the per-call status filter, so
        results are unchanged."""
        if self._active_order_cache is None:
            if self._active_set is None:
                self._active_set = frozenset(OrderStatus.get_active_statuses())
            self._active_order_cache = orders_where(
                account_id=self.id, statuses=OrderStatus.get_active_statuses())
        return self._active_order_cache

    def _orders_filtered(self, statuses=None, transaction_id=None) -> List[TradingOrder]:
        """This account's orders, filtered by status / transaction.

        Fast path (the per-bar fill engine): a status filter that's a SUBSET of the active
        statuses is served from the O(active) working set (``_active_orders``), so the loop
        never scans the thousands of terminal orders a long run accumulates. The active cache's
        objects are the SAME instances the fill engine mutates in place, so a fill/cancel/
        activation is immediately visible without a reload.

        Transaction-only filter (no statuses — ``_existing_legs`` / ``_cancel_oco_sibling``):
        read FRESH from the DB. Since the fill engine now persists its mutations on the SEPARATE
        active instances, the full ``_all_orders`` cache can hold STALE instances of orders that
        filled/cancelled this run; a fresh per-transaction query is needed so these callers see
        the current persisted leg statuses (and they only run on rare adjust/cancel/bracket
        events, so the query cost is negligible). A status filter that is NOT a subset of active
        (terminal-needing) likewise reads fresh."""
        if statuses is not None:
            sset = set(statuses)
            if self._active_set is None:
                self._active_set = frozenset(OrderStatus.get_active_statuses())
            if sset <= self._active_set:
                orders = [o for o in self._active_orders() if o.status in sset]
            else:
                # Terminal-needing: read fresh so persisted terminal state is reflected (the
                # cached full set may be stale). Rare path.
                rows = orders_where(account_id=self.id)
                orders = [o for o in rows if o.status in sset]
        else:
            # Transaction-only (no status filter): fresh read for current persisted state. Push
            # transaction_id into the filter so this loads ONLY the (few) legs of this transaction,
            # not every order ever created — keeps the rare adjust/cancel path O(legs).
            return orders_where(account_id=self.id, transaction_id=transaction_id)
        if transaction_id is not None:
            orders = [o for o in orders if o.transaction_id == transaction_id]
        return orders

    def get_order(self, order_id: str) -> Any:
        """Look up an order by broker_order_id, then by numeric PK as a fallback."""
        matches = orders_where(broker_order_id=str(order_id))
        if matches:
            return matches[0]
        if str(order_id).isdigit():
            from ba2_common.core import trade_store as _ts
            if _ts.inmem_trades_active():
                return _ts.store_get(TradingOrder, int(order_id))
            from sqlmodel import Session
            with Session(get_db().bind) as session:
                return session.get(TradingOrder, int(order_id))
        return None

    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        """A symbol "exists" iff the backtest price store has bars for it."""
        return {s: self._price.has_symbol(s) for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type: str = "bid"):
        """The time machine: the as-of bar's close for the symbol(s).

        Single symbol -> float (raises if unavailable, per the live no-fallback rule).
        List -> {symbol: price-or-None}.
        """
        if isinstance(symbol_or_symbols, (list, tuple, set)):
            return {s: self._price.close_at(s) for s in symbol_or_symbols}
        px = self._price.close_at(symbol_or_symbols)
        if px is None:
            raise ValueError(
                f"No backtest price for {symbol_or_symbols} at {self._price.now()}"
            )
        return px

    def _is_washtrade_lock_candidate(self, trading_order) -> bool:
        """Wash-trade friction is a LIVE-broker rejection risk, deliberately NOT modeled here.

        The inherited check would mark an order WASHTRADE_LOCKED when an opposing order is
        working — but the sim has no TradeManager unlock loop, and the fill engine's
        active-status working set includes WASHTRADE_LOCKED anyway, so a "locked" order used
        to just fill regardless (a confusing half-state: live delays/holds, backtest fills).
        Disabling the check makes the divergence explicit and the order state consistent:
        the backtest behaves as live-after-unlock (the order executes)."""
        return False

    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        """Submit an order through the inherited path, then drop the in-memory order cache.

        Every order (entry, exit, OCO/adjust leg) is created via this single entry point, so
        invalidating here keeps the cache correct for ANY caller — the engine AND direct/unit
        use — without each creation site having to know about the cache. The fill engine reads
        the fresh order on its next ``_orders_filtered`` call.
        """
        result = super().submit_order(
            trading_order, tp_price=tp_price, sl_price=sl_price, is_closing_order=is_closing_order
        )
        self.invalidate_order_cache()
        return result

    def submit_option_order(self, *args, **kwargs):
        """Submit option order(s) through the inherited path, then drop the order cache.

        Option entries and closes persist new option TradingOrder rows here; invalidate so the
        fill engine's next read sees them (mirrors the equity ``submit_order`` override).
        """
        result = super().submit_option_order(*args, **kwargs)
        self.invalidate_order_cache()
        return result

    def refresh_positions(self) -> bool:
        """No-op: the ledger is local and always current. Returns True."""
        return True

    def refresh_orders(self) -> bool:
        """Per-bar fill engine (THE core of the simulator).

        Called by the engine once per simulated bar (after ``set_clock``). Steps:

          1. ACTIVATE dependent WAITING_TRIGGER legs whose parent reached its trigger
             status — they become ACCEPTED (live) so they can fill on later bars.
          2. EVALUATE every working order against the chosen bar and FILL it if triggered:
             MARKET -> next-bar open (±slippage); LIMIT -> only if the bar crosses the
             limit; STOP -> only if the bar crosses the stop (then fills at stop ±slippage).
          3. CANCEL the OCO sibling when one OCO/TP/SL leg fills (first-leg-wins close).

        Activation runs first so a leg whose parent filled on THIS same bar (a same-bar
        MARKET entry) can be evaluated against the next bar on the following call — never
        on the entry bar (no look-ahead within a bar).

        Step 0 is the OPTION DAY-ORDER sweep (``_expire_stale_option_limits``), which runs
        BEFORE the fill loop so an aged-out limit cannot trade on the bar it dies.

        Returns whether ANY order filled OR was terminalised this bar. The engine uses this to
        skip the transaction roll + bracket attach on no-change bars (both are no-ops there),
        which is the common case on a fine fill clock (5-minute) and a large share of per-bar
        runtime. A DAY-order expiry counts as a change: it terminalises an entry order with no
        fill, and the roll is what releases the parent WAITING Transaction.
        """
        as_of = self._price.now()
        # Step 0: the DAY-order sweep. Its return is the "book changed without a fill" half of
        # the signal below: an expiry terminalises an entry order, which is exactly the state
        # refresh_transactions needs to roll in order to release the parent WAITING Transaction
        # (the engine's dup gate reads it). OPT-B4 shipped the sweep but the engine still gated
        # the roll on fills only, so an expired entry left its WAITING transaction behind and
        # locked the symbol out of the rest of the run (F1, option-grid probe 2026-08-27).
        book_changed = self._expire_stale_option_limits(as_of)

        active = OrderStatus.get_active_statuses()
        # Working orders: entries (MARKET/LIMIT/STOP), plain exit sells, and option legs. TP/SL
        # brackets are NO LONGER order rows — they are checked directly in _apply_bracket_exits
        # below (lean simulator). SQL-filtered to active statuses so terminal orders aren't materialised.
        working = list(self._orders_filtered(statuses=active))
        # SL-before-TP: when a single bar's range spans BOTH the take-profit (limit) and the
        # stop-loss (stop) leg of an OCO pair, the intrabar order is ambiguous — fill the STOP
        # FIRST so the conservative worst-case (stop-loss) wins and cancels the TP sibling. A
        # stable sort puts every stop-bearing leg ahead of the pure-limit (TP) legs.
        working.sort(key=lambda o: 0 if getattr(o, "stop_price", None) else 1)
        filled = False  # whether ANY order filled this bar; the engine gates the transaction
        #                 roll + bracket attach on this (both are no-ops with no fill).
        for o in working:
            if self._is_single_leg_option(o):
                # OPTION single-leg (or option child carrying a contract): fill off the
                # cached premium bar, NOT the equity branch (whose bar is the underlying's).
                fill_px = self._option_fill_price(o, as_of)
                if fill_px is None:
                    continue
                # CASH-SECURED guard for a lone LONG (debit) option entry: cap the contract count
                # to what current cash affords at the ACTUAL fill premium so a debit buy can never
                # drive cash below zero (the debit analog of the margin-call liquidation). A
                # sell-to-open (credit) leg receives cash and is left to the margin path.
                if not self._cap_single_leg_option_entry(o, fill_px):
                    continue  # unaffordable at >=1 contract -> entry did not open
                self._apply_option_fill(o, fill_px, as_of)
                filled = True
                continue
            if getattr(o, "asset_class", None) == AssetClass.OPTION:
                # Option PARENT with no contract_symbol -> multi-leg (spread/straddle):
                # fill ALL legs all-or-none off their own premium bars on this bar.
                self._fill_multi_leg_parent(o, as_of)
                if o.status == OrderStatus.FILLED:  # all-or-none parent filled this bar
                    filled = True
                continue
            # Cheap PLAIN-FLOAT pre-check: most bars cross NO threshold, so skip the heavier
            # ORM ``_evaluate_fill`` unless THIS bar's range could actually trigger the order.
            # Uses the SAME fill bar ``_evaluate_fill`` would (``_bar_for_fill``); a None bar
            # means no fill (identical to ``_evaluate_fill`` returning None). The gate mirrors
            # ``_evaluate_fill``'s comparisons exactly, so it lets through precisely the orders
            # the full path would fill — the real fill decision stays in ``_evaluate_fill``.
            bar = self._bar_for_fill(o, as_of)
            if bar is None:
                continue
            trig_hi, trig_lo = self._trigger_thresholds(o)
            if not (bar["high"] >= trig_hi or bar["low"] <= trig_lo):
                continue
            fill_px = self._evaluate_fill(o, as_of)
            if fill_px is None:
                continue
            self._apply_fill(o, fill_px, as_of)
            filled = True

        # LEAN bracket exits: check each OPEN transaction's take_profit/stop_loss directly against
        # this bar and synthesize the closing order when crossed (replaces WAITING_TRIGGER/OCO legs).
        if self._apply_bracket_exits(as_of):
            filled = True
        # An option DAY-order expiry changed the book WITHOUT a fill — the engine must still roll
        # transactions (see the Step 0 note above), so report it as a book change too.
        return filled or book_changed

    def _expire_stale_option_limits(self, as_of) -> bool:
        """TIME-IN-FORCE DAY for option LIMIT orders (OPT-B4).

        Live forces ``TimeInForce.DAY`` on every option order (``AlpacaAccount``), and all 17
        option entry builders submit ``order_type="limit"``. The simulator had no TIF and no
        age handling at all, so a limit the premium never crossed stayed working for the whole
        life of the contract: it kept its ``option_reserve`` charged against buying power, it
        held its parent Transaction WAITING — which locks the symbol out of the rest of the run
        via the engine's dup gate — and it could still fill weeks later at a price the strategy
        quoted on a different bar. That let the GA quote aggressively and never pay for the
        misses, which changes WHICH TRADES EXIST.

        An option limit therefore gets exactly the session it was placed in. ``refresh_orders``
        runs after the bar's analysis pass, so the order placed on bar N is attempted within
        that same call; the first pass on a LATER calendar date terminalises it as EXPIRED. The
        sweep runs BEFORE the fill loop so an aged-out order cannot trade on the bar it dies.
        Multiple passes on one intraday date leave it alone — the boundary is the DATE, not the
        call.

        SCOPE, deliberately narrow:
          * MARKET option orders are NOT aged out. One that did not fill here did not meet a
            market refusal, it met a MISSING PREMIUM BAR; terminalising it would turn a data
            gap into a cancelled trade — a different, invented fact.
          * EQUITY orders are untouched. This is the option TIF, not a global one.
          * An option order whose submission bar is UNKNOWN (no ``_option_order_day`` entry —
            no current path produces one, since every option order is staged through
            ``_submit_option_order_impl``) is left working. An unknown age must not be read as
            an old one.

        A partially filled row keeps its ``filled_qty``: the contracts that traded are real, and
        ``reserved_option_buying_power_detail`` pro-rates a terminal row to exactly that part.

        Returns True when ANY order was terminalised — the caller (``refresh_orders``) folds
        that into its "the book changed" signal, because an EXPIRED entry order is exactly the
        state ``refresh_transactions``' WAITING->CLOSED arm reads to release the parent
        Transaction. Without the roll the dup gate keeps the symbol locked for the whole run.
        """
        if not self._option_order_day:
            return False
        today = as_of.date() if hasattr(as_of, "date") else as_of
        day_limits = (OrderType.BUY_LIMIT, OrderType.SELL_LIMIT)
        expired_any = False
        for o in self._orders_filtered(statuses=OrderStatus.get_active_statuses()):
            if getattr(o, "asset_class", None) != AssetClass.OPTION:
                continue
            if o.order_type not in day_limits:
                continue
            placed = self._option_order_day.get(o.id)
            if placed is None or placed >= today:
                continue
            o.status = OrderStatus.EXPIRED
            o.comment = f"{(o.comment or '')} | day order expired {placed}".strip(" |")
            update_instance(o)
            self._option_order_day.pop(o.id, None)
            expired_any = True
            logger.warning(
                "[backtest] option DAY order expired unfilled: %s %s limit %s placed %s, "
                "now %s (live forces TimeInForce.DAY).",
                getattr(o, "side", None),
                getattr(o, "contract_symbol", None) or getattr(o, "option_strategy", None),
                getattr(o, "limit_price", None), placed, today,
            )
        if expired_any:
            self.invalidate_order_cache()
            self._option_memo_gen += 1
        return expired_any

    def _is_single_leg_option(self, order) -> bool:
        """True for an OPTION order that fills *independently* against a premium bar.

        That is a single-leg parent carrying a ``contract_symbol`` and NO ``parent_order_id``.
        Excluded:
          * a multi-leg PARENT (``asset_class == OPTION`` but NO ``contract_symbol``); its
            legs fill all-or-none via ``_fill_multi_leg_parent``.
          * a multi-leg CHILD leg (carries ``contract_symbol`` AND ``parent_order_id``); a
            child must fill ONLY through its parent's all-or-none path — never on its own —
            so it is excluded here to avoid double-filling.
        """
        return (
            getattr(order, "asset_class", None) == AssetClass.OPTION
            and bool(getattr(order, "contract_symbol", None))
            and getattr(order, "parent_order_id", None) is None
        )

    def _option_fill_price(self, order, as_of) -> Optional[float]:
        """Premium per share for an option order on its fill bar, per ``fill_model``.

        The fill BAR is chosen exactly like the equity branch (``_bar_for_fill``): the
        underlying's trading calendar picks the day — ``same_bar_close`` uses the current
        bar's date; ``next_bar_open`` (default) uses the next trading day strictly after the
        current bar. The premium is then read for that day from the as-of options cache.
        Returns None when no provider, no fill day, no premium bar, no usable price, or a
        LIMIT whose price the bar never crosses (the order stays pending, exactly like the
        equity branch's non-crossing limit).

        LIMIT handling mirrors the equity path: the premium bar yields ONE reference price
        (open/close per ``fill_model``), which is a MID — so the order first CROSSES the
        modeled bid-ask spread (``_option_cross``: a buy lifts the ask ``px + half``, a sell
        hits the bid ``px - half``) and the limit is re-tested against THAT price — a
        BUY_LIMIT fills only when ``px + half <= limit``, a SELL_LIMIT only when
        ``px - half >= limit``. An order that no longer clears once the spread is crossed
        does NOT fill; it stays pending and retries the next bar. The fill is at the crossed
        price, which is by construction no worse than the limit (never worse, and no
        execution ``slippage_bps`` on a limit fill — see ``_option_cross``). A LIMIT-typed
        leg carrying NO ``limit_price`` (a multi-leg child — the PARENT holds the combo's
        net limit) falls through to the market-style fill.

        NO-ARBITRAGE guard: a resolved premium is validated against the underlying's own bar
        on the fill day (``_arb_fill_reject_reason``); a junk indicative print is rejected
        like a non-crossing limit — the order stays pending and retries the next bar (a
        multi-leg parent fills NOTHING this bar, per its all-or-none semantics).

        LIQUIDITY guard: the fill bar must also be able to ABSORB the order's size —
        required contracts (a multi-leg child leg already carries parent structures x leg
        ratio_qty, so ``order.quantity`` is the required contracts for both shapes) may not
        exceed ``_OPTION_FILL_MAX_VOLUME_PARTICIPATION`` of the bar's traded volume
        (``_volume_cap_reject_reason``). Rejected the same no-fill idiom as the arb guard:
        the order stays pending and retries the next bar, a multi-leg parent fills NOTHING
        this bar, and expiry settlement remains the backstop for exits.
        """
        if self._options is None:
            return None
        same_bar = self._cfg["fill_model"] == "same_bar_close"
        # The trading calendar is the UNDERLYING's, not the contract's: a multi-leg CHILD's
        # ``symbol`` is its OCC contract (which has no underlying bars), so use the underlying.
        calendar_symbol = getattr(order, "underlying_symbol", None) or order.symbol
        if same_bar:
            fill_day = as_of.date() if hasattr(as_of, "date") else as_of
        else:
            fill_day = self._price.next_bar_date(calendar_symbol, as_of)
            if fill_day is None:
                return None
            if hasattr(fill_day, "date"):
                fill_day = fill_day.date()
        bar = self._options.get_bar(order.contract_symbol, fill_day)
        if not bar:
            return None
        px = bar.get("close") if same_bar else bar.get("open")
        if px is None:
            return None
        px = float(px)
        limit = getattr(order, "limit_price", None)
        ot = order.order_type
        if limit is not None and ot == OrderType.BUY_LIMIT:
            # CROSS FIRST, THEN RE-TEST. ``px`` is the bar's single reference premium (a
            # mid); a buy actually lifts the ASK. Testing the limit against the raw ``px``
            # let every single-leg option order — and they ALL carry a limit_price, unlike
            # multi-leg children — fill without paying the spread on either end.
            fill_px = self._option_cross(px, True, bar)
            if fill_px > float(limit):
                return None
        elif limit is not None and ot == OrderType.SELL_LIMIT:
            fill_px = self._option_cross(px, False, bar)   # a sell hits the BID
            if fill_px < float(limit):
                return None
        else:
            # Option-specific cost model (percent of premium), NOT the equity _slip. Multi-leg
            # combo CHILDREN are LIMIT-typed but carry no limit_price (the parent holds the net
            # limit), so they fall through to here — meaning iron condors / strangles / spreads
            # DO get charged the spread on every leg, which is the point.
            fill_px = self._option_slip(px, order.side == OrderDirection.BUY, bar)
        reason = self._arb_fill_reject_reason(order, fill_px, fill_day, same_bar, bar)
        if reason is not None:
            self.rejected_arb_fills += 1
            logger.warning(
                "[backtest] arb-inconsistent option fill REJECTED: %s %s @ premium %.4f — "
                "%s. The order stays pending and retries the next bar (rejections so far: "
                "%d).",
                getattr(order, "side", None), order.contract_symbol, fill_px, reason,
                self.rejected_arb_fills,
            )
            return None
        reason = self._volume_cap_reject_reason(order, bar)
        if reason is not None:
            self.rejected_illiquid_fills += 1
            logger.warning(
                "[backtest] illiquid option fill REJECTED: %s %s qty %s — %s. The order "
                "stays pending and retries the next bar (rejections so far: %d).",
                getattr(order, "side", None), order.contract_symbol,
                getattr(order, "quantity", None), reason, self.rejected_illiquid_fills,
            )
            return None
        return fill_px

    def _option_half_spread(self, premium: float, bar: dict) -> float:
        """Half the modeled bid-ask spread, in premium dollars per share (>= 0).

        See the _OPTION_SPREAD_* block for why options need a percent-of-premium model rather
        than the equity ``spread_bps``. Returns 0.0 when ``option_spread_pct`` is unset/0 and
        ``option_spread_min_tick`` is 0 — an exact no-op reproducing pre-2026-07-25 fills.
        """
        pct = float(self._cfg.get("option_spread_pct", 0.0) or 0.0)
        min_tick = float(self._cfg.get("option_spread_min_tick", 0.0) or 0.0)
        if pct <= 0 and min_tick <= 0:
            return 0.0
        full = max(min_tick, abs(premium) * pct / 100.0)
        volume = bar.get("volume")
        # Unknown volume is treated as THIN, not liquid: the participation cap already treats a
        # missing volume as 0, so assuming a tight quote here would contradict it.
        if volume is None or float(volume) < _OPTION_SPREAD_LIQUID_VOLUME:
            full *= _OPTION_SPREAD_THIN_MULT
        return full / 2.0

    def _option_cross(self, px: float, side_is_buy: bool, bar: dict) -> float:
        """Premium after CROSSING the modeled bid-ask spread — the LIMIT-fill cost.

        A buy lifts the ask (``px + half``), a sell hits the bid (``px - half``). This is
        ``_option_slip`` without ``slippage_bps``, and the split is deliberate and matches
        the equity path: generic execution slippage is a MARKET/STOP cost (``_slip``), while
        a LIMIT's realistic cost is having to cross the quote to trade at all (equity
        expresses the same thing by widening the limit's trigger threshold,
        ``_limit_trigger_price``). Options cross the price instead of widening a threshold
        because an option "bar" contributes ONE reference premium, not a [low, high] range
        the threshold could be tested against.

        Floored at zero on the sell side for the same reason as ``_option_slip``: a modeled
        spread wider than the premium must not pay the account to sell.
        """
        half = self._option_half_spread(px, bar)
        return px + half if side_is_buy else max(0.0, px - half)

    def option_modelled_half_spread(self, contract_symbol: str) -> Optional[float]:
        """Half the modeled spread for ``contract_symbol`` on the CURRENT (as-of) bar, or None.

        THE ENTRY-QUOTE SEAM (F3). ``_OptionEntryAction`` duck-types this off the account to
        size its ``entry_cross`` concession (see ``ba2_common.core.option_entry_quote``): the
        entry may quote away from the mid by a fraction of the SAME spread the fill will
        charge, instead of quoting the analysis mid and then being asked to earn the whole
        spread back overnight. Public, unlike its ``_option_half_spread`` delegate, precisely
        because a caller outside this class is meant to read it — and it is the ONLY such
        caller, which is why no live account has it (a live account has real quotes; its
        builders already quote at the real touch, so live concedes nothing extra).

        AS-OF, NOT FILL-DAY, and that is the hermetic point: this answers with the bar the
        action can already see (the one it selected the contract from), never the bar the
        order will fill on. The fill-day spread may differ (a different day's volume can flip
        the thin-widening); a quote set from the fill day's spread would be look-ahead.

        None when there is no options provider, no as-of bar for the contract, or no close on
        it — the caller then leaves the quote exactly as the builder priced it.
        """
        if self._options is None:
            return None
        bar = self._options.get_bar(contract_symbol, self._as_of_date())
        if not bar:
            return None
        px = bar.get("close")
        if px is None:
            return None
        return self._option_half_spread(float(px), bar)

    def _option_slip(self, px: float, side_is_buy: bool, bar: dict) -> float:
        """Option fill price after execution slippage + the modeled half bid-ask spread.

        Deliberately NOT ``_slip``: that adds the equity ``spread_bps`` (bps of price), whose
        shape is wrong for a premium and which would double-count against the option model
        here. ``slippage_bps`` (generic execution slippage) still applies to both asset
        classes. Never returns a negative premium — a spread wider than the premium itself
        would otherwise flip the sign on a deep-OTM contract, and the floor keeps a sell
        credit at worst zero rather than paying the account to sell.
        """
        bps = float(self._cfg["slippage_bps"]) / 10_000.0
        half = self._option_half_spread(px, bar)
        return px * (1.0 + bps) + half if side_is_buy else max(0.0, px * (1.0 - bps) - half)

    def _volume_cap_reject_reason(self, order, bar: dict) -> Optional[str]:
        """Why a candidate option fill exceeds the fill bar's liquidity, else None.

        A fill is only plausible if the bar's traded volume could have absorbed the
        order: the required contracts must be at most
        ``_OPTION_FILL_MAX_VOLUME_PARTICIPATION`` of the bar's volume. The required
        contracts are simply ``abs(order.quantity)`` — for a single-leg order that IS
        the order quantity, and a multi-leg CHILD leg is created with quantity =
        parent structures x leg ratio_qty (OptionsAccountInterface.submit_option_order),
        so the same read covers both. ``_fill_multi_leg_parent`` prices every leg
        through here, so one illiquid leg already blocks the whole all-or-none combo
        (no partial fills). A missing/zero bar volume is treated as 0 — nothing fills
        that bar (conservative; the order retries the next bar and expiry settlement
        remains the backstop for exits, so a position cannot be stranded forever).
        Applies to entries AND non-expiry exits alike.
        """
        required = abs(float(order.quantity or 0.0))
        if required <= 0:
            return None
        volume = bar.get("volume")
        volume = float(volume) if volume is not None else 0.0
        capacity = _OPTION_FILL_MAX_VOLUME_PARTICIPATION * volume
        if required > capacity:
            return (
                f"order requires {required:g} contracts but bar volume {volume:g} allows "
                f"at most {capacity:g} "
                f"({_OPTION_FILL_MAX_VOLUME_PARTICIPATION:.0%} participation cap)"
            )
        return None

    def _arb_fill_reject_reason(
        self, order, fill_px: float, fill_day, same_bar: bool, bar: dict
    ) -> Optional[str]:
        """Why a candidate option fill premium is untradable junk, else None (fill allowed).

        The sparse options cache can carry junk indicative prints (e.g. a call at $0.01
        while spot is $159.85 against a $105 strike — $54.85 of intrinsic); filling there
        and later settling against the REAL underlying fabricates P&L. The account prices
        the underlying itself (``self._price``), so a fill whose premium violates a
        no-arbitrage bound by more than ``_ARB_FILL_TOLERANCE`` per share is rejected:

          * ENTRY (opening intent, or intent unset): premium < intrinsic - tol, where
            intrinsic = max(0, spot-strike) for a call / max(0, strike-spot) for a put.
            Nobody sells an option below its immediate-exercise value; a lower print is
            stale/indicative junk. (Applies to sell_to_open too — a junk-cheap CREDIT is
            the same bad data and settles at the real intrinsic later.)
          * EXIT (closing intent): premium > spot + tol for a CALL (a call can never cost
            more than the stock) or premium > strike + tol for a PUT (a put can never be
            worth more than the strike) — impossible premiums. A below-intrinsic CLOSE is
            NOT rejected (a wide bid in a fast market is plausible), and expiry settlement
            still guarantees the eventual exit via intrinsic, so a position cannot get
            stuck forever.

        The contract's strike/type come from the order, falling back to the premium bar's
        own columns (minimal legs may not carry them). The spot reference is the
        underlying's bar on the FILL day at the same price point as the fill model (open
        for ``next_bar_open`` — the premium IS that bar's open — close for
        ``same_bar_close``). Fails OPEN (debug log, no rejection) when the contract terms
        are missing or the underlying has no bar on the fill day — a hermetic run may
        legitimately lack one.
        """
        strike = getattr(order, "strike", None)
        if strike is None:
            strike = bar.get("strike")  # the cache bar carries the contract terms too
        opt_type = getattr(order, "option_type", None) or bar.get("option_type")
        if strike is None or opt_type is None:
            logger.debug(
                "[backtest] arb check skipped for %s: no strike/option_type on the order "
                "or its premium bar.",
                getattr(order, "contract_symbol", None),
            )
            return None
        is_call = opt_type == OptionRight.CALL
        strike = float(strike)
        intent = (getattr(order, "position_intent", None) or "").lower()
        is_close = "close" in intent
        # The put upper bound needs no spot — check it before touching the price source.
        # (``_no_arb_premium_bounds`` with spot=None is exactly the spot-free bound; the
        # bounds themselves live THERE, shared with the mark/settlement clamps.)
        _, upper_spotless = self._no_arb_premium_bounds(strike, is_call, None)
        if is_close and upper_spotless is not None and fill_px > upper_spotless + _ARB_FILL_TOLERANCE:
            return (
                f"put premium {fill_px:.4f} exceeds strike {strike:.2f} + tolerance "
                f"{_ARB_FILL_TOLERANCE} (a put can never be worth more than its strike)"
            )

        underlying = getattr(order, "underlying_symbol", None) or order.symbol
        spot_bar = self._price.bar_at(underlying, fill_day)
        spot = None
        if spot_bar is not None:
            spot = float(spot_bar["close"] if same_bar else spot_bar["open"])
        if spot is None:
            logger.debug(
                "[backtest] arb check skipped for %s: no %s bar on %s (fail-open).",
                order.contract_symbol, underlying, fill_day,
            )
            return None
        intrinsic, upper = self._no_arb_premium_bounds(strike, is_call, spot)
        if is_close:
            # call close: a premium above the stock price itself is impossible. (The put
            # upper bound was already checked spot-free above.)
            if is_call and fill_px > upper + _ARB_FILL_TOLERANCE:
                return (
                    f"call premium {fill_px:.4f} exceeds spot {spot:.2f} + tolerance "
                    f"{_ARB_FILL_TOLERANCE} (a call can never cost more than the stock)"
                )
            return None
        if fill_px < intrinsic - _ARB_FILL_TOLERANCE:
            return (
                f"premium {fill_px:.4f} is below intrinsic {intrinsic:.4f} - tolerance "
                f"{_ARB_FILL_TOLERANCE} (spot {spot:.2f}, strike {strike:.2f})"
            )
        return None

    def _child_legs(self, parent) -> List[TradingOrder]:
        """The not-yet-filled child leg orders of a multi-leg option parent.

        Children are linked via ``parent_order_id`` (NOT ``depends_on_order`` — that FK is
        for OCO/TP/SL legs). Only non-terminal, non-FILLED legs are returned so a re-run on a
        later bar does not re-fill an already-filled leg.
        """
        if parent.id is None:
            return []
        terminal = OrderStatus.get_terminal_statuses()
        return [
            o
            for o in self.get_orders()
            if o.parent_order_id == parent.id
            and o.status not in terminal
            and o.status != OrderStatus.FILLED
        ]

    def _cap_single_leg_option_entry(self, order, fill_px: float) -> bool:
        """Cash-secured cap for a lone LONG (debit) single-leg option ENTRY.

        Caps ``order.quantity`` to ``floor(cash / (fill_px * multiplier + commission))`` so a
        debit buy can never drive cash below zero. Returns False (and CANCELs the order) when not
        even one contract is affordable, so the caller skips the fill.

        Only a BUY that OPENS (``position_intent`` starts with "buy_to_open", or is unset) is
        capped — a SELL-to-open (credit) leg receives cash, and a BUY/SELL-to-CLOSE is a
        legitimate close that must not be blocked. Returns True (no cap) for those.
        """
        if order.side != OrderDirection.BUY:
            return True
        intent = (getattr(order, "position_intent", None) or "").lower()
        if intent and "open" not in intent:
            return True  # a close (buy_to_close) — never block a close
        qty = float(order.quantity) if order.quantity is not None else 0.0
        if qty <= 0:
            return True
        multiplier = float(order.multiplier or 100)
        commission = float(self._cfg["commission_per_trade"])
        per_contract = fill_px * multiplier + commission
        if per_contract <= 0:
            return True
        cost = qty * fill_px * multiplier + commission
        if cost <= self._cash + 1e-6:
            return True  # affordable at full size -> no cap
        affordable = int(self._cash // per_contract)
        if affordable < 1:
            logger.error(
                "BACKTEST option cash-secured: LONG %s %g @ %.4f (cost $%.2f) exceeds cash "
                "$%.2f -> entry NOT opened.",
                order.contract_symbol, qty, fill_px, cost, self._cash,
            )
            order.status = OrderStatus.CANCELED
            order.quantity = 0
            update_instance(order)
            self._option_memo_gen += 1  # in-place quantity mutation: refresh the F6 memos
            self._cancel_oco_sibling(order)
            return False
        logger.error(
            "BACKTEST option cash-secured: LONG %s sized %g @ %.4f exceeds cash $%.2f -> "
            "capping to %d contract(s).",
            order.contract_symbol, qty, fill_px, self._cash, affordable,
        )
        order.quantity = float(affordable)
        self._option_memo_gen += 1  # in-place quantity mutation: refresh the F6 memos
        # Keep the shared Transaction row in sync (it was created at the pre-cap contract
        # count and otherwise over-reports the position size forever).
        self._sync_transaction_quantity(order.transaction_id, float(affordable))
        return True

    def _sync_transaction_quantity(self, transaction_id: Optional[int], quantity: float) -> None:
        """Write a fill-time quantity cap back to the shared Transaction row.

        ``_cap_single_leg_option_entry`` / the debit-combo rescale mutate order quantities at
        FILL time, after the Transaction was created at the ANALYSIS-time size; without this
        the transactions table over-reports the position (structures for a combo parent,
        contracts for a single leg). No-op when the order carries no transaction.
        """
        if transaction_id is None:
            return
        txn = get_instance(Transaction, transaction_id)
        if txn is None:
            return
        txn.quantity = float(quantity)
        update_instance(txn)

    def _fill_multi_leg_parent(self, parent, as_of: datetime) -> None:
        """ALL-OR-NONE fill of a multi-leg option parent (spread/straddle/...).

        On this bar, price every child leg off its OWN premium bar (each leg carries a
        ``contract_symbol`` so ``_option_fill_price`` works). If EVERY leg resolves to a
        price, fill all legs through the SAME per-leg path as single-leg fills
        (``_apply_option_fill`` -> per-contract lot + cash, scaled x multiplier), then mark
        the PARENT FILLED with ``open_price`` = net per-share debit PER STRUCTURE =
        Σ(sign x premium x leg ratio) (positive = debit, negative = credit) — the ratio
        (leg qty / structure qty) matters for ratio'd shapes (1-2-1 butterfly, 1-2 put
        ratio); all-ratio-1 shapes (verticals/condors) are unchanged. The parent moves NO
        cash (it already moved per leg). If ANY leg lacks a price, NOTHING fills this bar
        (retry next).

        NET LIMIT (OPT-S7). The combo's limit price lives on the PARENT — the children are
        built with no ``limit_price`` of their own, so ``_option_fill_price``'s two limit
        branches cannot fire for them and every leg prices market-style. Nothing then
        compared the achieved net against ``parent.limit_price``, so the simulator filled
        combos THROUGH their net limit, which live Alpaca cannot do. The invented fills are
        by construction the ones WORSE than the limit, so mean credit was understated while
        the trade COUNT was inflated — it changed which trades exist. Enforced here in the
        one sign convention the whole option stack uses (+debit / -credit): the achieved net
        must be no worse than the limit, i.e. ``net <= limit`` on BOTH sides — a 3.00 debit
        fails a 2.00 debit limit, and a 1.50 credit (net -1.50) fails a 2.00 credit limit
        (-2.00). A rejected combo does not fill this bar and retries the next, the same
        no-fill idiom the arb and liquidity guards use.
        """
        legs = self._child_legs(parent)
        if not legs:
            return
        priced = []
        for leg in legs:
            px = self._option_fill_price(leg, as_of)
            if px is None:
                return  # all-or-none: one leg can't price -> fill none this bar
            priced.append((leg, px))

        # An ABSENT structure count and a count of ZERO both stop the fill, but they are
        # not the same event and must not read the same in the log. Zero structures is a
        # measured "there is nothing to fill". A missing quantity is a data defect: the
        # order will be re-examined every bar and can never fill, silently, forever.
        if parent.quantity is None:
            logger.error(
                "[backtest] multi-leg %s NOT filled: the parent order carries no structure "
                "count, so its per-structure net cannot be computed. This is a DEFECT in the "
                "order, not a market condition -- it will retry every bar and never fill.",
                getattr(parent, "option_strategy", None))
            return
        structures = abs(float(parent.quantity))
        if structures <= 0:
            return

        # --- NET LIMIT (OPT-S7) ------------------------------------------------------
        # Per-share net PER STRUCTURE, in the parent's own +debit / -credit convention.
        net_per_share = 0.0
        for leg, px in priced:
            ratio = abs(float(leg.quantity or 0.0)) / structures
            net_per_share += (px if leg.side == OrderDirection.BUY else -px) * ratio
        limit = getattr(parent, "limit_price", None)
        if limit is not None and net_per_share > float(limit) + _NET_LIMIT_TOLERANCE:
            logger.warning(
                "[backtest] multi-leg %s NOT filled: achieved net %+.4f/share per structure "
                "is worse than the order's net limit %+.4f (+debit/-credit). The order stays "
                "pending and retries the next bar.",
                getattr(parent, "option_strategy", None), net_per_share, float(limit),
            )
            return

        # CASH-SECURED guard for DEBIT combos (defense-in-depth, the debit analog of the
        # margin-call liquidation for credit shorts). Options are sized from ANALYSIS-time quotes
        # but fill at the sparse cache's next-bar premiums, which can diverge sharply upward, so a
        # debit combo could otherwise buy far more debit than the account holds and drive cash
        # persistently negative. Compute the ACTUAL per-structure net debit from the FILL premiums
        # and cap the number of STRUCTURES that fill to what current cash can afford. All legs
        # scale together by the capped count (respecting each leg's ratio) so the combo stays
        # balanced/defined-risk. A CREDIT combo (net premium <= 0 -> cash inflow) is left alone
        # (its risk is bounded by the margin path, not cash spend).
        #
        # A CLOSE IS EXEMPT (OPT-B3), exactly as the single-leg sibling
        # ``_cap_single_leg_option_entry`` has always exempted one. This guard exists to stop
        # an ENTRY buying more debit than the account holds; closing a credit structure is
        # also a net debit, and refusing it leaves the short leg — and its assignment risk —
        # open because the account ran out of money, which no broker does. The rescale branch
        # was worse than the outright refusal: it wrote the number of structures CLOSED over
        # ``Transaction.quantity`` (the divisor for ``spread_pnl_percent``), and a 2-of-3 cap
        # then let the next close attempt read the ENTRY parent's filled_qty=3 and over-close,
        # flipping the position by 2.
        commission = float(self._cfg["commission_per_trade"])
        if self._multi_leg_is_closing(parent, [leg for leg, _ in priced]):
            self._apply_multi_leg_fill(parent, priced, as_of)
            return
        debit_per_structure = 0.0
        for leg, px in priced:
            ratio = abs(float(leg.quantity or 0.0)) / structures if structures else 0.0
            mult = float(leg.multiplier or 100)
            signed = px if leg.side == OrderDirection.BUY else -px
            debit_per_structure += signed * ratio * mult
        # + per-leg commission for one structure's worth of legs (flat charge per leg fill).
        per_structure_cost = debit_per_structure + commission * len(priced)
        capped = structures
        if per_structure_cost > 0:
            affordable = int((self._cash) // per_structure_cost)
            if affordable < structures:
                if affordable < 1:
                    # Not even one structure affordable -> the combo does NOT open this bar. Cancel
                    # the parent + legs so it isn't retried forever (mirrors the equity guard).
                    logger.error(
                        "BACKTEST option cash-secured: DEBIT combo %s per-structure cost $%.2f "
                        "exceeds cash $%.2f -> entry NOT opened.",
                        getattr(parent, "option_strategy", None), per_structure_cost, self._cash,
                    )
                    for leg, _ in priced:
                        leg.status = OrderStatus.CANCELED
                        leg.quantity = 0
                        update_instance(leg)
                    parent.status = OrderStatus.CANCELED
                    parent.quantity = 0
                    update_instance(parent)
                    self._option_memo_gen += 1  # in-place quantity mutation: refresh F6 memos
                    return
                logger.error(
                    "BACKTEST option cash-secured: DEBIT combo %s sized %g structures @ $%.2f "
                    "each exceeds cash $%.2f -> capping to %d.",
                    getattr(parent, "option_strategy", None), structures, per_structure_cost,
                    self._cash, affordable,
                )
                capped = float(affordable)

        # Rescale each leg's quantity to the capped structure count (ratio preserved).
        if capped != structures:
            for leg, _ in priced:
                ratio = abs(float(leg.quantity or 0.0)) / structures
                leg.quantity = ratio * capped
            parent.quantity = capped
            self._option_memo_gen += 1  # in-place quantity mutation: refresh F6 memos
            # Keep the shared Transaction row (created at the pre-cap STRUCTURE count) in sync.
            self._sync_transaction_quantity(parent.transaction_id, float(capped))

        self._apply_multi_leg_fill(parent, priced, as_of)

    @staticmethod
    def _multi_leg_is_closing(parent, legs) -> bool:
        """True when this multi-leg parent CLOSES a structure rather than opening one.

        The parent's own ``position_intent`` is deliberately None for a multi-leg
        (``OptionsAccountInterface.submit_option_order``: four legs have four intents), so
        the answer lives on the CHILDREN — ``build_closing_legs`` stamps every reversed leg
        ``buy_to_close``/``sell_to_close``.

        Reads as a close only on positive evidence, and never guesses: a single leg naming
        an OPEN intent makes the whole order an open (the cash guard then applies, which is
        the safe direction), and when NO leg names an intent at all the parent's
        ``option_strategy == "close"`` is the remaining evidence. Unknown falls through to
        "not a close" — an unknown must not buy an exemption.
        """
        intents = [str(getattr(leg, "position_intent", None) or "").lower() for leg in legs]
        named = [i for i in intents if i]
        if named:
            if any("open" in i for i in named):
                return False
            return all("close" in i for i in named)
        return str(getattr(parent, "option_strategy", None) or "").lower() == "close"

    def _apply_multi_leg_fill(self, parent, priced, as_of: datetime) -> None:
        """Fill every leg of a multi-leg parent and mark the parent FILLED at the net.

        ``priced`` is the ``[(leg, premium)]`` list the caller resolved. Leg ratio = leg
        contracts / structures, read AFTER any cash-cap rescale (legs and ``parent.quantity``
        are rescaled together, so the ratio is preserved).
        """
        net = 0.0
        struct_qty = abs(float(parent.quantity or 0.0))
        for leg, px in priced:
            self._apply_option_fill(leg, px, as_of)  # reuse single-leg per-leg lot+cash math
            signed = px if leg.side == OrderDirection.BUY else -px
            ratio = abs(float(leg.quantity or 0.0)) / struct_qty if struct_qty else 0.0
            net += signed * ratio

        parent.filled_qty = parent.quantity
        parent.open_price = net  # net per-share PER STRUCTURE: +debit / -credit. No cash moved on the parent.
        parent.status = OrderStatus.FILLED
        update_instance(parent)
        if parent.id is not None:
            self._fill_dates[parent.id] = as_of

    def _apply_bracket_exits(self, as_of) -> bool:
        """Lean TP/SL bracket exits — the replacement for WAITING_TRIGGER/OCO leg orders.

        For each OPEN transaction of THIS account carrying a ``take_profit`` and/or ``stop_loss``,
        if the current bar crosses the level, synthesize the closing order at that level (STOP wins
        on a straddle — the conservative worst case) for the full held quantity and fill it through
        the SAME fill path (``_evaluate_fill``/``_apply_fill``), so exit price + slippage are
        byte-identical to the old leg-based bracket. The closing order carries ``depends_on_order`` +
        an ``OCO-`` comment marker so the inherited ``refresh_transactions`` recognises the TP/SL
        close (and ``get_round_trip_trades._exit_reason`` labels take_profit/stop_loss from the
        stop/limit price). No pre-staged leg rows are ever created — that is the ORM-churn saving.

        Runs AFTER the working-order loop, so a plain ruleset-close SELL that already filled this bar
        balances the position and the ``net == 0`` guard skips the bracket (no double close).
        """
        # Read the OPEN transactions' (id, tp, sl) into plain tuples immediately (safe for both the
        # store objects and the flag-off session-loaded rows — plain columns, no lazy load).
        opened = [
            (t.id, t.take_profit, t.stop_loss)
            for t in transactions_where(status=TransactionStatus.OPENED)
        ]

        filled_any = False
        _min_dt = datetime.min.replace(tzinfo=timezone.utc)
        for txn_id, tp, sl in opened:
            if not tp and not sl:
                continue
            # Use the account's cache-safe order accessor (as refresh_orders does) — never the
            # detached Session rows. The ENTRY is the order with depends_on_order IS NULL (oldest);
            # NET filled position = filled buys - sells. Zero net => already flat (e.g. a plain
            # ruleset-close SELL filled this bar in the working loop) -> skip to avoid a double close.
            entry = None
            net = 0.0
            for o in self._orders_filtered(transaction_id=txn_id):
                if o.account_id != self.id:
                    continue
                if o.depends_on_order is None:
                    if entry is None or ((o.created_at or _min_dt), (o.id or 0)) < (
                            (entry.created_at or _min_dt), (entry.id or 0)):
                        entry = o
                if o.status == OrderStatus.FILLED and o.filled_qty:
                    net += o.filled_qty if o.side == OrderDirection.BUY else -o.filled_qty
            if entry is None or abs(net) < 1e-9:
                continue
            is_long = net > 0
            held = abs(net)
            close_side = OrderDirection.SELL if is_long else OrderDirection.BUY
            # SL first (conservative on a straddle), then TP.
            for leg, price, otype in (
                ("SL", sl, OrderType.SELL_STOP if is_long else OrderType.BUY_STOP),
                ("TP", tp, OrderType.SELL_LIMIT if is_long else OrderType.BUY_LIMIT),
            ):
                if not price:
                    continue
                # Cheap, non-ORM crossing test first (see _FillProbe) -- a real TradingOrder is
                # only constructed below on the rare bar where a leg actually fills.
                stop_price = price if leg == "SL" else None
                limit_price = price if leg == "TP" else None
                probe = _FillProbe(
                    symbol=entry.symbol, side=close_side, order_type=otype,
                    stop_price=stop_price, limit_price=limit_price,
                )
                fill_px = self._evaluate_fill(probe, as_of)
                if fill_px is None:
                    continue  # not crossed this bar
                # PLEDGED-COVER lock, asked BEFORE the row is written (OPT-L1 exit half).
                # _apply_fill would clamp this too, but this loop is the path that actually
                # leaked in O_CC and it builds + PERSISTS a brand-new order on every bar the
                # level is crossed: leaving the refusal to fill time would litter the book
                # with one CANCELED row and one cache invalidation per bar for as long as the
                # pledge stands (40 consecutive bars, measured). `held` here is NET FILLED
                # from the order rows; the lock reads the ledger, which is the more accurate
                # view of what is actually sellable right now.
                sell_qty = held
                if close_side == OrderDirection.SELL:
                    sell_qty = self._pledged_share_lock(
                        entry.symbol, held,
                        context=f"bracket {leg} exit on transaction {txn_id}")
                    if sell_qty <= 0:
                        # Nothing may be sold. The TP/SL still lives on the TRANSACTION, so
                        # the exit is deferred rather than lost: it re-arms and goes through
                        # the moment the short call is bought back or expires.
                        continue
                ts = int(as_of.timestamp()) if hasattr(as_of, "timestamp") else 0
                order = TradingOrder(
                    account_id=self.id, symbol=entry.symbol, quantity=sell_qty, side=close_side,
                    order_type=otype, stop_price=stop_price, limit_price=limit_price,
                    transaction_id=txn_id,
                    depends_on_order=entry.id,
                    status=OrderStatus.ACCEPTED,
                    open_type=OrderOpenType.AUTOMATIC,
                    broker_order_id=self._next_broker_id(),
                    expert_recommendation_id=entry.expert_recommendation_id,
                    comment=f"{ts}-OCO-{leg}-[PARENT:{entry.id}/BROKER:{entry.broker_order_id}]",
                    created_at=as_of,
                )
                oid = add_instance(order)
                # add_instance leaves `order` detached/expired (expire_on_commit); re-fetch a loaded
                # instance so _apply_fill can read its attributes without a lazy-load.
                persisted = get_instance(TradingOrder, oid) or order
                self._apply_fill(persisted, fill_px, as_of)
                self.invalidate_order_cache()  # a close order was persisted -> reload next bar
                filled_any = True
                break  # SL-first: at most one bracket close per txn per bar
        return filled_any

    def refresh_transactions(self) -> bool:
        """Roll order state into transactions, then fix ``open_date``/``close_date`` to sim time.

        The inherited lifecycle stamps BOTH ``open_date`` (on WAITING->OPENED) and
        ``close_date`` (on close) with ``datetime.now(timezone.utc)`` (WALL clock). In a
        backtest the simulated clock is years off wall time, so a wall-clock timestamp
        corrupts any as-of date math:

          * a wall-clock ``open_date`` collapses ``days_opened`` to ~0 forever, so a
            ``days_opened > N`` exit rule (and the optimization plan's time-exit) NEVER fires;
          * a wall-clock ``close_date`` corrupts the days-since-last-close cooldown.

        After the inherited roll we re-stamp:
          * ``open_date`` of every transaction OPENED (or already closed) on THIS bar to its
            entry order's simulated fill bar (``_fill_dates[entry.id]``);
          * ``close_date`` of every transaction CLOSED on THIS bar to the current sim clock
            (the closing leg fills on the current bar; ``refresh_orders`` ran just before).
        """
        ok = super().refresh_transactions()
        sim_now = self._price.now()

        # ---- open_date: re-stamp to the entry's SIM fill bar (overwrite wall-clock). ----
        open_stamped = self._stamped_open_ids
        for txn in self._open_date_unstamped_transactions():
            if txn.id in open_stamped:
                continue
            entry = self._entry_order_for_transaction(txn)
            fill_date = self._fill_dates.get(entry.id) if (entry is not None and entry.id is not None) else None
            if fill_date is None:
                # Entry not filled yet (or no fill date recorded) — leave the inherited value
                # and retry next bar once the fill lands.
                continue
            open_stamped.add(txn.id)
            txn.open_date = fill_date
            update_instance(txn)

        # ---- close_date: re-stamp CLOSED transactions to the current sim bar. ----
        stamped = self._stamped_closed_ids
        for txn in self._closed_transactions():
            if txn.id in stamped:
                continue  # already re-stamped on an earlier bar.
            stamped.add(txn.id)
            # A transaction closes when its closing order fills on THE CURRENT bar (refresh_orders
            # ran just before this), so the simulated close_date is the current clock — no per-txn
            # order lookup needed.
            txn.close_date = sim_now
            update_instance(txn)
        return ok

    def _open_date_unstamped_transactions(self) -> List[Transaction]:
        """OPENED or CLOSED transactions whose open_date has not yet been sim-stamped.

        Includes CLOSED as well as OPENED so a transaction that opens AND closes between two
        of our passes still gets its open_date corrected (the close pass no longer touches it).
        Filters already-stamped ids in SQL so the scan stays cheap on long runs.
        """
        from ba2_common.core.types import TransactionStatus

        return transactions_where(
            statuses=[TransactionStatus.OPENED, TransactionStatus.CLOSED],
            exclude_ids=(self._stamped_open_ids or None))

    def _closed_transactions(self) -> List[Transaction]:
        """CLOSED transactions not yet re-stamped (single-account backtest DB).

        Filters out already-stamped ids in SQL so the scan returns only the few freshly-closed
        rows each bar instead of every accumulated closed transaction.
        """
        from ba2_common.core.types import TransactionStatus

        return transactions_where(
            status=TransactionStatus.CLOSED,
            exclude_ids=(self._stamped_closed_ids or None))

    def get_dividends(
        self,
        symbol: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Dict]:
        """v1: no dividend simulation. Returns []."""
        return []

    def get_filled_trades(
        self,
        symbol: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Dict]:
        """Filled-trade history derived from executed ``TradingOrder`` rows."""
        executed = OrderStatus.get_executed_statuses()
        trades: List[Dict] = []
        for o in self.get_orders():
            if o.status not in executed:
                continue
            qty = o.filled_qty if o.filled_qty else o.quantity
            if not qty:
                continue
            if symbol is not None and o.symbol != symbol:
                continue
            trades.append(self._order_to_trade(o, qty))
        return trades

    def get_round_trip_trades(self) -> List[Dict[str, Any]]:
        """Pair opening fills with their closing fills into round-trip trades with realised P&L.

        ``get_filled_trades`` returns one row per FILLED order (opens AND closers separately),
        which has no round-trip P&L — so trade-quality metrics (win_rate, profit_factor,
        expectancy, best/worst trade) are all zero. This method instead groups FILLED orders by
        their ``transaction_id`` and produces ONE row per transaction.

        Entries vs exits are classified by SIDE, not by ``depends_on_order``: the OPENING order
        is the EARLIEST-filled order in the transaction (you cannot close before you open) and
        its side is the ``opening_side``. Then:

          * ENTRIES = same-side fills (the open + any rebalance ADDs);
          * EXITS   = opposite-side fills — this covers BOTH plain market sells (FactorRanker
            rebalance/stop closers, ``depends_on_order IS NULL``) AND dependent TP/SL/OCO legs.
            Classifying by ``depends_on_order`` instead would mis-read a plain closing sell as
            an entry and drop the transaction into the ``open_at_end`` branch with garbage.
          * entry/exit price = quantity-weighted average ``open_price`` over each side; ``size``
            is the realised (exit) quantity; pnl = (exit_px - entry_px) * size * dir - commissions.
          * a transaction with NO exit fill is still OPEN at run end -> marked-to-market at the
            symbol's last available price (``exit_reason='open_at_end'``) so its unrealised P&L
            is counted (otherwise a run that ends mid-trade would understate performance).

        ``_exit_reason`` (called on the LATEST exit fill) returns ``"exit"`` for a plain market
        sell (no limit/stop), and ``take_profit``/``stop_loss`` for an OCO/TP/SL leg by the
        nearest price level. This is an APPROXIMATION for scaled add/reduce (one weighted-avg
        round-trip row per transaction) and EXACT for the dominant buy-once / sell-once case.

        Rows carry the field names ``results._trade_row`` maps (entry_time/exit_time/direction/
        entry_price/exit_price/size/pnl/pnl_pct/bars_held/exit_reason).
        """
        executed = OrderStatus.get_executed_statuses()
        commission = float(self._cfg["commission_per_trade"])

        def _fill_key(o):
            """Sort key for fill ordering.

            Order by simulated fill date; when a fill date is missing, fall back to ``o.id``
            (a monotonic insertion counter). The first tuple element separates rows that HAVE a
            fill date (0) from those that do not (1) so the two cases never compare a datetime
            against an id, while keeping ``id`` as the stable tiebreaker within each group.
            """
            fd = self._fill_dates.get(o.id) if o.id is not None else None
            oid = o.id or 0
            return (0, fd, oid) if fd is not None else (1, oid, oid)

        # Group FILLED orders (with a usable price) into round-trips. For a MULTI-LEG option
        # spread (strangle/straddle/spread) each leg is its own per-contract round-trip: the legs
        # share ONE transaction but trade DIFFERENT contracts, so grouping the whole transaction
        # together (and using the PARENT's net-credit ``symbol``/``open_price``) produced one
        # garbage row per spread — the reported entry~0 / pnl=-market*100*qty defect. The group key
        # is therefore ``(transaction_id, contract_symbol)`` for an option leg carrying a contract,
        # and ``(transaction_id, None)`` for single-leg options + equities. The multi-leg PARENT
        # (asset_class OPTION, NO contract_symbol, net-only) lands alone under ``(txn, None)`` and
        # is dropped below (it moves no cash; its legs carry the real P&L).
        by_group: Dict[tuple, List[Any]] = {}
        for o in self.get_orders():
            if o.transaction_id is None:
                continue
            if o.status not in executed or not (o.filled_qty or o.quantity):
                continue
            if not o.open_price and o.open_price != 0.0:
                continue
            is_option_leg = (
                getattr(o, "asset_class", None) == AssetClass.OPTION
                and bool(getattr(o, "contract_symbol", None))
            )
            key = (o.transaction_id, o.contract_symbol) if is_option_leg else (o.transaction_id, None)
            by_group.setdefault(key, []).append(o)

        trades: List[Dict[str, Any]] = []
        for (txn_id, _grp_contract), orders in by_group.items():
            if not orders:
                continue
            # Drop a lone multi-leg PARENT group (option, no contract, net-only): its legs carry
            # the real per-contract P&L, so a parent-only group is not a real round-trip.
            if all(
                getattr(o, "asset_class", None) == AssetClass.OPTION
                and not getattr(o, "contract_symbol", None)
                for o in orders
            ):
                continue
            # A worthless-close leg has open_price 0 on BOTH sides after netting; keep it (it is a
            # real, fully-realised round-trip) — the ``!= 0.0`` guard above already let it through.
            # The opening order is the earliest-filled one; its side opens the position.
            orders_by_fill = sorted(orders, key=_fill_key)
            opening = orders_by_fill[0]
            opening_side = opening.side
            entries = [o for o in orders if o.side == opening_side]
            exits = [o for o in orders if o.side != opening_side]
            if not entries:
                continue

            def _wavg(group):
                """(quantity-weighted avg open_price, total qty) over a group of fills."""
                tot_qty = sum(abs(float(o.filled_qty or o.quantity or 0.0)) for o in group)
                if tot_qty <= 0:
                    return None, 0.0
                wsum = sum(
                    float(o.open_price) * abs(float(o.filled_qty or o.quantity or 0.0))
                    for o in group
                )
                return wsum / tot_qty, tot_qty

            entry_px, entry_qty = _wavg(entries)
            if entry_px is None or entry_qty <= 0:
                continue
            is_long = opening_side == OrderDirection.BUY
            direction = 1.0 if is_long else -1.0
            entry_dt = min(
                (self._fill_dates.get(o.id) for o in entries if self._fill_dates.get(o.id) is not None),
                default=None,
            )

            if exits:
                exit_px, exit_qty = _wavg(exits)
                size = exit_qty
                exits_by_fill = sorted(exits, key=_fill_key)
                last_exit_fill = exits_by_fill[-1]
                exit_dt = max(
                    (self._fill_dates.get(o.id) for o in exits if self._fill_dates.get(o.id) is not None),
                    default=None,
                )
                exit_reason = self._exit_reason(last_exit_fill, exit_px)
            else:
                # Still open at run end: mark-to-market at the last available price.
                size = entry_qty
                if (
                    getattr(opening, "asset_class", None) == AssetClass.OPTION
                    and getattr(opening, "contract_symbol", None)
                    and self._options is not None
                ):
                    # OPTION leg: ``opening.symbol`` is the UNDERLYING, so ``close_at``
                    # would record the share price as the exit (the run-782 NVDA defect:
                    # 200.09 spot instead of the 0.10 premium close). Mark at the
                    # contract's premium close via the same ``_options.get_bar`` path the
                    # equity curve's option MTM uses. No premium bar -> entry premium
                    # (breakeven), NEVER the underlying close.
                    bar = self._options.get_bar(opening.contract_symbol, self._as_of_date())
                    exit_px = bar["close"] if bar and bar.get("close") is not None else None
                    if exit_px is None:
                        # No premium bar at run end: the same intrinsic-floored fallback
                        # the equity curve's mark uses (review 2026-08-30 F2 twin) —
                        # max(intrinsic, entry premium), so a deep-ITM short still open
                        # at run end books its liability instead of a break-even row.
                        # Entry premium remains the mark when strike / right / spot are
                        # unresolvable (the fix is for the no-OPTION-bar case, not the
                        # no-equity-bar case).
                        intr = None
                        if opening.strike is not None and opening.option_type is not None:
                            und = getattr(opening, "underlying_symbol", None) or opening.symbol
                            spot = self._price.close_asof(und)
                            if spot is not None:
                                intr, _ = self._no_arb_premium_bounds(
                                    opening.strike,
                                    opening.option_type == OptionRight.CALL,
                                    spot,
                                )
                        if intr is not None:
                            exit_px = max(float(entry_px), intr)
                        else:
                            logger.warning(
                                "[backtest] round-trip recorder: no premium bar for open option "
                                "%s at run end (%s) and no resolvable intrinsic — marking "
                                "open_at_end at the entry premium.",
                                opening.contract_symbol,
                                self._as_of_date(),
                            )
                            exit_px = entry_px
                else:
                    # VALUATION (not a fill): forward-fill the last KNOWN close, exactly like the
                    # equity curve's MTM. ``close_at`` needs an EXACT bar on the run-end clock, but
                    # the clock is the UNION of every symbol's timestamps — a held symbol whose data
                    # ends EARLY (delisted/stale cache) has no bar there, so close_at returned None
                    # and the fallback stamped exit=entry, silently zeroing real unrealised P&L that
                    # the equity curve had already counted (the 2026-08-05 ED full-window $4.3k gap).
                    exit_px = self._price.close_asof(opening.symbol)
                    if exit_px is None:
                        exit_px = entry_px  # never priced after entry -> flat (near-zero trade)
                exit_dt = self._price.now()
                exit_reason = "open_at_end"

            # ONE commission PER FILL -- exactly what the cash ledger charged (``_apply_fill`` /
            # ``_apply_option_fill``: ``self._cash -= commission`` once per fill). The old flat
            # ``commission * 2`` (or * 1 on the open_at_end branch) assumed every round-trip was
            # a single buy + a single sell, so any transaction with MORE fills -- a rebalance ADD,
            # a scaled/partial exit, a multi-fill open still held at run end -- was undercharged
            # in the trade rows while the equity curve had already paid the real amount. A
            # one-sided error: it can only ever flatter the reported P&L, never penalise it.
            comm = commission * (len(entries) + len(exits))

            # Options quote premium PER SHARE but a contract controls ``multiplier`` (100)
            # shares, so realised option P&L scales by the contract multiplier. The NULL fallback
            # must be 100 -- the same fallback the cash ledger (``_apply_option_fill``), the MTM
            # equity curve and every other option site uses. It was ``or 1`` here alone, so a
            # NULL-multiplier option round-trip booked P&L 100x too small against an equity curve
            # that had already moved by the full 100x amount. Equity entries are not options ->
            # ``else 1`` is CORRECT and must stay (shares have no contract multiplier).
            mult = (
                (opening.multiplier or 100)
                if getattr(opening, "asset_class", None) == AssetClass.OPTION
                else 1
            )
            gross = (exit_px - entry_px) * size * direction * mult
            pnl = gross - comm
            # P&L % = realised dollar P&L (commission included) as a fraction of ACCOUNT EQUITY at
            # the time the position opened — the trade's true impact on the account, NOT the bare
            # price move (exit/entry). The price-ratio form ignored commission (so a +0.8% price
            # move with a net loss showed green) and size, and made a microcap's 90x price return
            # dominate Best-Trade though it barely moved the account. Equity-at-entry keeps the sign
            # consistent with ``pnl`` and makes Best/Worst/Expectancy account-relative.
            equity_at_entry = self._equity_at(entry_dt)
            pnl_pct = (pnl / equity_at_entry * 100.0) if equity_at_entry else 0.0
            bars_held = self._bars_between(entry_dt, exit_dt)
            trades.append(
                {
                    "symbol": opening.symbol,
                    "entry_time": entry_dt,
                    "exit_time": exit_dt,
                    "direction": "buy" if is_long else "sell",
                    "entry_price": entry_px,
                    "exit_price": exit_px,
                    "size": size,
                    # PUBLISH the contract multiplier the P&L above used (100 for an option,
                    # 1 for equity). Without it every downstream consumer that rebuilds "the
                    # capital deployed in this trade" from entry_price x size is 100x too small
                    # for an option -- which is exactly what the results profit cap and the
                    # monte-carlo spread-stress notional were doing. No consumer should have to
                    # re-derive it from asset_class.
                    "multiplier": float(mult),
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "bars_held": bars_held,
                    "exit_reason": exit_reason,
                    # Only set for option legs; lets a post-hoc consumer (intraday_drawdown)
                    # look up delta/underlying bars without re-deriving them from the order set.
                    "contract_symbol": getattr(opening, "contract_symbol", None),
                    "underlying_symbol": getattr(opening, "underlying_symbol", None),
                    # The STRUCTURE this row belongs to. Rows are per-LEG (see the group key
                    # above), but a multi-leg spread is ONE economic bet: results.py's profit
                    # cap re-joins the legs on this id so a condor is capped on its NET P&L
                    # against its NET cost, not leg-by-leg (which capped the winning leg while
                    # leaving the losing one, scoring a max-profit condor NEGATIVE).
                    "transaction_id": txn_id,
                    # The EXACT multiplier ``pnl`` above was computed with (1 for equity, the
                    # contract multiplier for an option). Recorded rather than re-derived so a
                    # consumer's cost basis (entry_price x size x multiplier) can never fall
                    # out of step with the P&L it is compared against.
                    "multiplier": mult,
                }
            )
        # Deterministic order: by entry time then symbol.
        trades.sort(key=lambda t: (str(t["entry_time"]), t["symbol"]))
        return trades

    def _exit_reason(self, exit_order, fill_px: float) -> str:
        """Classify an OCO/TP/SL exit fill as take_profit / stop_loss by nearest price level."""
        tp = exit_order.limit_price
        sl = exit_order.stop_price
        if tp is not None and sl is not None:
            return "take_profit" if abs(fill_px - tp) <= abs(fill_px - sl) else "stop_loss"
        if tp is not None:
            return "take_profit"
        if sl is not None:
            return "stop_loss"
        return "exit"

    def _equity_at(self, as_of: Optional[datetime]) -> float:
        """Account equity (net liquidating value) at/just-before ``as_of`` — the capital base a
        trade opened then was sized against. Bisects the ascending snapshot dates (O(log n)).
        Falls back to the first snapshot (initial capital) for a pre-curve entry, or the live
        equity if no snapshots exist yet."""
        snaps = self._equity_snapshots
        if not snaps:
            return self.equity()
        if as_of is None:
            return snaps[0]["net_liquidating_value"]
        idx = bisect.bisect_right(self._snapshot_dates, as_of) - 1
        if idx < 0:
            idx = 0
        return snaps[idx]["net_liquidating_value"]

    def _bars_between(self, start: Optional[datetime], end: Optional[datetime]) -> int:
        """Number of equity-curve bars between two simulated timestamps (>=0)."""
        if start is None or end is None:
            return 0
        # _snapshot_dates is ascending (appended in clock order), so the count of snapshots in
        # [start, end] is a bisect window (O(log n)) — not a full scan per trade.
        dates = self._snapshot_dates
        n = bisect.bisect_right(dates, end) - bisect.bisect_left(dates, start)
        return max(n - 1, 0)

    def get_balance_history(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Dict]:
        """The equity curve: the per-bar snapshots appended by ``snapshot_equity``."""
        snaps = list(self._equity_snapshots)
        if start_date is not None:
            snaps = [s for s in snaps if s["date"] >= start_date]
        if end_date is not None:
            snaps = [s for s in snaps if s["date"] <= end_date]
        return snaps

    # ======================================================================
    # The critical gotcha: defeat the inherited wall-clock price cache
    # ======================================================================
    def get_instrument_current_price(self, symbol_or_symbols, price_type: str = "bid"):
        """OVERRIDE: bypass the inherited _GLOBAL_PRICE_CACHE (wall-clock TTL).

        The virtual backtest clock moves far faster than wall time, so the inherited
        TTL cache would treat a price fetched on virtual day N as "fresh" on day N+5,
        leaking stale/look-ahead prices across bars. We delegate straight to the impl
        (the engine ALSO pops the per-account cache each bar as belt-and-braces).
        """
        return self._get_instrument_current_price_impl(symbol_or_symbols, price_type=price_type)

    # ======================================================================
    # OptionsAccountInterface — READ methods (Task 4)
    #
    # All option reads delegate to the injected as-of-clamped provider, snapping the
    # provider's ``as_of`` to the simulated bar's DATE (the engine sets the clock per bar
    # via ``self._price.set_clock``). When no provider is injected (equity-only path) the
    # reads degrade to empty/None so equity behaviour is unaffected. The two abstract
    # ORDER methods (``_submit_option_order_impl`` / ``close_option_position``) are stubs
    # here — they are implemented in Task 5 — but the class still instantiates (no abstract
    # method left). ``submit_option_order`` is concrete in the base mixin and is NOT
    # overridden; ``get_iv_rank`` IS (see below — the base reads a live-only SQL table).
    # ======================================================================
    def _as_of_date(self):
        """The simulated bar's calendar date (the provider's as-of clamp boundary)."""
        return self._price.now().date()

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        if self._options is None:
            return []
        return self._options.get_chain(
            underlying, self._as_of_date(), expiry_min=expiry_min, expiry_max=expiry_max,
            option_type=option_type, strike_min=strike_min, strike_max=strike_max)

    def get_option_quote(self, contract_symbol):
        return None if self._options is None else self._options.get_quote(
            contract_symbol, self._as_of_date())

    def get_atm_implied_volatility(self, underlying):
        return None if self._options is None else self._options.get_atm_iv(
            underlying, self._as_of_date())

    #: Spacing of the trailing ATM-IV grid, in SESSIONS. 1 == every trading day, which
    #: MATCHES the live recorder's daily cron so the two compute the same statistic over
    #: the same sample density. Raise it to trade parity for speed on a very wide
    #: universe: cost is bounded by the number of DISTINCT (symbol, date) pairs a run
    #: touches — ~lookback + run length per symbol — because get_atm_iv is memoized
    #: per (db_path, underlying, as_of) for the life of the worker process, not
    #: recomputed per bar.
    #:
    #: Sessions, not calendar days: the old calendar-day step SKIPPED weekends rather
    #: than stepping over them, so any value above 1 produced a grid that drifted through
    #: the week and dropped a varying number of samples. Counting sessions makes "every
    #: Nth sample" mean the same thing wherever the window starts.
    IV_RANK_SAMPLE_STEP_DAYS = 1

    def _iv_rank_sample_dates(self, as_of, lookback_days: int):
        """Trading-session grid over the trailing window, EXCLUDING ``as_of`` itself.

        NON-SESSIONS ARE SKIPPED BECAUSE THE PROVIDER CLAMPS. ``get_atm_iv`` returns the
        latest snapshot on or before a date, so any closed day silently returns the
        previous session's number again and that session gets counted twice. The original
        grid handled weekends (``weekday() < 5``) and missed market HOLIDAYS, which are
        the same bug at 1/12th the volume: nine weekdays a year on which the exchange is
        shut, each double-weighting the session before it — and clustered around
        year-end and quarter turns, where IV is least typical. The NYSE calendar answers
        both cases from one source, and half-days are real sessions with real closes so
        they correctly stay in.

        ``as_of`` is excluded because ``_iv_rank_from_series`` counts strictly ``<`` and
        the memoized provider returns a bit-identical value for the same date — including
        it would guarantee one sample that can never be below ``current`` and bias every
        rank down by 100/N (20 points at the production min_samples of 5). Live has the
        same shape: the stored series is yesterday-and-earlier, ``current`` is a fresh
        read.

        DEGRADES, NEVER DIES. ``pandas_market_calendars`` is a pinned dependency, but a
        remote GA worker that somehow lacks it must produce a slightly coarser grid — the
        old weekday fallback — rather than failing every trial in the job.
        """
        from datetime import timedelta
        from ba2_common.core import market_calendar

        first = as_of - timedelta(days=lookback_days)
        last = as_of - timedelta(days=1)
        try:
            days = [o.astimezone(market_calendar.NY_TZ).date()
                    for o, _c in market_calendar.nyse_regular_sessions(first, last)]
        except Exception as e:
            logger.warning(
                f"NYSE calendar unavailable ({e}); falling back to a weekday IV-rank grid. "
                f"Market holidays will be sampled, double-counting the session before "
                f"each one.")
            days, day = [], first
            while day <= last:
                if day.weekday() < 5:
                    days.append(day)
                day += timedelta(days=1)

        step = max(1, int(self.IV_RANK_SAMPLE_STEP_DAYS))
        return days[::step]

    def get_iv_rank(self, underlying, lookback_days: Optional[int] = None,
                    min_samples: int = 20, current=None):
        """OVERRIDE: percentile of today's ATM IV against a provider-derived series.

        The base mixin reads ``option_iv_snapshot`` — a live table keyed by an
        ``account_id`` FK into ``accountdefinition``, which nothing in a backtest ever
        writes. Inheriting it meant the rank was always None and every iv_rank rule was
        silently, permanently False.

        We deliberately do NOT start writing that table. A persisted, accumulating
        series would make GA trial N depend on trials 1..N-1 (per-trial reproducibility
        is the point of this platform), it has no as-of notion so it could leak
        look-ahead, and it would be a write plus a read to recover a number the memoized
        provider already holds. Building the series from ``self._options`` keeps every
        sample inside the as-of clamp by construction. The precedent is
        ``PremiumSeller._update_iv_history``, which seeds its own in-memory series the
        same way.

        The PERCENTILE MATH is the shared ``_iv_rank_from_series``, so live and backtest
        cannot drift on what "IV rank 60" means — including its plausibility bound, which
        is this path's ONLY boundary since nothing here writes a row a recorder could
        have vetted. Returns None (never 0.0) when the provider yields fewer than
        ``min_samples`` usable points — which is the case on every options cache built
        before the greeks columns existed, and is exactly what keeps ``IVRankCondition``
        failing closed there.

        WHAT IS NOT SHARED, and must not be claimed as such: the ATM CONTRACT. Live picks
        the strike nearest spot across the whole 20-45 DTE chain; this provider has no
        point-in-time spot, so it proxies with the CALL whose |delta| is nearest 0.50 (see
        ``HistoricalOptionsProvider.get_atm_iv``). The two series are the same KIND of
        number sampled on the same grid, but they are not the same statistic, and an
        ``iv_rank`` threshold tuned here does not transfer to live at full precision.

        ``lookback_days`` defaults to the shared ``IV_RANK_LOOKBACK_DAYS`` (calendar
        days), so live and backtest cannot disagree on the window width either.
        """
        if self._options is None:
            return None
        if lookback_days is None:
            lookback_days = self.IV_RANK_LOOKBACK_DAYS
        as_of = self._as_of_date()
        if current is None:
            current = self._options.get_atm_iv(underlying, as_of)
        series = [self._options.get_atm_iv(underlying, d)
                  for d in self._iv_rank_sample_dates(as_of, lookback_days)]
        return self._iv_rank_from_series(series, current, min_samples)

    def get_option_positions(self):
        """Held option positions, derived from OPENED transactions whose entry is an OPTION.

        single-leg : the transaction's entry order IS the contract -> one position from the
                     transaction's net open qty.
        multi-leg  : the entry is the parent (no contract_symbol); each FILLED child leg is a
                     SEPARATE per-contract position (both legs of a spread share one txn, and
                     their buy/sell qty would net to zero, so they cannot be read off the txn
                     net — they are read directly off the child legs).
        """
        # Equity-only backtest: no options provider was injected, so no option order could ever
        # have filled (``_option_fill_price`` requires it) and there can be no option positions.
        # Short-circuit BEFORE opening a Session — ``_apply_option_expiry`` calls this every bar,
        # and the empty OPENED-transaction query was ~21% of a 5-minute run (profiled).
        if self._options is None:
            return []

        out: List[OptionPosition] = []
        txns = transactions_where(status=TransactionStatus.OPENED)
        for t in txns:
            entry = self._entry_order_for_transaction(t)
            if entry is None or getattr(entry, "asset_class", None) != AssetClass.OPTION:
                continue
            # Multi-leg parent (no contract_symbol): one position per filled child leg.
            if not getattr(entry, "contract_symbol", None):
                out.extend(self._multi_leg_positions(entry))
                continue
            qty = t.get_current_open_qty()
            if qty == 0:
                continue
            out.append(
                OptionPosition(
                    contract_symbol=entry.contract_symbol,
                    underlying=entry.underlying_symbol,
                    option_type=entry.option_type,
                    strike=entry.strike,
                    expiry=entry.expiry,
                    side=(OrderDirection.BUY if qty > 0 else OrderDirection.SELL),
                    quantity=abs(qty),
                    avg_entry_price=t.open_price or 0.0,
                    multiplier=entry.multiplier or 100,
                )
            )
        return out

    def _multi_leg_positions(self, parent) -> List[OptionPosition]:
        """One OptionPosition per STILL-OPEN per-contract leg of a multi-leg option parent.

        Each opening child leg (linked by ``parent_order_id``) is a per-contract lot (buy leg ->
        long, sell leg -> short) at its own fill premium. CLOSING fills — the synthetic orders
        recorded by ``_record_option_expiry_close`` at expiry/liquidation, which share the
        transaction + contract but carry NO ``parent_order_id`` — are NETTED against the opening
        leg on the SAME contract, so a leg that has been settled/liquidated is NO LONGER reported
        as held. Without this netting a resolved leg was re-processed by ``_apply_option_expiry``
        every bar (re-assigning shares repeatedly -> the -256%/-8974% blow-up).
        """
        executed = OrderStatus.get_executed_statuses()
        # Net signed contract qty per OCC across ALL executed option orders on this transaction
        # (opening child legs + synthetic closing orders), plus a template of the opening leg for
        # the per-contract metadata (strike/expiry/type/premium).
        net: Dict[str, float] = {}
        opening: Dict[str, Any] = {}
        for o in self.get_orders():
            if o.transaction_id != parent.transaction_id:
                continue
            if getattr(o, "asset_class", None) != AssetClass.OPTION:
                continue
            if not o.contract_symbol:  # skip the parent (net-only, no contract)
                continue
            if o.status not in executed or not (o.filled_qty or o.quantity):
                continue
            qty = float(o.filled_qty or o.quantity)
            signed = qty if o.side == OrderDirection.BUY else -qty
            net[o.contract_symbol] = net.get(o.contract_symbol, 0.0) + signed
            # The OPENING leg is a child of the parent; keep it as the metadata template.
            if o.parent_order_id == parent.id:
                opening[o.contract_symbol] = o

        out: List[OptionPosition] = []
        for cs, signed_qty in net.items():
            if abs(signed_qty) < 1e-9:
                continue  # fully closed leg -> not held
            leg = opening.get(cs)
            if leg is None:
                continue
            out.append(
                OptionPosition(
                    contract_symbol=cs,
                    underlying=leg.underlying_symbol,
                    option_type=leg.option_type,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    side=(OrderDirection.BUY if signed_qty > 0 else OrderDirection.SELL),
                    quantity=abs(signed_qty),
                    avg_entry_price=leg.open_price or 0.0,
                    multiplier=leg.multiplier or 100,
                )
            )
        return out

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        """Stage the option order(s) so the per-bar fill engine fills them next bar.

        No broker round-trip: we simply move the order(s) from the base's freshly-persisted
        PENDING state into the SAME working/fillable status the equity ``_submit_order_impl``
        uses (``OrderStatus.ACCEPTED`` — see ``get_active_statuses()``), so the per-bar fill
        engine (Task 6/8) picks them up next bar.

        single-leg: the parent IS the contract (it carries ``contract_symbol``) and fills.
        multi-leg : the child leg orders carry the contracts that fill; the parent has no
                    ``contract_symbol`` and only tracks the net — it stays working (non-terminal)
                    but is not itself directly fillable.

        The SIMULATED submission bar is recorded per row in ``_option_order_day``; that is what
        ``_expire_stale_option_limits`` ages a DAY order against. ``TradingOrder.created_at``
        cannot be used for it — the ORM stamps it with the WALL clock, which in a backtest is
        years away from the simulated one.
        """
        fillable = OrderStatus.ACCEPTED  # matches the equity working status (_submit_order_impl)
        placed_on = self._as_of_date()
        rows = list(leg_orders or []) + [trading_order]
        for row in rows:
            row.status = fillable
            update_instance(row)
            if row.id is not None:
                self._option_order_day[row.id] = placed_on
        return trading_order

    def close_option_position(self, position, order_type="limit", limit_price=None):
        """Submit a closing order for a held option position (opposite intent).

        Builds a single-leg ``OptionLeg`` on the same contract with the opposite side
        (BUY long -> SELL_TO_CLOSE; SELL short -> BUY_TO_CLOSE) and routes it through the
        inherited ``submit_option_order`` so it is staged fillable like any other option order.

        The close RIDES the OPEN position's transaction (we look up the OPENED option
        transaction for the contract and pass its id), so the sell-to-close leg REDUCES the
        original position to flat (net open qty -> 0) instead of spawning a separate OPENED
        transaction holding the opposite-side leg. This also lets round-trip P&L pair the
        open and close (they share one ``transaction_id``).
        """
        from ba2_common.core.option_types import OptionLeg

        close_side = (
            OrderDirection.SELL if position.side == OrderDirection.BUY else OrderDirection.BUY
        )
        intent = (
            "sell_to_close" if position.side == OrderDirection.BUY else "buy_to_close"
        )
        leg = OptionLeg(
            contract_symbol=position.contract_symbol,
            side=close_side,
            position_intent=intent,
            option_type=position.option_type,
            strike=position.strike,
            expiry=position.expiry,
            underlying=position.underlying,
        )
        txn = self._option_transaction_for_contract(position.contract_symbol)
        txn_id = getattr(txn, "id", None) if txn is not None else None
        return self.submit_option_order(
            legs=[leg],
            quantity=int(position.quantity),
            order_type=order_type,
            limit_price=limit_price,
            option_strategy="close",
            transaction_id=txn_id,
        )

    def settle_option_expiry(
        self,
        position: OptionPosition,
        *,
        close_premium: float,
        share_side: Optional[OrderDirection] = None,
        shares: int = 0,
        share_price: Optional[float] = None,
    ) -> bool:
        """Settle a held single-leg option position at expiry (Task 7).

        Closes the option leg's OPENED transaction at ``close_premium`` (per-share intrinsic
        value, or 0 for worthless) and zeroes its lot in the option ledger, then — for an
        exercise/assignment — converts to shares in the EQUITY ledger settled at ``share_price``
        (the STRIKE). The option premium paid/collected at entry is already in cash, so the
        conversion only moves cash for the share leg (qty x strike); the resulting equity
        position marks-to-market at the underlying close on every subsequent bar.

        This is a deterministic AT-EXPIRY settlement (no next-bar fill, no slippage/commission):
        the option simply resolves on its expiry bar. Returns True if the position was settled.
        This is the settlement MECHANISM; whether a leg is assigned physically or is sold to
        close is decided by the no-orphaned-stock policy in ``settle_single_leg_expiry``.

        MULTI-LEG (strangle/straddle/spread) note: the two/four legs of a spread SHARE one
        ``Transaction`` whose ENTRY is the multi-leg PARENT (which carries NO ``contract_symbol``).
        Each leg is settled INDEPENDENTLY here (its own lot -> its own share conversion + its own
        closing fill for round-trip P&L); the shared transaction is closed ONLY once every leg has
        resolved (``_all_legs_resolved``), so settling the first leg does not orphan the second.
        """
        from ba2_common.core.utils import close_transaction_with_logging

        txn = self._option_transaction_for_contract(position.contract_symbol)
        if txn is None:
            return False

        # 1. Record a synthetic CLOSING fill on THIS leg at the resolved premium (intrinsic, or 0
        #    for worthless) so round-trip P&L can pair open<->close. This moves NO cash: the option
        #    premium was already settled at entry and the exercise/assignment cash is the share leg
        #    (step 3). Without this closing order the option round-trip is missing (single-leg) or
        #    mis-paired (the reported entry~0 / pnl=-market*100*qty defect in Backtest id=299).
        self._record_option_expiry_close(txn, position, float(close_premium))

        # 2. Remove THIS leg's option lot from the option ledger (its cash was settled at entry; the
        #    conversion below moves the share-leg cash). Worthless simply zeroes it out.
        lot = self._option_positions.get(position.contract_symbol)
        if lot is not None:
            lot.qty = 0.0
            lot.avg_price = 0.0

        # 3. Exercise/assignment -> create the resulting SHARE position settled at the STRIKE (NOT
        #    the market — the option holder transacts stock at the strike). The share cost basis is
        #    therefore the strike; the position then marks-to-market at the underlying close so an
        #    ITM assignment loss is real and PERSISTS. ``_book_assignment_share_leg`` also writes
        #    the delivery to the ORDER/TRANSACTION tables (OPT-B2/F7) — the ledger alone is not the
        #    book.
        if share_side is not None and shares and share_price is not None:
            signed = float(shares) if share_side == OrderDirection.BUY else -float(shares)
            self._cash -= signed * float(share_price)  # buy debits, sell credits — at strike.
            self._book_assignment_share_leg(
                position.underlying, signed, float(share_price), expert_id=txn.expert_id)

        # 4. Close the SHARED transaction only once every option leg on it has resolved. For a
        #    single-leg option this is immediate; for a multi-leg spread the transaction stays
        #    OPENED until the last leg settles so each leg can still find it in step 1.
        if self._all_legs_resolved(txn):
            txn.close_price = float(close_premium)
            if not txn.close_date:
                txn.close_date = self._price.now()
            close_transaction_with_logging(
                txn,
                account_id=self.id,
                close_reason="option_expiry",
                additional_data={"contract_symbol": position.contract_symbol},
            )
            update_instance(txn)
        return True

    def _book_assignment_share_leg(
        self, symbol: str, signed: float, price: float, *, expert_id: Optional[int] = None
    ) -> None:
        """Apply an assignment's SHARE delivery to the ledger AND to the order/transaction book.

        ``_update_position`` alone (what this used to be) mutates only the in-memory
        ``self._positions`` dict, so an assignment was invisible to everything that reads
        transactions. That is OPT-B2 and F7:

          * CALLED AWAY. The 100 long shares netted to zero while their equity Transaction
            stayed OPENED with a FILLED BUY and no SELL, so ``_held_equity_shares`` reported
            100 phantom shares forever — the covered-call overlay wrote another, NAKED, call
            every cycle, and a later equity exit sold shares that did not exist, opening a
            real short from ``qty=0``. ``process_pending_assignment_liquidations`` could not
            catch it: it short-circuits on ``held <= 0`` and the netted position IS zero.
          * PUT TO US / ASSIGNED SHORT. The new stock lot had no order and no transaction, so
            the next-bar liquidation order resolved to ``transaction_id=None`` and
            ``get_round_trip_trades`` dropped it — the realised stock P&L reached the equity
            CURVE and never the trade ROWS every fitness metric is built from.

        The delivery is therefore split at the ledger boundary:

          * the part that OFFSETS an existing lot is booked as a closing fill AT THE STRIKE on
            that lot's transaction (``_record_stock_liquidation_close`` resolves and links it;
            ``refresh_transactions`` then closes the transaction on the filled dependent leg);
          * the part that OPENS a new lot gets its own equity Transaction + FILLED entry order
            at the strike, tagged ``origin=csp_assignment`` exactly as the live door does
            (``AlpacaAccount._apply_option_activity``), and only THAT part is scheduled for
            the next-bar orphan liquidation — scheduling the full assignment would sell down
            an unrelated lot the strategy still owns.

        ``hold_assigned_stock`` (run setting, DEFAULT OFF) suppresses ONLY that last
        scheduling step. Everything else about an assignment — the cash at the strike, the
        ledger move, the closing fill on an offset lot, the new lot's transaction + entry
        order and its ``origin=csp_assignment`` tag — is identical either way, so a run with
        the switch off is bit-for-bit what it was before the switch existed. It is opt-in
        because the wheel is the only strategy whose rules manage the delivered shares; see
        the setting's description in ``get_settings_definitions`` for what holding costs.

        Cash has already moved in the caller; this moves no cash of its own.
        """
        pos = self._positions.get(symbol)
        held = float(pos.qty) if pos is not None else 0.0
        # The offsetting part (opposite sign to what is held) closes; the rest opens.
        closing = 0.0
        if held and (held > 0) != (signed > 0):
            closing = min(abs(signed), abs(held))
        opening = abs(signed) - closing

        self._update_position(symbol, signed, price)

        if closing > 0:
            self._record_stock_liquidation_close(
                symbol, closing, was_long=(held > 0), px=price, comment="option_assignment")
        if opening > 0:
            self._open_assigned_stock_transaction(
                symbol, opening, is_long=(signed > 0), price=price, expert_id=expert_id)
            # Only the newly-orphaned lot is unmanaged; schedule THAT for the next bar —
            # unless this run HOLDS assigned stock, in which case the lot is the strategy's
            # to manage (the wheel writes calls against it) and nothing is scheduled.
            if not self._cfg.get("hold_assigned_stock", False):
                self._pending_assignment_sells[symbol] = (
                    self._pending_assignment_sells.get(symbol, 0.0)
                    + (opening if signed > 0 else -opening)
                )

    def _open_assigned_stock_transaction(
        self, symbol: str, qty: float, *, is_long: bool, price: float,
        expert_id: Optional[int] = None,
    ) -> None:
        """Persist the equity Transaction + FILLED entry order for an assignment-created lot.

        Mirrors the live ``csp_assignment`` shape (``AlpacaAccount._apply_option_activity``):
        an OPENED equity transaction at the STRIKE carrying ``meta_data.origin =
        csp_assignment``, with one synthetic FILLED order underneath. The ``expert_id`` is
        carried over from the settling OPTION transaction so the shares are visible to the
        expert that wrote the contract — the wheel's covered call is sized off exactly these
        shares — and so ``has_assigned_shares`` can see them.

        Written as an ENTRY (no ``depends_on_order``): the next-bar liquidation's closing leg
        is what resolves against it, and ``refresh_transactions`` closes the transaction then.
        """
        from ba2_common.core.types import TXN_ORIGIN_CSP_ASSIGNMENT

        as_of = self._price.now()
        side = OrderDirection.BUY if is_long else OrderDirection.SELL
        txn = Transaction(
            symbol=symbol,
            quantity=abs(float(qty)),
            side=side,
            open_price=float(price),
            open_date=as_of,
            status=TransactionStatus.OPENED,
            asset_class=AssetClass.EQUITY,
            expert_id=expert_id,
            meta_data={"origin": TXN_ORIGIN_CSP_ASSIGNMENT},
        )
        txn_id = add_instance(txn)
        if txn_id is None:
            return
        # open_date is already the SIM clock — keep refresh_transactions' re-stamp pass off it.
        self._stamped_open_ids.add(txn_id)
        order = TradingOrder(
            account_id=self.id,
            symbol=symbol,
            quantity=abs(float(qty)),
            filled_qty=abs(float(qty)),
            side=side,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            open_price=float(price),
            transaction_id=txn_id,
            open_type=OrderOpenType.AUTOMATIC,
            broker_order_id=self._next_broker_id(),
            comment="option_assignment",
            created_at=as_of,
        )
        new_id = add_instance(order)
        if new_id is not None:
            self._fill_dates[new_id] = as_of
        self.invalidate_order_cache()

    def settle_single_leg_expiry(self, position: OptionPosition, spot: float) -> bool:
        """Expiry settlement POLICY for a SINGLE-LEG (non-defined-risk-combo) option position.

        Backtest expiry policy (no orphaned stock): a backtest strategy manages OPTIONS
        (its exit rules are all ``close_option``), so any SHARE position an expiry leaves
        behind would ride unmanaged to the end of the run — exercised ITM long calls riding
        to 67-85% of final equity was the OS1 blow-up. Concretely:

          * OTM -> worthless (transaction closed at premium 0) — unchanged.
          * LONG ITM (call or put): NEVER exercised. SELL-TO-CLOSE at the expiry bar's
            premium close (intrinsic when the sparse cache has no bar — near expiry the
            premium converges to intrinsic). NO share position is created; cash is credited
            premium x multiplier x contracts exactly once.
          * SHORT ITM (call or put): ALWAYS physical assignment at the strike, covered or
            naked — the share leg is the real-world outcome and keeps the assignment loss
            in the book. But the assigned stock is unmanaged here, so EVERY assignment
            schedules a broker-style liquidation of the resulting stock at the NEXT bar's
            open via ``process_pending_assignment_liquidations`` (not only the
            cash-negative case, which previously sold just enough to restore cash >= 0 and
            left the REST of the assigned shares orphaned). A run that sets
            ``hold_assigned_stock`` (the WHEEL) opts out of that last step only — the
            assignment itself is identical, the shares are simply kept.

        Assignment cash always moves at the STRIKE; the sell-to-close credit is the only
        premium-priced leg. Deterministic at-expiry settlement — no slippage/commission,
        matching ``settle_option_expiry``. Defined-risk combos must NOT be routed here (they
        unit-settle via ``settle_defined_risk_combo_expiry``).
        """
        strike = float(position.strike)
        is_call = position.option_type == OptionRight.CALL
        itm = (spot > strike) if is_call else (spot < strike)
        if not itm:
            return self.settle_option_expiry(position, close_premium=0.0)

        contracts = float(position.quantity)
        multiplier = float(position.multiplier or 100)
        shares = int(position.quantity) * int(multiplier)
        intrinsic = (spot - strike) if is_call else (strike - spot)

        if position.side == OrderDirection.SELL:
            # SHORT ITM -> ALWAYS physically assigned (called away / put to us) at the strike.
            share_side = OrderDirection.SELL if is_call else OrderDirection.BUY
            ok = self.settle_option_expiry(
                position, close_premium=intrinsic,
                share_side=share_side, shares=shares, share_price=strike,
            )
            if ok:
                # The stock the assignment ORPHANS (long from a short put, short from a naked
                # call) is unmanaged in a backtest and is scheduled for next-bar liquidation by
                # ``_book_assignment_share_leg`` — which schedules only the part that actually
                # opened a new lot. A COVERED call delivers shares the account already held:
                # that part is booked as a closing fill on the lot's own transaction and there
                # is nothing left to liquidate (scheduling it would sell down an unrelated lot).
                # ``hold_assigned_stock`` runs skip the scheduling entirely — say which
                # happened, or the log reads as a liquidation that never comes.
                logger.warning(
                    "[backtest] short-%s assignment of %d x %s at strike %.2f — %s "
                    "(cash now $%.2f).",
                    "call" if is_call else "put", shares, position.underlying, strike,
                    ("HOLDING the assigned stock (hold_assigned_stock is on; the strategy's "
                     "own rules must manage and exit it)"
                     if self._cfg.get("hold_assigned_stock", False) else
                     "scheduling assignment_liquidation of the assigned stock at the next "
                     "bar's open"),
                    self._cash,
                )
            return ok

        # LONG ITM -> SELL-TO-CLOSE, never exercise: the strategy's exits only manage
        # options, so exercised stock would ride unmanaged to the end of the run. Price at
        # the expiry bar's premium close; intrinsic when the cache has no bar (near expiry
        # the premium converges to intrinsic). Cash is credited at the premium; NO share
        # position is created.
        bar = self._options.get_bar(position.contract_symbol, self._as_of_date()) if self._options else None
        if bar and bar.get("close") is not None:
            # The expiry bar's print can be junk (the arb guard's own documented class: a
            # $0.01 call against $50+ of intrinsic) and this credit is REALISED cash, not
            # a mark — clamp it into the same no-arb bounds the guard enforces on fills
            # (floor at intrinsic, cap at spot for a call / strike for a put). Spot is a
            # parameter here, so the bounds always resolve. (Review 2026-08-30 F1.)
            premium = self._clamp_premium_to_no_arb(float(bar["close"]), strike, is_call, spot)
        else:
            premium = float(intrinsic)
        self._cash += premium * multiplier * contracts
        logger.warning(
            "[backtest] long %s %s ITM at expiry — sold to close at %.4f (backtests never "
            "exercise; intrinsic %.4f).",
            "call" if is_call else "put", position.contract_symbol, premium, intrinsic,
        )
        return self.settle_option_expiry(position, close_premium=premium)

    def process_pending_assignment_liquidations(self) -> bool:
        """Broker-style liquidation of stock orphaned by a short-option assignment.

        A short option is ALWAYS physically assigned at expiry; the resulting stock
        position (LONG from an assigned short put, SHORT from an assigned naked short
        call) is unmanaged by option strategies, so the account closes ALL of it at the
        NEXT bar's OPEN — never more than was assigned, never more than is still held in
        that direction (a pre-existing position the strategy opened itself is untouched,
        and an assignment that merely offset an opposite position leaves nothing to do).
        Each liquidation persists a synthetic FILLED closing order (comment
        ``assignment_liquidation``) so the equity move is a visible trade. A symbol with
        no bar this tick stays pending for the next bar. One dict check when nothing is
        pending; equity-only runs never schedule anything — and neither do runs that set
        ``hold_assigned_stock`` (the wheel), for which this method is a permanent no-op
        because ``_book_assignment_share_leg`` never fills the pending dict.

        THE PLEDGED-COVER LOCK APPLIES TO THE SELL-LONG BRANCH (OPT-L1 exit half). This
        method runs AFTER the manage pass, and ``daily_engine`` already documents the
        hazard in prose: the overlay writes a covered call against the assigned shares in
        step 3 and this would sell them on the same bar, leaving a naked short call. Until
        now the ONLY mitigation was the opt-in per-strategy ``hold_assigned_stock``, so
        every arm that is not O_WHEEL was exposed — reproduced on the DEFAULT config.
        The sale is therefore clamped to the unpledged excess and any remainder STAYS
        QUEUED (mirroring the existing "no bar this tick -> retry next bar" idiom rather
        than the unconditional pop), so the shares liquidate the moment the call is bought
        back or expires instead of being silently forgotten. The ``assigned < 0`` branch
        BUYS BACK short stock, which can only ever ADD cover, and is never touched.

        Returns True when any shares were liquidated.
        """
        if not self._pending_assignment_sells:
            return False
        traded_any = False
        for symbol in list(self._pending_assignment_sells.keys()):
            assigned = self._pending_assignment_sells[symbol]
            pos = self._positions.get(symbol)
            qty = float(pos.qty) if pos is not None else 0.0
            # +assigned -> SELL long stock; -assigned -> BUY BACK short stock. Bound by
            # what is actually held in that direction right now.
            held = qty if assigned > 0 else -qty
            if assigned == 0 or held <= 0:
                self._pending_assignment_sells.pop(symbol)  # nothing left to clean up
                continue
            bar = self._price.bar_at(symbol)
            if bar is None or bar.get("open") is None:
                continue  # no bar this tick -> retry next bar
            px = float(bar["open"])
            if px <= 0:
                continue
            to_close = float(min(abs(assigned), held))
            # PLEDGED-COVER lock on the SELL-LONG branch only (see the docstring). The
            # buy-back branch adds cover and is exempt. `locked_out` is what the lock — not
            # the `held` bound — held back, so a run where the lock never bites keeps the
            # historical unconditional pop and stays byte-identical.
            locked_out = 0.0
            if assigned > 0:
                sellable = self._pledged_share_lock(
                    symbol, to_close, context="assignment_liquidation (next-bar orphan sale)")
                locked_out = to_close - sellable
                if sellable <= 0:
                    continue  # stays queued: retry once the short call releases its cover
                to_close = float(sellable)
            signed = -to_close if assigned > 0 else to_close  # sell long / buy back short
            self._cash -= signed * px
            self._update_position(symbol, signed, px)
            self._record_stock_liquidation_close(
                symbol, to_close, was_long=(assigned > 0), px=px, comment="assignment_liquidation"
            )
            logger.warning(
                "[backtest] assignment_liquidation: %s %g x %s @ %.4f (next-bar open) — "
                "assigned stock is unmanaged in a backtest; cash now $%.2f.",
                "sold" if assigned > 0 else "bought back", to_close, symbol, px, self._cash,
            )
            if locked_out > 0:
                # Only the PLEDGED remainder stays queued. Popping it here (what the
                # unconditional pop did) would silently forget orphaned stock the lock
                # merely DEFERRED, leaving it unmanaged to run end — the OS1 blow-up this
                # method exists to prevent.
                self._pending_assignment_sells[symbol] = locked_out
            else:
                self._pending_assignment_sells.pop(symbol)
            traded_any = True
        return traded_any

    def defined_risk_combo_strategy(self, position: OptionPosition) -> Optional[str]:
        """Return the DEFINED-RISK ``option_strategy`` of the combo a leg belongs to, else None.

        A position qualifies when its transaction's PARENT order is a multi-leg option whose
        ``option_strategy`` is one of the defined-risk structures (debit or credit). Single-leg
        options, equities, and UNDEFINED-risk structures (short strangle/straddle, jade_lizard,
        put_ratio_spread) return None so they keep the per-leg share-assignment path.
        """
        txn = self._option_transaction_for_contract(position.contract_symbol)
        if txn is None:
            return None
        entry = self._entry_order_for_transaction(txn)
        if entry is None or getattr(entry, "contract_symbol", None):
            return None  # single-leg (entry carries a contract) -> not a multi-leg combo
        strat = getattr(entry, "option_strategy", None)
        if strat in self.DEFINED_RISK_LONG_STRATEGIES or strat in self.DEFINED_RISK_SHORT_STRATEGIES:
            return strat
        return None

    def settle_defined_risk_combo_expiry(self, positions: List[OptionPosition], spot: float) -> bool:
        """UNIT settlement of a DEFINED-RISK multi-leg combo at expiry.

        Settling a spread/butterfly/condor leg-by-leg into SHARES at each strike does NOT preserve
        the combo's bounded payoff — the deep-ITM legs' gross cash flows (e.g. buy 100 sh @325 =
        -$32.5k) dwarf the account before the offsetting legs net back, blowing equity past the
        defined-risk bound. Instead we settle the WHOLE combo ONCE:

          net_payoff = Σ legs ( sign * intrinsic_per_share * multiplier * contracts )
          where sign = +1 for a LONG leg, -1 for a SHORT leg, and
          intrinsic = max(0, spot-strike) for a call / max(0, strike-spot) for a put.

        This net is mathematically bounded to the structure's defined risk. We apply it directly to
        CASH (the entry premium is already in cash), record a synthetic closing fill per leg (so
        round-trip P&L pairs), zero all leg lots, and create NO per-leg stock positions. A safety
        clamp bounds the realized net to the theoretical [min,max] so data noise can't exceed
        defined risk. ``positions`` are the combo's still-held legs; ``spot`` is the underlying
        close at expiry. Returns True when settled.
        """
        from ba2_common.core.utils import close_transaction_with_logging

        if not positions:
            return False
        txn = self._option_transaction_for_contract(positions[0].contract_symbol)
        if txn is None:
            return False

        # Net intrinsic payoff across the legs (bounded to defined risk by construction).
        net_payoff = 0.0
        strikes: List[float] = []
        for pos in positions:
            is_call = pos.option_type == OptionRight.CALL
            intrinsic = max(0.0, spot - float(pos.strike)) if is_call else max(0.0, float(pos.strike) - spot)
            sign = 1.0 if pos.side == OrderDirection.BUY else -1.0
            mult = float(pos.multiplier or 100)
            net_payoff += sign * intrinsic * mult * float(pos.quantity)
            strikes.append(float(pos.strike))

        # Safety clamp: a defined-risk combo's expiry payoff magnitude can never exceed the
        # structure's defined risk. Bound both directions so rounding / bad cache data cannot
        # leak past it.
        bound = self._combo_expiry_bound(txn, positions, strikes)
        if bound is not None:
            net_payoff = max(-bound, min(net_payoff, bound))

        # Apply the net payoff to cash and book each leg's synthetic close (moves no extra cash).
        self._cash += net_payoff
        for pos in positions:
            is_call = pos.option_type == OptionRight.CALL
            intrinsic = max(0.0, spot - float(pos.strike)) if is_call else max(0.0, float(pos.strike) - spot)
            self._record_option_expiry_close(txn, pos, float(intrinsic))
            lot = self._option_positions.get(pos.contract_symbol)
            if lot is not None:
                lot.qty = 0.0
                lot.avg_price = 0.0

        if self._all_legs_resolved(txn):
            # Carry the NET payoff per contract-share on the transaction row (a hardcoded 0.0
            # threw the realised settlement value away; round-trips had to be consulted).
            denom = float(positions[0].multiplier or 100) * self._combo_structure_count(txn, positions)
            txn.close_price = (net_payoff / denom) if denom > 0 else 0.0
            if not txn.close_date:
                txn.close_date = self._price.now()
            close_transaction_with_logging(
                txn, account_id=self.id, close_reason="option_expiry_combo",
                additional_data={"strategy": self.defined_risk_combo_strategy(positions[0])},
            )
            update_instance(txn)
        logger.info(
            "[backtest] defined-risk combo unit-settled at expiry: net payoff $%.2f (bounded).",
            net_payoff,
        )
        return True

    def _combo_expiry_bound(self, txn: Transaction, positions: List[OptionPosition],
                            strikes: List[float]) -> Optional[float]:
        """Max |net payoff| a defined-risk combo can realise at expiry.

        ``strategy-aware width x multiplier x structures`` (the same width rule the MTM clamp
        uses — ``_defined_risk_width_per_structure``). ``structures`` comes from the combo's
        PARENT order quantity (the same source ``_option_group_bounds`` uses): ``max(leg qty)``
        counted a 1-2-1 butterfly's 2x body as the structure count, doubling the bound.
        Falls back to ``min(leg quantities)`` when the parent is unresolvable. The multiplier
        is the legs' (not a hardcoded 100). Returns None when the structure cannot be bounded.
        """
        entry = self._entry_order_for_transaction(txn)
        strategy = getattr(entry, "option_strategy", None) if entry is not None else None
        width_per = self._defined_risk_width_per_structure(strategy, strikes)
        if width_per is None:
            return None
        structures = self._combo_structure_count(txn, positions, entry=entry)
        if structures <= 0:
            return None
        multiplier = float(positions[0].multiplier or 100)
        return width_per * multiplier * structures

    def _combo_structure_count(self, txn: Transaction, positions: List[OptionPosition],
                               entry: Optional[TradingOrder] = None) -> float:
        """Structure count of a multi-leg combo: the PARENT order's quantity.

        Falls back to ``min(leg quantities)`` when the parent is unresolvable (never
        ``max`` — that counts a 1-2-1 butterfly's 2x body as the structure count). ``entry``
        skips the transaction lookup when the caller already resolved it.
        """
        if entry is None:
            entry = self._entry_order_for_transaction(txn)
        structures = abs(float(entry.quantity)) if (entry is not None and entry.quantity) else 0.0
        if structures <= 0:
            structures = min(float(p.quantity) for p in positions)
        return structures

    def _record_option_expiry_close(
        self, txn: Transaction, position: OptionPosition, close_premium: float
    ) -> None:
        """Persist a synthetic FILLED closing order for an expiring option leg.

        The closing side is the opposite of how the leg was opened (a SHORT leg is bought back,
        a LONG leg is sold), the fill quantity is the leg's contract count, and ``open_price`` is
        the per-share settlement premium (intrinsic, or 0 for worthless). This is a BOOK-KEEPING
        order only: it records the round-trip close so ``get_round_trip_trades`` pairs the option's
        open and close with the correct realised premium P&L. It moves NO cash (the premium was
        settled at entry; the exercise/assignment share leg carries the intrinsic value).
        """
        close_side = (
            OrderDirection.BUY if position.side == OrderDirection.SELL else OrderDirection.SELL
        )
        as_of = self._price.now()
        # Link the closing order to the transaction's ENTRY (depends_on_order) so it is classified
        # as a DEPENDENT leg — never as an entry. ``_entry_order_for_transaction`` returns the
        # earliest order with ``depends_on_order IS NULL``; without this link the closing order
        # (whose SIM created_at predates the real entry's WALL-clock created_at) would be mistaken
        # for the entry and break the sibling leg's transaction lookup on a multi-leg spread.
        entry = self._entry_order_for_transaction(txn)
        order = TradingOrder(
            account_id=self.id,
            symbol=position.contract_symbol,
            underlying_symbol=position.underlying,
            quantity=abs(float(position.quantity)),
            filled_qty=abs(float(position.quantity)),
            side=close_side,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            open_price=float(close_premium),
            asset_class=AssetClass.OPTION,
            multiplier=position.multiplier or 100,
            contract_symbol=position.contract_symbol,
            option_type=position.option_type,
            strike=position.strike,
            expiry=position.expiry,
            transaction_id=txn.id,
            depends_on_order=(entry.id if entry is not None else None),
            open_type=OrderOpenType.AUTOMATIC,
            broker_order_id=self._next_broker_id(),
            comment="option_expiry_close",
            created_at=as_of,
        )
        new_id = add_instance(order)
        if new_id is not None:
            self._fill_dates[new_id] = as_of
        self.invalidate_order_cache()

    def _all_legs_resolved(self, txn: Transaction) -> bool:
        """True when every FILLED option leg on ``txn`` now has a matching closing fill.

        A leg is "resolved" once its opening fill is offset by a closing fill on the SAME
        contract (recorded by ``_record_option_expiry_close``). For a single-leg option this is
        true as soon as the one leg settles; for a multi-leg spread it becomes true only after the
        last leg has settled — which is when the shared transaction may be closed.
        """
        executed = OrderStatus.get_executed_statuses()
        net: Dict[str, float] = {}
        for o in self.get_orders():
            if o.transaction_id != txn.id:
                continue
            if getattr(o, "asset_class", None) != AssetClass.OPTION:
                continue
            if not o.contract_symbol:  # skip the multi-leg PARENT (net-only, no contract)
                continue
            if o.status not in executed or not (o.filled_qty or o.quantity):
                continue
            qty = float(o.filled_qty or o.quantity)
            signed = qty if o.side == OrderDirection.BUY else -qty
            net[o.contract_symbol] = net.get(o.contract_symbol, 0.0) + signed
        return all(abs(v) < 1e-9 for v in net.values())

    def _option_transaction_for_contract(self, contract_symbol: str) -> Optional[Transaction]:
        """The OPENED option transaction that TRADES ``contract_symbol``.

        Matches the single-leg case (the transaction's ENTRY order IS the contract) AND the
        multi-leg case (the entry is the PARENT with no ``contract_symbol``, and the contract is
        carried by one of its FILLED child legs). Without the child-leg match a spread leg's
        expiry settlement could not find its transaction, so the legs never settled (the strangle
        assignment defect: option lots persisted, no share conversion, equity mis-marked).
        """
        txns = transactions_where(status=TransactionStatus.OPENED)
        for t in txns:
            entry = self._entry_order_for_transaction(t)
            if entry is None or getattr(entry, "asset_class", None) != AssetClass.OPTION:
                continue
            # single-leg: the entry itself carries the contract.
            if entry.contract_symbol == contract_symbol:
                return t
            # multi-leg: the entry is the parent (no contract) -> match a child leg's contract.
            if not entry.contract_symbol:
                for leg in self.get_orders():
                    if (
                        leg.parent_order_id == entry.id
                        and leg.contract_symbol == contract_symbol
                    ):
                        return t
        return None

    # ======================================================================
    # Trading abstracts — baseline; expanded into the full engine in Task 3
    # ======================================================================
    def _next_broker_id(self) -> str:
        self._broker_seq += 1
        return f"BT-{self.id}-{self._broker_seq}"

    def _submit_order_impl(
        self,
        trading_order: TradingOrder,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        is_closing_order: bool = False,
        use_complex_order: bool = False,
    ) -> Any:
        """Called by the INHERITED ``submit_order`` after validation/persistence.

        ``use_complex_order`` is accepted for signature parity and ignored: it is the live
        wash-trade escape (see docs/WASHTRADE-LOCK.md), and the simulated broker has no
        wash-trade rule, so it can never be set here — ``_find_opposing_working_order``
        would have to find a blocker first.

        Assign a synthetic broker id and mark the order working; the per-bar fill engine
        (``refresh_orders``) decides when/whether it fills. We do NOT reimplement
        ``submit_order`` (it is inherited and exercises the real validation path).

        Idempotency guard (mirrors AlpacaAccount): an order that already carries a
        broker_order_id was already "sent" — never re-stamp it.

        A WAITING_TRIGGER dependent leg keeps its WAITING_TRIGGER status (it must wait for
        its parent to reach the trigger status before becoming live); everything else
        becomes ACCEPTED (working / active per get_active_statuses()).
        """
        if trading_order.broker_order_id:
            return trading_order
        trading_order.broker_order_id = self._next_broker_id()
        if trading_order.status != OrderStatus.WAITING_TRIGGER:
            trading_order.status = OrderStatus.ACCEPTED
        update_instance(trading_order)
        return trading_order

    def cancel_order(self, order_id: str) -> Any:
        """Cancel a working order (reserved cash/position is notional-only in this sim)."""
        o = self.get_order(order_id)
        if o is None:
            return None
        o.status = OrderStatus.CANCELED
        update_instance(o)
        self.invalidate_order_cache()  # o may be a fresh DB instance, not the cached one
        return o

    def modify_order(self, order_id: str) -> Any:
        """In-place pre-fill edit of a working order.

        The live ``modify_order`` signature is ``modify_order(self, order_id)`` (no
        trading_order param) — the caller mutates the order row, then calls this to
        "push" the change to the broker. In the sim there is no broker round-trip, so we
        simply re-persist the (non-terminal) order. A terminal order cannot be modified.
        """
        o = self.get_order(order_id)
        if o is None or o.status in OrderStatus.get_terminal_statuses():
            return None
        update_instance(o)
        self.invalidate_order_cache()  # o may be a fresh DB instance, not the cached one
        return o

    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """Record a take-profit level on the transaction (LEAN SIMULATOR — no leg order).

        The fill engine's ``_apply_bracket_exits`` checks ``transaction.take_profit`` directly
        against each bar and synthesizes the closing order when it is crossed — there is NO
        pre-staged WAITING_TRIGGER/OCO leg order (that live-broker mechanism is not modelled in the
        backtest; it only cost ORM churn with no economic effect). TP for a LONG closes with a
        SELL_LIMIT at ``new_tp_price``; for a SHORT a BUY_LIMIT. Idempotent — safe to re-issue every
        manage bar (a trailing/updated level just overwrites the stored value). Returns False if the
        entry order can't be found or the price is invalid.
        """
        if not new_tp_price or new_tp_price <= 0:
            return False
        if self._entry_order_for_transaction(transaction) is None:
            return False
        transaction.take_profit = new_tp_price
        update_instance(transaction)
        return True

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """Record a stop-loss level on the transaction (LEAN SIMULATOR — no leg order).

        Mirror of ``adjust_tp``: ``_apply_bracket_exits`` closes with a SELL_STOP (long) / BUY_STOP
        (short) at ``new_sl_price`` when a bar crosses it. No WAITING_TRIGGER leg; idempotent.
        """
        if not new_sl_price or new_sl_price <= 0:
            return False
        if self._entry_order_for_transaction(transaction) is None:
            return False
        transaction.stop_loss = new_sl_price
        update_instance(transaction)
        return True

    def adjust_tp_sl(
        self,
        transaction: Transaction,
        new_tp_price: Optional[float] = None,
        new_sl_price: Optional[float] = None,
        source: str = "",
    ) -> bool:
        """Record a paired TP+SL on the transaction (LEAN SIMULATOR — no OCO leg order).

        Both levels live independently as ``transaction.take_profit``/``stop_loss``;
        ``_apply_bracket_exits`` fills whichever the bar crosses first (STOP wins on a straddle,
        the conservative worst case) and closes the trade. No single ``OrderType.OCO`` leg is
        materialised.
        """
        ok = True
        if new_tp_price is not None:
            ok &= self.adjust_tp(transaction, new_tp_price, source=source)
        if new_sl_price is not None:
            ok &= self.adjust_sl(transaction, new_sl_price, source=source)
        return ok

    # ======================================================================
    # Fill helpers (baseline MARKET path; Task 3 adds LIMIT/STOP/OCO branches)
    # ======================================================================
    def _bar_for_fill(self, order, as_of: datetime) -> Optional[Dict[str, float]]:
        """The bar an order fills against, per the configured fill model."""
        if self._cfg["fill_model"] == "same_bar_close":
            return self._price.bar_at(order.symbol, as_of)
        return self._price.next_bar(order.symbol, as_of)  # default: next_bar_open

    def _slip(self, px: float, side_is_buy: bool) -> float:
        """Apply slippage + half the bid-ask spread in the worsening direction (buys up, sells
        down). MARKET and STOP fills trade INTO the spread immediately once triggered (a stop
        becomes a market order), so the cost lands entirely on the fill PRICE here -- unlike a
        LIMIT fill, whose spread cost instead widens the TRIGGER threshold (see
        ``_limit_trigger_price``). spread_bps defaults to 0.0 (optional setting) so an existing
        config that never set it is an exact no-op, identical to before this was added."""
        bps = float(self._cfg["slippage_bps"]) / 10_000.0
        bps += float(self._cfg.get("spread_bps", 0.0)) / 10_000.0 / 2.0
        return px * (1.0 + bps) if side_is_buy else px * (1.0 - bps)

    def _limit_trigger_price(self, limit_price: float, is_sell: bool) -> float:
        """The RAW market price a LIMIT order's underlying mid/last must reach for a fill at
        ``limit_price`` to be realistic given a round-trip bid-ask spread: a SELL_LIMIT only
        crosses the BID, so the market needs to trade half-spread ABOVE the limit; a BUY_LIMIT
        only crosses the ASK, so half-spread BELOW. The fill price stays ``limit_price`` (the
        net target actually realized once the wider threshold is crossed) -- this only makes
        the order HARDER to trigger, so a marginal TP can miss the window entirely and the
        trade resolves via its SL/timeout instead, unlike a flat pnl haircut which can't change
        which exit a trade takes. spread_bps=0.0 (default) returns ``limit_price`` unchanged."""
        frac = float(self._cfg.get("spread_bps", 0.0)) / 10_000.0 / 2.0
        return limit_price * (1.0 + frac) if is_sell else limit_price * (1.0 - frac)

    def _gap_stop_fill(self, stop: float, bar: Dict[str, float], is_sell: bool) -> float:
        """Stop fill price, accounting for a bar that GAPPED THROUGH the stop (2026-07-26).

        A stop becomes a MARKET order the moment it is touched, so if the bar OPENED already
        beyond the stop the fill happens at that open, not at the stop. Before this, the engine
        only tested ``bar.low <= stop`` and filled at ``stop`` — which silently assumed you
        always got your stop price no matter how far the market gapped past it, understating
        losses precisely where real stops fail worst (overnight/earnings gaps).

        Returns the WORSE of stop/open for the closing side: a SELL stop (closing a long) fills
        at the lower of the two, a BUY stop (closing a short) at the higher. A bar with no open
        falls back to the stop (previous behaviour).
        """
        # CONFIGURABLE, because it encodes a real disagreement about execution.
        #
        # A plain stop becomes a MARKET order when touched, so a gap through it fills at the
        # open -- that is what stops DO, and it is why SELL_STOP_LIMIT exists (it floors the
        # price, at the risk of not filling at all). Modelling it is the accurate default.
        #
        # ``assume_stop_fills_at_price`` = True instead assumes the stop price is always
        # obtained. That is only true for a stop-LIMIT, and it makes gap risk invisible --
        # which matters most on low-priced/volatile names where overnight gaps are routine.
        # Kept as an explicit, named setting rather than a silent behaviour so the assumption
        # shows up in the run config instead of being buried in the engine.
        if self._cfg.get("assume_stop_fills_at_price"):
            return stop
        o = bar.get("open")
        if o is None:
            return stop
        o = float(o)
        return min(stop, o) if is_sell else max(stop, o)

    def _gap_limit_fill(self, limit: float, bar: Dict[str, float], is_sell: bool) -> float:
        """Limit fill price, accounting for a favourable gap (2026-07-26).

        A limit order fills at its price OR BETTER, so a bar that OPENED beyond the limit fills
        at that open. The mirror of _gap_stop_fill and deliberately shipped WITH it: modelling
        only the adverse gap would bias results pessimistic, just as modelling neither biased
        stops optimistic. Returns the BETTER of limit/open for the closing side.
        """
        o = bar.get("open")
        if o is None:
            return limit
        o = float(o)
        return max(limit, o) if is_sell else min(limit, o)

    def _trigger_thresholds(self, order) -> tuple:
        """The (trig_hi, trig_lo) PLAIN-float price thresholds for a working equity order.

        These mirror EXACTLY the price comparisons in ``_evaluate_fill`` so a cheap per-bar
        pre-check (``bar.high >= trig_hi or bar.low <= trig_lo``) lets through precisely the
        bars that could trigger a fill — the full (ORM-heavy) ``_evaluate_fill`` then makes the
        real decision. The thresholds are CACHED as plain (non-mapped) attributes on the order
        (``_trig_hi`` / ``_trig_lo``) on first evaluation so subsequent bars read plain floats,
        not instrumented SQLModel columns. A leg is never mutated in place (adjust_tp/sl REPLACES
        it with a fresh order), so the cache is always consistent with the order's prices.

          * MARKET     -> always triggers (hi=-inf so ``bar.high >= -inf`` is always True).
          * BUY_LIMIT  -> fills iff bar.low  <= limit  -> trig_lo = limit.
          * SELL_LIMIT -> fills iff bar.high >= limit  -> trig_hi = limit.
          * BUY_STOP   -> triggers iff bar.high >= stop -> trig_hi = stop.
          * SELL_STOP  -> triggers iff bar.low  <= stop -> trig_lo = stop.
          * OCO        -> both a stop side and a limit side -> trig_hi AND trig_lo set (the
                         side mapping differs by direction but each side is exactly one of
                         {bar.high >= X} / {bar.low <= X}; see ``_evaluate_oco_fill``).
        """
        hi = getattr(order, "_trig_hi", None)
        if hi is not None or getattr(order, "_trig_lo", None) is not None:
            return order._trig_hi, order._trig_lo

        INF = float("inf")
        trig_hi = INF   # the price bar.high must REACH (>=) to possibly trigger; INF = never via high
        trig_lo = -INF  # the price bar.low  must REACH (<=) to possibly trigger; -INF = never via low
        ot = order.order_type
        if ot == OrderType.MARKET:
            trig_hi = -INF  # always triggers (bar.high >= -inf is always True)
        elif ot == OrderType.BUY_LIMIT:
            trig_lo = self._limit_trigger_price(float(order.limit_price), is_sell=False)
        elif ot == OrderType.SELL_LIMIT:
            trig_hi = self._limit_trigger_price(float(order.limit_price), is_sell=True)
        elif ot == OrderType.BUY_STOP:
            trig_hi = float(order.stop_price)
        elif ot == OrderType.SELL_STOP:
            trig_lo = float(order.stop_price)
        elif ot == OrderType.OCO:
            # Both legs present: one side is a {bar.high >= X} test, the other {bar.low <= X}.
            # SELL OCO (closing long):  TP SELL_LIMIT (high>=limit), SL SELL_STOP (low<=stop).
            # BUY  OCO (closing short): SL BUY_STOP   (high>=stop),  TP BUY_LIMIT  (low<=limit).
            # The TP (limit) side's threshold is spread-widened; the SL (stop) side is not (its
            # spread cost lands on the fill PRICE via _slip once triggered, not the threshold).
            is_sell = order.side == OrderDirection.SELL
            tp = order.limit_price
            sl = order.stop_price
            if is_sell:
                if tp is not None:
                    trig_hi = self._limit_trigger_price(float(tp), is_sell=True)
                if sl is not None:
                    trig_lo = float(sl)
            else:
                if sl is not None:
                    trig_hi = float(sl)
                if tp is not None:
                    trig_lo = self._limit_trigger_price(float(tp), is_sell=False)
        # else: unknown type -> never triggers via the gate (INF/-INF). _evaluate_fill returns
        # None for it anyway, so the gate (which would skip it) stays results-identical.
        order._trig_hi = trig_hi
        order._trig_lo = trig_lo
        return trig_hi, trig_lo

    def _evaluate_fill(self, order, as_of: datetime) -> Optional[float]:
        """Return the fill price for ``order`` against the chosen bar, or None if untriggered.

        Per-type rules (the bar's [low, high] range is the day's traded range):
          * MARKET            -> fills at the bar's open (or close for same_bar_close),
                                 worsened by slippage.
          * BUY_LIMIT         -> fills at the limit iff bar.low  <= limit (price traded down to it).
          * SELL_LIMIT        -> fills at the limit iff bar.high >= limit (price traded up to it).
          * BUY_STOP          -> triggers iff bar.high >= stop; fills at stop +slippage, or at
                                 the bar OPEN when the bar gapped above the stop.
          * SELL_STOP         -> triggers iff bar.low  <= stop; fills at stop -slippage, or at
                                 the bar OPEN when the bar gapped below the stop.
          * OCO (TP+SL leg)   -> evaluate TP (limit) and SL (stop) sides; fill the side the
                                 bar crosses (SL preferred when the bar straddles both, the
                                 conservative assumption that the stop hit first).
        """
        bar = self._bar_for_fill(order, as_of)
        if bar is None:
            return None
        ot = order.order_type

        if ot == OrderType.MARKET:
            ref = bar["close"] if self._cfg["fill_model"] == "same_bar_close" else bar["open"]
            return self._slip(ref, order.side == OrderDirection.BUY)

        if ot == OrderType.BUY_LIMIT:
            trig = self._limit_trigger_price(float(order.limit_price), is_sell=False)
            return (self._gap_limit_fill(float(order.limit_price), bar, is_sell=False)
                    if bar["low"] <= trig else None)
        if ot == OrderType.SELL_LIMIT:
            trig = self._limit_trigger_price(float(order.limit_price), is_sell=True)
            return (self._gap_limit_fill(float(order.limit_price), bar, is_sell=True)
                    if bar["high"] >= trig else None)

        if ot == OrderType.BUY_STOP:
            return (self._slip(self._gap_stop_fill(float(order.stop_price), bar, is_sell=False), True)
                    if bar["high"] >= order.stop_price else None)
        if ot == OrderType.SELL_STOP:
            return (self._slip(self._gap_stop_fill(float(order.stop_price), bar, is_sell=True), False)
                    if bar["low"] <= order.stop_price else None)

        if ot == OrderType.OCO:
            return self._evaluate_oco_fill(order, bar)

        return None

    def _evaluate_oco_fill(self, order, bar: Dict[str, float]) -> Optional[float]:
        """Fill price for an OCO leg (limit_price=TP, stop_price=SL) against ``bar``.

        The OCO closes the position, so its ``side`` is opposite the entry:
          * SELL OCO (closing a LONG):  TP = SELL_LIMIT @ limit (bar.high >= TP),
                                        SL = SELL_STOP  @ stop  (bar.low  <= SL).
          * BUY  OCO (closing a SHORT): TP = BUY_LIMIT  @ limit (bar.low  <= TP),
                                        SL = BUY_STOP   @ stop  (bar.high >= SL).
        When a single bar's range crosses BOTH legs we fill the STOP (loss) side — the
        conservative, no-look-ahead assumption (intrabar order is unknown).
        """
        tp = order.limit_price
        sl = order.stop_price
        is_sell = order.side == OrderDirection.SELL  # closing a long

        if is_sell:
            sl_hit = sl is not None and bar["low"] <= sl
            tp_trig = self._limit_trigger_price(float(tp), is_sell=True) if tp is not None else None
            tp_hit = tp_trig is not None and bar["high"] >= tp_trig
            if sl_hit:
                # stop -slippage, or the OPEN when the bar gapped below it (see _gap_stop_fill)
                return self._slip(self._gap_stop_fill(float(sl), bar, is_sell=True), False)
            if tp_hit:
                # limit, or the OPEN when the bar gapped above it (trigger was widened)
                return self._gap_limit_fill(float(tp), bar, is_sell=True)
            return None
        else:
            sl_hit = sl is not None and bar["high"] >= sl
            tp_trig = self._limit_trigger_price(float(tp), is_sell=False) if tp is not None else None
            tp_hit = tp_trig is not None and bar["low"] <= tp_trig
            if sl_hit:
                return self._slip(self._gap_stop_fill(float(sl), bar, is_sell=False), True)
            if tp_hit:
                return self._gap_limit_fill(float(tp), bar, is_sell=False)
            return None

    def _apply_fill(self, order, fill_px: float, as_of: datetime) -> None:
        """Apply a fill to cash + ledger and mark the order FILLED."""
        qty = float(order.quantity) if order.quantity is not None else 0.0
        signed = qty if order.side == OrderDirection.BUY else -qty
        commission = float(self._cfg["commission_per_trade"])
        # CASH-SECURED safeguard (O(1)): a BUY that OPENS/ADDS to a long must never drive cash
        # negative — the backtest must not silently run on leverage. The classic RM already
        # self-limits (get_available_balance goes negative once capital is deployed, so it sizes
        # 0), so in a correct run this NEVER fires; it's a regression guard. If it ever trips we
        # log it LOUDLY and clamp the fill to the affordable share count (cancel if not even 1),
        # so a future sizing regression fails visibly + stays cash-secured instead of leveraging.
        if signed > 0 and fill_px > 0:
            cur = self._positions.get(order.symbol)
            if (cur.qty if cur else 0.0) >= 0 and signed * fill_px + commission > self._cash + 1e-6:
                affordable = int((self._cash - commission) / fill_px)
                logger.error(
                    "BACKTEST cash-secured safeguard TRIPPED on %s: BUY %g @ %.4f "
                    "(cost $%.2f + $%.2f commission = $%.2f) exceeds cash $%.2f -> clamping to "
                    "%d share(s). The RM over-sized; the engine bounds deployment to available "
                    "cash. NOTE the commission: the test is cost+commission, so a line where the "
                    "bare cost looks affordable is not a false trip.",
                    order.symbol, qty, fill_px, signed * fill_px, commission,
                    signed * fill_px + commission, self._cash, max(0, affordable),
                )
                if affordable < 1:
                    order.status = OrderStatus.CANCELED
                    order.quantity = 0
                    update_instance(order)
                    self._cancel_oco_sibling(order)
                    return
                qty = float(affordable)
                signed = qty
                order.quantity = qty
        # PLEDGED-COVER safeguard (OPT-L1 exit half) — the SELL-side MIRROR of the
        # cash-secured block above, and the single choke point for every strategy-driven
        # share sale (the working-order loop in refresh_orders and _apply_bracket_exits both
        # land here). A broker LOCKS the shares that collateralise a written call; the
        # simulator did not, so O_CC's trailing stop sold the collateral and left four
        # measurably NAKED short calls standing for up to 40 bars. _pledged_share_lock owns
        # the reasoning, the tri-state pledge and the (deduped) LOUD log; it returns `qty`
        # untouched for equity-only runs, for a sell from flat/short, and whenever the pledge
        # is a MEASURED zero — so every path that was correct before is byte-identical.
        #
        # Unlike the cash-secured branch this does NOT cancel the OCO sibling: the position
        # is still OPEN and still needs its other protective leg. The exit is refused, not
        # the position abandoned — a re-armed TP/SL simply retries on the next bar and gets
        # through the moment the call is bought back or expires.
        if signed < 0 and qty > 0:
            sellable = self._pledged_share_lock(
                order.symbol, qty,
                context=f"order {order.broker_order_id} / {order.comment or 'no comment'}")
            if sellable <= 0:
                order.status = OrderStatus.CANCELED
                order.quantity = 0
                update_instance(order)
                return
            if sellable < qty:
                qty = float(sellable)
                signed = -qty
                order.quantity = qty  # persist, or refresh_transactions' net-filled
                #                       arithmetic disagrees with the ledger
        # Buying spends cash (signed>0 -> cash decreases); selling adds cash.
        self._cash -= signed * fill_px
        self._cash -= commission
        self._update_position(order.symbol, signed, fill_px)
        order.filled_qty = qty
        order.open_price = fill_px
        order.status = OrderStatus.FILLED
        update_instance(order)
        # Record the SIMULATED fill bar (not wall-clock) so the trade history is deterministic.
        if order.id is not None:
            self._fill_dates[order.id] = as_of

    def _apply_option_fill(self, order, fill_px: float, as_of: datetime) -> None:
        """Apply a single-leg option fill to cash + option ledger and mark the order FILLED.

        Mirrors ``_apply_fill`` (the equity path) but scales the cash impact by the contract
        MULTIPLIER (100): buying ``q`` contracts at premium ``p`` debits ``q*p*multiplier``;
        commission is the same flat per-leg charge. ``open_price`` stays the premium PER
        SHARE (so round-trip P&L math reads premiums directly). The signed lot is recorded
        in the SEPARATE option ledger so the per-bar marking values it at premium-close x
        qty x multiplier — the equity ledger (``self._positions``) is untouched.
        """
        qty = float(order.quantity) if order.quantity is not None else 0.0
        signed = qty if order.side == OrderDirection.BUY else -qty
        multiplier = float(order.multiplier or 100)
        commission = float(self._cfg["commission_per_trade"])
        # Buying spends cash (signed>0 -> cash decreases); selling adds cash. Scaled x100.
        self._cash -= signed * fill_px * multiplier
        self._cash -= commission
        self._update_option_position(order.contract_symbol, signed, fill_px, multiplier)
        order.filled_qty = qty
        order.open_price = fill_px
        order.status = OrderStatus.FILLED
        update_instance(order)
        if order.id is not None:
            self._fill_dates[order.id] = as_of

    def _update_option_position(
        self, contract_symbol: str, signed_qty: float, fill_px: float, multiplier: float
    ) -> None:
        """Apply a signed option fill to the option ledger (weighted-avg premium on adds).

        Mirrors ``_update_position``'s averaging logic but on contracts: same-sign exposure
        updates the weighted-average premium; reducing/closing leaves the avg unchanged;
        flipping through zero re-bases the avg at the new fill premium.
        """
        lot = self._option_positions.get(contract_symbol)
        if lot is None:
            lot = _OptionLot(contract_symbol=contract_symbol, multiplier=multiplier)
            self._option_positions[contract_symbol] = lot
            # A NEW held contract changes _option_group_bounds' contract->group mapping, but a
            # fill mutates in place (no invalidate_order_cache) — bump the memo generation so
            # the F6 memos refresh. Adds to an EXISTING lot change nothing the memos read.
            self._option_memo_gen += 1
        lot.multiplier = multiplier
        old_qty = lot.qty
        new_qty = old_qty + signed_qty
        if old_qty == 0 or (old_qty > 0) == (signed_qty > 0):
            total_cost = lot.avg_price * abs(old_qty) + fill_px * abs(signed_qty)
            denom = abs(new_qty)
            lot.avg_price = (total_cost / denom) if denom > 0 else 0.0
        elif abs(signed_qty) > abs(old_qty):
            lot.avg_price = fill_px  # flipped through zero -> remainder opens at fill premium
        lot.qty = new_qty
        if lot.qty == 0:
            lot.avg_price = 0.0

    # ======================================================================
    # TP/SL/OCO leg helpers
    # ======================================================================
    def _entry_order_for_transaction(self, transaction: Transaction) -> Optional[TradingOrder]:
        """The market-entry order of a transaction: transaction_id matches + no parent.

        (Transaction has no entry_order_id column; the entry order is the one with
        ``depends_on_order IS NULL``. If several exist — e.g. scaled entries — the oldest
        is returned so legs depend on the original entry.)
        """
        rows = orders_where(account_id=self.id, transaction_id=transaction.id,
                            depends_on_order=None)
        if not rows:
            return None
        rows.sort(key=lambda o: (o.created_at or datetime.min.replace(tzinfo=timezone.utc), o.id or 0))
        return rows[0]

    # NOTE: the WAITING_TRIGGER/OCO leg factory (_replace_leg) + its helper (_existing_legs) were
    # REMOVED — the lean simulator stores TP/SL on the transaction and closes directly via
    # _apply_bracket_exits (no pre-staged dependent-leg orders). The live broker accounts still use
    # the real leg mechanism; that lives in AlpacaAccount, not here.

    def _cancel_oco_sibling(self, filled_order) -> None:
        """When an OCO/TP/SL leg fills, cancel the sibling protective leg(s).

        A single ``OrderType.OCO`` leg has both TP+SL internally (no sibling). For the
        separate-TP + separate-SL case, the two legs share the same transaction and
        ``depends_on_order``; filling one cancels the other so the position closes once.
        """
        if filled_order.transaction_id is None or filled_order.depends_on_order is None:
            return
        terminal = OrderStatus.get_terminal_statuses()
        for o in self._orders_filtered(transaction_id=filled_order.transaction_id):
            if (
                o.id != filled_order.id
                and o.depends_on_order is not None
                and o.status not in terminal
                and o.status != OrderStatus.FILLED
            ):
                o.status = OrderStatus.CANCELED
                update_instance(o)

    def _order_to_trade(self, order, qty: float) -> Dict[str, Any]:
        """Map a filled ``TradingOrder`` row to the documented filled-trade dict shape.

        ``date`` is the SIMULATED fill bar (from ``_fill_dates``), NOT ``order.created_at``
        (which the DB stamps with wall-clock ``datetime.now()`` and would make the trade
        history non-deterministic run-to-run). Falls back to ``created_at`` only if a fill
        date was not recorded (e.g. an order that fills outside the engine loop in a unit
        test) so the field is never None for a filled order.
        """
        fill_date = self._fill_dates.get(order.id) if order.id is not None else None
        return {
            "symbol": order.symbol,
            "qty": abs(float(qty)),
            "side": order.side.value if order.side else None,
            "date": fill_date if fill_date is not None else order.created_at,
            "price": order.open_price,
        }
