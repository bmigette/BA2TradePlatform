from nicegui import ui

from ...core.db import get_all_instances
from ...core.models import AccountDefinition
from ...modules.accounts import providers

class AccountOverviewTab:
    def __init__(self):
        self.render()

    def render(self):
        accounts = get_all_instances(AccountDefinition)
        all_positions = []
        for acc in accounts:
            provider_cls = providers.get(acc.provider)
            if provider_cls:
                provider_obj = provider_cls(acc.id)
                try:
                    positions = provider_obj.get_positions()
                    # Attach account name to each position for clarity
                    for pos in positions:
                        pos_dict = pos if isinstance(pos, dict) else dict(pos)
                        pos_dict['account'] = acc.name
                    # Format all float values to 2 decimal places
                    for k, v in pos_dict.items():
                        if isinstance(v, float):
                            pos_dict[k] = f"{v:.2f}"
                    all_positions.append(pos_dict)
                except Exception as e:
                    all_positions.append({'account': acc.name, 'error': str(e)})
        
        columns = [
            {'name': 'account', 'label': 'Account', 'field': 'account'},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol'},
            {'name': 'exchange', 'label': 'Exchange', 'field': 'exchange'},
            {'name': 'asset_class', 'label': 'Asset Class', 'field': 'asset_class'},
            {'name': 'side', 'label': 'Side', 'field': 'side'},
            {'name': 'qty', 'label': 'Quantity', 'field': 'qty'},
            {'name': 'current_price', 'label': 'Current Price', 'field': 'current_price'},
            {'name': 'avg_entry_price', 'label': 'Entry Price', 'field': 'avg_entry_price'},
            {'name': 'market_value', 'label': 'Market Value', 'field': 'market_value'},
            {'name': 'unrealized_pl', 'label': 'Unrealized P/L', 'field': 'unrealized_pl'},
            {'name': 'unrealized_plpc', 'label': 'P/L %', 'field': 'unrealized_plpc'},
            {'name': 'change_today', 'label': 'Today Change %', 'field': 'change_today'}
        ]
        with ui.card():
            ui.label('Open Positions Across All Accounts')
            ui.table(columns=columns, rows=all_positions, row_key='account').classes('w-full')

class TradeRecommendationHistoryTab:
    def __init__(self):
        self.render()
    def render(self):
        with ui.card():
            ui.label('Trade Recommendation History content goes here.')

def content() -> None:
    with ui.tabs() as tabs:
        ui.tab('Account Overview')
        ui.tab('Trade Recommendation History')

    with ui.tab_panels(tabs, value='Account Overview').classes('w-full'):
        with ui.tab_panel('Account Overview'):
            AccountOverviewTab()
        with ui.tab_panel('Trade Recommendation History'):
            TradeRecommendationHistoryTab()