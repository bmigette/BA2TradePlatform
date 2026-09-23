from nicegui import ui
from . import svg


# Module-level so the navigation contract is unit-testable without rendering
# anything (importing ui.main to inspect routes pulls the whole expert stack).
MENU_ITEMS = [
    {'icon': 'dashboard', 'label': 'Overview', 'route': '/', 'description': 'Dashboard & Stats'},
    {'icon': 'analytics', 'label': 'Market Analysis', 'route': '/marketanalysis', 'description': 'Experts Analysis'},
    {'icon': 'receipt_long', 'label': 'Activity Monitor', 'route': '/activitymonitor', 'description': 'System Logs'},
    {'icon': 'trending_up', 'label': 'Live Trades', 'route': '/livetrades', 'description': 'Active Positions'},
    {'icon': 'pie_chart', 'label': 'Portfolio Allocation', 'route': '/portfolioallocation', 'description': 'Manual Rebalancing'},
    {'icon': 'build', 'label': 'Tools', 'route': '/tools', 'description': 'Utilities'},
    {'icon': 'settings', 'label': 'Settings', 'route': '/settings', 'description': 'Configuration'},
]


def sidemenu() -> None:
    """Modern sidebar navigation menu"""
    with ui.column().classes('w-full gap-1 px-2'):
        for item in MENU_ITEMS:
            with ui.item(on_click=lambda r=item['route']: ui.navigate.to(r)).classes('rounded-lg hover:bg-white/10'):
                with ui.item_section().props('avatar'):
                    ui.icon(item['icon']).classes('text-accent')
                with ui.item_section():
                    ui.item_label(item['label']).classes('text-white font-medium')
                    ui.item_label(item['description']).props('caption').classes('text-secondary-custom text-xs')


def topmenu() -> None:
    """Top bar navigation actions"""
    with ui.row().classes('items-center gap-2'):
        # GitHub link
        # ``phone-hidden`` (styles.css), not an inline Tailwind breakpoint: the header's
        # phone layout is decided in ONE place, and this link was the item that pushed
        # the account selector onto a third header row below 640px.
        with ui.link(target='https://github.com/bmigette/BA2TradePlatform').classes('phone-hidden').tooltip('GitHub'):
            svg.github().classes('fill-white scale-125 m-1')
