# SA Power Networks Meter Data for Home Assistant

Imports your interval meter data from SA Power Networks' **Your Meter Data**
portal into Home Assistant's long-term statistics, ready for the Energy
dashboard. Each meter channel (general consumption, controlled load, solar
export, …) becomes its own statistic, with its complete history.

SAPN's data is historical: each day is published the following morning. This
integration is not a real-time power feed.

> Not affiliated with SA Power Networks. The portal has no public API, so
> changes SAPN makes to it can temporarily break the integration.

## Upgrading from 0.x

Version 1.0 is a rewrite and does not upgrade old entries:

1. Update the integration and restart Home Assistant. The old entry shows a
   migration error.
2. Delete the old entry, then add the integration again.
3. On first sync, each channel's existing statistics are **replaced** by a
   fresh import of the complete history SAPN provides. Statistic IDs are
   unchanged (`sapnmeterdata:<nmi>_<channel>`), so Energy dashboard
   selections keep working. Anything older than SAPN's history is removed.

Why the full re-import: versions before 1.0 read SAPN's timestamps as
Adelaide local time. NEM12 meter data is in NEM time (UTC+10 all year, no
daylight saving), so older imports placed every reading 30 minutes out and
mangled the hours around each daylight-saving change.

## Installation

**HACS:** add `https://github.com/bfulham/HAsapnmeterdata` as a custom
repository of type *Integration*, install **SA Power Networks Meter Data**,
and restart Home Assistant.

**Manual:** copy `custom_components/sapnmeterdata` into your configuration's
`custom_components` directory and restart Home Assistant.

## Setup

1. Register for SAPN's free
   [Your Meter Data](https://www.sapowernetworks.com.au/your-power/manage-your-power-use/your-meter-data/)
   service if you have not already.
2. In **Settings → Devices & services**, add **SA Power Networks Meter
   Data** and sign in with your portal email and password.
3. Choose the meters to import. Basic and manually read meters have no
   interval data and are left out.
4. For each meter, name its channels and choose how each is used. Defaults:
   `E*` channels are grid consumption, `B*` channels are return to grid, and
   anything else (such as reactive energy) is ignored.

The complete history then imports in the background. This takes a few
minutes per meter; the **Sync status** sensor shows *Syncing* meanwhile.

### Energy dashboard

In **Settings → Dashboards → Energy**, add each consumption channel under
*Grid consumption* and each export channel under *Return to grid*. They are
listed by name, for example *SAPN Home General* and *SAPN Home Solar*.

## How syncing works

- SAPN publishes each day at 3:00 am Adelaide time. The integration syncs
  daily at 3:20 am.
- If a published day is still missing, it checks again hourly until noon,
  then every three hours.
- Each sync signs in once and requests only the days it still needs.
- A missing day never holds up later days. It is re-requested on each sync
  for up to 60 days.
- Estimated or substituted readings are imported straight away and
  re-requested until SAPN publishes final readings. Corrections flow through.
- Portal outages and maintenance are retried after 15 minutes, backing off to
  every three hours. You are only asked to re-enter your password when SAPN
  actually rejects it (or sign-in fails three syncs in a row).
- A channel that first appears after setup (for example after installing
  solar) is imported automatically using the defaults above.

Use **Configure** on the integration to change meters, channel names, or
channel use. Newly enabled channels get their full history.

## Entities

| Entity | Description |
|---|---|
| Sync status | `Up to date`, `Waiting for SAPN`, `Syncing`, or `Error`. Attributes: last successful sync, next sync, error. |
| Latest data (per meter) | The newest day imported for all of the meter's channels. Attributes: days awaiting data, error. |
| Sync now | Checks SAPN immediately. |

Meter readings live in long-term statistics, not in these entities. View them
in **Developer tools → Statistics** or in the Energy dashboard.

## Removing the integration

Deleting the integration keeps the imported statistics. Remove them in
**Developer tools → Statistics** if you no longer want them.

## Troubleshooting

- Download diagnostics from the integration's menu. Credentials, meter names,
  and NMIs are removed.
- `tools/sapn_probe.py` checks the portal behaviour the integration relies on,
  using your login from your own terminal:

  ```bash
  python -m pip install aiohttp
  python tools/sapn_probe.py
  ```

## Development

```bash
python -m pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

The portal client and NEM12 parser in `custom_components/sapnmeterdata/portal`
do not depend on Home Assistant.
