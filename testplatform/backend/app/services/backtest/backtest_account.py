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
    DEFINED_RISK_LONG_STRATEGIES = frozenset(
        {"bull_call_spread", "bear_put_spread", "call_butterfly", "debit_spread"}
    )
    DEFINED_RISK_SHORT_STRATEGIES = frozenset(
        {"bear_call_spread", "iron_condor", "credit_spread"}
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
                px = bar["close"]
            elif is_defined_risk:
                # (2a) NO premium bar for a defined-risk leg on this bar -> mark at INTRINSIC (not
                # the stale entry premium / 0) so an open combo whose sparse cache lacks a bar this
                # tick is not understated (the offsetting leftover-long value is preserved).
                px = self._leg_intrinsic(lot.contract_symbol, gb)
                if px is None:
                    px = lot.avg_price
            else:
                px = lot.avg_price
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
        strike / option_type / underlying from its order; returns None if unresolvable (caller
        then falls back to the entry premium).
        """
        for o in self.get_orders():
            if getattr(o, "contract_symbol", None) != contract_symbol:
                continue
            if o.strike is None or o.option_type is None:
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
        return None

    @staticmethod
    def _defined_risk_width_per_structure(strategy: Optional[str], strikes) -> Optional[float]:
        """Defined-risk width PER STRUCTURE, PER SHARE, for a combo's strike set.

        This is the span both the mid-life MTM clamp and the expiry safety clamp scale by
        ``multiplier x structures``, so it must be the structure's TRUE defined risk:

          * ``iron_condor`` (4 strikes k1<k2<k3<k4): ``max(k2-k1, k4-k3)`` — the wider WING.
            The widest adjacent gap is usually the BODY ``k3-k2``, which is not risk (both
            short strikes sit inside it) and made the bound ~2x too loose.
          * 2-strike verticals (bull_call/bear_put/bear_call spread): the single gap.
          * ``call_butterfly`` (3 strikes k1<k2<k3): ``min(k2-k1, k3-k2)`` — the binding wing
            of a (possibly broken-wing) fly; equal wings unchanged.
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
            return min(gaps)
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
        """
        held = self._option_positions
        # parent order id -> (strategy, structure quantity, [opening strikes], multiplier)
        parent_info: Dict[int, Dict[str, Any]] = {}
        single_info: Dict[str, Dict[str, Any]] = {}
        # collect opening legs' strikes per parent + parent strategy/qty
        for o in self.get_orders():
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
        for o in self.get_orders():
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
            for o in self.get_orders():
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
        return contract_group, group_bounds

    def equity(self) -> float:
        """Net liquidating value = cash + mark-to-market of open positions."""
        return self._cash + self._open_positions_mtm()

    # ======================================================================
    # Maintenance margin + forced liquidation (broker-style, bounds equity)
    # ======================================================================
    #: Reg-T maintenance margin fraction for a SHORT stock position (~30% of notional).
    SHORT_STOCK_MAINTENANCE_FRACTION = 0.30

    def maintenance_margin_requirement(self) -> float:
        """Total maintenance-margin dollars this book must hold against its SHORT risk.

        The requirement is the sum of the (unbounded-risk) short positions' broker
        maintenance margins:

          * short OPTION legs -> ``naked_margin_per_contract(strike, spot)`` x contracts
            (Reg-T naked ~20% of notional less OTM, floored 10%) — the SAME model the entry
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
            strike, spot = self._lot_strike_and_spot(lot.contract_symbol)
            if strike is None:
                continue
            req += self.naked_margin_per_contract(strike, spot=spot) * abs(lot.qty)
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
        """
        for o in self.get_orders():
            if o.contract_symbol == contract_symbol and o.strike is not None:
                return o
        return None

    def _lot_strike_and_spot(self, contract_symbol: str):
        """(strike, underlying_spot) for a held option lot, resolved from its FILLED order.

        Returns (None, None) when the order/strike cannot be resolved (the caller then skips
        that lot's margin contribution rather than guessing).
        """
        o = self._lot_order(contract_symbol)
        if o is None:
            return None, None
        spot = None
        if o.underlying_symbol:
            spot = self._price.close_at(o.underlying_symbol)
            if spot is None:
                spot = self._price.close_asof(o.underlying_symbol)
        return float(o.strike), (float(spot) if spot is not None else None)

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
        if self.equity() >= self.maintenance_margin_requirement() and self.equity() >= 0:
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
        while True:
            if self.equity() >= self.maintenance_margin_requirement() and self.equity() >= 0:
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

        # Then unwind short STOCK if still breaching.
        while True:
            if self.equity() >= self.maintenance_margin_requirement() and self.equity() >= 0:
                break
            shorts = [p for p in self._positions.values() if p.qty < 0]
            if not shorts:
                break
            pos = max(shorts, key=lambda p: abs(p.qty))
            if not self._liquidate_stock_position(pos):
                break
            liquidated = True

        return liquidated

    def _liquidate_option_lot(self, lot: "_OptionLot") -> bool:
        """Buy back a SHORT option lot at the current premium close; book cash + close the txn."""
        bar = self._options.get_bar(lot.contract_symbol, self._as_of_date()) if self._options else None
        if bar and bar.get("close") is not None:
            premium = float(bar["close"])
        else:
            # No premium bar on the liquidation bar. The entry premium books the buyback at
            # break-even — understating the loss at exactly the moment a breach implies the
            # premium moved against the short. Use INTRINSIC, floored at the entry premium
            # (a forced buyback is never booked BELOW entry mid-blow-up); the entry premium
            # remains the last resort when strike/spot/right are unresolvable.
            premium = lot.avg_price
            o = self._lot_order(lot.contract_symbol)
            if o is not None and o.option_type is not None:
                spot = None
                if o.underlying_symbol:
                    spot = self._price.close_at(o.underlying_symbol)
                    if spot is None:
                        spot = self._price.close_asof(o.underlying_symbol)
                if spot is not None:
                    intrinsic = (
                        max(0.0, float(spot) - float(o.strike))
                        if o.option_type == OptionRight.CALL
                        else max(0.0, float(o.strike) - float(spot))
                    )
                    premium = max(intrinsic, lot.avg_price)
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
        for o in self.get_orders():
            if o.contract_symbol == lot.contract_symbol and o.strike is not None:
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
        return None

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
        self, symbol: str, qty: float, was_long: bool, px: float
    ) -> None:
        """Persist a synthetic FILLED closing order for a margin-call STOCK liquidation.

        BOOK-KEEPING only (the caller already moved cash + ledger). Linked to the symbol's
        OPENED equity transaction when one resolves — the entry order's side must match the
        liquidated direction — carrying ``depends_on_order`` so the sim-dated close is never
        mistaken for the entry by ``_entry_order_for_transaction`` (same guard as
        ``_record_option_expiry_close``). When no transaction resolves the order is persisted
        unlinked; a transaction is never invented.
        """
        from sqlmodel import select, Session

        want_side = OrderDirection.BUY if was_long else OrderDirection.SELL
        txn_id = None
        entry_id = None
        with Session(get_db().bind) as session:
            txns = list(
                session.exec(
                    select(Transaction).where(
                        Transaction.status == TransactionStatus.OPENED,
                        Transaction.symbol == symbol,
                    )
                ).all()
            )
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
            comment="margin_call_liquidation",
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
        """
        equity_value = self._open_positions_mtm()
        nlv = self._cash + equity_value
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
        """Current cash balance (the simulated cash ledger)."""
        return self._cash

    def get_account_info(self) -> Dict[str, Any]:
        """Account info dict; exposes ``.equity`` (read by _validate_position_size_limits)."""
        eq = self.equity()
        return _AttrDict(
            {
                "balance": self._cash,
                "cash": self._cash,
                "equity": eq,
                "buying_power": max(self._cash, 0.0),
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
        from sqlmodel import select, Session

        with Session(get_db().bind) as session:
            stmt = select(TradingOrder).where(TradingOrder.account_id == self.id)
            if status is not None and status != OrderStatus.ALL:
                stmt = stmt.where(TradingOrder.status == status)
            return list(session.exec(stmt).all())

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

        from sqlmodel import select, Session

        snapshot: Dict[str, List[tuple]] = {}
        with Session(get_db().bind) as session:
            txns = session.exec(
                select(Transaction)
                .where(Transaction.expert_id == expert_id)
                .where(Transaction.status == TransactionStatus.OPENED)
            ).all()
            # Build inside the session so attribute access is safe; get_current_open_qty opens its
            # own session (keyed by txn id) and is computed ONCE here, not per bar.
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
            from sqlmodel import select, Session

            with Session(get_db().bind) as session:
                self._order_cache = list(
                    session.exec(
                        select(TradingOrder).where(TradingOrder.account_id == self.id)
                    ).all()
                )
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
            from sqlmodel import select, Session

            with Session(get_db().bind) as session:
                self._active_order_cache = list(
                    session.exec(
                        select(TradingOrder).where(
                            TradingOrder.account_id == self.id,
                            TradingOrder.status.in_(OrderStatus.get_active_statuses()),
                        )
                    ).all()
                )
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
                from sqlmodel import select, Session

                with Session(get_db().bind) as session:
                    rows = session.exec(
                        select(TradingOrder).where(TradingOrder.account_id == self.id)
                    ).all()
                orders = [o for o in rows if o.status in sset]
        else:
            # Transaction-only (no status filter): fresh read for current persisted state. Push
            # transaction_id into SQL so this loads ONLY the (few) legs of this transaction, not
            # every order ever created — keeps the rare adjust/cancel path O(legs), not O(total).
            from sqlmodel import select, Session

            with Session(get_db().bind) as session:
                stmt = select(TradingOrder).where(TradingOrder.account_id == self.id)
                if transaction_id is not None:
                    stmt = stmt.where(TradingOrder.transaction_id == transaction_id)
                orders = list(session.exec(stmt).all())
            return orders
        if transaction_id is not None:
            orders = [o for o in orders if o.transaction_id == transaction_id]
        return orders

    def get_order(self, order_id: str) -> Any:
        """Look up an order by broker_order_id, then by numeric PK as a fallback."""
        from sqlmodel import select, Session

        with Session(get_db().bind) as session:
            row = session.exec(
                select(TradingOrder).where(TradingOrder.broker_order_id == str(order_id))
            ).first()
            if row is None and str(order_id).isdigit():
                row = session.get(TradingOrder, int(order_id))
            return row

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

        Returns whether ANY order filled this bar. The engine uses this to skip the
        transaction roll + bracket attach on no-fill bars (both are no-ops there), which is
        the common case on a fine fill clock (5-minute) and a large share of per-bar runtime.
        """
        as_of = self._price.now()
        self._activate_triggered_dependents()

        active = OrderStatus.get_active_statuses()
        # Re-read AFTER activation so newly-activated legs are seen this bar. SQL-filtered to
        # active statuses so terminal orders (the bulk after a while) aren't materialised.
        working = [
            o
            for o in self._orders_filtered(statuses=active)
            if o.status != OrderStatus.WAITING_TRIGGER
        ]
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
                self._cancel_oco_sibling(o)
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
            self._cancel_oco_sibling(o)
            filled = True
        return filled

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
        Returns None when no provider, no fill day, no premium bar, or no usable price.
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
        return self._slip(float(px), order.side == OrderDirection.BUY)

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
            self._cancel_oco_sibling(order)
            return False
        logger.error(
            "BACKTEST option cash-secured: LONG %s sized %g @ %.4f exceeds cash $%.2f -> "
            "capping to %d contract(s).",
            order.contract_symbol, qty, fill_px, self._cash, affordable,
        )
        order.quantity = float(affordable)
        return True

    def _fill_multi_leg_parent(self, parent, as_of: datetime) -> None:
        """ALL-OR-NONE fill of a multi-leg option parent (spread/straddle/...).

        On this bar, price every child leg off its OWN premium bar (each leg carries a
        ``contract_symbol`` so ``_option_fill_price`` works). If EVERY leg resolves to a
        price, fill all legs through the SAME per-leg path as single-leg fills
        (``_apply_option_fill`` -> per-contract lot + cash, scaled x multiplier), then mark
        the PARENT FILLED with ``open_price`` = net per-share debit = Σ(buy premium) -
        Σ(sell premium) (positive = debit, negative = credit). The parent moves NO cash (it
        already moved per leg). If ANY leg lacks a price, NOTHING fills this bar (retry next).
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

        # CASH-SECURED guard for DEBIT combos (defense-in-depth, the debit analog of the
        # margin-call liquidation for credit shorts). Options are sized from ANALYSIS-time quotes
        # but fill at the sparse cache's next-bar premiums, which can diverge sharply upward, so a
        # debit combo could otherwise buy far more debit than the account holds and drive cash
        # persistently negative. Compute the ACTUAL per-structure net debit from the FILL premiums
        # and cap the number of STRUCTURES that fill to what current cash can afford. All legs
        # scale together by the capped count (respecting each leg's ratio) so the combo stays
        # balanced/defined-risk. A CREDIT combo (net premium <= 0 -> cash inflow) is left alone
        # (its risk is bounded by the margin path, not cash spend).
        structures = abs(float(parent.quantity or 0.0))
        if structures <= 0:
            return
        commission = float(self._cfg["commission_per_trade"])
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

        net = 0.0
        for leg, px in priced:
            self._apply_option_fill(leg, px, as_of)  # reuse single-leg per-leg lot+cash math
            signed = px if leg.side == OrderDirection.BUY else -px
            net += signed

        parent.filled_qty = parent.quantity
        parent.open_price = net  # net per-share: +debit / -credit. No cash moved on the parent.
        parent.status = OrderStatus.FILLED
        update_instance(parent)
        if parent.id is not None:
            self._fill_dates[parent.id] = as_of

    def _activate_triggered_dependents(self) -> None:
        """Promote WAITING_TRIGGER legs to ACCEPTED once their parent hits the trigger.

        A leg created by ``adjust_tp``/``adjust_sl``/``adjust_tp_sl`` waits with
        ``depends_on_order`` = the entry order id and ``depends_order_status_trigger`` =
        FILLED. When the parent reaches that status the leg goes live (ACCEPTED) so the
        fill engine evaluates it. Legs with no parent / unmet trigger are left waiting.
        """
        waiting = self._orders_filtered(statuses=[OrderStatus.WAITING_TRIGGER])
        if not waiting:
            return
        # Look the parent up FRESH per waiting leg. The parent is usually the entry order, which
        # by the time a leg waits is typically FILLED (TERMINAL) — so it is NOT in the active
        # working set, and a cached ``_all_orders`` instance of it may be STALE (the fill engine
        # persists fills on the separate active instances). ``get_instance`` reads the current
        # persisted status. Only runs when waiting legs exist (a handful per run), so the per-leg
        # read is negligible.
        for leg in waiting:
            if leg.depends_on_order is None:
                continue
            parent = get_instance(TradingOrder, leg.depends_on_order)
            if parent is None:
                continue
            trigger = leg.depends_order_status_trigger or OrderStatus.FILLED
            if parent.status == trigger:
                leg.status = OrderStatus.ACCEPTED
                update_instance(leg)

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
        from sqlmodel import select, Session
        from ba2_common.core.types import TransactionStatus

        with Session(get_db().bind) as session:
            stmt = select(Transaction).where(
                Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSED])
            )
            if self._stamped_open_ids:
                stmt = stmt.where(Transaction.id.not_in(self._stamped_open_ids))
            return list(session.exec(stmt).all())

    def _closed_transactions(self) -> List[Transaction]:
        """CLOSED transactions not yet re-stamped (single-account backtest DB).

        Filters out already-stamped ids in SQL so the scan returns only the few freshly-closed
        rows each bar instead of every accumulated closed transaction.
        """
        from sqlmodel import select, Session
        from ba2_common.core.types import TransactionStatus

        with Session(get_db().bind) as session:
            stmt = select(Transaction).where(Transaction.status == TransactionStatus.CLOSED)
            if self._stamped_closed_ids:
                stmt = stmt.where(Transaction.id.not_in(self._stamped_closed_ids))
            return list(session.exec(stmt).all())

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
                comm = commission * 2.0
            else:
                # Still open at run end: mark-to-market at the last available price.
                size = entry_qty
                exit_px = self._price.close_at(opening.symbol)
                if exit_px is None:
                    exit_px = entry_px  # no closing price -> flat (counts as a near-zero trade)
                exit_dt = self._price.now()
                exit_reason = "open_at_end"
                comm = commission

            # Options quote premium PER SHARE but a contract controls ``multiplier`` (100)
            # shares, so realised option P&L scales by the contract multiplier. Equity entries
            # are not options -> mult stays 1 and the P&L is unchanged.
            mult = (
                (opening.multiplier or 1)
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
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "bars_held": bars_held,
                    "exit_reason": exit_reason,
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
    # method left). ``get_iv_rank`` / ``submit_option_order`` are concrete in the base mixin
    # and are NOT overridden.
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

        from sqlmodel import select, Session

        out: List[OptionPosition] = []
        with Session(get_db().bind) as session:
            txns = list(
                session.exec(
                    select(Transaction).where(Transaction.status == TransactionStatus.OPENED)
                ).all()
            )
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
        """
        fillable = OrderStatus.ACCEPTED  # matches the equity working status (_submit_order_impl)
        if leg_orders:
            for child in leg_orders:
                child.status = fillable
                update_instance(child)
            trading_order.status = fillable
            update_instance(trading_order)
        else:
            trading_order.status = fillable
            update_instance(trading_order)
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
        #    ITM assignment loss is real and PERSISTS.
        if share_side is not None and shares and share_price is not None:
            signed = float(shares) if share_side == OrderDirection.BUY else -float(shares)
            self._cash -= signed * float(share_price)  # buy debits, sell credits — at strike.
            self._update_position(position.underlying, signed, float(share_price))

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
            txn.close_price = 0.0
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
        structures = abs(float(entry.quantity)) if (entry is not None and entry.quantity) else 0.0
        if structures <= 0:
            structures = min(float(p.quantity) for p in positions)
        if structures <= 0:
            return None
        multiplier = float(positions[0].multiplier or 100)
        return width_per * multiplier * structures

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
        from sqlmodel import select, Session

        with Session(get_db().bind) as session:
            txns = list(
                session.exec(
                    select(Transaction).where(Transaction.status == TransactionStatus.OPENED)
                ).all()
            )
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
    ) -> Any:
        """Called by the INHERITED ``submit_order`` after validation/persistence.

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
        """Create/replace a TP leg for a transaction.

        TP for a LONG (BUY) transaction is a SELL_LIMIT above entry; for a SHORT (SELL)
        transaction it is a BUY_LIMIT below entry. The leg is created WAITING_TRIGGER on
        the entry order's FILL (mirrors AlpacaAccount). Returns False if the entry order
        cannot be found or the price is invalid.
        """
        if not new_tp_price or new_tp_price <= 0:
            return False
        entry = self._entry_order_for_transaction(transaction)
        if entry is None:
            return False
        # PRESERVE the existing SL: the protective bracket is a SINGLE OCO order, and
        # _replace_leg cancels ALL existing legs before creating the one passed in. Issuing a
        # TP-only leg here would silently DROP the stop-loss. If the transaction still carries a
        # stop_loss, re-issue a full OCO (new TP + existing SL) so moving one leg never nukes the
        # other. (Symmetric to adjust_sl preserving the TP — the bug behind inflated open_at_end
        # winners: a break-even-lock adjust_sl was dropping the take-profit.)
        existing_sl = getattr(transaction, "stop_loss", None)
        if existing_sl and existing_sl > 0:
            return self.adjust_tp_sl(transaction, new_tp_price=new_tp_price,
                                     new_sl_price=existing_sl, source=source)
        is_long = entry.side == OrderDirection.BUY
        leg_type = OrderType.SELL_LIMIT if is_long else OrderType.BUY_LIMIT
        self._replace_leg(transaction, entry, leg="TP", order_type=leg_type,
                          limit_price=new_tp_price, stop_price=None, source=source)
        transaction.take_profit = new_tp_price
        update_instance(transaction)
        return True

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """Create/replace an SL leg for a transaction.

        SL for a LONG (BUY) transaction is a SELL_STOP below entry; for a SHORT (SELL)
        transaction it is a BUY_STOP above entry. WAITING_TRIGGER on the entry's FILL.
        """
        if not new_sl_price or new_sl_price <= 0:
            return False
        entry = self._entry_order_for_transaction(transaction)
        if entry is None:
            return False
        # PRESERVE the existing TP: _replace_leg cancels ALL legs (the bracket is a single OCO),
        # so an SL-only leg here would DROP the take-profit. This was THE bug behind the inflated
        # open_at_end winners: a break-even-lock (adjust_sl) cancelled the OCO and re-issued an
        # SL-only stop, leaving the position with NO take-profit so it rode past +TP% unbounded.
        # If a take_profit is still set, re-issue a full OCO (existing TP + new SL).
        existing_tp = getattr(transaction, "take_profit", None)
        if existing_tp and existing_tp > 0:
            return self.adjust_tp_sl(transaction, new_tp_price=existing_tp,
                                     new_sl_price=new_sl_price, source=source)
        is_long = entry.side == OrderDirection.BUY
        leg_type = OrderType.SELL_STOP if is_long else OrderType.BUY_STOP
        self._replace_leg(transaction, entry, leg="SL", order_type=leg_type,
                          limit_price=None, stop_price=new_sl_price, source=source)
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
        """Set a paired TP+SL as an OCO bracket (one-cancels-other).

        When BOTH prices are given we create a single ``OrderType.OCO`` leg carrying both
        ``limit_price`` (TP) and ``stop_price`` (SL); the fill engine fills it at whichever
        side the bar crosses first and ``refresh_transactions`` recognises the close via
        the ``OrderType.OCO`` / ``"OCO-"`` marker. When only one price is given we fall
        back to a single TP or SL leg.
        """
        if new_tp_price is not None and new_sl_price is not None:
            if new_tp_price <= 0 or new_sl_price <= 0:
                return False
            entry = self._entry_order_for_transaction(transaction)
            if entry is None:
                return False
            self._replace_leg(transaction, entry, leg="TPSL", order_type=OrderType.OCO,
                              limit_price=new_tp_price, stop_price=new_sl_price, source=source)
            transaction.take_profit = new_tp_price
            transaction.stop_loss = new_sl_price
            update_instance(transaction)
            return True

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
        """Apply slippage in the worsening direction (buys up, sells down)."""
        bps = float(self._cfg["slippage_bps"]) / 10_000.0
        return px * (1.0 + bps) if side_is_buy else px * (1.0 - bps)

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
            trig_lo = float(order.limit_price)
        elif ot == OrderType.SELL_LIMIT:
            trig_hi = float(order.limit_price)
        elif ot == OrderType.BUY_STOP:
            trig_hi = float(order.stop_price)
        elif ot == OrderType.SELL_STOP:
            trig_lo = float(order.stop_price)
        elif ot == OrderType.OCO:
            # Both legs present: one side is a {bar.high >= X} test, the other {bar.low <= X}.
            # SELL OCO (closing long):  TP SELL_LIMIT (high>=limit), SL SELL_STOP (low<=stop).
            # BUY  OCO (closing short): SL BUY_STOP   (high>=stop),  TP BUY_LIMIT  (low<=limit).
            is_sell = order.side == OrderDirection.SELL
            tp = order.limit_price
            sl = order.stop_price
            if is_sell:
                if tp is not None:
                    trig_hi = float(tp)
                if sl is not None:
                    trig_lo = float(sl)
            else:
                if sl is not None:
                    trig_hi = float(sl)
                if tp is not None:
                    trig_lo = float(tp)
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
          * BUY_STOP          -> triggers iff bar.high >= stop; fills at stop +slippage.
          * SELL_STOP         -> triggers iff bar.low  <= stop; fills at stop -slippage.
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
            return order.limit_price if bar["low"] <= order.limit_price else None
        if ot == OrderType.SELL_LIMIT:
            return order.limit_price if bar["high"] >= order.limit_price else None

        if ot == OrderType.BUY_STOP:
            return self._slip(order.stop_price, True) if bar["high"] >= order.stop_price else None
        if ot == OrderType.SELL_STOP:
            return self._slip(order.stop_price, False) if bar["low"] <= order.stop_price else None

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
            tp_hit = tp is not None and bar["high"] >= tp
            if sl_hit:
                return self._slip(sl, False)   # SELL_STOP fills at stop -slippage
            if tp_hit:
                return tp                       # SELL_LIMIT fills at limit (no slippage)
            return None
        else:
            sl_hit = sl is not None and bar["high"] >= sl
            tp_hit = tp is not None and bar["low"] <= tp
            if sl_hit:
                return self._slip(sl, True)    # BUY_STOP fills at stop +slippage
            if tp_hit:
                return tp                       # BUY_LIMIT fills at limit
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
                    "BACKTEST cash-secured safeguard TRIPPED on %s: BUY %g @ %.4f (cost $%.2f) "
                    "exceeds cash $%.2f -> clamping to %d share(s). A sizing regression let the RM "
                    "over-size; the engine should bound deployment to available cash.",
                    order.symbol, qty, fill_px, signed * fill_px, self._cash, max(0, affordable),
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
        from sqlmodel import select, Session

        with Session(get_db().bind) as session:
            rows = session.exec(
                select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction.id,
                    TradingOrder.account_id == self.id,
                    TradingOrder.depends_on_order.is_(None),
                )
            ).all()
        if not rows:
            return None
        rows.sort(key=lambda o: (o.created_at or datetime.min.replace(tzinfo=timezone.utc), o.id or 0))
        return rows[0]

    def _existing_legs(self, transaction: Transaction) -> List[TradingOrder]:
        """All non-terminal dependent (TP/SL/OCO) legs for a transaction.

        SQL-scoped to this transaction's orders — scanning ALL account orders here (once per
        bracket, with thousands accumulated) was the dominant super-linear cost of a long run.
        """
        terminal = OrderStatus.get_terminal_statuses()
        return [
            o
            for o in self._orders_filtered(transaction_id=transaction.id)
            if o.depends_on_order is not None and o.status not in terminal
        ]

    def _replace_leg(
        self,
        transaction: Transaction,
        entry: TradingOrder,
        leg: str,
        order_type: OrderType,
        limit_price: Optional[float],
        stop_price: Optional[float],
        source: str,
    ) -> TradingOrder:
        """Cancel any existing protective leg(s) and create a fresh WAITING_TRIGGER leg.

        The new leg is the side that CLOSES the position (opposite the entry side), carries
        an ``OCO-`` comment marker + (for paired) ``OrderType.OCO`` so the inherited
        ``refresh_transactions`` recognises a TP/SL close, and depends on the entry order
        reaching FILLED before going live. Quantity is synced to the entry order's quantity.
        """
        # Cancel any existing non-terminal legs (single TP/SL replaced; OCO supersedes both).
        for old in self._existing_legs(transaction):
            old.status = OrderStatus.CANCELED
            update_instance(old)

        close_side = OrderDirection.SELL if entry.side == OrderDirection.BUY else OrderDirection.BUY
        ts = int(datetime.now(timezone.utc).timestamp())
        comment = f"{ts}-OCO-{leg}-[PARENT:{entry.id}/BROKER:{entry.broker_order_id}]"

        leg_order = TradingOrder(
            account_id=self.id,
            symbol=entry.symbol,
            quantity=entry.quantity,
            side=close_side,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            transaction_id=transaction.id,
            status=OrderStatus.WAITING_TRIGGER,
            depends_on_order=entry.id,
            depends_order_status_trigger=OrderStatus.FILLED,
            open_type=OrderOpenType.AUTOMATIC,
            broker_order_id=self._next_broker_id(),
            expert_recommendation_id=entry.expert_recommendation_id,
            comment=comment,
            created_at=datetime.now(timezone.utc),
        )
        add_instance(leg_order)
        self.invalidate_order_cache()  # a new leg was persisted -> fill engine must reload
        return leg_order

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
