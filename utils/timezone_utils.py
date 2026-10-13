"""
Timezone handling for display of stored timestamps.

All timestamps are persisted as naive UTC; this module converts them for
display according to the user's 'local' / 'utc' preference.
"""
from datetime import datetime, timezone
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class TimezoneManager:
    """Manages timezone conversions and user preferences."""
    
    # Timezone modes
    UTC = "utc"
    LOCAL = "local"
    
    def __init__(self, mode: str = LOCAL):
        """
        Initialize timezone manager.
        
        Args:
            mode: 'utc' or 'local' (default: 'local')
        """
        self.mode = mode
        logger.debug(f"TimezoneManager initialized in {mode} mode")
    
    def now(self) -> datetime:
        """
        Get current datetime in UTC (always store as naive UTC).
        
        Returns:
            datetime object (naive UTC)
        """
        return datetime.now(timezone.utc).replace(tzinfo=None)
    
    def to_display(self, dt: Optional[datetime]) -> str:
        """
        Convert naive UTC datetime to display string based on mode.
        
        IMPORTANT: Database always stores naive UTC times.
        This method converts them for display based on user preference.
        
        Args:
            dt: datetime object (assumed to be naive UTC from database)
            
        Returns:
            Formatted string for display
        """
        if dt is None:
            return "N/A"
        
        try:
            # Database stores naive UTC times
            # Make it timezone-aware UTC first
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            
            # Convert based on mode
            if self.mode == self.LOCAL:
                # Convert UTC to local time
                dt = dt.astimezone()
                return dt.strftime('%Y-%m-%d %I:%M:%S %p')
            else:
                # Display as UTC (24-hour format)
                return dt.strftime('%Y-%m-%d %H:%M:%S')
            
        except Exception as e:
            logger.error(f"Error formatting datetime: {e}")
            return str(dt)
    
    def to_short_display(self, dt: Optional[datetime]) -> str:
        """
        Convert naive UTC datetime to short display format.

        The year is always included - the product history spans multiple
        years, and "Oct 12" alone is ambiguous across them.

        Args:
            dt: datetime object (assumed to be naive UTC from database)

        Returns:
            Short formatted string (e.g., "Oct 12 2025, 3:45 PM")
        """
        if dt is None:
            return "N/A"

        try:
            # Database stores naive UTC times
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            # Convert based on mode
            if self.mode == self.LOCAL:
                dt = dt.astimezone()
                return dt.strftime('%b %d %Y, %I:%M %p')
            else:
                # UTC format (24-hour)
                return dt.strftime('%b %d %Y, %H:%M')

        except Exception as e:
            logger.error(f"Error formatting datetime: {e}")
            return str(dt)

    def to_date_display(self, dt: Optional[datetime]) -> str:
        """
        Format a timestamp as a date only, including the year.

        Args:
            dt: datetime object (assumed to be naive UTC from database)

        Returns:
            Date string (e.g., "Oct 12 2025")
        """
        if dt is None:
            return "N/A"

        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            if self.mode == self.LOCAL:
                dt = dt.astimezone()

            return dt.strftime('%b %d %Y')

        except Exception as e:
            logger.error(f"Error formatting date: {e}")
            return str(dt)
    
    def to_iso(self, dt: Optional[datetime]) -> Optional[str]:
        """
        Convert datetime to ISO format string.
        
        Args:
            dt: datetime object
            
        Returns:
            ISO format string or None
        """
        if dt is None:
            return None
        return dt.isoformat()
    
    def from_iso(self, iso_string: Optional[str]) -> Optional[datetime]:
        """
        Parse ISO format string to datetime.
        
        Args:
            iso_string: ISO format datetime string
            
        Returns:
            datetime object or None
        """
        if not iso_string:
            return None
        
        try:
            return datetime.fromisoformat(iso_string)
        except Exception as e:
            logger.error(f"Error parsing datetime: {e}")
            return None
    
    def format_duration(self, minutes: Optional[int]) -> str:
        """
        Format duration in minutes to human-readable string.
        
        Args:
            minutes: Duration in minutes
            
        Returns:
            Formatted string (e.g., "3d 4h", "2h 15m" or "45m")
        """
        if minutes is None or minutes < 0:
            return "N/A"

        minutes = int(minutes)

        if minutes < 60:
            return f"{minutes}m"

        if minutes < 1440:
            hours, mins = divmod(minutes, 60)
            return f"{hours}h" if mins == 0 else f"{hours}h {mins}m"

        # Long-running items read as nonsense in hours (e.g. "6920h 4m")
        days, remainder = divmod(minutes, 1440)
        hours = remainder // 60
        return f"{days}d" if hours == 0 else f"{days}d {hours}h"
    
    def get_relative_time(self, dt: Optional[datetime]) -> str:
        """
        Get relative time string (e.g., "5 minutes ago").
        
        Args:
            dt: datetime object (assumed to be naive UTC from database)
            
        Returns:
            Relative time string
        """
        if dt is None:
            return "N/A"
        
        try:
            # Current time in UTC
            now = datetime.now(timezone.utc)
            
            # Make input datetime timezone-aware UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            
            # Calculate difference
            delta = now - dt
            seconds = int(delta.total_seconds())
            
            if seconds < 0:
                return "in the future"
            elif seconds < 60:
                return "just now"
            elif seconds < 3600:
                mins = seconds // 60
                return f"{mins} minute{'s' if mins != 1 else ''} ago"
            elif seconds < 86400:
                hours = seconds // 3600
                return f"{hours} hour{'s' if hours != 1 else ''} ago"
            else:
                days = seconds // 86400
                return f"{days} day{'s' if days != 1 else ''} ago"
                
        except Exception as e:
            logger.error(f"Error calculating relative time: {e}")
            return self.to_short_display(dt)


# Global instance
_tz_manager = None


def get_timezone_manager(mode: str = TimezoneManager.LOCAL) -> TimezoneManager:
    """Get or create the global timezone manager."""
    global _tz_manager
    if _tz_manager is None or _tz_manager.mode != mode:
        _tz_manager = TimezoneManager(mode)
    return _tz_manager


def set_timezone_mode(mode: str):
    """Set the global timezone mode."""
    global _tz_manager
    _tz_manager = TimezoneManager(mode)