"""Tests for the NEM12 parser."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.sapnmeterdata.portal.nem12 import (
    NEM12Error,
    nmi_matches,
    parse_nem12,
)

from .common import NMI, nem12_200, nem12_300

DAY = date(2025, 8, 7)


def test_sapn_style_file_without_header() -> None:
    """SAPN omits the 100 record and repeats 200 blocks per few days."""
    text = "\n".join(
        [
            nem12_200(NMI, "E1"),
            nem12_300(DAY, [0.006] * 288),
            nem12_200(NMI, "B1"),
            nem12_300(DAY, [0.0] * 288),
            nem12_200(NMI, "E1"),
            nem12_300(date(2025, 8, 8), [0.005] * 288),
            "",
            "900",
        ]
    )
    readings = {(r.suffix, r.day): r for r in parse_nem12(text)}
    assert set(readings) == {("E1", DAY), ("B1", DAY), ("E1", date(2025, 8, 8))}
    e1 = readings[("E1", DAY)]
    assert e1.nmi == NMI[:10]
    assert e1.uom == "KWH"
    assert e1.interval_minutes == 5
    assert len(e1.values) == 288
    assert e1.is_energy and e1.is_final


def test_quality_method_and_400_events() -> None:
    """A V quality takes per-interval quality from 400 records."""
    text = "\n".join(
        [
            nem12_200(NMI, "E1"),
            nem12_300(DAY, [0.01] * 288, quality="V"),
            "400,1,12,E52,,",
            "400,13,288,A,,",
            nem12_300(date(2025, 8, 8), [0.01] * 288, quality="S53"),
        ]
    )
    first, second = sorted(parse_nem12(text), key=lambda r: r.day)
    assert first.qualities[:12] == ("E",) * 12
    assert first.qualities[12:] == ("A",) * 276
    assert not first.is_final
    assert set(second.qualities) == {"S"}
    assert not second.is_final


def test_blank_values_are_missing() -> None:
    """Blank interval values are kept as None and make the day non-final."""
    values: list[float | None] = [0.01] * 288
    values[100] = None
    text = "\n".join([nem12_200(NMI, "E1"), nem12_300(DAY, values)])
    (reading,) = parse_nem12(text)
    assert reading.values[100] is None
    assert not reading.is_final


def test_duplicate_day_keeps_latest_update() -> None:
    """A re-sent day with a later UpdateDateTime replaces the earlier one."""
    text = "\n".join(
        [
            nem12_200(NMI, "E1"),
            nem12_300(DAY, [0.02] * 288, updated="20250810000000"),
            nem12_300(DAY, [0.01] * 288, updated="20250809000000"),
        ]
    )
    (reading,) = parse_nem12(text)
    assert reading.values[0] == 0.02


def test_thirty_minute_intervals_and_units() -> None:
    """Interval length and unit come from the 200 record."""
    text = "\n".join(
        [
            nem12_200(NMI, "E1", uom="WH", interval=30),
            nem12_300(DAY, [250.0] * 48),
            nem12_200(NMI, "Q1", uom="KVARH", interval=30),
            nem12_300(DAY, [1.0] * 48),
        ]
    )
    readings = {r.suffix: r for r in parse_nem12(text)}
    assert len(readings["E1"].values) == 48
    assert readings["E1"].is_energy
    assert not readings["Q1"].is_energy


@pytest.mark.parametrize(
    "text",
    [
        nem12_300(DAY, [0.01] * 288),
        nem12_200(NMI, "E1") + "\n300,20250807,0.1,0.2,A",
        nem12_200(NMI, "E1", interval=7),
        nem12_200(NMI, "E1") + "\n" + nem12_300(DAY, [0.01] * 288) + "\n400,0,300,E,,",
        "250,NEM13 record",
    ],
)
def test_invalid_files(text: str) -> None:
    """Malformed files raise NEM12Error."""
    with pytest.raises(NEM12Error):
        parse_nem12(text)


def test_nmi_matching() -> None:
    """NEM12 NMIs may omit the account NMI's checksum digit."""
    assert nmi_matches("20012345678", "2001234567")
    assert nmi_matches("2001234567", "20012345678")
    assert nmi_matches("20012345678", "20012345678")
    assert not nmi_matches("20012345678", "2001234568")
    assert not nmi_matches("20012345678", "200123456")
