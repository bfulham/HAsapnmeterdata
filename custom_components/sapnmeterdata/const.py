"""Constants for the SA Power Networks Meter Data integration."""

import logging
from datetime import time, timedelta

DOMAIN = "sapnmeterdata"
LOGGER = logging.getLogger(__package__)
MANUFACTURER = "SA Power Networks"

CONF_AVAILABLE_NMIS = "available_nmis"
CONF_NMIS = "nmis"
CONF_NMI_NAMES = "nmi_names"
CONF_CHANNEL_CONFIG = "channel_config"
CONF_CHANNEL_NAME = "name"
CONF_CHANNEL_TYPE = "type"
CONF_CONSUMPTION_CHANNELS = "consumption_channels"
CONF_RETURN_CHANNELS = "return_channels"

CHANNEL_TYPE_CONSUMPTION = "consumption"
CHANNEL_TYPE_RETURN = "return"
CHANNEL_TYPE_IGNORE = "ignore"
CHANNEL_TYPES = (
    CHANNEL_TYPE_CONSUMPTION,
    CHANNEL_TYPE_RETURN,
    CHANNEL_TYPE_IGNORE,
)

DEFAULT_CONSUMPTION_CHANNELS = "E*"
DEFAULT_RETURN_CHANNELS = "B*"
CHANNEL_DISCOVERY_DAYS = 14
SAPN_TIME_ZONE = "Australia/Adelaide"

# SAPN publishes the completed previous day at 03:00 Adelaide time. Schedule
# five minutes later to avoid racing the portal's refresh.
DATA_AVAILABLE_TIME = time(hour=3)
DAILY_REFRESH_TIME = time(hour=3, minute=5)
UPDATE_INTERVAL = timedelta(hours=3)
HISTORICAL_CHUNK_DAYS = 7
HISTORICAL_CHUNK_DELAY = timedelta(minutes=1)
STATISTICS_ALIGNMENT_VERSION = 3
STORE_VERSION = 1

STATUS_UP_TO_DATE = "up_to_date"
STATUS_IMPORTED = "imported"
STATUS_PARTIAL = "partial"
STATUS_WAITING = "waiting_for_data"
STATUS_ATTENTION = "attention"
STATUS_BACKFILLING = "backfilling_history"
STATUS_OPTIONS = [
    STATUS_UP_TO_DATE,
    STATUS_IMPORTED,
    STATUS_PARTIAL,
    STATUS_WAITING,
    STATUS_ATTENTION,
    STATUS_BACKFILLING,
]
