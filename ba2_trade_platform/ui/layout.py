from contextlib import contextmanager

from .menus import topmenu, sidemenu
from .theme import COLORS

from nicegui import ui


@contextmanager
def layout_render(navigation_title: str):
    """Custom page frame for modern AI trading platform UI"""
    
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
    
    # Add custom CSS for modern dark theme
    ui.add_head_html('''
    <style>
        /* Base styles */
        body {
            background: linear-gradient(135deg, #1a1f2e 0%, #0f1419 100%) !important;
            min-height: 100vh;
        }
        
        /* Cards - glassmorphism effect */
        .q-card, .nicegui-card {
            background: rgba(37, 43, 59, 0.8) !important;
            backdrop-filter: blur(10px) !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            border-radius: 16px !important;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3) !important;
        }
        
        /* Tables */
        .q-table {
            background: transparent !important;
        }
        .q-table thead tr, .q-table thead th {
            background: rgba(26, 31, 46, 0.9) !important;
            color: #a0aec0 !important;
            font-weight: 600 !important;
            text-transform: uppercase !important;
            font-size: 0.75rem !important;
            letter-spacing: 0.05em !important;
        }
        .q-table tbody tr {
            background: rgba(37, 43, 59, 0.5) !important;
            transition: all 0.2s ease !important;
        }
        .q-table tbody tr:hover {
            background: rgba(0, 212, 170, 0.1) !important;
        }
        .q-table td, .q-table th {
            border-color: rgba(255, 255, 255, 0.05) !important;
        }
        
        /* Buttons */
        .q-btn {
            border-radius: 8px !important;
            text-transform: none !important;
            font-weight: 500 !important;
        }
        .q-btn--flat {
            background: rgba(255, 255, 255, 0.05) !important;
        }
        .q-btn--flat:hover {
            background: rgba(255, 255, 255, 0.1) !important;
        }
        
        /* Inputs */
        .q-field--outlined .q-field__control {
            background: rgba(26, 31, 46, 0.6) !important;
            border-color: rgba(255, 255, 255, 0.1) !important;
            border-radius: 8px !important;
        }
        .q-field--outlined:hover .q-field__control {
            border-color: rgba(0, 212, 170, 0.5) !important;
        }
        .q-field--focused .q-field__control {
            border-color: #00d4aa !important;
            box-shadow: 0 0 0 2px rgba(0, 212, 170, 0.2) !important;
        }
        
        /* Badges */
        .q-badge {
            border-radius: 6px !important;
            font-weight: 600 !important;
            padding: 4px 8px !important;
        }
        
        /* Scrollbar */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        ::-webkit-scrollbar-track {
            background: rgba(26, 31, 46, 0.5);
        }
        ::-webkit-scrollbar-thumb {
            background: rgba(160, 174, 192, 0.3);
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: rgba(160, 174, 192, 0.5);
        }
        
        /* Text colors */
        .text-primary-custom { color: #ffffff !important; }
        .text-secondary-custom { color: #a0aec0 !important; }
        .text-accent { color: #00d4aa !important; }
        .text-bullish { color: #00d4aa !important; }
        .text-bearish { color: #ff6b6b !important; }
        
        /* Stat cards */
        .stat-card {
            background: linear-gradient(135deg, rgba(37, 43, 59, 0.9) 0%, rgba(26, 31, 46, 0.9) 100%) !important;
            border: 1px solid rgba(255, 255, 255, 0.08) !important;
            border-radius: 16px !important;
            padding: 1.5rem !important;
            transition: all 0.3s ease !important;
        }
        .stat-card:hover {
            border-color: rgba(0, 212, 170, 0.3) !important;
            transform: translateY(-2px) !important;
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.4) !important;
        }
        .stat-value {
            font-size: 2rem !important;
            font-weight: 700 !important;
            background: linear-gradient(135deg, #00d4aa 0%, #4dabf7 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .stat-label {
            color: #a0aec0 !important;
            font-size: 0.875rem !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            margin-top: 0.5rem !important;
        }
        
        /* Alert banners */
        .alert-banner {
            border-radius: 12px !important;
            backdrop-filter: blur(10px) !important;
        }
        .alert-banner.warning {
            background: rgba(255, 217, 61, 0.15) !important;
            border-left: 4px solid #ffd93d !important;
        }
        .alert-banner.danger {
            background: rgba(255, 107, 107, 0.15) !important;
            border-left: 4px solid #ff6b6b !important;
        }
        .alert-banner.info {
            background: rgba(77, 171, 247, 0.15) !important;
            border-left: 4px solid #4dabf7 !important;
        }
        .alert-banner.success {
            background: rgba(0, 212, 170, 0.15) !important;
            border-left: 4px solid #00d4aa !important;
        }
        
        /* Left drawer */
        .q-drawer {
            background: linear-gradient(180deg, #1a1f2e 0%, #0f1419 100%) !important;
        }
        .q-item {
            border-radius: 8px !important;
            margin: 4px 8px !important;
            transition: all 0.2s ease !important;
        }
        .q-item:hover {
            background: rgba(0, 212, 170, 0.15) !important;
        }
        .q-item--active {
            background: rgba(0, 212, 170, 0.2) !important;
            border-left: 3px solid #00d4aa !important;
        }
        
        /* Header */
        .q-header {
            background: rgba(26, 31, 46, 0.95) !important;
            backdrop-filter: blur(10px) !important;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08) !important;
        }
        
        /* Tabs */
        .q-tabs {
            background: transparent !important;
        }
        .q-tab {
            color: #a0aec0 !important;
            opacity: 0.8 !important;
        }
        .q-tab--active {
            color: #00d4aa !important;
            opacity: 1 !important;
        }
        .q-tab__indicator {
            background: #00d4aa !important;
        }
        
        /* Charts container */
        .chart-container {
            background: rgba(37, 43, 59, 0.5) !important;
            border-radius: 12px !important;
            padding: 1rem !important;
            border: 1px solid rgba(255, 255, 255, 0.05) !important;
        }
        
        /* Glow effects for important elements */
        .glow-accent {
            box-shadow: 0 0 20px rgba(0, 212, 170, 0.3) !important;
        }
        .glow-danger {
            box-shadow: 0 0 20px rgba(255, 107, 107, 0.3) !important;
        }
        
        /* Pulse animation for live indicators */
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .pulse {
            animation: pulse 2s ease-in-out infinite;
        }
        
        /* Number formatting */
        .number-positive { color: #00d4aa !important; }
        .number-negative { color: #ff6b6b !important; }
        
        /* Separator */
        .q-separator {
            background: rgba(255, 255, 255, 0.08) !important;
        }
        
        /* Dialog */
        .q-dialog__inner > .q-card {
            background: rgba(37, 43, 59, 0.98) !important;
            backdrop-filter: blur(20px) !important;
        }
        
        /* Tooltip */
        .q-tooltip {
            background: rgba(26, 31, 46, 0.95) !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            border-radius: 8px !important;
        }
        
        /* Expansion items */
        .q-expansion-item {
            background: transparent !important;
        }
        .q-expansion-item__container {
            background: rgba(37, 43, 59, 0.3) !important;
            border-radius: 8px !important;
        }
        
        /* Select dropdown */
        .q-menu {
            background: rgba(37, 43, 59, 0.98) !important;
            backdrop-filter: blur(10px) !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            border-radius: 8px !important;
        }
        .q-item__label {
            color: #e2e8f0 !important;
        }
    </style>
    ''')
    
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
            
            topmenu()
    
    # Main content area with padding
    with ui.column().classes('w-full p-6 text-white'):
        yield
