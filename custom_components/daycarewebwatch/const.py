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
STATUS_OPEN_INTERVAL: Final = timedelta(minutes=60)
STATUS_CLOSED_IN_WINDOW_INTERVAL: Final = timedelta(minutes=10)
STATUS_CLOSED_MAX_INTERVAL: Final = timedelta(hours=3)
STATUS_ERROR_MIN_INTERVAL: Final = timedelta(minutes=5)
STATUS_ERROR_MAX_INTERVAL: Final = timedelta(minutes=60)
# How often an active stream is checked; a new token is only requested when
# the stream has failed (tokens are not refreshed while playback is healthy).
STREAM_CHECK_INTERVAL: Final = timedelta(seconds=60)
