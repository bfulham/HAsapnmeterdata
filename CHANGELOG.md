# Changelog

## 0.3.3

- Fix a `KeyError` while building status details when an excluded assignment is
  no longer present in the user's selected interval-meter list.
- Resolve excluded-meter names generically with a safe NMI fallback; no meter
  identifier is hard-coded in the integration.
- Remove account-specific meter identifiers and friendly names from public
  documentation and tests.

## 0.3.2

- Use the meter-type metadata already exposed by `sapnmeterdata==0.3.3` to
  identify interval-capable assignments.
- Exclude SAPN assignments described as basic or manually read meters from
  setup, options, daily imports, and historical backfilling.
- Automatically discover and persist exclusions for existing config entries.
- Show excluded NMIs, friendly names, and meter types on the Import status
  sensor.
- Resume an active historical backfill after the non-interval meter blocking
  the forward queue is excluded.
- Keep a per-NMI forward checkpoint on missing or partially published SAPN
  data instead of permanently skipping the affected date.
- Reconcile the previous seven days once during upgrade to recover dates an
  earlier release may already have skipped.
- Continue multi-day forward catch-up at the bounded one-minute chunk cadence.

## 0.3.1

- Handle NEM12 parser output containing one copy of Adelaide's repeated
  daylight-saving fallback hour instead of raising `AmbiguousTimeError`.
- Preserve all 288 five-minute readings on 5 April 2026 by assigning a lone
  ambiguous 2:00 am hour to standard time.
- Retain pandas' normal offset inference when the parser supplies both copies
  of a repeated wall-clock hour.

## 0.3.0

- Discover the actual NEM12 channels for every selected meter from a bounded
  recent 14-day sample during setup and options.
- Import each enabled NMI/channel pair as its own long-term statistic instead
  of combining all E or B channels.
- Let every channel have a different user-facing name and classification for
  each meter.
- Default E channels to grid consumption, B channels to return to grid, and
  reactive or unknown channels to Ignore.
- Use stable `sapnmeterdata:<nmi>_<channel>` statistic IDs so later channel
  renames retain their history.
- Migrate 0.1.x and 0.2.x entries, remove legacy aggregate statistics, and
  preserve the 3:00 am publication and resumable historical-import behaviour.

## 0.2.5

- Defer the initial SAPN refresh and Recorder statistics migration until Home
  Assistant has completed startup, preventing the integration setup deadlock
  seen in 0.2.4.
- Use `sapnmeterdata==0.3.3` to discover each assigned meter's SAPN
  description.
- Show friendly meter names in setup, options, status data, and external
  statistic names while preserving NMI-based statistic IDs.
- Persist discovered meter names and refresh missing names automatically.

## 0.2.4

- Run the UTC-alignment statistics deletion through Home Assistant's dedicated
  Recorder task queue.
- Wait for the queued deletion to finish before saving the migration checkpoint
  and importing replacement statistics.
- Fix the `Detected unsafe call not in recorder thread` startup failure in
  Home Assistant releases using Recorder thread-safety checks.

## 0.2.3

- Align external statistics to UTC hour boundaries so SAPN grid and
  return-to-grid readings share Energy Dashboard bars with Home Assistant's
  native solar statistics.
- Automatically replace the half-hour-shifted rows written by versions
  0.2.0–0.2.2 while preserving the existing statistic IDs.
- Store each hourly interval as the statistic state and maintain its continuous
  cumulative sum.
- Add an **Update historical data** button.
- Backfill each NMI in rate-limited seven-day chunks, persist progress across
  restarts, and stop separately when SAPN has no older data.
- Keep current daily imports ahead of historical backfill work.

## 0.2.2

- Use `sapnmeterdata==0.3.2`, which replaces the native Polars parser with the
  pandas-based `nemreader` 0.9.2 parser for older Home Assistant processors.
- Avoid importing the SAPN client, pandas, and the NEM12 parser when the config
  flow is opened.
- Run portal imports, NEM12 parsing, and interval transformation in Home
  Assistant's executor.

## 0.2.1

- Use `sapnmeterdata==0.3.1`, which supports Home Assistant's pinned
  `pandas==2.3.3` dependency.

## 0.2.0

- Import historical SAPN readings into Home Assistant Recorder statistics.
- Add Energy Dashboard grid-consumption and return-to-grid statistics.
- Support multiple NMIs per config entry.
- Aggregate NEM12 five-minute intervals into local hourly statistics.
- Handle Adelaide daylight-saving transition days.
- Wait until SAPN's 3:00 am Adelaide publication time.
- Schedule a dedicated daily import at 3:05 am Adelaide time.
- Retry delayed data every three hours.
- Skip unavailable NMIs without blocking successful meters.
- Migrate existing 0.1.x single-NMI config entries.
- Update the client dependency to `sapnmeterdata==0.3.0`.
