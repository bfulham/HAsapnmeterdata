# Changelog

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
