"""
Trade Performance Analytics Page

This page provides comprehensive analytics and visualizations for trading performance,
including metrics per expert, time-based analysis, and statistical measures.

Layout decisions (formatting, expert order/colour, chart sizes, empty states) are pure
functions in ``ui/components/performance_view.py``; this module only places them.
"""

from nicegui import ui
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder, ExpertInstance, AccountDefinition
from ba2_trade_platform.core.types import TransactionStatus
from ba2_trade_platform.core.utils import calculate_transaction_pnl
from ba2_trade_platform.ui.components.performance_charts import MultiMetricDashboard
from ba2_trade_platform.ui.components import performance_view as pv
from ba2_trade_platform.ui.utils.responsive import (
    CssOnce, PinnedColumn, grid_phone_class, phone_media, scroll_table_phone_css,
    tag_quasar_columns,
)
from ba2_common.analytics.performance import expert_performance, calculate_sharpe_ratio
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict
from ba2_trade_platform.logger import logger
from ba2_trade_platform.ui.utils.perf_logger import PerfLogger
from ba2_trade_platform.ui.account_filter_context import get_expert_ids_for_account

CSS_ONCE = CssOnce()

DETAIL_TABLE_CLASS = 'perf-detail-table'
DETAIL_TAG_PREFIX = 'perf-c-'
#: (column key, label, minimum px on a phone). The first column is pinned.
DETAIL_COLUMNS = [
    ("expert", "Expert Instance", 170), ('transactions', 'Transactions', 110),
    ('avg_duration', 'Avg Duration (days)', 150), ('total_pnl', 'Total P&L', 100),
    ('avg_pnl', 'Avg P&L', 90), ('win_rate', 'Win Rate', 90),
    ('profit_factor', 'Profit Factor', 110), ('largest_win', 'Largest Win', 110),
    ('largest_loss', 'Largest Loss', 110), ('max_dd', 'Max DD', 140),
    ('sharpe', 'Sharpe Ratio', 110),
]
DETAIL_PINNED = (PinnedColumn("expert", 0, 170),)

# One cell template for every column: the raw number is the sortable ``field`` (a formatted
# "$-1.98" string sorts wrongly), the text shown is ``<key>_txt``, the optional tooltip
# ``<key>_tip`` and colour class ``<key>_cls``.
DETAIL_CELL_SLOT = r'''
<q-td :props="props" :title="props.row[props.col.name + '_tip'] || (props.col.name === 'expert' ? props.row.expert : undefined)"
      :class="props.row[props.col.name + '_cls']">
  {{ props.row[props.col.name + '_txt'] !== undefined ? props.row[props.col.name + '_txt'] : props.value }}
</q-td>
'''


def performance_page_css() -> str:
    """Every stylesheet of this tab. Installed by ``_install_page_styles`` BEFORE anything
    else is built (NiceGUI drops head additions made after a page's first await)."""
    desktop = '''
    .perf-root { min-width: 0; max-width: 100%; }
    .perf-filters { background: rgba(30, 41, 59, 0.5); border: 1px solid rgba(160, 174, 192, 0.2); }
    .perf-period-scroll { max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch;
        scrollbar-width: none; }
    .perf-period-scroll::-webkit-scrollbar { display: none; }
    .perf-period.q-btn-group, .perf-period .q-btn-group { flex-wrap: nowrap; }
    .perf-period .q-btn { white-space: nowrap; flex: 0 0 auto; padding: 4px 14px;
        background: rgba(160, 174, 192, 0.12) !important; color: #a0aec0 !important; }
    .perf-period .q-btn.bg-primary { background: #00d4aa !important; color: #10141f !important;
        font-weight: 700; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.25); }
    .perf-chart-card { background: rgba(30, 41, 59, 0.35); border: 1px solid rgba(160, 174, 192, 0.15);
        padding: 12px 12px 4px 12px; min-width: 0; }
    .perf-chart-title { font-weight: 600; font-size: 0.95rem; color: #e2e8f0; line-height: 1.25;
        white-space: normal; overflow-wrap: anywhere; }
    .perf-chart-sub { font-size: 0.75rem; color: #a0aec0; line-height: 1.25; white-space: normal;
        overflow-wrap: anywhere; }
    .perf-chart-card .js-plotly-plot, .perf-chart-card .nicegui-plotly { max-width: 100%; }
    .perf-charts-grid { min-width: 0; }
    .perf-charts-grid > * { min-width: 0; }
    .perf-span-all { grid-column: 1 / -1; }
    .perf-empty { background: rgba(30, 41, 59, 0.35); border: 1px dashed rgba(160, 174, 192, 0.3); }
    .perf-detail-table td.perf-pos { color: #00d4aa; }
    .perf-detail-table td.perf-neg { color: #ff6b6b; }
    .perf-detail-table td { font-variant-numeric: tabular-nums; }
    .perf-detail-table th { white-space: nowrap; }
    '''
    phone = phone_media('''
    .perf-filters { padding: 10px; }
    .perf-filter-row { flex-direction: column; align-items: stretch; gap: 8px; }
    .perf-period-scroll { width: 100%; }
    .perf-period .q-btn { padding: 4px 9px; }
    .perf-expert-select { min-width: 0 !important; width: 100%; }
    .perf-chart-card { padding: 10px 8px 2px 8px; }
    .perf-chart-title { font-size: 0.9rem; }
    .perf-detail-table td.perf-c-expert { overflow: hidden; text-overflow: ellipsis; }
    ''')
    table = scroll_table_phone_css(
        DETAIL_TABLE_CLASS, DETAIL_TAG_PREFIX, DETAIL_PINNED,
        {key: width for key, _label, width in DETAIL_COLUMNS}, max_height='none')
    # The pinned name cell is cut with an ellipsis (full name in its tooltip) instead of
    # growing past its pin width; scroll_table_phone_css sets max-width: none on it.
    table_extra = phone_media(f'''
    .{DETAIL_TABLE_CLASS} td.{DETAIL_TAG_PREFIX}expert {{ max-width: 170px !important;
        overflow: hidden; text-overflow: ellipsis; }}
    ''')
    return desktop + phone + table + table_extra


def _install_page_styles() -> None:
    """Install the tab's stylesheets, synchronously, as the FIRST thing ``render`` does.

    NiceGUI silently drops ``ui.add_css`` / ``ui.add_head_html`` made after a page's first
    ``await`` (before the websocket connects). Nothing here awaits, and ``render`` calls
    this before it builds a single element -- the same rule as
    ``portfolio_allocation._install_page_styles``. ``tests/test_performance_css_before_await.py``
    pins it.
    """
    CSS_ONCE.add(ui.context.client, 'performance-page', performance_page_css(), ui.add_css)


class PerformanceTab:
    """Trade performance analytics and visualization tab."""

    def __init__(self, account_id: Optional[int]):
        """
        Initialize performance tab for an account.

        Args:
            account_id: Account ID to analyze, or None to include all accounts
                (driven by the global account selector).
        """
        self.account_id = account_id
        self.date_range_days = pv.read_period_days()  # persisted choice, default 30
        self.selected_experts = []  # Empty means all experts (within the account scope)
        self.data_loaded = False
        self.viewport_width: Optional[float] = None

    def _effective_expert_ids(self) -> Optional[List[int]]:
        """Expert ids the queries should be scoped to.

        - Explicit expert filter selected -> those experts.
        - Otherwise, if an account is selected globally -> that account's experts.
        - Otherwise (account = All, no filter) -> None (no expert scoping).

        NOTE: Transaction has no account column, so an account is "its expert instances".
        Transactions with no expert (manual / Portfolio Allocation trades) can therefore
        never match, and an account with no expert instances matches nothing at all.
        """
        if self.selected_experts:
            return self.selected_experts
        if self.account_id is not None:
            return get_expert_ids_for_account(self.account_id) or []
        return None

    def _get_closed_transactions(self) -> List[Transaction]:
        """Get all closed transactions for the account within date range."""
        session = get_db()
        try:
            cutoff_date = datetime.now() - timedelta(days=self.date_range_days)

            query = session.query(Transaction).filter(
                Transaction.status == TransactionStatus.CLOSED,
                Transaction.close_date.isnot(None),
                Transaction.close_date >= cutoff_date
            )

            expert_ids = self._effective_expert_ids()
            if expert_ids is not None:
                query = query.filter(Transaction.expert_id.in_(expert_ids))

            return query.all()
        finally:
            session.close()

    def _get_monthly_transactions(self) -> List[Transaction]:
        """Get closed transactions for up to 12 months for monthly trend charts."""
        session = get_db()
        try:
            # Always get up to 12 months for monthly charts
            cutoff_date = datetime.now() - timedelta(days=365)

            query = session.query(Transaction).filter(
                Transaction.status == TransactionStatus.CLOSED,
                Transaction.close_date.isnot(None),
                Transaction.close_date >= cutoff_date
            )

            expert_ids = self._effective_expert_ids()
            if expert_ids is not None:
                query = query.filter(Transaction.expert_id.in_(expert_ids))

            return query.all()
        finally:
            session.close()

    def _expert_names(self, expert_ids) -> Dict[Any, str]:
        """``{expert_id: display name}`` in ONE query (alias, else ``Class-ID``, else
        ``Expert-<id>`` for an id with no instance row)."""
        ids = {i for i in expert_ids if i}
        found: Dict[Any, str] = {}
        if ids:
            session = get_db()
            try:
                from sqlmodel import select
                for expert in session.scalars(select(ExpertInstance).where(ExpertInstance.id.in_(ids))):
                    found[expert.id] = expert.alias if expert.alias else f"{expert.expert}-{expert.id}"
            finally:
                session.close()
        return {i: found.get(i, f"Expert-{i}") for i in expert_ids}

    def _load_expert_options(self) -> Dict[int, str]:
        """``{expert id: name}`` for the filter dropdown (this account's experts)."""
        session = get_db()
        try:
            expert_q = session.query(ExpertInstance)
            if self.account_id is not None:
                expert_q = expert_q.filter(ExpertInstance.account_id == self.account_id)
            return {e.id: (e.alias if e.alias else f"{e.expert}-{e.id}") for e in expert_q.all()}
        finally:
            session.close()

    def _calculate_transaction_metrics(self, transactions: List[Transaction]) -> Dict[str, Any]:
        """Calculate comprehensive metrics from transactions."""
        if not transactions:
            return {}

        # Group by expert instance ID
        expert_transactions = defaultdict(list)
        for txn in transactions:
            expert_transactions[txn.expert_id].append(txn)

        names = self._expert_names(list(expert_transactions.keys()))
        expert_metrics = {}
        for expert_id, txns in expert_transactions.items():
            expert_metrics[names[expert_id]] = expert_performance(txns)

        return expert_metrics

    def _calculate_monthly_metrics(self, transactions: List[Transaction]) -> Dict[str, Dict[str, float]]:
        """Calculate monthly metrics per expert instance."""
        monthly_data = defaultdict(lambda: defaultdict(lambda: {'pnl': 0, 'count': 0}))

        if not transactions:
            return monthly_data

        names = self._expert_names(list({txn.expert_id for txn in transactions}))
        for txn in transactions:
            pnl = calculate_transaction_pnl(txn)
            if txn.close_date and pnl is not None:
                month_key = txn.close_date.strftime('%Y-%m')
                expert_name = names[txn.expert_id]
                monthly_data[month_key][expert_name]['pnl'] += pnl
                monthly_data[month_key][expert_name]['count'] += 1

        return monthly_data

    # ------------------------------------------------------------------ rendering

    @property
    def _layout(self) -> pv.ChartLayout:
        return pv.choose_layout(self.viewport_width)

    def _chart_card(self, title: str, subtitle: str = '', span_all: bool = False):
        """A card with an HTML title (wraps instead of being cut) and an optional
        subtitle; returns the card to build the plot in."""
        card = ui.element('div').classes(
            'perf-chart-card rounded-lg w-full' + (' perf-span-all' if span_all else ''))
        with card:
            ui.label(title).classes('perf-chart-title')
            if subtitle:
                ui.label(subtitle).classes('perf-chart-sub')
        return card

    def _plot(self, fig):
        ui.plotly(fig).classes('w-full')

    def _render_summary_metrics(self, expert_metrics: Dict[str, Any]):
        """Render top-level summary metric cards."""
        if not expert_metrics:
            ui.label("No transaction data available for the selected period").classes('text-center p-4').style('color: #a0aec0;')
            return

        # Calculate overall metrics
        total_transactions = sum(m['total_transactions'] for m in expert_metrics.values())
        total_pnl = sum(m['total_pnl'] for m in expert_metrics.values())
        all_wins = sum(m['wins'] for m in expert_metrics.values())
        all_losses = sum(m['losses'] for m in expert_metrics.values())
        overall_win_rate = (all_wins / (all_wins + all_losses) * 100) if (all_wins + all_losses) > 0 else 0

        # All returns for Sharpe
        all_returns = []
        for metrics in expert_metrics.values():
            all_returns.extend(metrics['returns'])

        overall_sharpe = calculate_sharpe_ratio(all_returns) if len(all_returns) >= 30 else None

        # Create metric cards
        metrics_list = [
            {
                'title': 'Total Transactions',
                'value': str(total_transactions),
                'subtitle': pv.period_caption(self.date_range_days),
                'color': 'primary'
            },
            {
                'title': 'Total P&L',
                'value': pv.format_money(total_pnl),
                'subtitle': 'Net profit/loss',
                'color': 'positive' if total_pnl >= 0 else 'negative'
            },
            {
                'title': 'Win Rate',
                'value': f'{overall_win_rate:.1f}%',
                'subtitle': f'{all_wins}W / {all_losses}L',
                'color': 'positive' if overall_win_rate >= 50 else 'neutral'
            },
            {
                'title': 'Sharpe Ratio',
                'value': f'{overall_sharpe:.2f}' if overall_sharpe is not None else 'N/A',
                'subtitle': 'Risk-adjusted return' if overall_sharpe is not None else 'Need 30+ transactions',
                'color': 'primary' if overall_sharpe and overall_sharpe > 1 else 'neutral'
            }
        ]

        dashboard = MultiMetricDashboard(metrics_list, columns=4)
        dashboard.render()

    def _render_expert_comparison_charts(self, expert_metrics: Dict[str, Any],
                                         order: List[str], colors: Dict[str, str]):
        """Horizontal bar charts comparing expert instances: same order and colour on each."""
        if not expert_metrics:
            return
        layout = self._layout
        names = [n for n in order if n in expert_metrics]

        def values(key):
            return [expert_metrics[n][key] for n in names]

        with ui.grid(columns=2).classes(f'w-full gap-4 mt-6 perf-charts-grid {grid_phone_class(1)}'):
            with self._chart_card('Average Transaction Duration', 'Days a position was held, per expert instance'):
                self._plot(pv.hbar_figure(names, values('avg_duration_days'), colors, layout, 'days'))

            with self._chart_card('Total P&L', 'Net profit/loss per expert instance'):
                self._plot(pv.hbar_figure(names, values('total_pnl'), colors, layout, 'money'))

            with self._chart_card('Win/Loss Distribution', 'Winning and losing transactions per expert instance'):
                self._plot(pv.win_loss_figure(names, values('wins'), values('losses'), layout))

            with self._chart_card('Average P&L per Transaction', 'Mean profit/loss of a closed transaction'):
                self._plot(pv.hbar_figure(names, values('avg_pnl'), colors, layout, 'money'))

    def _render_monthly_trends(self, monthly_data, order: List[str], colors: Dict[str, str]):
        """Monthly trend charts by expert instance (legend below each, drawdown separate)."""
        if not monthly_data:
            return
        layout = self._layout
        profit_series, transaction_series, drawdown_series = pv.monthly_series(monthly_data)

        ui.label("Monthly Performance Trends").classes('text-xl font-bold mt-8 mb-2').style('color: #e2e8f0;')

        with ui.grid(columns=2).classes(f'w-full gap-4 perf-charts-grid {grid_phone_class(1)}'):
            with self._chart_card('Monthly P&L', 'Realised profit/loss by close month, per expert instance (last 12 months)'):
                self._plot(pv.monthly_line_figure(profit_series, order, colors, layout, 'money'))

            with self._chart_card('Monthly Transaction Count', 'Closed transactions by close month, per expert instance'):
                self._plot(pv.monthly_line_figure(transaction_series, order, colors, layout, 'count'))

            if drawdown_series:
                # Its own chart rather than bars on a second axis behind the P&L lines: the
                # right-hand axis put two units in one plot and the bars hid the lines.
                # Same colours as above, so no legend of its own (hover names the expert).
                with self._chart_card(
                        'Drawdown from Peak',
                        "Dollars each expert's cumulative P&L sits below its running peak, "
                        'month by month (deeper = further below). In dollars because a '
                        'percentage of a small peak is meaningless.', span_all=True):
                    self._plot(pv.monthly_line_figure(
                        drawdown_series, order, colors, layout, 'money', show_legend=False,
                        reverse_y=True, y_title='Drawdown ($ below peak cumulative P&L)'))

    def _detail_rows(self, expert_metrics: Dict[str, Any], order: List[str]) -> List[Dict[str, Any]]:
        rows = []
        shown = [n for n in order if n in expert_metrics]
        short = dict(zip(shown, pv.shorten_labels(
            shown, None if self._layout.label_max_chars is None else self._layout.label_max_chars + 2)))
        for name in order:
            if name not in expert_metrics:
                continue
            m = expert_metrics[name]
            pf_txt, pf_tip = pv.format_profit_factor(m['profit_factor'])
            sh_txt, sh_tip = pv.format_sharpe(m['sharpe_ratio'], len(m['returns']))
            dd_txt = pv.format_money(m['max_drawdown'])
            dd_tip = ('Worst fall of cumulative P&L from its peak, in dollars. No percentage is '
                      'shown: it would be relative to the peak cumulative P&L, which is often '
                      'tiny, so it would not mean anything.')

            def sign_cls(v):
                return '' if v is None or v == 0 else ('perf-pos' if v > 0 else 'perf-neg')

            rows.append({
                'expert': name, 'expert_txt': short[name],
                'transactions': m['total_transactions'],
                'avg_duration': round(float(m['avg_duration_days']), 4),
                'avg_duration_txt': pv.format_days(m['avg_duration_days']),
                'total_pnl': m['total_pnl'], 'total_pnl_txt': pv.format_money(m['total_pnl']),
                'total_pnl_cls': sign_cls(m['total_pnl']),
                'avg_pnl': m['avg_pnl'], 'avg_pnl_txt': pv.format_money(m['avg_pnl']),
                'avg_pnl_cls': sign_cls(m['avg_pnl']),
                'win_rate': m['win_rate'], 'win_rate_txt': f"{m['win_rate']:.1f}%",
                'profit_factor': (None if m['profit_factor'] is None
                                  else (1e18 if m['profit_factor'] == float('inf') else m['profit_factor'])),
                'profit_factor_txt': pf_txt, 'profit_factor_tip': pf_tip,
                'largest_win': m['largest_win'],
                'largest_win_txt': pv.format_money(m['largest_win']) if m['largest_win'] is not None else 'N/A',
                'largest_win_tip': '' if m['largest_win'] is not None else 'No winning trades in this period.',
                'largest_loss': m['largest_loss'],
                'largest_loss_txt': pv.format_money(m['largest_loss']) if m['largest_loss'] is not None else 'N/A',
                'largest_loss_tip': '' if m['largest_loss'] is not None else 'No losing trades in this period.',
                # Dollars AND percent: the percent is unavailable for an expert whose
                # cumulative P&L never rose above zero, and the dollar figure is the only
                # honest answer there.
                'max_dd': m['max_drawdown'], 'max_dd_txt': dd_txt, 'max_dd_tip': dd_tip,
                'sharpe': m['sharpe_ratio'], 'sharpe_txt': sh_txt, 'sharpe_tip': sh_tip,
            })
        return rows

    def _render_detailed_table(self, expert_metrics: Dict[str, Any], order: List[str]):
        """Detailed performance table: scrolls inside its own box on a phone, name pinned."""
        if not expert_metrics:
            return

        ui.label("Detailed Performance Metrics").classes('text-xl font-bold mt-8 mb-2').style('color: #e2e8f0;')

        columns = tag_quasar_columns([
            {'name': key, 'label': label, 'field': key, 'align': 'left', 'sortable': True}
            for key, label, _w in DETAIL_COLUMNS], DETAIL_TAG_PREFIX)
        table = ui.table(columns=columns, rows=self._detail_rows(expert_metrics, order),
                         row_key='expert', pagination={'rowsPerPage': 0}
                         ).classes(f'w-full dark-table {DETAIL_TABLE_CLASS}')
        table.props('flat dense hide-bottom')
        table.add_slot('body-cell', DETAIL_CELL_SLOT)

    def _render_filters(self):
        """Render filter controls."""
        with ui.card().classes('w-full mb-4 perf-filters'):
            ui.label("Filters").classes('text-lg font-bold mb-2').style('color: #e2e8f0;')

            with ui.row().classes('w-full gap-4 items-center perf-filter-row'):
                ui.label("Time Period:").classes('self-center')

                def update_date_range(e):
                    days = pv.normalize_period_days(e.value)
                    self.date_range_days = days
                    pv.write_period_days(days)
                    self._refresh_data()

                # A segmented control (QBtnToggle), so the active period is always shown;
                # on a phone it stays on one line and scrolls sideways.
                with ui.element('div').classes('perf-period-scroll'):
                    ui.toggle(pv.PERIOD_OPTIONS, value=self.date_range_days,
                              on_change=update_date_range
                              ).props('no-caps unelevated').classes('perf-period')

                expert_options = self._load_expert_options()
                if expert_options:
                    ui.label("Expert Instances:").classes('self-center md:ml-8')

                    def update_expert_filter(selected):
                        self.selected_experts = selected
                        self._refresh_data()

                    ui.select(
                        options=expert_options,
                        multiple=True,
                        label="Experts (empty = all)"
                    ).style('min-width: 280px').classes('perf-expert-select').bind_value_to(
                        self, 'selected_experts').on_value_change(
                        lambda e: update_expert_filter(e.value)
                    )

    def _refresh_data(self):
        """Refresh all data and re-render."""
        self.content_container.clear()
        with self.content_container:
            self._load_and_render_content()

    def _render_empty_state(self):
        account_has_experts = (self.account_id is None
                               or bool(get_expert_ids_for_account(self.account_id)))
        headline, detail = pv.empty_state(
            account_selected=self.account_id is not None,
            account_has_experts=account_has_experts,
            expert_filter_active=bool(self.selected_experts),
            days=self.date_range_days)
        with ui.column().classes('w-full items-center p-6 gap-1 rounded-lg perf-empty'):
            ui.label(headline).classes('text-lg font-medium text-center').style('color: #e2e8f0;')
            ui.label(detail).classes('text-sm text-center').style('color: #a0aec0; max-width: 640px;')

    def _load_and_render_content(self):
        """Load transaction data and render all charts."""
        # Get transactions for main metrics (filtered by date range)
        transactions = self._get_closed_transactions()

        if not transactions:
            self._render_empty_state()
            return

        # Calculate metrics
        expert_metrics = self._calculate_transaction_metrics(transactions)

        # Monthly data first: one expert order and one colour per expert, across every
        # chart and the table, includes experts that only appear in the 12-month view.
        monthly_data = self._calculate_monthly_metrics(self._get_monthly_transactions())
        monthly_names = {n for per_expert in monthly_data.values() for n in per_expert}
        order = pv.order_experts(expert_metrics, monthly_names)
        colors = pv.assign_colors(order)

        # Render components
        self._render_summary_metrics(expert_metrics)
        self._render_expert_comparison_charts(expert_metrics, order, colors)
        self._render_monthly_trends(monthly_data, order, colors)
        self._render_detailed_table(expert_metrics, order)

        self.data_loaded = True

    async def _first_load(self):
        """Read the viewport width, THEN draw the charts (the layout depends on it)."""
        try:
            width = await ui.run_javascript('window.innerWidth', timeout=3.0)
            self.viewport_width = float(width)
        except Exception as e:  # noqa: BLE001 -- unreadable width => desktop layout
            logger.warning(f"Performance: could not read the viewport width ({e}); using the desktop layout")
            self.viewport_width = None
        self._refresh_data()

    def render(self):
        """Render the complete performance tab."""
        # FIRST, before any element: see _install_page_styles.
        _install_page_styles()
        render_timer = PerfLogger.start(PerfLogger.PAGE, PerfLogger.RENDER, "Performance")
        with ui.column().classes('w-full gap-4 perf-root'):
            ui.label("Trade Performance Analytics").classes('text-2xl font-bold mb-2').style('color: #e2e8f0;')

            # Filters
            self._render_filters()

            # Content container for refresh
            self.content_container = ui.column().classes('w-full gap-4')

            with self.content_container:
                ui.label("Loading…").style('color: #a0aec0;')
            # OUTSIDE the container: _refresh_data clears the container, and that must
            # not delete the timer whose callback is running it.
            ui.timer(0.05, self._first_load, once=True)
        render_timer.stop()
