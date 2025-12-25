"""
Date/Timezone Utility Functions for BA2 Trade Platform

This module provides utilities for consistent date/timezone handling throughout the application:
- All dates are stored in UTC in the database
- All dates are converted to local timezone for UI display
- All date inputs from users are converted to UTC before storage

Key Principles:
1. Database Storage: All DateTime fields are stored in UTC (timezone.utc)
2. UI Display: All dates shown to users are in their local timezone
3. API/Data: All dates returned from external APIs should be stored in UTC
4. Consistency: All DateTime fields use datetime.now(timezone.utc) as default factory
"""

from datetime import datetime, timezone, timedelta
import pytz
from typing import Optional, Tuple
from ..logger import logger


def get_user_local_timezone() -> pytz.timezone:
    """
    Get the user's local timezone.
    
    For now, returns system local timezone.
    In future, can be extended to read from user preferences/settings.
    
    Returns:
        pytz.timezone: The user's local timezone
    """
    try:
        # Try to get local timezone using tzlocal package
        try:
            from tzlocal import get_localzone
            local_tz = get_localzone()
            # tzlocal returns a ZoneInfo or pytz timezone, ensure it's pytz
            if hasattr(local_tz, 'zone'):
                return pytz.timezone(local_tz.zone)
            elif hasattr(local_tz, 'key'):
                return pytz.timezone(local_tz.key)
            else:
                # Fallback if we can't get zone name
                raise ValueError(f"Cannot get zone name from {local_tz}")
        except ImportError:
            # tzlocal not available, fall back to system time
            import time
            if time.daylight:
                utc_offset_sec = -time.altzone
            else:
                utc_offset_sec = -time.timezone
            
            # Convert offset to hours for timezone lookup
            offset_hours = utc_offset_sec / 3600
            
            # For common offsets, use fixed offset timezone
            # This creates a timezone like Etc/GMT+X (note: sign is reversed in Etc/GMT)
            if offset_hours == int(offset_hours):
                # Use Etc/GMT notation (sign is reversed: Etc/GMT-2 is UTC+02:00)
                etc_offset = -int(offset_hours)
                if etc_offset == 0:
                    return pytz.utc
                elif etc_offset > 0:
                    tz_name = f"Etc/GMT+{etc_offset}"
                else:
                    tz_name = f"Etc/GMT{etc_offset}"  # Negative sign already included
                
                try:
                    return pytz.timezone(tz_name)
                except:
                    pass
            
            # Last resort: return UTC
            logger.warning(f"Could not map UTC offset {offset_hours} hours to timezone, using UTC")
            return pytz.utc
            
    except Exception as e:
        logger.warning(f"Could not determine user local timezone: {e}, defaulting to UTC")
        return pytz.utc


def utc_to_local(utc_dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convert a UTC datetime to local timezone.
    
    Args:
        utc_dt: Datetime in UTC timezone (or naive datetime assumed to be UTC)
        
    Returns:
        Datetime in local timezone, or None if input is None
        
    Example:
        utc_dt = datetime.now(timezone.utc)
        local_dt = utc_to_local(utc_dt)
        ui.label(f"Time: {local_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    """
    if utc_dt is None:
        return None
    
    try:
        # If datetime is naive, assume it's UTC
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        
        # Convert to local timezone
        local_tz = get_user_local_timezone()
        local_dt = utc_dt.astimezone(local_tz)
        return local_dt
    except Exception as e:
        logger.error(f"Error converting UTC to local timezone: {e}", exc_info=True)
        return utc_dt


def local_to_utc(local_dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convert a local timezone datetime to UTC.
    
    Args:
        local_dt: Datetime in local timezone (or naive datetime assumed to be local)
        
    Returns:
        Datetime in UTC timezone, or None if input is None
        
    Example:
        user_input = datetime(2025, 10, 22, 14, 30, 0)  # User enters 2:30 PM
        utc_dt = local_to_utc(user_input)
        save_to_database(utc_dt)  # Save UTC time to DB
    """
    if local_dt is None:
        return None
    
    try:
        # If datetime is naive, assume it's in local timezone
        if local_dt.tzinfo is None:
            local_tz = get_user_local_timezone()
            local_dt = local_dt.replace(tzinfo=local_tz)
        
        # Convert to UTC
        utc_dt = local_dt.astimezone(timezone.utc)
        return utc_dt
    except Exception as e:
        logger.error(f"Error converting local to UTC timezone: {e}", exc_info=True)
        return local_dt


def format_for_display(dt: Optional[datetime], format_string: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """
    Format a datetime for display in UI.
    
    Automatically converts from UTC to local timezone if needed.
    
    Args:
        dt: Datetime to format (can be UTC or local, naive or aware)
        format_string: strftime format string
        
    Returns:
        Formatted datetime string in local timezone
        
    Examples:
        >>> dt_utc = datetime.now(timezone.utc)
        >>> format_for_display(dt_utc)
        '2025-10-22 09:15:30 PDT'
        
        >>> dt_utc = datetime.now(timezone.utc)
        >>> format_for_display(dt_utc, "%m/%d/%Y %I:%M %p")
        '10/22/2025 09:15 AM'
    """
    if dt is None:
        return "N/A"
    
    try:
        # Convert to local timezone
        local_dt = utc_to_local(dt)
        if local_dt:
            return local_dt.strftime(format_string)
        return str(dt)
    except Exception as e:
        logger.error(f"Error formatting datetime for display: {e}", exc_info=True)
        return str(dt)


def format_relative(dt: Optional[datetime]) -> str:
    """
    Format a datetime as relative time (e.g., "2 hours ago", "tomorrow").
    
    Args:
        dt: Datetime to format
        
    Returns:
        Relative time string
        
    Examples:
        >>> now = datetime.now(timezone.utc)
        >>> past = now - timedelta(hours=2)
        >>> format_relative(past)
        '2 hours ago'
        
        >>> future = now + timedelta(days=1)
        >>> format_relative(future)
        'tomorrow'
    """
    if dt is None:
        return "N/A"
    
    try:
        # Convert to local timezone
        local_dt = utc_to_local(dt)
        if not local_dt:
            return str(dt)
        
        # Calculate time difference
        now = datetime.now(timezone.utc)
        now_local = utc_to_local(now)
        if not now_local:
            now_local = now
        
        diff = now_local - local_dt
        
        # Format based on difference
        if diff.total_seconds() < 0:
            # Future time
            diff = -diff
            if diff < timedelta(minutes=1):
                return "in a moment"
            elif diff < timedelta(hours=1):
                mins = int(diff.total_seconds() // 60)
                return f"in {mins} minute{'s' if mins != 1 else ''}"
            elif diff < timedelta(days=1):
                hours = int(diff.total_seconds() // 3600)
                return f"in {hours} hour{'s' if hours != 1 else ''}"
            elif diff < timedelta(days=7):
                days = int(diff.total_seconds() // 86400)
                if days == 1:
                    return "tomorrow"
                return f"in {days} days"
            else:
                return "in the future"
        else:
            # Past time
            if diff < timedelta(minutes=1):
                return "just now"
            elif diff < timedelta(hours=1):
                mins = int(diff.total_seconds() // 60)
                return f"{mins} minute{'s' if mins != 1 else ''} ago"
            elif diff < timedelta(days=1):
                hours = int(diff.total_seconds() // 3600)
                return f"{hours} hour{'s' if hours != 1 else ''} ago"
            elif diff < timedelta(days=7):
                days = int(diff.total_seconds() // 86400)
                if days == 1:
                    return "yesterday"
                return f"{days} days ago"
            else:
                return "long ago"
    except Exception as e:
        logger.error(f"Error formatting relative datetime: {e}", exc_info=True)
        return str(dt)


def get_utc_now() -> datetime:
    """
    Get current time in UTC.
    
    Use this instead of datetime.now() when you need UTC time.
    
    Returns:
        Current time in UTC timezone
        
    Example:
        >>> created_at = get_utc_now()
        >>> db_model.created_at = created_at
    """
    return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Ensure a datetime is in UTC timezone.
    
    If the datetime is naive, assumes it's already in UTC.
    If the datetime is timezone-aware, converts it to UTC.
    
    Args:
        dt: Datetime to ensure is UTC
        
    Returns:
        Datetime in UTC timezone, or None if input is None
    """
    if dt is None:
        return None
    
    try:
        if dt.tzinfo is None:
            # Naive datetime - assume it's already UTC
            return dt.replace(tzinfo=timezone.utc)
        else:
            # Timezone-aware - convert to UTC
            return dt.astimezone(timezone.utc)
    except Exception as e:
        logger.error(f"Error ensuring datetime is UTC: {e}", exc_info=True)
        return dt
