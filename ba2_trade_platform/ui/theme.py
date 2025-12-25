"""
BA2 Trade Platform - UI Theme Constants

Modern dark theme color palette for AI Trading Platform.
Import these constants to maintain consistent styling across the application.
"""

# Main color palette
COLORS = {
    # Backgrounds
    'primary': '#1a1f2e',      # Deep navy - main background
    'secondary': '#252b3b',    # Lighter navy - cards/panels
    
    # Accent colors
    'accent': '#00d4aa',       # Teal/mint - primary accent (bullish)
    'accent_red': '#ff6b6b',   # Coral red - bearish/alerts
    'accent_blue': '#4dabf7',  # Sky blue - info/links
    'accent_purple': '#9775fa', # Purple - AI/expert related
    
    # Text colors
    'text_primary': '#ffffff',  # White text
    'text_secondary': '#a0aec0', # Muted text
    
    # Utility colors
    'border': '#2d3748',       # Border color
    'success': '#00d4aa',      # Green/teal
    'warning': '#ffd93d',      # Yellow
    'danger': '#ff6b6b',       # Red
    'info': '#4dabf7',         # Blue
}


# CSS classes for common styling patterns
CSS_CLASSES = {
    # Cards
    'card': 'rounded-2xl shadow-lg',
    'card_hover': 'hover:shadow-xl transition-all duration-300',
    
    # Alert banners
    'alert_danger': 'alert-banner danger',
    'alert_warning': 'alert-banner warning',
    'alert_info': 'alert-banner info',
    'alert_success': 'alert-banner success',
    
    # Stats/KPI cards
    'stat_card': 'stat-card',
    'stat_value': 'stat-value',
    'stat_label': 'stat-label',
    
    # Numbers
    'number_positive': 'number-positive font-bold',
    'number_negative': 'number-negative font-bold',
    
    # Text
    'text_muted': 'text-secondary-custom',
    'text_accent': 'text-accent',
}


# Status colors for various states
STATUS_COLORS = {
    # Order status
    'PENDING': 'blue-grey',
    'SUBMITTED': 'blue',
    'PARTIAL': 'cyan',
    'FILLED': 'positive',
    'CANCELLED': 'grey',
    'ERROR': 'negative',
    'REJECTED': 'red',
    
    # Transaction status
    'WAITING': 'blue-grey',
    'OPENED': 'positive',
    'CLOSING': 'orange',
    'CLOSED': 'grey',
    
    # Expert status
    'ACTIVE': 'positive',
    'INACTIVE': 'grey',
    'ERROR': 'negative',
}


def get_pnl_class(value: float) -> str:
    """Get CSS class for P/L value coloring."""
    if value > 0:
        return CSS_CLASSES['number_positive']
    elif value < 0:
        return CSS_CLASSES['number_negative']
    return ''


def get_pnl_color(value: float) -> str:
    """Get color code for P/L value."""
    if value > 0:
        return COLORS['success']
    elif value < 0:
        return COLORS['danger']
    return COLORS['text_secondary']
