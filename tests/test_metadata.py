"""Repository metadata tests."""

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "sapnmeterdata"


def test_manifest_is_the_compatible_022_update() -> None:
    """The release preserves the domain and pins the tested portal client."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert manifest["domain"] == "sapnmeterdata"
    assert manifest["version"] == "0.2.2"
    assert manifest["config_flow"] is True
    assert "recorder" in manifest["dependencies"]
    assert manifest["requirements"] == ["sapnmeterdata==0.3.2"]


def test_english_translation_matches_strings() -> None:
    """The English translation remains synchronized with strings.json."""
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())
    assert english == strings


def test_config_flow_and_migration_use_version_two() -> None:
    """The old single-NMI entry has an explicit migration path."""
    config_flow = (INTEGRATION / "config_flow.py").read_text()
    setup = (INTEGRATION / "__init__.py").read_text()
    assert "VERSION = 2" in config_flow
    assert "async_migrate_entry" in setup
    assert "entry.version == 1" in setup


def test_opening_config_flow_does_not_import_the_data_stack() -> None:
    """Selecting Add Integration must not load native data dependencies."""
    config_flow = (INTEGRATION / "config_flow.py").read_text()
    setup = (INTEGRATION / "__init__.py").read_text()
    coordinator = (INTEGRATION / "coordinator.py").read_text()

    config_flow_prefix = config_flow.split("def _discover_nmis", maxsplit=1)[0]
    setup_prefix = setup.split("async def async_setup_entry", maxsplit=1)[0]
    coordinator_prefix = coordinator.split("def _fetch_meter_data", maxsplit=1)[0]

    assert "from sapnmeterdata import" not in config_flow_prefix
    assert "from .coordinator import" not in setup_prefix
    assert "from sapnmeterdata import" not in coordinator_prefix
    assert "import pandas" not in coordinator_prefix
