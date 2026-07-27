"""Repository metadata tests."""

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "sapnmeterdata"


def test_manifest_is_the_dst_safe_031_update() -> None:
    """The release preserves the domain and pins the tested portal client."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert manifest["domain"] == "sapnmeterdata"
    assert manifest["version"] == "0.3.1"
    assert manifest["config_flow"] is True
    assert "recorder" in manifest["dependencies"]
    assert manifest["requirements"] == ["sapnmeterdata==0.3.3"]


def test_english_translation_matches_strings() -> None:
    """The English translation remains synchronized with strings.json."""
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())
    assert english == strings


def test_config_flow_and_migration_use_version_three() -> None:
    """Old aggregate entries have an explicit per-channel migration path."""
    config_flow = (INTEGRATION / "config_flow.py").read_text()
    setup = (INTEGRATION / "__init__.py").read_text()
    assert "VERSION = 3" in config_flow
    assert "async_migrate_entry" in setup
    assert "entry.version == 1" in setup
    assert "entry.version < 3" in setup
    assert "CONF_CHANNEL_CONFIG" in setup


def test_opening_config_flow_does_not_import_the_data_stack() -> None:
    """Selecting Add Integration must not load native data dependencies."""
    config_flow = (INTEGRATION / "config_flow.py").read_text()
    setup = (INTEGRATION / "__init__.py").read_text()
    coordinator = (INTEGRATION / "coordinator.py").read_text()

    config_flow_prefix = config_flow.split("def _connect_account", maxsplit=1)[0]
    setup_prefix = setup.split("async def async_setup_entry", maxsplit=1)[0]
    coordinator_prefix = coordinator.split("def _fetch_meter_data", maxsplit=1)[0]

    assert "from sapnmeterdata import" not in config_flow_prefix
    assert "from .coordinator import" not in setup_prefix
    assert "from sapnmeterdata import" not in coordinator_prefix
    assert "import pandas" not in coordinator_prefix


def test_historical_backfill_is_exposed_and_chunked() -> None:
    """The UI starts a resumable bounded historical import."""
    button = (INTEGRATION / "button.py").read_text()
    coordinator = (INTEGRATION / "coordinator.py").read_text()
    constants = (INTEGRATION / "const.py").read_text()

    assert "update_historical_data" in button
    assert "async_start_historical_backfill" in coordinator
    assert "HISTORICAL_CHUNK_DAYS = 7" in constants
    assert "HISTORICAL_CHUNK_DELAY" in coordinator


def test_alignment_migration_replaces_old_statistics() -> None:
    """The migration queues deletion on Home Assistant's recorder thread."""
    coordinator = (INTEGRATION / "coordinator.py").read_text()
    transform = (INTEGRATION / "transform.py").read_text()

    assert "recorder.async_clear_statistics(stat_ids)" in coordinator
    assert "await recorder.async_block_till_done()" in coordinator
    assert "clear_statistics," not in coordinator
    assert "STATISTICS_ALIGNMENT_VERSION" in coordinator
    assert "tz_convert(UTC)" in transform


def test_recorder_work_is_deferred_until_home_assistant_started() -> None:
    """Bootstrap must not wait for Recorder tasks that cannot run yet."""
    setup = (INTEGRATION / "__init__.py").read_text()
    coordinator = (INTEGRATION / "coordinator.py").read_text()

    assert "startup_pending = hass.state is not CoreState.running" in setup
    assert "coordinator.async_set_updated_data(coordinator.startup_data())" in setup
    assert "async_at_started(hass, coordinator.async_start_after_hass)" in setup
    assert "if self.hass.state is not CoreState.running:" in coordinator
    assert "return self.startup_data()" in coordinator
    assert "await self.async_request_refresh()" in coordinator


def test_friendly_meter_names_are_discovered_and_used() -> None:
    """SAPN descriptions label selectors and external statistics."""
    config_flow = (INTEGRATION / "config_flow.py").read_text()
    coordinator = (INTEGRATION / "coordinator.py").read_text()
    constants = (INTEGRATION / "const.py").read_text()

    assert "getNMIAssignments()" in config_flow
    assert "assignment.friendly_name" in config_flow
    assert "CONF_NMI_NAMES" in constants
    assert "options=meter_options" in config_flow
    assert "friendly_name," in coordinator


def test_channels_are_discovered_named_and_imported_separately() -> None:
    """Every NMI/channel pair has configurable metadata and a stable ID."""
    config_flow = (INTEGRATION / "config_flow.py").read_text()
    coordinator = (INTEGRATION / "coordinator.py").read_text()
    transform = (INTEGRATION / "transform.py").read_text()
    statistics = (INTEGRATION / "statistics.py").read_text()

    assert "CHANNEL_DISCOVERY_DAYS" in config_flow
    assert "async_step_channels" in config_flow
    assert "CONF_CHANNEL_NAME" in config_flow
    assert "CONF_CHANNEL_TYPE" in config_flow
    assert "extract_hourly_channels" in coordinator
    assert "for channel, stream in streams.items()" in coordinator
    assert "streams[channel] = HourlyStream" in transform
    assert "safe_channel" in statistics
