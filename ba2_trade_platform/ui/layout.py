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
        /* Base styles - DEFAULT TEXT COLOR WHITE */
        body {
            background: linear-gradient(135deg, #1a1f2e 0%, #0f1419 100%) !important;
            min-height: 100vh;
            color: #ffffff !important;
        }
        
        /* Global default text color */
        *, *::before, *::after {
            color: inherit;
        }
        
        /* Force white text on common elements */
        p, span, div, label, h1, h2, h3, h4, h5, h6, li, td, th, a {
            color: #ffffff;
        }
        
        /* Cards - glassmorphism effect */
        .q-card, .nicegui-card {
            background: rgba(37, 43, 59, 0.8) !important;
            backdrop-filter: blur(10px) !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            border-radius: 16px !important;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3) !important;
            color: #ffffff !important;
        }
        .q-card__section, .q-card__actions {
            background: transparent !important;
            color: #ffffff !important;
        }
        .q-card--dark {
            background: rgba(37, 43, 59, 0.8) !important;
        }
        
        /* Fix white backgrounds */
        .q-field__control,
        .q-field__native,
        .q-select__dropdown-icon,
        .q-field__append,
        .q-field__prepend {
            background: transparent !important;
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
        .q-table tbody td {
            color: #ffffff !important;
        }
        
        /* Table pagination/bottom - dark theme */
        .q-table__bottom {
            background: rgba(26, 31, 46, 0.9) !important;
            color: #a0aec0 !important;
            border-top: 1px solid rgba(255, 255, 255, 0.1) !important;
        }
        .q-table__bottom .q-btn {
            color: #a0aec0 !important;
        }
        .q-table__bottom .q-btn:hover {
            color: #ffffff !important;
            background: rgba(0, 212, 170, 0.2) !important;
        }
        .q-table .q-table__control {
            color: #a0aec0 !important;
        }
        .q-table__bottom .q-select {
            color: #ffffff !important;
        }
        .q-table__bottom .q-select .q-field__native {
            color: #ffffff !important;
        }
        .q-table__bottom .q-field__control {
            background: rgba(37, 43, 59, 0.8) !important;
        }
        .q-table__separator {
            background: rgba(255, 255, 255, 0.1) !important;
        }
        
        /* Pagination */
        .q-pagination .q-btn {
            color: #a0aec0 !important;
        }
        .q-pagination .q-btn--active {
            color: #ffffff !important;
            background: rgba(0, 212, 170, 0.3) !important;
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
        
        /* Inputs - ALL variants */
        .q-field .q-field__control {
            background: rgba(26, 31, 46, 0.6) !important;
            border-color: rgba(255, 255, 255, 0.1) !important;
            border-radius: 8px !important;
        }
        .q-field--outlined .q-field__control {
            background: rgba(26, 31, 46, 0.6) !important;
            border-color: rgba(255, 255, 255, 0.1) !important;
            border-radius: 8px !important;
        }
        .q-field--filled .q-field__control {
            background: rgba(26, 31, 46, 0.6) !important;
        }
        .q-field--standout .q-field__control {
            background: rgba(26, 31, 46, 0.6) !important;
        }
        .q-field:hover .q-field__control {
            border-color: rgba(0, 212, 170, 0.5) !important;
        }
        .q-field--outlined:hover .q-field__control {
            border-color: rgba(0, 212, 170, 0.5) !important;
        }
        .q-field--focused .q-field__control {
            border-color: #00d4aa !important;
            box-shadow: 0 0 0 2px rgba(0, 212, 170, 0.2) !important;
        }
        /* Input text colors */
        .q-field .q-field__native,
        .q-field input,
        .q-field textarea,
        .q-field .q-field__prefix,
        .q-field .q-field__suffix {
            color: #ffffff !important;
        }
        .q-field .q-field__label {
            color: #a0aec0 !important;
        }
        .q-field--float .q-field__label {
            color: #a0aec0 !important;
        }
        
        /* Fix label/placeholder overlap - float label when placeholder present */
        .q-field--labeled .q-field__native[placeholder]:not(:placeholder-shown) ~ .q-field__label,
        .q-field--labeled .q-field__native[placeholder] ~ .q-field__label {
            transform: translateY(-60%) scale(0.75) !important;
            background: #1a1f2e !important;
            padding: 0 4px !important;
        }
        /* Also fix for inputs with placeholder attribute */
        .q-field .q-field__native::placeholder {
            color: rgba(160, 174, 192, 0.6) !important;
            opacity: 1 !important;
        }
        
        /* Select/Dropdown */
        .q-select .q-field__native span,
        .q-select .q-chip__content,
        .q-select__dropdown-icon {
            color: #ffffff !important;
        }
        .q-menu {
            background: rgba(37, 43, 59, 0.98) !important;
            backdrop-filter: blur(10px) !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            border-radius: 8px !important;
        }
        .q-item__label {
            color: #ffffff !important;
        }
        .q-item__label--caption {
            color: #a0aec0 !important;
        }
        /* Multi-select chips */
        .q-chip {
            background: rgba(0, 212, 170, 0.2) !important;
            color: #ffffff !important;
        }
        .q-chip--dense {
            background: rgba(255, 255, 255, 0.1) !important;
        }
        .q-chip__icon,
        .q-chip__content {
            color: #ffffff !important;
        }
        /* Virtual scroll list items */
        .q-virtual-scroll__content .q-item {
            color: #ffffff !important;
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
        
        /* Override Tailwind gray colors for dark theme visibility */
        .text-gray-400 { color: #b0bec5 !important; }
        .text-gray-500 { color: #a0aec0 !important; }
        .text-gray-600 { color: #90a4ae !important; }
        .text-gray-700 { color: #78909c !important; }
        
        /* Override Tailwind colored text for better visibility on dark */
        .text-blue-600 { color: #4dabf7 !important; }
        .text-green-600 { color: #00d4aa !important; }
        .text-red-600 { color: #ff6b6b !important; }
        .text-orange-600 { color: #ffa94d !important; }
        .text-yellow-600 { color: #ffd93d !important; }
        
        /* Override Quasar grey text classes for dark theme */
        .text-grey, .text-grey-1, .text-grey-2, .text-grey-3 { color: #ffffff !important; }
        .text-grey-4, .text-grey-5 { color: #e0e0e0 !important; }
        .text-grey-6 { color: #b0bec5 !important; }
        .text-grey-7 { color: #90a4ae !important; }
        .text-grey-8 { color: #78909c !important; }
        .text-grey-9, .text-grey-10 { color: #607d8b !important; }
        
        /* Quasar dark mode overrides - FORCE ALL TEXT WHITE */
        .q-dark, body.body--dark {
            color: #ffffff !important;
        }
        
        /* All Quasar text elements */
        .q-field__label,
        .q-field__native,
        .q-input__inner,
        .q-select__dropdown-icon,
        .q-icon,
        .q-item,
        .q-item__section,
        .q-list,
        .q-expansion-item,
        .q-tab__label,
        .q-toolbar__title,
        .q-btn__content {
            color: #ffffff !important;
        }
        
        /* Form labels - slightly muted */
        .q-field__label {
            color: #a0aec0 !important;
        }
        
        /* Textarea and input placeholder */
        .q-field input::placeholder,
        .q-field textarea::placeholder {
            color: #78909c !important;
        }
        
        /* Quasar page/body backgrounds */
        .q-page, .q-layout, .q-page-container {
            background: transparent !important;
        }
        
        /* Override any white backgrounds */
        .bg-white, .bg-grey-1, .bg-grey-2, .bg-grey-3, .bg-grey-4 {
            background: rgba(37, 43, 59, 0.8) !important;
        }
        
        /* Quasar specific white background overrides */
        .q-field--filled .q-field__control:before,
        .q-field--standout .q-field__control {
            background: rgba(26, 31, 46, 0.6) !important;
        }
        
        /* Tabs panel backgrounds */
        .q-tab-panel, .q-tab-panels {
            background: transparent !important;
            color: #ffffff !important;
        }
        
        /* Stepper backgrounds */
        .q-stepper, .q-stepper__content {
            background: transparent !important;
            color: #ffffff !important;
        }
        
        /* Any inline style overrides - more aggressive */
        [style*="background-color: white"],
        [style*="background-color: #fff"],
        [style*="background-color: rgb(255, 255, 255)"],
        [style*="background: white"],
        [style*="background: #fff"] {
            background: rgba(37, 43, 59, 0.8) !important;
        }
        
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
        
        /* Progress bars */
        .q-linear-progress {
            background: rgba(255, 255, 255, 0.1) !important;
        }
        .q-linear-progress__track {
            background: rgba(255, 255, 255, 0.1) !important;
        }
        .q-linear-progress__model {
            background: #00d4aa !important;
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
        .q-dialog .q-card__section {
            color: #ffffff !important;
        }
        .q-dialog .q-card-section--vert {
            color: #ffffff !important;
        }
        .q-dialog-plugin__form {
            color: #ffffff !important;
        }
        .q-dialog .q-card-actions {
            color: #ffffff !important;
        }
        
        /* Tooltip */
        .q-tooltip {
            background: rgba(26, 31, 46, 0.95) !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            border-radius: 8px !important;
            color: #ffffff !important;
        }
        
        /* Expansion items */
        .q-expansion-item {
            background: transparent !important;
        }
        .q-expansion-item__container {
            background: rgba(37, 43, 59, 0.3) !important;
            border-radius: 8px !important;
        }
        .q-expansion-item__content {
            color: #ffffff !important;
            background: rgba(37, 43, 59, 0.3) !important;
        }
        .q-expansion-item .q-item {
            background: rgba(37, 43, 59, 0.5) !important;
            color: #ffffff !important;
        }
        .q-expansion-item .q-item__label {
            color: #ffffff !important;
        }
        .q-expansion-item .q-icon {
            color: #a0aec0 !important;
        }
        
        /* Checkbox and Radio */
        .q-checkbox__label,
        .q-radio__label,
        .q-toggle__label {
            color: #ffffff !important;
        }
        
        /* List items */
        .q-list .q-item__section--main {
            color: #ffffff !important;
        }
        
        /* General text within cards */
        .q-card__section {
            color: #ffffff !important;
        }
        
        /* Date picker */
        .q-date {
            background: rgba(37, 43, 59, 0.98) !important;
            color: #ffffff !important;
        }
        .q-date__header {
            background: rgba(0, 212, 170, 0.2) !important;
        }
        
        /* Time picker */
        .q-time {
            background: rgba(37, 43, 59, 0.98) !important;
            color: #ffffff !important;
        }
        
        /* Hide NiceGUI documentation/help button - aggressive selectors */
        .nicegui-documentation,
        .q-page-sticky,
        button[title="Documentation"],
        .q-fab,
        .q-btn--fab,
        [class*="q-page-sticky"],
        div[style*="position: fixed"][style*="bottom"][style*="right"] > button,
        div[style*="position: fixed"][style*="bottom: 0"][style*="right: 0"] {
            display: none !important;
            visibility: hidden !important;
            opacity: 0 !important;
            pointer-events: none !important;
        }
        
        /* Colored buttons - ensure text is white and proper dark background blend */
        .q-btn[class*="bg-orange"], .q-btn.bg-orange {
            background: rgba(255, 152, 0, 0.9) !important;
            color: #ffffff !important;
        }
        .q-btn[class*="bg-orange"]:hover, .q-btn.bg-orange:hover {
            background: rgba(255, 152, 0, 1) !important;
        }
        .q-btn[class*="bg-blue"], .q-btn.bg-blue {
            background: rgba(33, 150, 243, 0.9) !important;
            color: #ffffff !important;
        }
        .q-btn[class*="bg-blue"]:hover, .q-btn.bg-blue:hover {
            background: rgba(33, 150, 243, 1) !important;
        }
        .q-btn[class*="bg-red"], .q-btn.bg-red {
            background: rgba(244, 67, 54, 0.9) !important;
            color: #ffffff !important;
        }
        .q-btn[class*="bg-red"]:hover, .q-btn.bg-red:hover {
            background: rgba(244, 67, 54, 1) !important;
        }
        .q-btn[class*="bg-green"], .q-btn.bg-green {
            background: rgba(76, 175, 80, 0.9) !important;
            color: #ffffff !important;
        }
        .q-btn[class*="bg-green"]:hover, .q-btn.bg-green:hover {
            background: rgba(76, 175, 80, 1) !important;
        }
        
        /* Ensure button text/icons are white */
        .q-btn .q-btn__content,
        .q-btn .q-icon {
            color: #ffffff !important;
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
