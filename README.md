# SA Power Networks Meter Data for Home Assistant

A Home Assistant custom integration that imports completed daily meter data
from SA Power Networks' customer portal into Home Assistant's long-term
statistics. The resulting grid-consumption and return-to-grid statistics can be
selected directly in the Energy Dashboard.

This integration uses
[`sapnmeterdata`](https://pypi.org/project/sapnmeterdata/) 0.3.1.

Version 0.2.1 widens the client library's pandas compatibility so it can use
Home Assistant's pinned pandas 2.3.3 installation.

## Version 0.2.0

Version 0.2.0 replaces the original current-value sensors with historical
Recorder statistics suitable for the Energy Dashboard.

Existing 0.1.x config entries are migrated automatically. Each old entry keeps
its configured email, password, and NMI. Open **Configure** afterward to refresh
the account's NMI list and select additional meters.

## What it does

- Discovers the NMIs assigned to an SAPN portal account.
- Imports SAPN's latest completed Adelaide calendar day.
- Combines matching NEM12 channels:
  - `E*` is grid consumption by default.
  - `B*` is return to grid by default.
- Aggregates five-minute readings into Home Assistant's required hourly
  external statistics, including 23- and 25-hour daylight-saving days.
- Maintains continuous cumulative kWh totals for Energy Dashboard reporting.
- Retries delayed data without creating duplicate rows.
- Catches up one day at a time after Home Assistant has been offline.

SAPN's portal is not a documented public API, so portal changes can temporarily
break data retrieval.

## SAPN publication time

SAPN publishes the previous day's data at **3:00 am Adelaide time**. For
example, data for 26 July becomes eligible at 3:00 am on 27 July.

The integration:

1. Never requests the previous day before 3:00 am.
2. Runs a dedicated daily import at 3:05 am Adelaide time.
3. Checks again every three hours if SAPN reports that the data is not ready.

This avoids expected failures between midnight and SAPN's 3:00 am publication.

## Installation

### HACS custom repository

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/bfulham/HAsapnmeterdata`.
3. Choose **Integration**.
4. Install **SA Power Networks Meter Data**.
5. Restart Home Assistant.

### Manual

Copy `custom_components/sapnmeterdata` into the `custom_components` directory
under your Home Assistant configuration directory, then restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & services**.
2. Select **Add integration** and search for
   **SA Power Networks Meter Data**.
3. Enter the email and password used for the SAPN meter-data portal.
4. Select one or more NMIs.
5. Keep the defaults unless your NEM12 channels use different directions:
   - Grid consumption: `E*`
   - Return to grid: `B*`

Multiple patterns can be separated by commas, for example `E1,E2`.

## Add it to the Energy Dashboard

After the first successful import:

1. Go to **Settings → Dashboards → Energy**.
2. Under **Electricity grid**, choose **Add consumption**.
3. Select `SAPN <NMI> Grid consumption`.
4. If the meter exports energy, add **Return to grid** and select
   `SAPN <NMI> Return to grid`.
5. Add a tariff entity only if you want Home Assistant to calculate cost.

The external statistic IDs are:

- `sapnmeterdata:<nmi>_consumption`
- `sapnmeterdata:<nmi>_return`

## Import behavior

The integration stores a checkpoint per NMI. Repeating an import is safe:
Home Assistant updates rows with the same statistic ID and hour instead of
adding duplicates.

If Home Assistant missed several days, the integration catches up one day per
three-hour refresh. SAPN data unavailable for the newest eligible day is
retried. An older permanently unavailable day is skipped so later dates are not
blocked.

The **Import previous day** button requests an immediate check. Before 3:00 am
it still respects SAPN's availability cutoff and will not request yesterday
early.

## Channel mapping

| Pattern | Imported as | Examples |
|---|---|---|
| `E*` | Grid consumption | `E1`, `E2` |
| `B*` | Return to grid | `B1` |
| unmatched | Ignored | `K1`, `Q1` |

Change the selected NMIs or channel patterns from the integration's
**Configure** dialog.

## Data retention and removal

The imported readings are long-term Recorder statistics rather than ordinary
sensor history. Removing the integration does not automatically delete those
statistics. They can be inspected or removed from **Developer tools →
Statistics**.

## Development

```bash
python -m pip install pandas pytest ruff
ruff check .
python -m compileall -q custom_components
pytest
```

GitHub Actions runs the tests, Ruff, HACS validation, and Home Assistant's
`hassfest` validation.
