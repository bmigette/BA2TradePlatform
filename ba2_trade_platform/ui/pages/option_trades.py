"""The OPTIONS tab of Live Trades (spec 2026-09-20, decision 5).

A SEPARATE tab class rather than a refactor of ``LiveTradesTab``, deliberately: the equity
page is 2 300 lines of live trading UI and the requirement is that the Stocks tab stays
behaviourally identical. Nothing here is reached from that path.

What is different from the equity tab, and why:

* **The filter is ``Transaction.asset_class == OPTION``** -- the explicit, indexed field. The
  pre-``asset_class`` tell (``multiplier == 100``) is NOT used: it is a heuristic, and a
  mis-filed row would be listed as the wrong asset class instead of showing as unclassified.
* **The P&L is priced off option premiums and multiplied by the contract multiplier**
  (``core/option_pnl_display.py``), never off the underlying's price. The equity path's
  ``(close - open) * qty`` understates an option by 100x and its "current price" is the
  underlying's, which is not the premium.
* **Columns carry the terms**: underlying, strategy, expiry + DTE, legs, contracts, net
  premium, premium-based TP/SL (``LiveTradesTable.OPTION_TRANSACTION_COLUMNS``).
* **Unknown stays unknown**: a row whose quote or multiplier is missing shows no P/L and is
  counted in the totals strip as unpriced, rather than contributing a zero.

An option transaction is ONE row here, because that is what it is: ``Transaction`` is the
intent, keyed on the underlying, and the child ``TradingOrder`` rows are the legs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from nicegui import ui

from ...core.db import get_db
from ...core.option_pnl_display import (
    UNAVAILABLE_INCOMPLETE, open_structure_pnl, option_closed_pnl, quote_caching_account,
    unavailable_pnl,
)
from ...core.option_positions import opening_legs
from ...core.utils import get_account_instance_from_id, get_expert_options_for_ui
from ...logger import logger
from ..account_filter_context import get_selected_account_id
from ..components.LiveTradesTable import (
    LiveTradesTable, LiveTradesTableConfig, related_order_rows, transaction_status_color,
)
from ..components.account_scope import scope_transactions_to_account
from ..components.refresh_button import refresh_button

#: Columns whose sort is computed per row (money, DTE, leg count) and therefore sorted in
#: memory, exactly as the equity tab does for its P&L columns.
#: The totals pass covers the WHOLE filtered set so the numbers cannot change with
#: pagination or sort mode (review R9), but the work stays bounded: beyond this many rows the
#: strip says it is partial instead of quietly summing a subset.
_TOTALS_ROW_LIMIT = 500

_COMPUTED_SORT_FIELDS = frozenset({
    'current_pnl_numeric', 'closed_pnl_numeric', 'value', 'expiry_display', 'leg_count',
    'account_name',
})
_SORT_FIELDS = {column.name: column.field
                for column in LiveTradesTable.OPTION_TRANSACTION_COLUMNS if column.sortable}

_STATUS_MAP = None


def _status_map():
    global _STATUS_MAP
    if _STATUS_MAP is None:
        from ...core.types import TransactionStatus
        _STATUS_MAP = {
            'Open': TransactionStatus.OPENED,
            'Closed': TransactionStatus.CLOSED,
            'Closing': TransactionStatus.CLOSING,
            'Waiting': TransactionStatus.WAITING,
        }
    return _STATUS_MAP


def _money(value: Optional[float]) -> str:
    return '—' if value is None else f'${value:,.2f}'


def _row_sort_key(sort_by: str):
    """Compare computed row fields numerically or as text, never formatted P&L."""
    def key(row: Dict[str, Any]):
        value = row.get(sort_by)
        if value is None:
            return (1, 0.0, '')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (0, float(value), '')
        return (0, 0.0, str(value))
    return key


def _pnl_text(amount: Optional[float], percent: Optional[float]) -> str:
    if amount is None:
        return '—'
    if percent is None:
        return f'${amount:+,.2f}'
    return f'${amount:+,.2f} ({percent:+.1f}%)'


class OptionTradesTab:
    """Transactions where ``asset_class == OPTION``, with option-specific columns."""

    def __init__(
        self,
        on_view_details: Optional[Any] = None,
        on_close_transaction: Optional[Any] = None,
        on_edit_transaction: Optional[Any] = None,
        on_retry_close: Optional[Any] = None,
        on_recreate_tpsl: Optional[Any] = None,
        on_view_recommendation: Optional[Any] = None,
    ):
        # Handlers are INJECTED from the page so the option tab reuses the existing
        # dialogs rather than growing a second copy of each -- and so NO button in the
        # actions column is dead on this tab.
        self.on_view_details = on_view_details
        self.on_close_transaction = on_close_transaction
        self.on_edit_transaction = on_edit_transaction
        self.on_retry_close = on_retry_close
        self.on_recreate_tpsl = on_recreate_tpsl
        self.on_view_recommendation = on_view_recommendation
        #: One broker read per contract per load pass (review R10), and whether the totals
        #: pass hit its row cap (review R9) -- both are per-refresh state.
        self._quote_snapshot: Dict[str, Any] = {}
        self._totals_truncated = False

        self.table: Optional[LiveTradesTable] = None
        self.status_filter = None
        self.expert_filter = None
        self.symbol_filter = None
        self.strategy_filter = None
        self.expert_id_map: Dict[str, Any] = {}
        self.container = None
        self._totals_row = None
        self._totals: Dict[str, float] = {}

    # ---- filters -------------------------------------------------------------
    def _filter_state(self) -> Tuple[Any, ...]:
        status = tuple(sorted(self.status_filter.value or [])) if self.status_filter else ()
        expert = self.expert_filter.value if self.expert_filter else 'All'
        symbol = (self.symbol_filter.value or '') if self.symbol_filter else ''
        strategy = (self.strategy_filter.value or '') if self.strategy_filter else ''
        return (status, expert, symbol, strategy)

    def _refresh(self):
        if self.table:
            asyncio.create_task(self.table.refresh())

    # ---- render --------------------------------------------------------------
    async def render(self):
        with ui.column().classes('w-full gap-2'):
            with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                self.status_filter = ui.select(
                    label='Status Filter',
                    options=['Waiting', 'Open', 'Closing', 'Closed'],
                    value=['Waiting', 'Open', 'Closing'],
                    multiple=True,
                    on_change=lambda: self._refresh(),
                ).classes('w-56')

                resolved = get_expert_options_for_ui()
                expert_options, self.expert_id_map = (
                    resolved if isinstance(resolved, tuple) else (resolved, {}))

                self.expert_filter = ui.select(
                    label='Expert', options=expert_options, value='All',
                    on_change=lambda: self._refresh(),
                ).classes('w-48')

                self.symbol_filter = ui.input(
                    label='Underlying', placeholder='Filter by underlying...',
                    on_change=lambda: self._refresh(),
                ).props('stack-label').classes('w-40')

                self.strategy_filter = ui.input(
                    label='Strategy', placeholder='e.g. iron_condor',
                    on_change=lambda: self._refresh(),
                ).props('stack-label').classes('w-40')

                refresh_button(lambda: self._refresh())

            ui.label(
                'One row per option structure: the transaction is the intent (keyed on the '
                'underlying), its orders are the legs. P&L is priced off option premiums and '
                'scaled by the contract multiplier — never off the underlying quote.'
            ).classes('text-xs text-secondary-custom')

            self.container = ui.column().classes('w-full')
            with self.container:
                await self._render_table()

            # In the table's footer: right under the rows, above the pagination controls.
            with self.table.footer:
                self._totals_row = ui.row().classes(
                    'w-full justify-end items-center gap-6 px-4 py-3 bg-white/5 border-b border-white/10')
            self._refresh_totals()

    async def _render_table(self):
        self.table = LiveTradesTable(
            data_loader=self._data_loader,
            config=LiveTradesTableConfig(
                page_size=20,
                table_name='OptionTradesTable',
                show_global_filter=True,
                # The filter row's Refresh calls this same table's refresh().
                show_refresh=False,
                show_selection=True,
                dense=True,
                auto_refresh_interval=30,
            ),
            columns=LiveTradesTable.OPTION_TRANSACTION_COLUMNS,
            on_close=self._handle_close,
            on_edit=self._handle_edit,
            on_view_transaction_details=self._handle_view_details,
            on_retry_close=self._handle_retry_close,
            on_recreate_tpsl=self._handle_recreate_tpsl,
            on_view_recommendation=self._handle_view_recommendation,
        )
        await self.table.render()

    # ---- data ----------------------------------------------------------------
    async def _data_loader(
        self, page: int, page_size: int, filters: Dict[str, Any],
        sort_by: str, descending: bool,
    ) -> Tuple[List[Dict], int]:
        """Async entry point: the blocking reads happen OFF the event loop (review R10).

        ``_collect_rows`` opens a DB session and can call the broker once per contract; run
        on the event loop that would freeze every other UI callback for the duration. It goes
        to a worker thread, and the totals strip is repainted here, on the UI thread, once the
        work is done.
        """
        rows, total_count = await asyncio.to_thread(
            self._collect_rows, page, page_size, filters, sort_by, descending,
        )
        try:
            self._refresh_totals()
        except Exception as exc:  # a totals strip must never take the table down
            logger.debug(f"[OPTION TABS] totals repaint skipped: {exc}")
        return rows, total_count

    def _collect_rows(
        self, page: int, page_size: int, filters: Dict[str, Any],
        sort_by: str, descending: bool,
    ) -> Tuple[List[Dict], int]:
        from sqlmodel import select
        from sqlalchemy import func

        from ...core.models import AccountDefinition, ExpertInstance, Transaction, TradingOrder
        from ...core.types import AssetClass

        global_filter = filters.get('_global', '')
        session = get_db()
        try:
            base = select(Transaction, ExpertInstance).outerjoin(
                ExpertInstance, Transaction.expert_id == ExpertInstance.id
            ).outerjoin(
                AccountDefinition, ExpertInstance.account_id == AccountDefinition.id
            )
            base = scope_transactions_to_account(base, get_selected_account_id())

            # THE SPLIT. Explicit field only -- see the module docstring.
            base = base.where(Transaction.asset_class == AssetClass.OPTION)

            status_values = self.status_filter.value if self.status_filter else None
            if status_values:
                statuses = [_status_map()[s] for s in status_values if s in _status_map()]
                if statuses:
                    base = base.where(Transaction.status.in_(statuses))

            if self.expert_filter and self.expert_filter.value != 'All':
                expert_id = self.expert_id_map.get(self.expert_filter.value)
                if expert_id and expert_id != 'All':
                    base = base.where(Transaction.expert_id == expert_id)

            if self.symbol_filter and self.symbol_filter.value:
                base = base.where(Transaction.symbol.contains(self.symbol_filter.value.upper()))

            if self.strategy_filter and self.strategy_filter.value:
                base = base.where(
                    Transaction.option_strategy.contains(self.strategy_filter.value.strip().lower()))

            if global_filter:
                base = base.where(Transaction.symbol.contains(global_filter.upper()))

            total_count = session.exec(select(func.count()).select_from(base.subquery())).one()

            # ONE pass over the whole filtered set (bounded), so the totals cannot depend on
            # which page happened to be loaded -- the review's R9 defect was that merely
            # changing sort mode changed the displayed cost and P&L.
            self._quote_snapshot = {}
            totals_rows = list(session.exec(
                base.order_by(Transaction.created_at.desc()).limit(_TOTALS_ROW_LIMIT)
            ).all())
            self._build_rows(
                [row[0] for row in totals_rows],
                {row[0].id: row[1] for row in totals_rows},
                session,
            )
            self._totals_truncated = total_count > len(totals_rows)

            # BROWSING is a SEPARATE pass and is NOT capped by the totals limit (second review,
            # N3): sharing one fetch meant the 500-row cap also removed transactions from the
            # table, so the last advertised page came back empty and older rows were
            # unreachable. The quote cache above keeps the second pass cheap.
            # Quasar sends the column NAME, not its field (current_pnl vs
            # current_pnl_numeric). Resolve before choosing SQL pagination or a global sort.
            sort_field = _SORT_FIELDS.get(sort_by, sort_by or 'created_at')
            computed_sort = sort_field in _COMPUTED_SORT_FIELDS
            if computed_sort:
                # A computed column can only be ordered once the rows are built, so this pass
                # needs the whole filtered set (as it did before the totals work).
                page_rows = list(session.exec(
                    base.order_by(Transaction.created_at.desc(), Transaction.id.desc())).all())
            else:
                order_column = {
                    'id': Transaction.id,
                    'symbol': Transaction.symbol,
                    'option_strategy': Transaction.option_strategy,
                    'direction': Transaction.side,
                    'status': Transaction.status,
                    'quantity': Transaction.quantity,
                    'open_price': Transaction.open_price,
                    'created_at': Transaction.created_at,
                    'closed_at': Transaction.close_date,
                }.get(sort_field, Transaction.created_at)
                page_rows = list(session.exec(
                    base.order_by(
                        (order_column.desc() if descending else order_column.asc()).nulls_last(),
                        Transaction.id.desc(),
                    )
                    .offset((page - 1) * page_size).limit(page_size)
                ).all())

            rows = self._build_rows(
                [row[0] for row in page_rows], {row[0].id: row[1] for row in page_rows}, session,
                collect_totals=False,
            )
            if computed_sort:
                rows.sort(key=_row_sort_key(sort_field), reverse=descending)
                # Missing valuations are unknown, not zero; keep them last in both directions.
                rows.sort(key=lambda row: row.get(sort_field) in (None, '—'))
                rows = rows[(page - 1) * page_size: page * page_size]
            return rows, total_count
        except Exception as exc:
            logger.error(f"[OPTION TABS] data load failed: {exc}", exc_info=True)
            return [], 0
        finally:
            session.close()

    def _build_rows(self, transactions, transaction_experts, session, *,
                    collect_totals: bool = True) -> List[Dict]:
        """Build display rows. ``collect_totals=False`` leaves ``self._totals`` alone.

        The loader runs TWO passes: a bounded totals pass over the whole filtered set, then the
        page. The page pass must not overwrite the totals with its own page-sized sums.
        """
        from sqlmodel import select

        from ...core.models import AccountDefinition, TradingOrder
        from ...core.types import TransactionStatus

        today = date.today()
        rows: List[Dict] = []
        account_names: Dict[int, str] = {}
        totals = {'cost': 0.0, 'pnl': 0.0, 'unpriced': 0.0}

        for txn in transactions:
            orders = list(session.exec(
                select(TradingOrder).where(TradingOrder.transaction_id == txn.id)
                .order_by(TradingOrder.created_at)
            ).all())
            option_orders = [o for o in orders if getattr(o, 'contract_symbol', None)]
            # The ENTRY structure, not the order history: exits, cancels and unfilled legs
            # are not positions (review R2), and the count is what the Legs column means.
            # `leg_set.incomplete` means an executed opening leg could not be read, which
            # changes the pricing decision below (second review, N4).
            leg_set = opening_legs(txn, orders)
            legs = leg_set.count
            first_order = orders[0] if orders else None
            account_id = getattr(first_order, 'account_id', None)

            if account_id and account_id not in account_names:
                acc = session.get(AccountDefinition, account_id)
                if acc:
                    account_names[account_id] = acc.name

            multiplier = getattr(txn, 'multiplier', None)
            expiry = getattr(txn, 'expiry', None)
            dte = (expiry - today).days if isinstance(expiry, date) else None

            current_pnl = None
            current_price = None
            is_open = txn.status in (TransactionStatus.OPENED, TransactionStatus.CLOSING)
            if is_open and leg_set.incomplete:
                # Refuse rather than price the remainder: the seam would see a DIFFERENT
                # structure (second review, N4 -- a spread missing one leg's recorded premium
                # prices as a lone long call).
                current_pnl = unavailable_pnl(
                    f'{UNAVAILABLE_INCOMPLETE}: ' + '; '.join(leg_set.incomplete_reasons))
            elif is_open:
                if account_id:
                    account_inst = get_account_instance_from_id(account_id, session=session)
                    if account_inst is not None:
                        # One quote per (account, contract) for the whole refresh, shared by
                        # the pricing seam and the Current column (second review, N5).
                        account_inst = quote_caching_account(
                            account_inst, self._quote_snapshot, account_id)
                        # The one display rule, shared with the Floating P/L cards.
                        priced = open_structure_pnl(account_inst, txn, orders, leg_set=leg_set)
                        current_pnl = priced
                        if leg_set.count == 1:
                            quote = self._contract_quote(account_inst, leg_set.legs[0])
                            current_price = quote

            closed = None if is_open else option_closed_pnl(txn)

            cost = None
            if txn.open_price is not None and txn.quantity and multiplier:
                cost = abs(float(txn.open_price) * float(txn.quantity) * float(multiplier))

            if is_open:
                if cost is not None:
                    totals['cost'] += cost
                if current_pnl is not None and current_pnl.available:
                    totals['pnl'] += current_pnl.amount or 0.0
                else:
                    totals['unpriced'] += 1

            rows.append({
                'id': txn.id,
                'account_name': account_names.get(account_id, '—'),
                'symbol': txn.symbol,
                'option_strategy': getattr(txn, 'option_strategy', None) or '—',
                'expiry_display': (
                    '—' if expiry is None else f'{expiry.isoformat()}'
                    + (f' · {dte} DTE' if dte is not None else '')
                ),
                'leg_count': legs,
                'direction': 'LONG' if getattr(txn.side, 'value', '') == 'BUY' else 'SHORT',
                'quantity': txn.quantity,
                'open_price': txn.open_price,
                'current_price': current_price,
                'value': cost,
                'close_price': txn.close_price,
                'take_profit': txn.take_profit,
                'stop_loss': txn.stop_loss,
                'current_pnl': _pnl_text(current_pnl.amount, current_pnl.percent) if current_pnl else '—',
                # WHY a row has no P&L, when the reason is not "the broker had no quote": an
                # incompletely recorded structure must not read as a plain blank (N4).
                'pnl_reason': getattr(current_pnl, 'reason', None),
                'current_pnl_numeric': current_pnl.percent if current_pnl else None,
                'closed_pnl': _pnl_text(closed.amount, closed.percent) if closed else '—',
                'closed_pnl_numeric': closed.percent if closed else None,
                'status': getattr(txn.status, 'value', '') or '—',
                'status_color': transaction_status_color(txn.status),
                'orders': related_order_rows(orders),
                'order_count': len(orders),
                'created_at': txn.created_at.strftime('%Y-%m-%d %H:%M') if txn.created_at else '—',
                'closed_at': txn.close_date.strftime('%Y-%m-%d %H:%M') if txn.close_date else '—',
                'expert': (
                    f"{transaction_experts[txn.id].alias}-{transaction_experts[txn.id].id}"
                    if transaction_experts.get(txn.id) and transaction_experts[txn.id].alias
                    else (f'Expert-{txn.expert_id}' if txn.expert_id else '—')
                ),
                'legs_detail': [
                    {
                        'contract': o.contract_symbol, 'side': getattr(o.side, 'value', ''),
                        'qty': o.quantity, 'status': getattr(o.status, 'value', ''),
                        'premium': o.open_price,
                    }
                    for o in option_orders
                ],
            })

        if collect_totals:
            self._totals = totals
        # NOT repainted here: this now runs in a worker thread, and UI calls belong to the
        # loader's async wrapper (_data_loader) once the work is done.
        return rows

    def _contract_quote(self, account_inst, order) -> Optional[float]:
        """Current premium for a single-leg contract, or None. Never fabricated.

        ``account_inst`` is normally the quote-caching wrapper, so this read and the pricing
        seam share ONE broker call per (account, contract) per refresh (second review, N5: the
        old cache was keyed by contract alone, so two accounts holding the same contract shared
        one account's quote, and the seam's own reads were not covered at all).
        """
        try:
            quote = account_inst.get_option_quote(order.contract_symbol)
        except Exception as exc:
            logger.debug(f"[OPTION TABS] no quote for {order.contract_symbol}: {exc}")
            return None
        if quote is None:
            return None
        if getattr(order.side, 'value', '') == 'BUY':
            return quote.bid if quote.bid is not None else quote.last
        return quote.ask if quote.ask is not None else quote.last

    def _refresh_totals(self):
        if self._totals_row is None:
            return
        totals = self._totals or {}
        unpriced = int(totals.get('unpriced', 0))
        self._totals_row.clear()
        with self._totals_row:
            label = 'TOTAL (open option structures):'
            if getattr(self, '_totals_truncated', False):
                label = f'TOTAL (open option structures, first {_TOTALS_ROW_LIMIT} matching):'
            ui.label(label).classes('text-sm font-bold text-secondary-custom')
            ui.label(f"Cost: {_money(totals.get('cost', 0.0))}").classes('text-sm font-semibold')
            pnl = totals.get('pnl', 0.0)
            ui.label(f"Unrealized P/L: ${pnl:+,.2f}").classes(
                f'text-sm font-bold {"text-green-500" if pnl >= 0 else "text-red-500"}')
            if unpriced:
                ui.label(
                    f'({unpriced} unpriced: no quote, no recorded contract multiplier, or an '
                    f'incompletely recorded structure — excluded, not counted as zero)'
                ).classes('text-xs text-secondary-custom')

    # ---- handlers ------------------------------------------------------------
    def _handle_view_details(self, transaction_id: int):
        if self.on_view_details:
            self.on_view_details(transaction_id)

    def _handle_close(self, transaction_id: int):
        if self.on_close_transaction:
            self.on_close_transaction(transaction_id)

    def _handle_edit(self, transaction_id: int):
        if self.on_edit_transaction:
            self.on_edit_transaction(transaction_id)

    def _handle_retry_close(self, transaction_id: int):
        if self.on_retry_close:
            self.on_retry_close(transaction_id)

    def _handle_recreate_tpsl(self, transaction_id: int):
        if self.on_recreate_tpsl:
            self.on_recreate_tpsl(transaction_id)

    def _handle_view_recommendation(self, rec_id: int):
        if self.on_view_recommendation:
            self.on_view_recommendation(rec_id)
