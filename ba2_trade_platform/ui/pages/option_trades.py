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
from ...core.option_pnl_display import option_closed_pnl, option_transaction_pnl
from ...core.utils import get_account_instance_from_id, get_expert_options_for_ui
from ...logger import logger
from ..account_filter_context import get_selected_account_id
from ..components.LiveTradesTable import LiveTradesTable, LiveTradesTableConfig
from ..components.account_scope import scope_transactions_to_account

#: Columns whose sort is computed per row (money, DTE, leg count) and therefore sorted in
#: memory, exactly as the equity tab does for its P&L columns.
_COMPUTED_SORT_FIELDS = frozenset({
    'current_pnl_numeric', 'closed_pnl_numeric', 'value', 'expiry_display', 'leg_count',
})

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
    ):
        # Handlers are INJECTED from the page so the option tab reuses the existing
        # transaction-details popup, close dialog and edit dialog rather than growing a
        # second copy of each.
        self.on_view_details = on_view_details
        self.on_close_transaction = on_close_transaction
        self.on_edit_transaction = on_edit_transaction

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
                ).classes('w-48')

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

                ui.button('Refresh', icon='refresh', on_click=lambda: self._refresh()).props('outline')

            ui.label(
                'One row per option structure: the transaction is the intent (keyed on the '
                'underlying), its orders are the legs. P&L is priced off option premiums and '
                'scaled by the contract multiplier — never off the underlying quote.'
            ).classes('text-xs text-secondary-custom')

            self.container = ui.column().classes('w-full')
            with self.container:
                await self._render_table()

            ui.separator().classes('my-2')
            self._totals_row = ui.row().classes(
                'w-full justify-end items-center gap-6 px-4 py-3 bg-white/5 border-t border-white/10')
            self._refresh_totals()

    async def _render_table(self):
        self.table = LiveTradesTable(
            data_loader=self._data_loader,
            config=LiveTradesTableConfig(
                page_size=20,
                table_name='OptionTradesTable',
                show_global_filter=True,
                show_selection=True,
                dense=True,
                auto_refresh_interval=30,
            ),
            columns=LiveTradesTable.OPTION_TRANSACTION_COLUMNS,
            on_close=self._handle_close,
            on_edit=self._handle_edit,
            on_view_transaction_details=self._handle_view_details,
        )
        await self.table.render()

    # ---- data ----------------------------------------------------------------
    async def _data_loader(
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

            computed_sort = sort_by in _COMPUTED_SORT_FIELDS
            if computed_sort:
                results = list(session.exec(base.order_by(Transaction.created_at.desc())).all())
            else:
                order_column = {
                    'id': Transaction.id,
                    'symbol': Transaction.symbol,
                    'status': Transaction.status,
                    'quantity': Transaction.quantity,
                    'open_price': Transaction.open_price,
                    'created_at': Transaction.created_at,
                    'closed_at': Transaction.close_date,
                }.get(sort_by, Transaction.created_at)
                results = list(session.exec(
                    base.order_by(order_column.desc() if descending else order_column.asc())
                    .offset((page - 1) * page_size).limit(page_size)
                ).all())

            transactions = [row[0] for row in results]
            experts = {row[0].id: row[1] for row in results}
            rows = self._build_rows(transactions, experts, session)

            if computed_sort:
                rows.sort(key=lambda row: row.get(sort_by) if isinstance(row.get(sort_by), (int, float)) else 0,
                          reverse=descending)
                rows = rows[(page - 1) * page_size: page * page_size]

            return rows, total_count
        except Exception as exc:
            logger.error(f"[OPTION TABS] data load failed: {exc}", exc_info=True)
            return [], 0
        finally:
            session.close()

    def _build_rows(self, transactions, transaction_experts, session) -> List[Dict]:
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
            legs = len(option_orders) or len(orders)
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
            if is_open:
                # The representative order: a leg prices off its own contract, a multi-leg
                # parent off the structure's net premium (the seam decides).
                representative = option_orders[0] if option_orders else first_order
                if representative is not None and account_id:
                    account_inst = get_account_instance_from_id(account_id, session=session)
                    if account_inst is not None:
                        priced = option_transaction_pnl(account_inst, representative)
                        if priced.available:
                            current_pnl = priced
                        if len(option_orders) == 1:
                            quote = self._contract_quote(account_inst, option_orders[0])
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
                'current_pnl_numeric': current_pnl.percent if current_pnl and current_pnl.percent is not None else 0,
                'closed_pnl': _pnl_text(closed.amount, closed.percent) if closed else '—',
                'closed_pnl_numeric': closed.percent if closed and closed.percent is not None else 0,
                'status': getattr(txn.status, 'value', '') or '—',
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

        self._totals = totals
        # Repaint from the loader, like the equity tab does; a loader that runs outside a
        # UI slot must not take the page down over a totals strip.
        try:
            self._refresh_totals()
        except Exception as exc:
            logger.debug(f"[OPTION TABS] totals repaint skipped: {exc}")
        return rows

    @staticmethod
    def _contract_quote(account_inst, order) -> Optional[float]:
        """Current premium for a single-leg contract, or None. Never fabricated."""
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
            ui.label('TOTAL (open option structures):').classes('text-sm font-bold text-secondary-custom')
            ui.label(f"Cost: {_money(totals.get('cost', 0.0))}").classes('text-sm font-semibold')
            pnl = totals.get('pnl', 0.0)
            ui.label(f"Unrealized P/L: ${pnl:+,.2f}").classes(
                f'text-sm font-bold {"text-green-500" if pnl >= 0 else "text-red-500"}')
            if unpriced:
                ui.label(
                    f'({unpriced} unpriced: no quote or contract multiplier recorded — excluded, not counted as zero)'
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
