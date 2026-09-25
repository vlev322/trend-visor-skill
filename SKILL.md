---
name: trend-visor
description: Collect daily Wikipedia pageviews, check date coverage, and describe two periods in a saved dataset. Use when downloading article traffic, investigating missing dates, comparing daily averages, or inspecting monthly summaries. Provides descriptive changes, not statistical trend verdicts, charts or PDF reports.
compatibility: Requires Python 3.11 or newer. Network access is needed for new downloads; there are no third-party Python dependencies.
metadata:
  author: Vladyslav Levchenko
  version: "0.0.1"
---

# Wikipedia pageview collection and descriptive analysis

## Collection workflow

1. Confirm the article title and language edition before downloading. Topic search and Wikidata resolution are not implemented in this command. Keep unresolved requested languages visible to the user instead of substituting a broader article.
2. Obtain an inclusive start and end date. The command excludes today in UTC plus seven completed days by default, clipping the requested end if needed. This buffer is a precaution, not a completeness guarantee. Use an explicit `--as-of` to freeze this decision for repeated runs.
3. From this skill directory, run `python3 -m tools.pageviews --help` to inspect arguments. Supply a descriptive `--user-agent`; include real contact information for ongoing Wikimedia access. Dates earlier than 2015-07-01 are unsupported.
4. Run the download and read its JSON summary, not the full daily series. A successful run returns paths to the saved raw response, calendar-aligned series and metadata. Data values are validated again when loading a cached response.

Example for an already confirmed Czech article:

`python3 -m tools.pageviews --project cs.wikipedia.org --article "Přerušovaný půst" --start 2026-07-12 --end 2026-07-14 --as-of 2026-09-25 --user-agent "trend-visor/0.0.1 (<your real contact>)"`

## Reading results

| Status | Meaning |
| --- | --- |
| `complete` | Every expected date has one valid record; this describes calendar coverage only. |
| `partial` | Some dates have no record. Report the missing count and dates. |
| `no_observations` | An empty items array was returned. This is not a zero-view series or proof of article absence. |
| `error` | Inspect `error.code` and `error.details`; no usable result was produced. |

In `series.json`, an observed zero is `views: 0`, `status: observed`. An absent record is `views: null`, `status: missing`. All dates are preserved; the summary previews at most ten missing dates. Collection does not impute gaps or calculate change.

`agent=user` means Wikimedia classified the traffic as user traffic; it does not guarantee that all bots were removed. Pageviews count events, not unique people or purchase intent. Counts apply to the requested title; redirect titles and historical page moves are not automatically combined.

When `request.end_was_clipped` is true, tell the user the effective dates. A complete calendar alone does not establish reliable trends or complete underlying traffic measurement.

## Analyze an existing snapshot offline

1. Select the exact directory containing `raw.json`, `series.json` and `metadata.json`; use the parent directory of any path in the collection result's `artifacts`. An older snapshot remains selectable even after a refresh. This operation reads files only and never downloads or updates them.
2. Specify two inclusive periods: `--baseline-start`, `--baseline-end`, `--current-start`, `--current-end`. Both must fit inside the snapshot's effective window, and the baseline must end before the current period starts. Periods may have different lengths or a gap; dates are never silently clipped for analysis. Keep the research windows explicit rather than moving them to hide missing observations.
3. Run `python3 -m tools.pageviews analyze --help`, then supply `--snapshot` and the four dates. No `--user-agent` is needed. Add `--monthly` when the user needs within-period detail; the default response contains only the two period summaries and their comparison.

Replace `<snapshot-directory>` below with the selected directory:

`python3 -m tools.pageviews analyze --snapshot "<snapshot-directory>" --baseline-start 2024-09-18 --baseline-end 2025-09-17 --current-start 2025-09-18 --current-end 2026-09-17 --monthly`

The JSON result includes the snapshot path, original source and request, `analysis_version`, method, `baseline`, `current`, and `comparison`. It is returned on stdout, not saved automatically. Analysis `status` describes coverage of the two selected periods, not all dates in the source snapshot and not statistical confidence.

- `sum_observed_views` is the sum for observed days only, not a full-period total when coverage is partial. It is `null` when there are no observations.
- `mean_daily_views_observed` divides that sum by observed days, counting explicit zeros and excluding unknown days. It is `null` when no days were observed. For complete coverage it is also the full-period daily mean.
- `comparison.change_percent` is `100 × (current daily mean − baseline daily mean) / baseline daily mean`. Calculation uses unrounded means; displayed means and change are rounded to six decimal places. The unit is percent, not a fraction or percentage points.

| Comparison status / reason | How to report it |
| --- | --- |
| `computed` / `null` | Both periods have full coverage and a positive baseline. Report the descriptive change between these exact windows, not persistent growth or a forecast. |
| `not_computed` / `incomplete_coverage` | Keep the observed summaries, but report that a full-period change was not computed. `affected_periods` identifies the incomplete side(s). Leave `change_percent` null; do not reconstruct it from the observed-day means. |
| `not_computed` / `zero_baseline` | Coverage is complete, but the baseline daily mean is zero. A relative percentage is undefined, including when both means are zero. |

`--monthly` adds separate baseline/current arrays of calendar-month summaries. `window` contains the actual included dates; `partial_calendar_month` marks clipped first or last months. This flag is distinct from missing observations inside those dates. The same calendar month can appear in both arrays if the two periods divide it.

These are descriptive results: there is no seasonal adjustment, imputation, smoothing, uncertainty interval or automatic assessment of sustained growth. The same mean change can come from gradual growth or a short spike. Neither complete coverage nor a positive change proves demand for a product.

## Saved snapshots and failures

Snapshots default to `assets/pageviews/` inside the skill. Each contains `raw.json` (unchanged response bytes), `series.json` (validated calendar), and `metadata.json` (request, source URL, fetch time, versions, checksums and coverage). The CLI returns their paths.

An exact request, including `--as-of` and the safety-buffer setting, reuses its latest saved snapshot without HTTP. This is snapshot reuse, not a freshness guarantee. Overlapping but different requests are not merged in this first version. Use `--refresh` when a new observation is needed; it preserves previous snapshots. Use `--output-dir` to choose another storage location.

Snapshot reads verify checksums, validate the raw response, and reconcile the saved calendar and coverage with it. An explicit snapshot path does not require a usable cache index. These checks detect local inconsistencies, not prove source authenticity against deliberate edits to all files and checksums.

- `data_unavailable`: HTTP 404 can mean zero views or data not loaded. Report the ambiguity; verify availability separately rather than declaring the article missing.
- `rate_limited`: respect `Retry-After`. The command does not retry automatically.
- `network_error` / `http_error`: report the failure; it is not evidence about interest in the topic.
- `invalid_response`: malformed values, mismatched dimensions, duplicates or out-of-window records were rejected.
- `cache_error`: a saved snapshot failed integrity checks. Inspect it or explicitly request a new snapshot with `--refresh`.
- `snapshot_error`: the explicitly selected snapshot is missing, unreadable or inconsistent. Check its directory and files; analysis does not silently refetch or select a newer snapshot.
- `invalid_request` / `invalid_arguments`: correct the input using the error message and `--help`.
- `storage_error` / `response_too_large`: correct the filesystem issue or request a smaller date range.

Exit code 0 means the operation succeeded, including partial summaries or an undefined comparison; 2 means invalid input; 1 means another failure. Normal results and errors are JSON on stdout; `--help` and `--version` are plain text.

## Code map and tests

- [models.py](tools/pageviews/models.py): request parameters, date bounds and data records.
- [client.py](tools/pageviews/client.py): one bounded HTTP request and network errors.
- [validation.py](tools/pageviews/validation.py): response validation and calendar coverage.
- [storage.py](tools/pageviews/storage.py): immutable snapshots and exact-request reuse.
- [cli.py](tools/pageviews/cli.py): argument parsing and orchestration.
- [analysis.py](tools/pageviews/analysis.py): pure period summaries, calendar months and descriptive mean comparison.
- [analysis_cli.py](tools/pageviews/analysis_cli.py): read-only snapshot analysis command.
- [cli_common.py](tools/pageviews/cli_common.py): shared JSON output and argument errors.

Run the offline suite from the skill directory with `python3 -m unittest discover -s tests -v`. Tests use synthetic responses and temporary directories, not live Wikimedia traffic. These commands have not yet been evaluated as a full agent workflow on a small model.

