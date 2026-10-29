"""Constants for the DaycareWebWatch integration."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Final

DOMAIN: Final = "daycarewebwatch"

CONF_INSTANCE_ID: Final = "instance_id"
CONF_WINDOW_START: Final = "window_start"
CONF_WINDOW_END: Final = "window_end"
CONF_WINDOW_DAYS: Final = "window_days"

# Monday=0 ... Sunday=6, matching datetime.weekday().
WEEKDAYS: Final = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_WINDOW_START: Final = time(6, 30)
DEFAULT_WINDOW_END: Final = time(18, 30)
DEFAULT_WINDOW_DAYS: Final = ["mon", "tue", "wed", "thu", "fri"]

CAMERA_LIST_INTERVAL: Final = timedelta(hours=6)
STATUS_OPEN_INTERVAL: Final = timedelta(minutes=15)
STATUS_CLOSED_IN_WINDOW_INTERVAL: Final = timedelta(minutes=10)
STATUS_CLOSED_MAX_INTERVAL: Final = timedelta(hours=3)
STATUS_ERROR_MIN_INTERVAL: Final = timedelta(minutes=5)
STATUS_ERROR_MAX_INTERVAL: Final = timedelta(minutes=60)
# The official app re-validates an open stream every 150 s.
STREAM_REFRESH_INTERVAL: Final = timedelta(seconds=150)
