"""
Custom UI renderer for PennyMomentumTrader market analysis results.

Renders a 5-tab NiceGUI interface showing scan results, triage details,
active monitors, executed trades, and raw data from AnalysisOutput records.
"""

import json
from typing import Any, Dict, List

from nicegui import ui
from sqlmodel import select

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AnalysisOutput, MarketAnalysis
from ba2_trade_platform.core.types import MarketAnalysisStatus
from ba2_trade_platform.logger import logger


class PennyMomentumTraderUI:
    """
    UI rendering class for PennyMomentumTrader market analysis results.
    Provides a tabbed interface for scan results, triage, monitors, trades,
    and raw data.
    """

    def __init__(self, market_analysis: MarketAnalysis):
        self.market_analysis = market_analysis
        self.state = market_analysis.state or {}

    def render(self):
        """Render the complete PennyMomentumTrader analysis UI with 5 tabs."""
        try:
            # Auto-refresh while analysis is running
            if self.market_analysis.status == MarketAnalysisStatus.RUNNING:
                ui.label('Analysis is running - page will auto-refresh every 30s').classes(
                    'text-caption text-grey-7 mb-2'
                )
                analysis_id = self.market_analysis.id

                async def _auto_refresh():
                    try:
                        with get_db() as session:
                            stmt = select(MarketAnalysis.state).where(
                                MarketAnalysis.id == analysis_id
                            )
                            current_state = session.execute(stmt).scalar()
                        if current_state != self.state:
                            ui.navigate.reload()
                    except Exception:
                        pass

                ui.timer(30.0, _auto_refresh)

            with ui.tabs().classes('w-full') as tabs:
                summary_tab = ui.tab('Summary')
                scan_tab = ui.tab('Scan Results')
                filtered_tab = ui.tab('Filtered')
                triage_tab = ui.tab('Triage')
                monitors_tab = ui.tab('Active Monitors')
                trades_tab = ui.tab('Trades')
                raw_tab = ui.tab('Raw Data')

            with ui.tab_panels(tabs, value=summary_tab).classes('w-full'):
                with ui.tab_panel(summary_tab):
                    self._render_summary()
                with ui.tab_panel(scan_tab):
                    self._render_scan_results()
                with ui.tab_panel(filtered_tab):
                    self._render_filtered_stocks()
                with ui.tab_panel(triage_tab):
                    self._render_triage()
                with ui.tab_panel(monitors_tab):
                    self._render_monitors()
                with ui.tab_panel(trades_tab):
                    self._render_trades()
                with ui.tab_panel(raw_tab):
                    self._render_raw_data()
        except Exception as e:
            logger.error(f"Error rendering PennyMomentumTrader UI: {e}", exc_info=True)
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='red')
                ui.label('Error rendering analysis UI').classes('text-h6 text-red-600')
                ui.label(str(e)).classes('text-caption text-grey-7')

    # ------------------------------------------------------------------
    # Tab 0: Summary
    # ------------------------------------------------------------------

    def _render_summary(self):
        """Pipeline summary: phase timings, current status, and top symbols."""
        phase = self.state.get('phase', 'unknown')
        timings: Dict[str, float] = self.state.get('phase_timings', {})
        total_time = self.state.get('pipeline_total_seconds')
        deep_triage: Dict[str, Dict] = self.state.get('deep_triage_results', {})
        monitored: Dict[str, Dict] = self.state.get('monitored_symbols', {})
        scan_results = self.state.get('scan_results', [])
        survivors = self.state.get('quick_filter_survivors', [])
        filtered_stocks: Dict = self.state.get('filtered_stocks', {})

        with ui.card().classes('w-full'):
            ui.label('Pipeline Summary').classes('text-h5 mb-2')

            # Status line
            if phase == 'complete':
                ui.label(
                    f'Pipeline completed in {total_time:.0f}s' if total_time else 'Pipeline completed'
                ).classes('text-subtitle1 text-green-700 mb-4')
            else:
                ui.label(f'Current phase: {phase}').classes('text-subtitle1 text-orange-700 mb-4')

            # --- Phase timings ---
            if timings:
                ui.label('Phase Timings').classes('text-h6 mb-2')

                phase_labels = {
                    'review': 'Phase 0 - Review Positions',
                    'screen': 'Phase 1 - Screener',
                    'quick_filter': 'Phase 2 - Quick Filter (LLM)',
                    'discovery': 'Phase 1b - LLM Discovery',
                    'deep_triage': 'Phase 3 - Deep Triage (LLM)',
                    'entry_setup': 'Phase 4 - Entry Conditions',
                    'monitoring': 'Phase 5 - Monitoring',
                    'eod': 'Phase 6 - EOD Wrap-up',
                }

                # Order phases logically
                ordered_phases = [
                    'review', 'screen', 'quick_filter', 'discovery',
                    'deep_triage', 'entry_setup', 'monitoring', 'eod',
                ]

                rows = []
                for p in ordered_phases:
                    if p in timings:
                        rows.append({
                            'phase': phase_labels.get(p, p),
                            'time': f'{timings[p]:.1f}s',
                            'seconds': timings[p],
                        })

                if rows:
                    columns = [
                        {'name': 'phase', 'label': 'Phase', 'field': 'phase', 'align': 'left'},
                        {'name': 'time', 'label': 'Duration', 'field': 'time', 'align': 'right'},
                    ]
                    ui.table(columns=columns, rows=rows, row_key='phase').classes('w-full mb-4')

                    if total_time:
                        ui.label(f'Total: {total_time:.0f}s').classes('text-subtitle2 font-bold')
            else:
                ui.label('No timing data yet.').classes('text-grey-7 mb-4')

            ui.separator().classes('my-4')

            # --- Pipeline funnel ---
            ui.label('Pipeline Funnel').classes('text-h6 mb-2')
            total_filtered = sum(len(v) for v in filtered_stocks.values()) if filtered_stocks else 0
            funnel_items = [
                ('Screener candidates', len(scan_results)),
                ('Quick filter survivors', len(survivors)),
                ('Filtered out', total_filtered),
                ('Deep triage finalists', len(deep_triage)),
                ('Active monitors', len(monitored)),
            ]
            with ui.row().classes('gap-6 flex-wrap mb-4'):
                for label, count in funnel_items:
                    with ui.column().classes('items-center'):
                        ui.label(str(count)).classes('text-h4 font-bold')
                        ui.label(label).classes('text-caption text-grey-7')

            ui.separator().classes('my-4')

            # --- Top symbols ---
            ui.label('Top Symbols').classes('text-h6 mb-2')
            if deep_triage:
                # Sort by confidence descending
                sorted_symbols = sorted(
                    deep_triage.items(),
                    key=lambda x: x[1].get('confidence', 0),
                    reverse=True,
                )
                rows = []
                for symbol, data in sorted_symbols:
                    is_monitored = symbol in monitored
                    rows.append({
                        'symbol': symbol,
                        'confidence': f'{data.get("confidence", 0):.0f}%',
                        'catalyst': data.get('catalyst', '-'),
                        'strategy': data.get('strategy', '-'),
                        'expected_profit': f'+{data.get("expected_profit_pct", 0):.1f}%',
                        'status': 'Monitoring' if is_monitored else 'Finalist',
                    })

                columns = [
                    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                    {'name': 'confidence', 'label': 'Confidence', 'field': 'confidence', 'align': 'right'},
                    {'name': 'expected_profit', 'label': 'Expected', 'field': 'expected_profit', 'align': 'right'},
                    {'name': 'strategy', 'label': 'Strategy', 'field': 'strategy', 'align': 'left'},
                    {'name': 'catalyst', 'label': 'Catalyst', 'field': 'catalyst', 'align': 'left'},
                    {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'center'},
                ]
                ui.table(columns=columns, rows=rows, row_key='symbol').classes('w-full')
            else:
                ui.label('No finalists yet.').classes('text-grey-7')

    # ------------------------------------------------------------------
    # Tab 1: Scan Results
    # ------------------------------------------------------------------

    def _render_scan_results(self):
        """Table of all candidates from the screener, color-coded by filter survival."""
        scan_results: List[Dict] = self.state.get('scan_results', [])
        survivors: List[str] = self.state.get('quick_filter_survivors', [])
        survivor_set = set(survivors)

        with ui.card().classes('w-full'):
            ui.label('Screener Candidates').classes('text-h5 mb-2')
            phase = self.state.get('phase', '')
            if phase:
                ui.label(f'Current phase: {phase}').classes('text-caption text-grey-7 mb-2')
            ui.label(f'{len(scan_results)} candidates scanned, {len(survivors)} passed quick filter').classes('text-subtitle2 mb-4')

            if not scan_results:
                ui.label('No scan results available yet.').classes('text-grey-7')
                return

            columns = [
                {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
                {'name': 'price', 'label': 'Price', 'field': 'price', 'sortable': True},
                {'name': 'volume', 'label': 'Volume', 'field': 'volume', 'sortable': True},
                {'name': 'market_cap', 'label': 'Market Cap', 'field': 'market_cap', 'sortable': True},
                {'name': 'sector', 'label': 'Sector', 'field': 'sector', 'align': 'left'},
                {'name': 'exchange', 'label': 'Exchange', 'field': 'exchange', 'align': 'left'},
            ]

            rows = []
            for item in scan_results:
                symbol = item.get('symbol', item.get('ticker', ''))
                rows.append({
                    'symbol': symbol,
                    'price': self._fmt_number(item.get('price', item.get('last_price'))),
                    'volume': self._fmt_int(item.get('volume')),
                    'market_cap': self._fmt_market_cap(item.get('market_cap', item.get('marketCap'))),
                    'sector': item.get('sector', '-'),
                    'exchange': item.get('exchange', '-'),
                    '_survived': symbol in survivor_set,
                })

            table = ui.table(
                columns=columns,
                rows=rows,
                row_key='symbol',
                pagination={'rowsPerPage': 25},
            ).classes('w-full')

            # Color-code rows that passed the quick filter
            table.add_slot('body-cell-symbol', r'''
                <q-td :props="props">
                    <span :class="props.row._survived ? 'text-weight-bold text-green-8' : ''">
                        {{ props.row.symbol }}
                        <q-badge v-if="props.row._survived" color="green" label="passed" class="q-ml-sm" />
                    </span>
                </q-td>
            ''')

    # ------------------------------------------------------------------
    # Tab 2: Filtered Stocks
    # ------------------------------------------------------------------

    def _render_filtered_stocks(self):
        """Table showing all stocks that were filtered out and why."""
        filtered: Dict[str, Dict] = self.state.get('filtered_stocks', {})

        # Reason display labels and colors
        reason_labels = {
            'already_held': ('Already Held', 'grey'),
            'volume_cap': ('Volume Cap', 'blue-grey'),
            'not_tradeable': ('Not Tradeable', 'orange'),
            'llm_rejected': ('LLM Rejected', 'purple'),
            'low_confidence': ('Low Confidence', 'red'),
            'capped_by_limit': ('Capped by Limit', 'amber'),
            'llm_parse_failed': ('Parse Failed', 'red'),
            'deep_triage_error': ('Triage Error', 'red'),
        }

        phase_labels = {
            'screen': 'Phase 1: Screen',
            'quick_filter': 'Phase 2: Quick Filter',
            'deep_triage': 'Phase 3: Deep Triage',
        }

        with ui.card().classes('w-full'):
            ui.label('Filtered Stocks').classes('text-h5 mb-2')
            ui.label(
                f'{len(filtered)} stocks filtered out during analysis'
            ).classes('text-subtitle2 text-grey-7 mb-4')

            if not filtered:
                ui.label('No filtered stocks recorded yet.').classes('text-grey-7')
                return

            # Group by phase for display
            by_phase: Dict[str, List] = {}
            for symbol, info in filtered.items():
                phase = info.get('phase', 'unknown')
                by_phase.setdefault(phase, []).append((symbol, info))

            for phase in ['screen', 'quick_filter', 'deep_triage', 'unknown']:
                items = by_phase.get(phase, [])
                if not items:
                    continue

                phase_label = phase_labels.get(phase, phase)
                with ui.expansion(
                    f'{phase_label} ({len(items)} filtered)',
                    icon='filter_alt',
                    value=True,
                ).classes('w-full mb-2'):
                    columns = [
                        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
                        {'name': 'reason', 'label': 'Reason', 'field': 'reason', 'sortable': True, 'align': 'left'},
                        {'name': 'details', 'label': 'Details', 'field': 'details', 'align': 'left'},
                    ]

                    rows = []
                    for symbol, info in items:
                        reason_key = info.get('reason', 'unknown')
                        label, _ = reason_labels.get(reason_key, (reason_key, 'grey'))
                        rows.append({
                            'symbol': symbol,
                            'reason': label,
                            'details': info.get('details', '-'),
                            '_reason_key': reason_key,
                        })

                    table = ui.table(
                        columns=columns,
                        rows=rows,
                        row_key='symbol',
                        pagination={'rowsPerPage': 25},
                    ).classes('w-full')

                    # Color-code reason badges
                    table.add_slot('body-cell-reason', r'''
                        <q-td :props="props">
                            <q-badge
                                :color="props.row._reason_key === 'llm_rejected' ? 'purple'
                                      : props.row._reason_key === 'low_confidence' ? 'red'
                                      : props.row._reason_key === 'not_tradeable' ? 'orange'
                                      : props.row._reason_key === 'volume_cap' ? 'blue-grey'
                                      : props.row._reason_key === 'already_held' ? 'grey'
                                      : props.row._reason_key === 'capped_by_limit' ? 'amber'
                                      : 'red'"
                                :label="props.row.reason"
                            />
                        </q-td>
                    ''')

                    # Word-wrap the details column
                    table.add_slot('body-cell-details', r'''
                        <q-td :props="props" style="white-space: normal; word-break: break-word; min-width: 300px; max-width: 600px;">
                            {{ props.row.details }}
                        </q-td>
                    ''')

    # ------------------------------------------------------------------
    # Tab 3: Triage
    # ------------------------------------------------------------------

    def _render_triage(self):
        """Quick filter survivors and deep triage finalist cards."""
        survivors: List[str] = self.state.get('quick_filter_survivors', [])
        deep_triage: Dict[str, Dict] = self.state.get('deep_triage_results', {})

        with ui.card().classes('w-full'):
            # Section 1 - Quick Filter
            ui.label('Quick Filter Survivors').classes('text-h5 mb-2')
            ui.label(f'{len(survivors)} symbols passed quick screening').classes('text-subtitle2 text-grey-7 mb-4')

            if survivors:
                with ui.row().classes('gap-2 flex-wrap mb-6'):
                    for sym in survivors:
                        ui.badge(sym, color='green').classes('text-sm')
            else:
                ui.label('No survivors yet.').classes('text-grey-7 mb-6')

            ui.separator().classes('my-4')

            # Section 2 - Deep Triage
            ui.label('Deep Triage Results').classes('text-h5 mb-2')
            ui.label(f'{len(deep_triage)} finalists analysed in depth').classes('text-subtitle2 text-grey-7 mb-4')

            if not deep_triage:
                ui.label('No deep triage results available yet.').classes('text-grey-7')
                return

            for symbol, data in deep_triage.items():
                self._render_triage_card(symbol, data)

    def _render_triage_card(self, symbol: str, data: Dict[str, Any]):
        """Render a single deep-triage finalist card."""
        confidence = data.get('confidence', 0)
        catalyst = data.get('catalyst', '-')
        strategy = data.get('strategy', 'unknown')
        expected_profit = data.get('expected_profit_pct', 0)
        reasoning = data.get('reasoning', '')
        detailed_report = data.get('detailed_report', '')
        qty = data.get('qty', '-')
        allocation = data.get('allocation', '-')

        with ui.card().classes('w-full mb-3 p-4'):
            with ui.row().classes('w-full items-center gap-4'):
                ui.label(symbol).classes('text-h6 font-bold')

                # Strategy badge
                strategy_lower = strategy.lower() if isinstance(strategy, str) else ''
                if 'intraday' in strategy_lower:
                    ui.badge(strategy, color='blue').classes('text-sm')
                elif 'swing' in strategy_lower:
                    ui.badge(strategy, color='orange').classes('text-sm')
                else:
                    ui.badge(strategy, color='grey').classes('text-sm')

                ui.space()
                ui.label(f'Expected: +{expected_profit:.1f}%').classes('text-green-700 font-bold')

            # Confidence bar
            with ui.row().classes('w-full items-center gap-2 mt-2'):
                ui.label('Confidence:').classes('text-sm text-grey-7')
                ui.linear_progress(
                    value=confidence / 100.0,
                    show_value=False,
                    color='green' if confidence >= 70 else 'orange' if confidence >= 50 else 'red',
                ).classes('flex-grow').style('height: 12px;')
                ui.label(f'{confidence:.0f}%').classes('text-sm font-bold')

            # Details
            with ui.row().classes('w-full gap-6 mt-2'):
                ui.label(f'Catalyst: {catalyst}').classes('text-sm')
                ui.label(f'Qty: {qty}').classes('text-sm')
                if allocation and allocation != '-':
                    ui.label(f'Allocation: {allocation}').classes('text-sm')

            # Expandable reasoning
            if reasoning:
                with ui.expansion('Reasoning', icon='psychology').classes('w-full mt-2'):
                    ui.markdown(reasoning).classes('text-sm')

            # Expandable detailed analysis report
            if detailed_report:
                with ui.expansion('Detailed Analysis', icon='article').classes('w-full mt-1'):
                    ui.markdown(detailed_report).classes('text-sm')

    # ------------------------------------------------------------------
    # Tab 3: Active Monitors
    # ------------------------------------------------------------------

    def _render_monitors(self):
        """Per-symbol cards from monitored_symbols with condition checklists."""
        monitored: Dict[str, Dict] = self.state.get('monitored_symbols', {})
        analysis_id = self.market_analysis.id

        with ui.card().classes('w-full') as monitors_container:
            ui.label('Active Monitors').classes('text-h5 mb-2')
            ui.label(f'{len(monitored)} symbols being monitored').classes('text-subtitle2 text-grey-7 mb-4')

            if not monitored:
                ui.label('No active monitors.').classes('text-grey-7')
                return

            for symbol, info in monitored.items():
                self._render_monitor_card(symbol, info, analysis_id)

    def _render_monitor_card(self, symbol: str, info: Dict[str, Any], analysis_id: int):
        """Render a single monitor card with status badge, condition checklist, and remove button."""
        status = info.get('status', 'unknown')
        entry_conditions = info.get('entry_conditions', {})
        exit_conditions = info.get('exit_conditions', {})
        conditions_last_eval = info.get('conditions_last_eval')
        last_checked = info.get('last_checked', '-')
        last_price = info.get('last_price')
        entry_price = info.get('entry_price')
        days_remaining = info.get('days_remaining')

        # Status badge colour mapping
        status_colors = {
            'watching': 'blue',
            'triggered': 'green',
            'expired': 'grey',
            'closed': 'red',
        }
        badge_color = status_colors.get(status.lower(), 'grey') if isinstance(status, str) else 'grey'

        with ui.card().classes('w-full mb-3 p-4'):
            # Header row
            with ui.row().classes('w-full items-center gap-4'):
                ui.label(symbol).classes('text-h6 font-bold')
                ui.badge(status, color=badge_color).classes('text-sm')
                ui.space()
                if days_remaining is not None:
                    ui.label(f'{days_remaining} days remaining').classes('text-sm text-grey-7')

                # Remove button
                async def remove_symbol(sym=symbol):
                    await self._remove_monitored_symbol(analysis_id, sym)

                ui.button(
                    icon='delete', on_click=remove_symbol
                ).props('flat dense color=red size=sm').tooltip(f'Remove {symbol} from monitors')

            # Price info
            with ui.row().classes('w-full gap-6 mt-2'):
                if last_price is not None:
                    ui.label(f'Last Price: ${last_price}').classes('text-sm')
                if last_checked and last_checked != '-':
                    ui.label(f'Last Checked: {last_checked}').classes('text-sm text-grey-7')

            # P&L if entry price exists
            if entry_price is not None and last_price is not None:
                try:
                    pnl_pct = ((float(last_price) - float(entry_price)) / float(entry_price)) * 100
                    pnl_color = 'text-green-700' if pnl_pct >= 0 else 'text-red-700'
                    ui.label(
                        f'Entry: ${entry_price} | P&L: {pnl_pct:+.2f}%'
                    ).classes(f'text-sm font-bold mt-1 {pnl_color}')
                except (ValueError, ZeroDivisionError):
                    pass

            # Last manual evaluation results (from Evaluate Conditions action)
            if conditions_last_eval:
                met_count = sum(1 for v in conditions_last_eval.values() if v is True)
                total_count = len(conditions_last_eval)
                color = 'text-green-700' if met_count == total_count else 'text-orange-700'
                ui.label(
                    f'Last eval: {met_count}/{total_count} conditions met'
                ).classes(f'text-sm font-bold mt-2 {color}')
                self._render_condition_checklist('Evaluated Conditions', conditions_last_eval)
            elif entry_conditions:
                self._render_condition_checklist('Entry Conditions', entry_conditions)

            # Exit conditions checklist
            if exit_conditions:
                self._render_condition_checklist('Exit Conditions', exit_conditions)

    def _render_condition_checklist(self, title: str, conditions: Any):
        """Render a condition set as a checklist with green/red icons."""
        with ui.expansion(title, icon='checklist').classes('w-full mt-2'):
            if isinstance(conditions, dict):
                # Flat dict of condition_name -> bool
                for cond_name, met in conditions.items():
                    if isinstance(met, bool):
                        icon_name = 'check_circle' if met else 'cancel'
                        icon_color = 'green' if met else 'red'
                        with ui.row().classes('items-center gap-1'):
                            ui.icon(icon_name, color=icon_color, size='sm')
                            ui.label(str(cond_name)).classes('text-sm')
                    else:
                        # Nested structure - just display as text
                        ui.label(f'{cond_name}: {met}').classes('text-sm text-grey-7')
            elif isinstance(conditions, list):
                for cond in conditions:
                    if isinstance(cond, dict):
                        cond_type = cond.get('type', 'unknown')
                        met = cond.get('met', None)
                        if met is not None:
                            icon_name = 'check_circle' if met else 'cancel'
                            icon_color = 'green' if met else 'red'
                            with ui.row().classes('items-center gap-1'):
                                ui.icon(icon_name, color=icon_color, size='sm')
                                ui.label(str(cond_type)).classes('text-sm')
                        else:
                            ui.label(f'{cond_type}: {json.dumps(cond, default=str)}').classes('text-sm text-grey-7')
                    else:
                        ui.label(str(cond)).classes('text-sm')
            else:
                # Fallback: render as JSON
                ui.label(json.dumps(conditions, indent=2, default=str)).classes('text-sm font-mono')

    async def _remove_monitored_symbol(self, analysis_id: int, symbol: str):
        """Remove a symbol from the monitored_symbols state and reload the page."""
        try:
            from sqlalchemy.orm import attributes

            with get_db() as session:
                ma = session.get(MarketAnalysis, analysis_id)
                if ma and ma.state and 'monitored_symbols' in ma.state:
                    monitored = ma.state.get('monitored_symbols', {})
                    if symbol in monitored:
                        del monitored[symbol]
                        ma.state['monitored_symbols'] = monitored
                        attributes.flag_modified(ma, 'state')
                        session.add(ma)
                        session.commit()
                        logger.info(f'Removed {symbol} from monitored symbols (analysis {analysis_id})')
                    else:
                        logger.warning(f'{symbol} not found in monitored symbols')
            ui.navigate.reload()
        except Exception as e:
            logger.error(f'Error removing {symbol} from monitors: {e}', exc_info=True)
            ui.notify(f'Error removing {symbol}: {e}', type='negative')

    # ------------------------------------------------------------------
    # Tab 4: Trades
    # ------------------------------------------------------------------

    def _render_trades(self):
        """Table of executed trades."""
        executed: List[Dict] = self.state.get('executed_trades', [])

        with ui.card().classes('w-full'):
            ui.label('Executed Trades').classes('text-h5 mb-2')
            ui.label(f'{len(executed)} trades executed').classes('text-subtitle2 text-grey-7 mb-4')

            if not executed:
                with ui.card().classes('w-full p-8 text-center bg-grey-1'):
                    ui.icon('inbox', size='3rem', color='grey-5')
                    ui.label('No trades executed yet').classes('text-h6 text-grey-7 mt-2')
                return

            columns = [
                {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
                {'name': 'entry_price', 'label': 'Entry Price', 'field': 'entry_price', 'sortable': True},
                {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'sortable': True},
                {'name': 'strategy', 'label': 'Strategy', 'field': 'strategy', 'align': 'left'},
                {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'left'},
            ]

            rows = []
            for trade in executed:
                rows.append({
                    'symbol': trade.get('symbol', '-'),
                    'entry_price': self._fmt_number(trade.get('entry_price')),
                    'qty': trade.get('qty', '-'),
                    'strategy': trade.get('strategy', '-'),
                    'status': trade.get('status', '-'),
                })

            table = ui.table(
                columns=columns,
                rows=rows,
                row_key='symbol',
                pagination={'rowsPerPage': 25},
            ).classes('w-full')

            # Color-code the status column
            table.add_slot('body-cell-status', r'''
                <q-td :props="props">
                    <q-badge
                        :color="props.row.status === 'filled' ? 'green'
                              : props.row.status === 'pending' ? 'orange'
                              : props.row.status === 'cancelled' ? 'red'
                              : 'grey'"
                        :label="props.row.status"
                    />
                </q-td>
            ''')

    # ------------------------------------------------------------------
    # Tab 5: Raw Data
    # ------------------------------------------------------------------

    def _render_raw_data(self):
        """Query AnalysisOutput records and display in expandable sections grouped by category."""
        with ui.card().classes('w-full'):
            ui.label('Raw Data').classes('text-h5 mb-2')
            ui.label('AnalysisOutput records associated with this analysis').classes('text-caption text-grey-7 mb-4')

            outputs: List[AnalysisOutput] = []
            try:
                with get_db() as session:
                    statement = (
                        select(AnalysisOutput)
                        .where(AnalysisOutput.market_analysis_id == self.market_analysis.id)
                        .order_by(AnalysisOutput.created_at)
                    )
                    outputs = list(session.exec(statement).all())
            except Exception as e:
                logger.error(f"Error querying AnalysisOutput records: {e}", exc_info=True)
                ui.label(f'Error loading raw data: {e}').classes('text-red-600')
                return

            if not outputs:
                ui.label('No raw data records found.').classes('text-grey-7')
                return

            ui.label(f'{len(outputs)} records found').classes('text-subtitle2 mb-4')

            # Group by category (provider_category or type)
            grouped: Dict[str, List[AnalysisOutput]] = {}
            for output in outputs:
                group_key = output.provider_category or output.type or 'other'
                grouped.setdefault(group_key, []).append(output)

            for category, items in grouped.items():
                with ui.expansion(
                    f'{category} ({len(items)} records)',
                    icon='folder',
                ).classes('w-full mb-2'):
                    for item in items:
                        label_parts = [item.name]
                        if item.symbol:
                            label_parts.append(f'[{item.symbol}]')
                        if item.provider_name:
                            label_parts.append(f'({item.provider_name})')

                        with ui.expansion(' '.join(label_parts), icon='description').classes('w-full mb-1'):
                            if item.text:
                                try:
                                    parsed = json.loads(item.text)
                                except (json.JSONDecodeError, TypeError):
                                    parsed = None

                                # Render as chat conversation if it has prompt + response
                                if isinstance(parsed, dict) and 'prompt' in parsed and 'response' in parsed:
                                    self._render_llm_conversation(parsed)
                                elif parsed is not None:
                                    # Regular JSON pretty display
                                    formatted = json.dumps(parsed, indent=2, default=str)
                                    with ui.scroll_area().classes('w-full').style('min-height: 50vh; max-height: 80vh;'):
                                        with ui.element('pre').classes(
                                            'whitespace-pre-wrap text-xs p-3 rounded font-mono overflow-x-auto'
                                        ).style('background-color: var(--q-dark-page, #1d1d1d); color: #e0e0e0;'):
                                            ui.label(formatted).style('color: #e0e0e0;')
                                else:
                                    with ui.scroll_area().classes('w-full').style('min-height: 50vh; max-height: 80vh;'):
                                        ui.markdown(item.text).classes('text-sm')
                            else:
                                ui.label('No text content').classes('text-grey-6 text-sm')

    # ------------------------------------------------------------------
    # LLM Conversation renderer
    # ------------------------------------------------------------------

    def _render_llm_conversation(self, data: Dict[str, Any]):
        """Render a prompt/response pair as a chat conversation."""
        prompt_text = data.get('prompt', '')
        response_text = data.get('response', '')

        with ui.scroll_area().classes('w-full').style('min-height: 50vh; max-height: 80vh;'):
            with ui.column().classes('w-full gap-3 p-2'):
                # Prompt bubble (user/system side - left aligned)
                with ui.row().classes('w-full justify-start'):
                    with ui.card().classes('p-3').style(
                        'max-width: 85%; background-color: #2d3748; border-radius: 12px 12px 12px 2px;'
                    ):
                        with ui.row().classes('items-center gap-2 mb-1'):
                            ui.icon('person', size='xs').style('color: #90cdf4;')
                            ui.label('Prompt').classes('text-xs font-bold').style('color: #90cdf4;')
                        with ui.element('pre').classes(
                            'whitespace-pre-wrap text-xs font-mono m-0'
                        ).style('color: #e2e8f0;'):
                            ui.label(prompt_text).style('color: #e2e8f0;')

                # Response bubble (assistant side - right aligned)
                with ui.row().classes('w-full justify-end'):
                    with ui.card().classes('p-3').style(
                        'max-width: 85%; background-color: #1a365d; border-radius: 12px 12px 2px 12px;'
                    ):
                        with ui.row().classes('items-center gap-2 mb-1'):
                            ui.icon('smart_toy', size='xs').style('color: #68d391;')
                            ui.label('LLM Response').classes('text-xs font-bold').style('color: #68d391;')

                        # Try to parse response as JSON for pretty display
                        try:
                            parsed_response = json.loads(response_text)
                            formatted = json.dumps(parsed_response, indent=2, default=str)
                            with ui.element('pre').classes(
                                'whitespace-pre-wrap text-xs font-mono m-0'
                            ).style('color: #e2e8f0;'):
                                ui.label(formatted).style('color: #e2e8f0;')
                        except (json.JSONDecodeError, TypeError):
                            with ui.element('pre').classes(
                                'whitespace-pre-wrap text-xs font-mono m-0'
                            ).style('color: #e2e8f0;'):
                                ui.label(response_text).style('color: #e2e8f0;')

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_number(value: Any) -> str:
        """Format a numeric value for display, preserving None as '-'."""
        if value is None:
            return '-'
        try:
            v = float(value)
            if v >= 1:
                return f'${v:,.2f}'
            return f'${v:,.4f}'
        except (ValueError, TypeError):
            return str(value)

    @staticmethod
    def _fmt_int(value: Any) -> str:
        """Format an integer value with thousands separators."""
        if value is None:
            return '-'
        try:
            return f'{int(value):,}'
        except (ValueError, TypeError):
            return str(value)

    @staticmethod
    def _fmt_market_cap(value: Any) -> str:
        """Format market cap as a human-readable string."""
        if value is None:
            return '-'
        try:
            v = float(value)
            if v >= 1_000_000_000:
                return f'${v / 1_000_000_000:.2f}B'
            if v >= 1_000_000:
                return f'${v / 1_000_000:.2f}M'
            if v >= 1_000:
                return f'${v / 1_000:.1f}K'
            return f'${v:,.0f}'
        except (ValueError, TypeError):
            return str(value)
