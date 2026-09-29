# Changelog

## 1.0.0

Complete rewrite. Existing entries must be deleted and added again; their
statistics are replaced by a fresh import of SAPN's full history under the
same statistic IDs.

### Fixed

- Read NEM12 timestamps as NEM time (UTC+10, no daylight saving) instead of
  Adelaide local time. Readings were 30 minutes late in winter and 30 minutes
  early in summer, and daylight-saving changeover days had merged or split
  hours.
- A missing or partially published day no longer stops a meter from
  importing newer days, and no longer pauses historical imports.
- Portal maintenance pages, expired sessions, and unexpected responses are
  retried with backoff instead of being treated as a rejected password, which
  stopped regular syncing until the user re-authenticated.
- Filling a gap or correcting a day rebuilds the running total from that day
  onwards, so the Energy dashboard no longer shows spikes or negative values.
- Estimated and substituted readings are re-requested until SAPN publishes
  final readings, so corrections reach Home Assistant.
- Readings in Wh or MWh are converted to kWh.

### Changed

- The portal client and NEM12 parser are built in. The integration has no
  Python requirements (no pandas or nemreader).
- Each sync signs in once and requests every missing day in 30-day chunks,
  instead of signing in once per day per meter.
- History imports automatically on setup, newest first, until SAPN has no
  more data.
- New channels that appear after setup are imported with default settings.
- Entities: a **Sync status** sensor, a **Latest data** date sensor per
  meter, and a **Sync now** button. Status details are no longer stored as
  large, recorded attributes.
- Diagnostics are available, with credentials, meter names, and NMIs removed.
- `tools/sapn_probe.py` checks the portal's behaviour with your own login.

## 0.3.3 and earlier

See the history of this file before 1.0.0.
