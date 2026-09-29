"""Constants for the SA Power Networks Meter Data integration."""

import logging

DOMAIN = "sapnmeterdata"
LOGGER = logging.getLogger(__package__)
MANUFACTURER = "SA Power Networks"

# Config entry options
CONF_NMIS = "nmis"
CONF_METER_NAMES = "meter_names"
CONF_CHANNELS = "channels"
CONF_CHANNEL_NAME = "name"
CONF_CHANNEL_TYPE = "type"

CHANNEL_CONSUMPTION = "consumption"
CHANNEL_RETURN = "return"
CHANNEL_IGNORE = "ignore"
CHANNEL_TYPES = (CHANNEL_CONSUMPTION, CHANNEL_RETURN, CHANNEL_IGNORE)

# Days of recent data inspected to find a meter's channels.
DISCOVERY_DAYS = 14
# Days requested at once. A range the portal refuses is split in half,
# down to single days, before giving up.
HISTORY_CHUNK_DAYS = 30
# Upper bound on how far back history is requested.
HISTORY_MAX_DAYS = 3 * 366
# A run of this many days without data marks the start of a meter's history.
HISTORY_EMPTY_DAYS = 60
# Missing or not-yet-final days are re-requested for this many days.
PENDING_RETENTION_DAYS = 60
# Pause between portal requests to keep the load on SAPN light.
REQUEST_DELAY_SECONDS = 2.0
# Pause before retrying a request that failed to reach the portal.
CONNECTION_RETRY_SECONDS = 10.0
# Login failures without a rejection message before asking to reauthenticate.
LOGIN_FAILURES_BEFORE_REAUTH = 3

STORE_VERSION = 1

STATUS_UP_TO_DATE = "up_to_date"
STATUS_WAITING = "waiting_for_data"
STATUS_SYNCING = "syncing"
STATUS_ERROR = "error"
STATUS_OPTIONS = [STATUS_UP_TO_DATE, STATUS_WAITING, STATUS_SYNCING, STATUS_ERROR]
