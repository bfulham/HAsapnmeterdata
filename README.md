# SA Power Networks Meter Data for Home Assistant

A Home Assistant custom integration that imports completed daily meter data
from SA Power Networks' customer portal into Home Assistant's long-term
statistics. The resulting grid-consumption and return-to-grid statistics can be
selected directly in the Energy Dashboard.

This integration uses
[`sapnmeterdata`](https://pypi.org/project/sapnmeterdata/) 0.3.3.

Version 0.3.0 imports every selected NEM12 channel as its own statistic.
Channels are discovered from a bounded recent sample and can be named and
classified separately for every meter.

## Upgrading to 0.3.0

Versions through 0.2.5 combined all matching E channels into one consumption
statistic and all matching B channels into one return-to-grid statistic.
Version 0.3.0 replaces those aggregate streams with stable NMI/channel pairs:

- `sapnmeterdata:20023157519_e1`
- `sapnmeterdata:20023157519_e2`
- `sapnmeterdata:20023157519_b1`

The one-time migration removes the old aggregate SAPN statistics and imports
the latest available day using the new channel IDs. Existing Energy Dashboard
selections that point to `<nmi>_consumption` or `<nmi>_return` must be replaced
with the appropriate channel statistics. Press **Update historical data**
afterward to populate older history for every enabled channel.

Existing entries retain their credentials, selected NMIs, friendly meter
names, and the previous E/B classification defaults. Open **Configure** after
upgrading to inspect the channels SAPN currently returns and give each one a
useful name.

## What it does

- Discovers assigned NMIs and their SAPN meter descriptions.
- Discovers the actual NEM12 channels returned for each selected meter.
- Imports every enabled channel separately.
- Lets each NMI/channel pair have its own name and classification.
- Defaults `E*` to grid consumption and `B*` to return to grid.
- Detects other registers such as `K1` and `Q1` but ignores them by default.
- Aggregates five-minute readings into Home Assistant's required hourly
  external statistics, including 23- and 25-hour daylight-saving days.
- Aligns every imported row to Home Assistant's UTC hour boundaries so grid,
  solar, and return-to-grid values share the same Energy Dashboard bars.
- Maintains continuous cumulative kWh totals for Energy Dashboard reporting.
- Retries delayed data without creating duplicate rows.
- Catches up one day at a time after Home Assistant has been offline.
- Backfills older portal history in bounded, resumable seven-day chunks.

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
4. Select one or more meters. The list shows SAPN's friendly description and
   NMI so similarly named meters can still be distinguished.
5. Wait while the integration inspects a recent 14-day sample for each selected
   meter.
6. For every discovered channel:
   - enter the name that should appear in Home Assistant;
   - choose **Grid consumption**, **Return to grid**, or **Ignore**.

For example:

| Meter | Channel | Name | Use as |
|---|---|---|---|
| NMI 1 | `E1` | Standard Consumption | Grid consumption |
| NMI 1 | `E2` | Controlled Load | Grid consumption |
| NMI 1 | `B1` | Solar | Return to grid |
| NMI 2 | `E1` | Pump Station | Grid consumption |

## Add it to the Energy Dashboard

After the first successful import:

1. Go to **Settings → Dashboards → Energy**.
2. Under **Electricity grid**, choose **Add consumption**.
3. Add each consumption channel you want included, such as
   `SAPN MRC Standard Consumption` and `SAPN MRC Controlled Load`.
4. Under **Return to grid**, select the named export channel, such as
   `SAPN MRC Solar`.
5. Add a tariff entity only if you want Home Assistant to calculate cost.

External statistic IDs use
`sapnmeterdata:<nmi>_<channel>`. Renaming a channel changes only its displayed
name; its statistic ID and accumulated history remain attached to the NMI and
SAPN channel code.

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

### Historical import

Press **Update historical data** once to import everything the SAPN portal
makes available before the integration's earliest recorded day.

- Each NMI is requested in seven-day chunks rather than one multi-year
  download.
- Successful chunks are separated by one minute to limit portal load.
- Daily forward imports remain the priority.
- Progress is saved after every chunk and resumes after a Home Assistant
  restart.
- Importing stops separately for each NMI when SAPN reports that no older data
  is available.
- A failed NMI is paused rather than retried continuously. Press the button
  again to clear failed markers and retry from its saved checkpoint.

The **Import status** sensor shows `Updating historical data` while work
remains. Its `historical_backfill` attribute contains each NMI's checkpoint,
the completed and failed NMIs, and the number of imported chunks. Its
`meter_names` attribute maps each stable NMI to the friendly name returned by
SAPN.

## Channel configuration

Each meter has its own channel map, so `E1` can be named **Standard
Consumption** on one NMI and **Pump Station** on another.

| Channel default | Initial classification |
|---|---|
| `E*` | Grid consumption |
| `B*` | Return to grid |
| Other channels | Ignore |

Change selected meters, channel names, or classifications from the
integration's **Configure** dialog. If you enable a previously ignored channel,
the integration reimports the latest available day. Press **Update historical
data** to fill its older history in bounded seven-day chunks.

## Data retention and removal

The imported readings are long-term Recorder statistics rather than ordinary
sensor history. Removing the integration does not automatically delete those
statistics. They can be inspected or removed from **Developer tools →
Statistics**.

## Development

```bash
python -m pip install "pandas==2.3.3" pytest ruff sapnmeterdata==0.3.3
ruff check .
python -m compileall -q custom_components
pytest
```

GitHub Actions runs the tests, Ruff, HACS validation, and Home Assistant's
`hassfest` validation.
