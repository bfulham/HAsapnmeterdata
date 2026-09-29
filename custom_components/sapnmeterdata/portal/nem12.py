"""Parser for AEMO NEM12 interval meter data.

Only the records needed for interval energy are interpreted:

- ``100`` header (optional; SAPN's portal omits it)
- ``200`` NMI data details (NMI, suffix, unit, interval length)
- ``300`` one day of interval values plus its quality method
- ``400`` per-interval quality when the ``300`` quality method is ``V``

Interval dates and times in NEM12 are NEM time: Australian Eastern Standard
Time (UTC+10) all year, with no daylight saving. Every ``300`` record
therefore always holds ``1440 / interval`` values.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date, datetime

# Multipliers that convert supported energy units to kWh. Reactive units such
# as kVArh are parsed but are not energy for Home Assistant's purposes.
KWH_MULTIPLIERS = {"WH": 0.001, "KWH": 1.0, "MWH": 1000.0}

# Actual and final-substituted readings are not expected to change. Estimated
# (E), substituted (S), and null (N) readings may be replaced by SAPN later.
FINAL_QUALITIES = frozenset({"A", "F"})


class NEM12Error(ValueError):
    """Raised when a NEM12 payload cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class DayReadings:
    """One NEM12 ``300`` record with its quality resolved per interval."""

    nmi: str
    suffix: str
    day: date
    uom: str
    interval_minutes: int
    values: tuple[float | None, ...]
    qualities: tuple[str, ...]
    updated: str

    @property
    def is_energy(self) -> bool:
        """Return whether the unit can be converted to kWh."""
        return self.uom in KWH_MULTIPLIERS

    @property
    def is_final(self) -> bool:
        """Return whether every interval is present and final."""
        return all(value is not None for value in self.values) and all(
            quality in FINAL_QUALITIES for quality in self.qualities
        )


def nmi_matches(account_nmi: str, file_nmi: str) -> bool:
    """Match an account NMI with a NEM12 NMI that may omit the checksum digit.

    SAPN's account API returns the 11-character NMI including its checksum,
    while the NEM12 ``200`` record uses the 10-character base NMI.
    """
    account_nmi = account_nmi.strip().upper()
    file_nmi = file_nmi.strip().upper()
    if account_nmi == file_nmi:
        return True
    shorter, longer = sorted((account_nmi, file_nmi), key=len)
    return len(longer) == len(shorter) + 1 and longer.startswith(shorter)


def _value(raw: str) -> float | None:
    """Return a finite interval value, or None when it is blank or invalid."""
    text = raw.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class _DayBuilder:
    """Mutable ``300`` record so following ``400`` records can adjust it."""

    __slots__ = (
        "nmi",
        "suffix",
        "uom",
        "interval",
        "day",
        "values",
        "qualities",
        "updated",
    )

    def __init__(
        self,
        nmi: str,
        suffix: str,
        uom: str,
        interval: int,
        row: list[str],
    ) -> None:
        count = 1440 // interval
        if len(row) < 2 + count + 1:
            raise NEM12Error(
                f"300 record for {nmi} {suffix} has {len(row) - 2} fields; "
                f"expected at least {count + 1}"
            )
        try:
            self.day = datetime.strptime(row[1].strip(), "%Y%m%d").date()
        except ValueError as err:
            raise NEM12Error(f"Invalid IntervalDate {row[1]!r}") from err
        self.nmi = nmi
        self.suffix = suffix
        self.uom = uom
        self.interval = interval
        self.values = [_value(raw) for raw in row[2 : 2 + count]]
        # Quality method is e.g. "A", "E52", "S53", "F", or "V" (per interval).
        method = row[2 + count].strip().upper()[:1] or "A"
        self.qualities = [method] * count
        self.updated = row[2 + count + 3].strip() if len(row) > 2 + count + 3 else ""

    def apply_event(self, row: list[str]) -> None:
        """Apply a ``400`` interval event (1-indexed, inclusive range)."""
        try:
            first = int(row[1])
            last = int(row[2])
        except (IndexError, ValueError) as err:
            raise NEM12Error(f"Invalid 400 record: {row!r}") from err
        method = row[3].strip().upper()[:1] if len(row) > 3 else ""
        if not method or first < 1 or last > len(self.qualities) or first > last:
            raise NEM12Error(f"Invalid 400 record: {row!r}")
        for index in range(first - 1, last):
            self.qualities[index] = method

    def build(self) -> DayReadings:
        # A "V" quality without covering 400 records gives no information.
        qualities = tuple("A" if q == "V" else q for q in self.qualities)
        return DayReadings(
            nmi=self.nmi,
            suffix=self.suffix,
            day=self.day,
            uom=self.uom,
            interval_minutes=self.interval,
            values=tuple(self.values),
            qualities=qualities,
            updated=self.updated,
        )


def parse_nem12(text: str) -> list[DayReadings]:
    """Parse NEM12 text into one ``DayReadings`` per NMI, suffix, and date.

    When a day appears more than once, the record with the latest
    UpdateDateTime wins, and a later record wins a tie.
    """
    header: tuple[str, str, str, int] | None = None
    current: _DayBuilder | None = None
    days: dict[tuple[str, str, date], _DayBuilder] = {}

    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip():
            continue
        record = row[0].strip()

        if record == "200":
            if len(row) < 9:
                raise NEM12Error(f"Invalid 200 record: {row!r}")
            try:
                interval = int(row[8])
            except ValueError as err:
                raise NEM12Error(f"Invalid IntervalLength {row[8]!r}") from err
            if interval <= 0 or 60 % interval:
                raise NEM12Error(f"Unsupported IntervalLength {interval}")
            header = (
                row[1].strip().upper(),
                row[4].strip().upper(),
                row[7].strip().upper(),
                interval,
            )
            current = None
        elif record == "300":
            if header is None:
                raise NEM12Error("300 record appeared before any 200 record")
            current = _DayBuilder(*header, row=row)
            key = (current.nmi, current.suffix, current.day)
            existing = days.get(key)
            if existing is None or current.updated >= existing.updated:
                days[key] = current
        elif record == "400":
            if current is None:
                raise NEM12Error("400 record appeared without a 300 record")
            current.apply_event(row)
        elif record in ("100", "500", "900"):
            continue
        else:
            raise NEM12Error(f"Unexpected NEM12 record type {record!r}")

    return [builder.build() for builder in days.values()]
