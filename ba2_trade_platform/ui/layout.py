from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .menus import topmenu, sidemenu
from .theme import COLORS
from .account_filter_context import get_accounts_for_filter, get_selected_account_id, set_selected_account_id

from nicegui import ui, app


@contextmanager
def layout_render(navigation_title: str):
    """Custom page frame for modern AI trading platform UI"""
    
    # Serve static files
    static_dir = Path(__file__).parent / 'static'
    app.add_static_files('/static', static_dir)
    
    # Set Quasar/NiceGUI colors
    ui.colors(
        primary=COLORS['accent'],
        secondary=COLORS['accent_blue'],
        accent=COLORS['accent_purple'],
        positive=COLORS['success'],
        negative=COLORS['danger'],
        warning=COLORS['warning'],
        info=COLORS['accent_blue'],
        dark=COLORS['primary']
    )
    
    # Link to external CSS file
    ui.add_head_html('<link rel="stylesheet" href="/static/styles.css">')
    
    # Footer (hidden by default)
    with ui.footer(value=False) as footer:
        ui.label('BA2 Trade Platform © 2025').classes('text-secondary-custom')

    # Modern side drawer
    with ui.left_drawer().classes('bg-transparent') as left_drawer:
        # Logo/Brand section
        with ui.column().classes('w-full p-4 mb-4'):
            with ui.row().classes('items-center gap-3'):
                ui.icon('show_chart', size='lg').classes('text-accent')
                ui.label('BA2 Trade').classes('text-xl font-bold text-white')
            ui.label('AI Trading Platform').classes('text-xs text-secondary-custom mt-1')
        
        ui.separator().classes('mb-2')
        sidemenu()
        
        # Version info at bottom
        with ui.column().classes('absolute bottom-4 left-4 right-4'):
            ui.separator().classes('mb-4')
            ui.label('v2.0.0').classes('text-xs text-secondary-custom text-center w-full')

    # Help button
    with ui.page_sticky(position='bottom-right', x_offset=20, y_offset=20):
        ui.button(on_click=footer.toggle, icon='help_outline').props('fab color=accent').classes('glow-accent')

    # Modern header
    with ui.header().classes('items-center'):
        ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat round color=white')
        ui.space()
        
        # Page title with breadcrumb style
        with ui.row().classes('items-center gap-2'):
            ui.icon('chevron_right', size='sm').classes('text-secondary-custom')
            ui.label(navigation_title).classes('text-lg font-medium')
        
        ui.space()
        
        # Right side actions
        with ui.row().classes('items-center gap-2'):
            # Live indicator
            with ui.row().classes('items-center gap-1 mr-4'):
                ui.icon('fiber_manual_record', size='xs').classes('text-accent pulse')
                ui.label('LIVE').classes('text-xs font-medium text-accent')
            
            # Account filter dropdown
            _render_account_filter_dropdown()
            
            topmenu()
    
    # Main content area with padding
    with ui.column().classes('w-full p-6 text-white'):
        yield


def _render_account_filter_dropdown():
    """Render the account filter dropdown in the header."""
    # Get accounts for dropdown options
    account_options = get_accounts_for_filter()
    
    # Build options dict for ui.select: {value: label}
    # Use "all" string instead of None for the "All" option (None doesn't work well with ui.select)
    options_dict = {}
    for label, acc_id in account_options:
        key = "all" if acc_id is None else acc_id
        options_dict[key] = label
    
    # Get current selection - convert None to "all" for ui.select
    current_selection = get_selected_account_id()
    current_value = "all" if current_selection is None else current_selection
    
    async def on_account_change(e):
        """Handle account selection change."""
        new_value = e.value
        # Convert "all" back to None for storage
        account_id = None if new_value == "all" else new_value
        set_selected_account_id(account_id)
        # Soft reload via NiceGUI navigation (faster than full browser reload)
        # Gets current path and navigates to it, triggering page re-render
        current_path = await ui.run_javascript('window.location.pathname')
        await ui.navigate.to(current_path)
    
    with ui.row().classes('items-center gap-1 mr-4'):
        ui.icon('account_circle', size='xs').classes('text-secondary-custom')
        ui.select(
            options=options_dict,
            value=current_value,
            on_change=on_account_change
        ).props('dense outlined dark color=white').classes('text-xs min-w-32').style('font-size: 0.75rem;')
