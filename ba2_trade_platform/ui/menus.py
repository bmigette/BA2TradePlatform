from nicegui import ui
from . import svg 


def sidemenu() -> None:
    """Modern sidebar navigation menu"""
    
    menu_items = [
        {'icon': 'dashboard', 'label': 'Overview', 'route': '/', 'description': 'Dashboard & Stats'},
        {'icon': 'analytics', 'label': 'Market Analysis', 'route': '/marketanalysis', 'description': 'Charts & Indicators'},
        {'icon': 'receipt_long', 'label': 'Activity Monitor', 'route': '/activitymonitor', 'description': 'System Logs'},
        {'icon': 'trending_up', 'label': 'Live Trades', 'route': '/livetrades', 'description': 'Active Positions'},
        {'icon': 'build', 'label': 'Tools', 'route': '/tools', 'description': 'Utilities'},
        {'icon': 'settings', 'label': 'Settings', 'route': '/settings', 'description': 'Configuration'},
    ]
    
    with ui.column().classes('w-full gap-1 px-2'):
        for item in menu_items:
            with ui.item(on_click=lambda r=item['route']: ui.navigate.to(r)).classes('rounded-lg hover:bg-white/10'):
                with ui.item_section().props('avatar'):
                    ui.icon(item['icon']).classes('text-accent')
                with ui.item_section():
                    ui.item_label(item['label']).classes('text-white font-medium')
                    ui.item_label(item['description']).props('caption').classes('text-secondary-custom text-xs')


def topmenu() -> None:
    """Top bar navigation actions"""
    with ui.row().classes('items-center gap-2'):
        # Notifications button (placeholder)
        ui.button(icon='notifications_none').props('flat round color=white size=sm').tooltip('Notifications')
        
        # GitHub link
        with ui.link(target='https://github.com/bmigette/BA2TradePlatform').classes('max-[365px]:hidden').tooltip('GitHub'):
            svg.github().classes('fill-white scale-125 m-1')
