"""Market hours utilities.

Provides functions for computing market-hours-aware timestamps,
e.g. "24 market hours from now" accounting for weekends and
non-trading periods.

All times are US/Eastern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("US/Eastern")

# Regular market hours
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0
MARKET_HOURS_PER_DAY = 6.5  # 9:30-16:00 = 6.5 hours


def is_market_open(dt: datetime | None = None) -> bool:
    """Check if the given time (or now) falls within regular market hours on a weekday."""
    if dt is None:
        dt = datetime.now(tz=ET)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    else:
        dt = dt.astimezone(ET)

    # Weekday check (Mon=0 ... Fri=4)
    if dt.weekday() > 4:
        return False

    minutes = dt.hour * 60 + dt.minute
    open_min = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN
    close_min = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    return open_min <= minutes < close_min


def add_market_hours(start: datetime, hours: float) -> datetime:
    """Compute the timestamp that is `hours` market hours after `start`.

    Skips weekends and non-market hours. If start is outside market
    hours, begins counting from the next market open.

    Args:
        start: Starting datetime (timezone-aware or tz-naive Eastern).
        hours: Number of market hours to add.

    Returns:
        Timezone-aware datetime (US/Eastern) after `hours` market hours.
    """
    if start.tzinfo is None:
        dt = start.replace(tzinfo=ET)
    else:
        dt = start.astimezone(ET)

    remaining_minutes = hours * 60.0

    # If outside market hours, advance to next market open
    dt = _advance_to_market_open(dt)

    while remaining_minutes > 0:
        # Minutes left in current market session
        close_today = dt.replace(
            hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0,
        )
        minutes_left_today = (close_today - dt).total_seconds() / 60.0

        if remaining_minutes <= minutes_left_today:
            dt = dt + timedelta(minutes=remaining_minutes)
            remaining_minutes = 0
        else:
            remaining_minutes -= minutes_left_today
            # Advance to next trading day's open
            dt = _next_market_open(dt)

    return dt


def _advance_to_market_open(dt: datetime) -> datetime:
    """If dt is before market open or on a weekend, advance to next open."""
    for _ in range(10):  # safety limit
        if dt.weekday() > 4:
            # Skip to Monday
            days_ahead = 7 - dt.weekday()
            dt = dt.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
            dt = dt + timedelta(days=days_ahead)
            continue

        minutes = dt.hour * 60 + dt.minute
        open_min = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN
        close_min = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN

        if minutes < open_min:
            # Before market open today — advance to open
            dt = dt.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
            return dt
        elif minutes >= close_min:
            # After market close — advance to next trading day
            dt = _next_market_open(dt)
            continue
        else:
            # During market hours
            return dt

    return dt


def _next_market_open(dt: datetime) -> datetime:
    """Advance to the next trading day's market open."""
    dt = dt + timedelta(days=1)
    dt = dt.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
    # Skip weekends
    while dt.weekday() > 4:
        dt = dt + timedelta(days=1)
    return dt
