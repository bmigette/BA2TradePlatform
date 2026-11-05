"""Floating P/L calculators (Overview "Floating P/L Per Account" / "Per Expert" cards), UI-free.

The cards and ``GET /stats/overview`` call these same classes (one function, two callers); the widgets in
``ui/components`` add rendering on top. What a row is allowed to say: measured, measured ZERO (the broker answered
``[]``), or unknown (``pl=None``) -- never a zero nobody measured. See ``PLRow``."""
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from ..core.db import get_db
from ..core.models import AccountDefinition, ExpertInstance, TradingOrder, Transaction
from ..core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ..core.utils import get_account_instance_from_id
from ..logger import logger
from ._positions_cache import read_positions
from .position_prices import broker_position_prices_from
from .account_scope import scope_transactions_to_account
from .stats import expert_ids_for_account


@dataclass
class PLRow:
    """One line of the card: a name, and what we could or could not measure for it.

    ``pl is None`` means UNMEASURABLE, and is never produced by a measurement that
    happened to come out at zero -- a flat account is ``pl=0.0``. Same contract for
    ``balance``.

    ``unpriced`` names the symbols this row HOLDS but the broker did not quote.
    Those legs are missing from ``pl``, so ``pl`` is real but incomplete: the row
    is rendered with :data:`PARTIAL_SUFFIX` rather than silently understated,
    which is what dropping them did.

    ``broker_bp`` is the broker's own REMAINING buying power -- what it will still
    let this account buy right now -- and is what the 'BP:' cell shows.
    ``tradable`` is the STOCK tradable balance, the balance times the account's
    effective margin factor: this platform's own ceiling on what its experts may
    deploy, which is a different question and rides on the cell's hover. Same
    None-is-unknown contract as ``balance``: neither is ever a zero nobody
    measured, and a margin figure that could not be read leaves ONLY that figure
    unknown.
    """
    name: str
    pl: Optional[float]
    balance: Optional[float] = None
    unpriced: Tuple[str, ...] = ()
    tradable: Optional[float] = None
    broker_bp: Optional[float] = None


def combine_measurements(
        reads: Sequence[Tuple[str, Optional[float]]]
) -> Tuple[Optional[float], List[str]]:
    """Total the legs that could be read, and NAME the ones that could not. Pure.

    THE RULE: an unreadable leg never silently disappears into the sum. Either the
    reader is told the total is partial and which account is missing from it, or --
    when nothing at all was readable -- there is no total to show.

    ``layout.combine_account_values`` -- the header badge's total -- now says the
    same thing by the same rule, and for the same reason: both surfaces show the
    accounts they are totalling, so "which one is missing" is on screen and
    discarding the readable part would throw away information the reader can see
    is real. Two totals over the same accounts must not be able to disagree.

    Nothing is rounded here; the legs are summed at full precision and formatted
    once, by the caller.

    Returns:
        ``(total, unreadable_names)``. ``total`` is ``None`` iff NOTHING was
        readable (which includes an empty input: no total, as opposed to a total
        of nothing). ``unreadable_names`` is what the total excludes, in input
        order, and is non-empty exactly when something was dropped.
    """
    unreadable = [name for name, value in reads if value is None]
    readable = [value for _, value in reads if value is not None]
    if not readable:
        return None, unreadable
    return float(sum(readable)), unreadable



class FloatingPLCalculator:
    """Base of the floating P/L calculators.

    Subclasses define ``_scope_query``, ``_get_extra_filters``, ``_group_transactions`` and (optionally)
    ``_seed_rows``; see the individual hooks below. ``_scope_query`` is the seam that keeps 'Per Expert' (scoped by
    expert id) and 'Per Account' (scoped by account) honest; ``_seed_rows`` makes an account-level card list every
    account even with nothing open."""

    # When True the calculator also reads each account's broker balance, tradable balance and buying power
    # (only meaningful for the per-account view; per-expert balance is not a broker concept).
    _show_balance: bool = False

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def _get_extra_filters(self) -> list:
        """Return additional where-clause expressions to apply to the transaction query.

        Default: no extra filters.
        """
        return []

    def _scope_query(self, query, selected_account_id: Optional[int],
                     account_expert_ids: Optional[List[int]]):
        """Narrow *query* to the transactions this widget is about.

        Args:
            query: the open/waiting ``Transaction`` select built by the base.
            selected_account_id: the header's account filter, ``None`` for "All".
            account_expert_ids: that account's expert ids -- ``None`` for "All",
                and ``[]`` for an account that HAS NO EXPERTS (which is not the
                same thing as an account with no data).

        Returns:
            The narrowed query, or ``None`` to mean "nothing can match" so the
            caller can skip the round trip entirely.
        """
        raise NotImplementedError

    def _seed_rows(self, selected_account_id: Optional[int],
                   session: Session) -> Dict[int, str]:
        """``{account_id: display_name}`` that must appear even with no transactions.

        Default: nothing. A widget whose rows ARE its subjects (the per-account
        card) overrides this so a subject with nothing open still gets measured
        and drawn, instead of vanishing.

        Deliberately NOT wrapped in a try/except: if the accounts cannot be listed
        we do not know what to draw, and the caller's error state ('❌ Error
        calculating P/L') is the honest answer. Swallowing it here would render
        :data:`NO_ACCOUNTS_TEXT` -- a confident claim that there are none.
        """
        return {}

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        """Group *transactions* by account_id for bulk price fetching.

        Must return ``{account_id: [(transaction, display_name), ...]}``.
        *display_name* is the label shown in the UI (e.g. account name or expert alias).

        Subclasses override this to implement their own grouping logic.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Core P/L calculation (shared)
    # ------------------------------------------------------------------

    def _calculate_pl_sync(
        self,
        selected_account_id: Optional[int],
        account_expert_ids: Optional[List[int]],
    ) -> List[PLRow]:
        """Synchronous P/L calculation (runs in thread pool to avoid blocking).

        Uses bulk price fetching per broker account.

        Args:
            selected_account_id: The selected account ID from filter, or None for all.
            account_expert_ids: List of expert IDs belonging to selected account, or None for all.

        Returns:
            One :class:`PLRow` per display name, in no particular order.
        """
        session = get_db()
        try:
            # Build query for open transactions
            query = (
                select(Transaction)
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            )

            # Subclass-specific filters (e.g. expert_id IS NOT NULL)
            for clause in self._get_extra_filters():
                query = query.where(clause)

            # Apply the header's account filter the way THIS widget means it.
            query = self._scope_query(query, selected_account_id, account_expert_ids)

            transactions = session.exec(query).all() if query is not None else []

            # Group transactions by account for bulk price fetching
            account_transactions = self._group_transactions(transactions, session)

            # ...then add every account that MUST be shown regardless. setdefault,
            # not assignment: an account that does have transactions keeps them, or
            # it would be measured as flat and reported at a confident $0.00.
            seeds = self._seed_rows(selected_account_id, session)
            for account_id in seeds:
                account_transactions.setdefault(account_id, [])

            rows: List[PLRow] = []
            for account_id, trans_list in account_transactions.items():
                rows.extend(self._rows_for_account(
                    account_id, trans_list, seeds.get(account_id), session))
            return rows

        finally:
            session.close()

    def _read_money(self, account, account_id: int
                    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """``(balance, tradable, broker_bp)``, each ``None`` when unreadable, never zero.

        ONE copy, called by both row-building paths. It was two, and a figure added
        to only one of them is a card that tells the truth about half its accounts:
        the manual path and the expert-driven path draw into the same columns.

        WHY EACH FIGURE IS TRIED, AND SWALLOWED, SEPARATELY. A failed margin read
        leaves ONLY that figure unknown -- the balance is the headline, and
        blanking it because a multiplier was unavailable throws away the number
        that WAS read. Same split, for the same reason, as the header's
        ``layout._read_account_figures``.
        """
        if not self._show_balance:
            # Per-EXPERT: an expert has no broker relationship, so not one of these
            # three figures exists for it. Not fetched at all rather than fetched
            # and hidden -- this runs once per account on every render.
            return None, None, None

        balance = tradable = broker_bp = None
        try:
            bal = account.get_balance()
            # ``is not None``, never truthiness: an account really worth $0.00
            # must print $0.00, not 'unknown'.
            balance = float(bal) if bal is not None else None
            if bal is None:
                logger.warning(f"Balance unavailable for account {account_id}; "
                               f"showing it as unknown rather than as zero")
        except Exception as e:
            logger.error(f"Could not fetch balance for account {account_id}: {e}",
                         exc_info=True)

        # ASKED BEFORE CALLING, not caught after. "Does this class have the
        # accessor?" and "did the accessor fail?" are different diagnoses with
        # different log levels, and an ``except AttributeError`` cannot tell them
        # apart: an AttributeError raised INSIDE a working accessor (a broker SDK
        # returning None where an object was expected, say) would be filed as
        # "this class was never brought under margin" and send the reader hunting
        # for a missing method that is right there.
        # ``getattr`` with a default swallows an AttributeError raised INSIDE a property
        # getter, so the accessors must stay plain methods -- ``callable`` pins that.
        fn = getattr(account, 'get_tradable_balance', None)
        if fn is None or not callable(fn):
            # NOT an unknown, and so NOT a warning: every account this platform
            # ships implements the accessor, so one that does not is a class that
            # was never brought under margin -- a defect in the code, which a
            # WARNING beside the broker's own "no multiplier" would bury.
            logger.error(f"Account {account_id} ({type(account).__name__}) does not "
                         f"implement get_tradable_balance -- a defect, not an "
                         f"unknown")
        else:
            try:
                tradable = float(fn())
            except Exception as e:
                # WARNING: with a broker that publishes no multiplier, the accessor
                # raising IS the documented shape of "unknown", and the cell already
                # says so. Logged all the same -- a BP that quietly stays missing is
                # one nobody ever investigates.
                logger.warning(f"Tradable balance unavailable for account {account_id}: {e}")

        try:
            broker_bp = account.get_account_snapshot().buying_power
        except Exception as e:
            logger.warning(f"Broker buying power unavailable for account {account_id}: {e}")

        return balance, tradable, broker_bp

    def _rows_for_account(
        self, account_id: int, trans_list: List[Tuple[Transaction, str]],
        seed_name: Optional[str], session: Session,
    ) -> List[PLRow]:
        """Measure one broker account, and return the row(s) it is responsible for.

        Every exit from here produces a row for every name. There is no path that
        returns nothing: "we could not read this account" is a state the card
        displays, not a reason to leave it out.
        """
        # Ordered, de-duplicated: a group can carry several experts, and dict keys
        # preserve insertion order so the card is stable between renders.
        names = list(dict.fromkeys(name for _, name in trans_list))
        if not names and seed_name is not None:
            names = [seed_name]
        if not names:
            return []

        account = get_account_instance_from_id(account_id, session=session)
        if account is None:
            # Named explicitly rather than left to blow up on None.get_positions():
            # both end at the same 'P/L unknown', but the AttributeError sends
            # whoever reads the log hunting a broker outage that never happened.
            logger.error(f"Could not build an account instance for account {account_id}; "
                         f"its floating P/L is unknown, not zero")
            return [PLRow(name=name, pl=None) for name in names]

        # Read BEFORE the position book, and carried into every row below, the
        # error rows included: a failed position fetch costs the P/L and nothing
        # else, and a row with no money on it at all reads as an unreachable
        # account when in fact only its book was.
        balance, tradable, broker_bp = self._read_money(account, account_id)

        try:
            broker_positions = read_positions(account, account_id)
        except Exception as e:
            logger.error(f"get_positions() raised for account {account_id}: {e}",
                         exc_info=True)
            return [PLRow(name=name, pl=None, balance=balance, tradable=tradable,
                          broker_bp=broker_bp) for name in names]

        # TRI-STATE (see ReadOnlyAccountInterface.get_positions): None is a FETCH
        # FAILURE, [] is a genuinely flat account. The old ``if broker_positions:``
        # folded them together and then dropped the row entirely, so a broker outage
        # and an empty book were both reported as "this account does not exist".
        if broker_positions is None:
            logger.error(f"get_positions() failed for account {account_id}; its "
                         f"floating P/L is unknown, not zero")
            return [PLRow(name=name, pl=None, balance=balance, tradable=tradable,
                          broker_bp=broker_bp) for name in names]

        # An unpriced position is left OUT of the map so it surfaces as unpriced below,
        # rather than being coerced into a zero price. Same broker price the Live Trades
        # rows use (``position_prices``), so the card and the table agree.
        prices: Dict[str, float] = broker_position_prices_from(broker_positions)

        pl_by_name: Dict[str, float] = {name: 0.0 for name in names}
        unpriced_by_name: Dict[str, List[str]] = {name: [] for name in names}
        # One quote per option contract for this whole refresh.
        option_quotes: Dict = {}

        for trans, display_name in trans_list:
            try:
                orders = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .order_by(TradingOrder.created_at)
                ).all()
                if any(getattr(order, 'contract_symbol', None) for order in orders):
                    measured = self._option_transaction_pl(
                        trans, orders, account, account_id, option_quotes, prices)
                else:
                    measured = self._transaction_pl(trans, prices, session)
            except Exception as e:
                logger.error(f"Error calculating P/L for transaction {trans.id}: {e}",
                             exc_info=True)
                if trans.symbol not in unpriced_by_name[display_name]:
                    unpriced_by_name[display_name].append(trans.symbol)
                continue

            if measured is None:
                # HELD but not quoted. Not zero: the leg is missing, and the row
                # says so instead of quietly understating the account.
                if trans.symbol not in unpriced_by_name[display_name]:
                    unpriced_by_name[display_name].append(trans.symbol)
                continue
            pl_by_name[display_name] += measured

        return [PLRow(name=name, pl=pl_by_name[name], balance=balance,
                      unpriced=tuple(unpriced_by_name[name]),
                      tradable=tradable, broker_bp=broker_bp)
                for name in names]

    @staticmethod
    def _option_transaction_pl(trans: Transaction, orders, account, account_id,
                               quotes: Dict, marks: Dict[str, float]) -> Optional[float]:
        """An OPTION transaction's floating P/L, or ``None`` if it holds but is unquoted.

        Through ``open_structure_pnl`` -- the Options tab's own rule (option premium, contract
        multiplier, spreads at their net premium) -- never the equity formula below, which
        looks the transaction's UNDERLYING up in the broker's book: an option book holds the
        contract, so every structure read "no broker price for GILD". A structure whose entry
        has not executed holds nothing and contributes a measured zero, like a resting equity
        order.
        """
        from ..core.option_pnl_display import (
            UNAVAILABLE_FLAT, UNAVAILABLE_NO_FILLS, open_structure_pnl, quote_caching_account,
        )
        # Each held contract at the BROKER's position price (``marks``) -- what the equity
        # path and the positions table use -- not at a bid/ask liquidation quote, which on
        # the indicative feed put the cards $1,400 away from the table.
        priced = open_structure_pnl(quote_caching_account(account, quotes, account_id, marks),
                                    trans, orders)
        if priced.available:
            return priced.amount
        if priced.reason in (UNAVAILABLE_NO_FILLS, UNAVAILABLE_FLAT):
            return 0.0
        logger.warning(f"Option transaction {trans.id} ({trans.symbol}) not priced: {priced.reason}")
        return None

    def _transaction_pl(self, trans: Transaction, prices: Dict[str, float],
                        session: Session) -> Optional[float]:
        """This transaction's floating P/L, or ``None`` if it HOLDS but is unquoted.

        Returns ``0.0`` -- a measurement -- for a transaction that holds nothing:
        a WAITING order that has not filled has no position, so it contributes
        nothing and there is nothing missing. That case is checked BEFORE the price
        lookup on purpose; asking for a quote first (what this used to do) made
        every resting order look like a position the broker had failed to quote.
        """
        all_orders = session.exec(
            select(TradingOrder)
            .where(TradingOrder.transaction_id == trans.id)
        ).all()

        # Get all FILLED orders (any type that affects position)
        # Exclude: OCO, OTO orders (they are TP/SL brackets, not position-affecting until triggered)
        filled_orders = [
            o for o in all_orders
            if o.status in OrderStatus.get_executed_statuses()
            and o.order_type not in [OrderType.OCO, OrderType.OTO]
            and o.filled_qty and o.filled_qty > 0
        ]

        # Calculate net position and weighted average cost
        total_buy_cost = 0.0
        total_buy_qty = 0.0
        total_sell_cost = 0.0
        total_sell_qty = 0.0

        for order in filled_orders:
            if not order.open_price or not order.filled_qty:
                continue

            if order.side == OrderDirection.BUY:
                total_buy_cost += order.filled_qty * order.open_price
                total_buy_qty += order.filled_qty
            elif order.side == OrderDirection.SELL:
                total_sell_cost += order.filled_qty * order.open_price
                total_sell_qty += order.filled_qty

        # Net filled quantity = buys - sells
        net_filled_qty = total_buy_qty - total_sell_qty

        if abs(net_filled_qty) < 0.01:  # No net position: nothing held, nothing missing
            return 0.0

        # Get position side from transaction.side field
        # BUY = LONG position, SELL = SHORT position
        position_direction = trans.side

        # Calculate weighted average entry price based on position direction
        if position_direction == OrderDirection.BUY:
            # Long position: entry price is avg BUY price
            if total_buy_qty < 0.01:
                return 0.0
            avg_price = total_buy_cost / total_buy_qty
        else:
            # Short position: entry price is avg SELL price
            if total_sell_qty < 0.01:
                return 0.0
            avg_price = total_sell_cost / total_sell_qty

        current_price = prices.get(trans.symbol)
        if current_price is None:
            return None

        # Debug: Log when net filled qty differs from transaction qty
        if abs(net_filled_qty - abs(trans.quantity)) > 0.01:
            logger.debug(
                f"Transaction {trans.id}: net_filled_qty={net_filled_qty:.2f} "
                f"(buys={total_buy_qty:.2f} - sells={total_sell_qty:.2f}), "
                f"transaction.quantity={trans.quantity}"
            )

        # Use transaction.quantity as source of truth for current position
        # Calculate P/L: (current_price - avg_price) * position_quantity
        pl = (current_price - avg_price) * trans.quantity
        if position_direction == OrderDirection.SELL:
            pl = -pl  # Invert for short positions
        return pl


class PerAccountPL(FloatingPLCalculator):
    """Floating P/L per account."""

    _show_balance = True

    def _is_manual_account(self, account_id: int, session: Session) -> bool:
        """True when NO ``ExpertInstance`` trades this account -- a hand-managed book.

        TastyTrade is the motivating case: 25 real, broker-held positions and a real
        +$215 of floating P/L, but ZERO ``Transaction``/``TradingOrder`` rows anywhere
        in the platform, because nothing on that account was ever opened through it.
        Reconstructing P/L from local fills (what ``_rows_for_account`` does, and must
        keep doing for an expert-driven account) answers "what has this platform
        recorded", which is the wrong question for a book the platform never wrote
        to -- it silently measures a real account at $0.00.

        The fix applies to EVERY manual account, not only empty ones: a manual
        account's local order rows (if any exist -- a user CAN place a trade by hand
        through the platform) are at best a partial view of what the broker actually
        holds, while ``get_positions()``'s own ``unrealized_pl`` per position is the
        broker's real, complete number. See ``_rows_for_manual_account``.
        """
        return session.exec(
            select(ExpertInstance.id).where(ExpertInstance.account_id == account_id).limit(1)
        ).first() is None

    def _rows_for_account(
        self, account_id: int, trans_list: List[Tuple[Transaction, str]],
        seed_name: Optional[str], session: Session,
    ) -> List[PLRow]:
        if self._is_manual_account(account_id, session):
            return self._rows_for_manual_account(account_id, trans_list, seed_name, session)
        return super()._rows_for_account(account_id, trans_list, seed_name, session)

    def _rows_for_manual_account(
        self, account_id: int, trans_list: List[Tuple[Transaction, str]],
        seed_name: Optional[str], session: Session,
    ) -> List[PLRow]:
        """A manual account's row, priced from the BROKER's own per-position P/L.

        Exactly one row: a manual account is scoped by account (never split by
        expert), so every name in ``trans_list`` -- if there even are any -- is the
        same account name ``_seed_rows`` already seeded.
        """
        names = list(dict.fromkeys(name for _, name in trans_list))
        if not names and seed_name is not None:
            names = [seed_name]
        if not names:
            return []
        name = names[0]

        account = get_account_instance_from_id(account_id, session=session)
        if account is None:
            logger.error(f"Could not build an account instance for account {account_id}; "
                         f"its floating P/L is unknown, not zero")
            return [PLRow(name=name, pl=None)]

        # The same read as the expert-driven path, from the same helper: the two
        # paths feed the same columns and must not be able to disagree about them.
        balance, tradable, broker_bp = self._read_money(account, account_id)

        # THE BROKER'S OWN NUMBER FIRST, where it publishes one that beats a per-position
        # sum. On TastyTrade the per-position figure has to be derived from `mark_price`,
        # the bid/ask MIDPOINT -- and on a book of thin ETFs the midpoint is not where the
        # broker marks: one position quoted 22.55/37.99 with no volume that day sat ~20%
        # off and moved the account total by $19 on its own (measured 2026-09-17, $66.91
        # across 75 positions, against a cost basis that agreed to the cent). Default is
        # None, so every other broker keeps summing per-position as before.
        try:
            broker_pl = account.get_broker_floating_pl()
        except Exception as e:
            logger.error(f"get_broker_floating_pl() raised for account {account_id}: {e}",
                         exc_info=True)
            broker_pl = None
        if broker_pl is not None:
            return [PLRow(name=name, pl=float(broker_pl), balance=balance,
                          tradable=tradable, broker_bp=broker_bp)]

        try:
            broker_positions = read_positions(account, account_id)
        except Exception as e:
            logger.error(f"get_positions() raised for account {account_id}: {e}",
                         exc_info=True)
            return [PLRow(name=name, pl=None, balance=balance, tradable=tradable,
                          broker_bp=broker_bp)]

        # TRI-STATE, same rule as the base: None is a fetch failure, [] is a
        # genuinely flat (or genuinely empty) account.
        if broker_positions is None:
            logger.error(f"get_positions() failed for account {account_id}; its "
                         f"floating P/L is unknown, not zero")
            return [PLRow(name=name, pl=None, balance=balance, tradable=tradable,
                          broker_bp=broker_bp)]

        total = 0.0
        unpriced: List[str] = []
        for pos in broker_positions:
            pos_dict = pos if isinstance(pos, dict) else dict(pos)
            symbol = pos_dict.get('symbol')
            pl = pos_dict.get('unrealized_pl')
            # A position the broker returned but published no unrealized_pl for is
            # left OUT of the sum, same rule as an unpriced leg in the base path --
            # it must not be coerced into contributing zero.
            if pl is None:
                if symbol and symbol not in unpriced:
                    unpriced.append(symbol)
                continue
            total += float(pl)

        return [PLRow(name=name, pl=total, balance=balance,
                      unpriced=tuple(unpriced), tradable=tradable,
                      broker_bp=broker_bp)]

    def _scope_query(self, query, selected_account_id: Optional[int],
                     account_expert_ids: Optional[List[int]]):
        """Scope by ACCOUNT. ``account_expert_ids`` is deliberately ignored.

        Every open position in the account counts towards the account's floating
        P/L, whoever opened it -- an expert, the Smart Risk Manager, or the user by
        hand. There is no "nothing can match" answer here: an account with no
        transactions still gets a row, seeded by ``_seed_rows``.
        """
        return scope_transactions_to_account(query, selected_account_id)

    def _seed_rows(self, selected_account_id: Optional[int],
                   session: Session) -> Dict[int, str]:
        """Every account the header is showing -- the card's rows ARE the accounts.

        This is the list the card is about, so it comes from ``AccountDefinition``
        and from nothing else. Deriving it from the open transactions (what the
        card used to do) drops any account that is flat; deriving it from the
        experts drops any account that is manual. TastyTrade is both, which is why
        it had no row at all -- and 'no row' is not one of the three things this
        card is allowed to say about an account.

        Honours the dropdown exactly as ``scope_transactions_to_account`` does:
        ``None`` is "All" and is the only value that widens the list.
        """
        query = select(AccountDefinition)
        if selected_account_id is not None:
            query = query.where(AccountDefinition.id == selected_account_id)
        return {account.id: account.name for account in session.exec(query).all()}

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        # No account filter here: ``_scope_query`` already applied it IN SQL, in one
        # place. Re-deriving it here also meant calling get_selected_account_id()
        # from inside the executor thread, where app.storage.user does not exist --
        # the exact thing _load_data_async captures the filter up-front to avoid.
        account_transactions: Dict[int, List[Tuple[Transaction, str]]] = {}

        for trans in transactions:
            try:
                first_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .limit(1)
                ).first()

                if not first_order or not first_order.account_id:
                    continue

                account_def = session.get(AccountDefinition, first_order.account_id)
                if not account_def:
                    continue

                account_id = first_order.account_id
                if account_id not in account_transactions:
                    account_transactions[account_id] = []
                account_transactions[account_id].append((trans, account_def.name))

            except Exception as e:
                logger.error(f"Error grouping transaction {trans.id}: {e}", exc_info=True)
                continue

        return account_transactions



class PerExpertPL(FloatingPLCalculator):
    """Floating P/L per expert (only transactions with an expert attribution)."""

    _title = '📊 Floating P/L Per Expert'

    def _get_extra_filters(self) -> list:
        """Only include transactions that have an expert attribution."""
        return [Transaction.expert_id.isnot(None)]

    def _scope_query(self, query, selected_account_id: Optional[int],
                     account_expert_ids: Optional[List[int]]):
        """Scope by EXPERT -- this widget really is per-expert.

        An account with no experts has nothing to say here, and ``[]`` must mean
        exactly that: NO ROWS. It must never widen to "all experts" (that would put
        every other account's experts under this account's name) and must never
        fall back to the account (that would make this widget a duplicate of
        'Floating P/L Per Account').
        """
        if account_expert_ids is None:
            return query
        if not account_expert_ids:
            return None
        return query.where(Transaction.expert_id.in_(account_expert_ids))

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        logger.debug(f"FloatingPLPerExpertWidget: Found {len(transactions)} open/waiting transactions with expert_id")

        account_transactions: Dict[int, List[Tuple[Transaction, str]]] = {}

        for trans in transactions:
            try:
                # Get expert info
                expert = session.get(ExpertInstance, trans.expert_id)
                if not expert:
                    logger.warning(f"Transaction {trans.id} (symbol={trans.symbol}, expert_id={trans.expert_id}) expert not found in database")
                    continue

                expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                logger.debug(f"Processing transaction {trans.id} (symbol={trans.symbol}, status={trans.status.value}, expert={expert_name})")

                # Get account ID from first order
                first_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .limit(1)
                ).first()

                if not first_order or not first_order.account_id:
                    logger.warning(f"Transaction {trans.id} (symbol={trans.symbol}, expert={expert_name}) has no orders or account_id")
                    continue

                account_id = first_order.account_id

                if account_id not in account_transactions:
                    account_transactions[account_id] = []
                account_transactions[account_id].append((trans, expert_name))

            except Exception as e:
                logger.error(f"Error grouping transaction {trans.id}: {e}", exc_info=True)
                continue

        return account_transactions



def floating_pl_per_account(account_id: Optional[int]) -> List[PLRow]:
    """One ``PLRow`` per account in scope (``None`` = all)."""
    return PerAccountPL()._calculate_pl_sync(account_id, expert_ids_for_account(account_id))


def floating_pl_per_expert(account_id: Optional[int]) -> List[PLRow]:
    """One ``PLRow`` per expert with an open position (``None`` = all accounts)."""
    return PerExpertPL()._calculate_pl_sync(account_id, expert_ids_for_account(account_id))
