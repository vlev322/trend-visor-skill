# Selective evidence details

This document defines the read-only interface implemented by `tools/pageviews/evidence_details.py`.
The interface exposes bounded detail from one
host-pinned research state without adding a second analysis path or allowing the caller
to select files, URLs, snapshots, or diagnostic parameters.

## Public interface

```python
read_evidence_detail(
    state: JsonArtifact,
    *,
    language: str,
    kind: str,
    start: str | None = None,
    end: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> dict
```

`state` is supplied by the host as an exact absolute path and lowercase SHA256. A model
may select only `language`, `kind`, the applicable date range, and pagination values.
The function performs filesystem reads only. It makes no HTTP request, writes no file,
and does not mutate the state or any referenced artifact.

The CLI mirrors the same seam:

```text
python -m tools.pageviews research detail \
  --state FILE SHA256 --language LANGUAGE --kind KIND \
  [--start YYYY-MM-DD --end YYYY-MM-DD] [--offset N] [--limit N]
```

The CLI emits the returned object or the existing structured `PageviewsError` envelope.
It has no network, output-file, approval, rule, or analysis-parameter option.

## Common response envelope

Every available page has this shape:

```json
{
  "operation": "evidence_detail",
  "detail_version": 1,
  "research": {
    "research_id": "…",
    "revision": 2,
    "status": "descriptive|awaiting_confirmation|approved",
    "state_sha256": "…",
    "rules_evaluated": false
  },
  "study": {"sha256": "…"},
  "language": "uk",
  "kind": "observations",
  "status": "available",
  "reason": null,
  "unit": "views_per_day",
  "order": "date_ascending",
  "selection": {"start": "2026-01-01", "end": "2026-01-31"},
  "page": {
    "offset": 0,
    "limit": 100,
    "returned": 31,
    "total": 31,
    "next_offset": null
  },
  "items": [],
  "verification": {
    "status": "recomputed_and_matched",
    "source": "validated_snapshot",
    "raw_sha256": "…",
    "attachment_sha256": null
  },
  "limitations": []
}
```

`total` is the exact number after semantic filtering and before pagination. `items` is
never clipped independently of `page`; `next_offset` is present whenever more rows
exist. Ordering and ties are deterministic. The encoded response must not exceed
`reports.MAX_EVIDENCE_BYTES`, currently the existing 24,000 UTF-8 byte evidence
envelope. A page that exceeds it fails with
`evidence_too_large` and tells the caller to request a smaller `limit`; fields are never
silently removed.

An unavailable detail returns the same identity and selection fields, with
`status: "unavailable"`, a stable `reason`, `items: []`, `page.total: 0`, and
`next_offset: null`. Invalid requests or failed integrity checks are errors, not
unavailable evidence.

## Detail kinds

### `observations`

Source: the exact snapshot pinned by the study row. The reader calls `read_snapshot`
and `validate_response`, then confirms request, source, artifacts, coverage, and the
recorded descriptive analysis through the existing report-source verification path.

`start` and `end` are required, inclusive, and must be within the study snapshot window
from baseline start through current end. Each item is:

```json
{"date": "2026-01-01", "views": 42, "observation": "observed", "study_period": "baseline|between_periods|current"}
```

A missing value has `views: null` and `observation: "missing"`; an observed zero has
`views: 0` and `observation: "observed"`. Rows are date-ascending. This is a read of
validated observations, not a new comparison or scenario.

Default/max `limit`: 50/100 rows.

### `missing_dates`

Source and verification are the same as `observations`. `start` and `end` are required
and have the same inclusive bounds. Items contain only `{"date": "YYYY-MM-DD",
"study_period": "…"}` for days whose validated value is unknown. An in-range selection
with no missing values is a successful empty result, not unavailable evidence and not
proof of zero traffic.

Default/max `limit`: 50/100 rows. Order: date ascending.

### `largest_days`

Source: exactly one pinned `analyze` attachment for the language. The implementation
reads the complete checksum-pinned attachment, not the date-list-free projection made by
`load_report_attachments` for report pages. If none is attached,
the detail is unavailable with `reason: "diagnostics_not_supplied"`. The reader uses the
attachment's recorded `top_days` parameter; callers cannot supply or change it. It
revalidates the attachment against the pinned snapshot and reruns the existing diagnostic
scenario. `top_dates_truncated` in that complete attachment identifies a saved preview
shorter than the recorded parameter. The reader reconstructs the complete selected set
from the same validated series, parameter, tie rule (views descending, then date
ascending), and aggregate checks. This is replay of the recorded scenario, not creation
of a different scenario.

No `start` or `end` is accepted. Baseline rows precede current rows; within each period,
rank follows views descending and date ascending:

```json
{"period": "baseline", "rank": 1, "date": "2026-01-03", "views": 900}
```

The envelope also records the fixed diagnostic parameter and attachment SHA256. It does
not claim that large days are errors, bots, or corrected observations.

Default/max `limit`: 20/50 rows.

### `monthly_summaries`

Source: the `monthly` arrays recorded in the full pinned study row. Their presence is
checked before `load_report_source` deliberately removes them from its compact
model-facing projection. The reader recomputes those arrays with
`analyze_series(..., include_monthly=True)` and requires an exact match.
If the study did not record monthly analysis, this detail is unavailable with
`reason: "monthly_not_recorded"`; the reader does not create a new aggregation on demand.

No `start` or `end` is accepted. Baseline months precede current months; each group is
month ascending. Each item contains `period`, `month`, exact calendar `window`, `status`,
`coverage`, `sum_observed_views`, `mean_daily_views_observed`, and
`partial_calendar_month`. Units are views and views per observed day as named by those
fields.

Default/max `limit`: 12/24 rows.

### `calendar_comparisons`

Source: the calendar comparison inside a recorded study methodology. The reader reruns
`compare_calendar_months` over the pinned validated series and requires an exact match to
the recorded comparison. If no methodology/calendar comparison was recorded, the detail
is unavailable with `reason: "calendar_comparison_not_recorded"`. It never fits or reports
the optional statistical trend model.

No `start` or `end` is accepted. Items are the complete recorded pairs in current-month
ascending order. They include baseline/current month summaries, `status`, `reason`,
`affected_periods`, difference, direction, percentage change and its separate reason.
Excluded pairs remain rows; their reasons are not filtered out. The envelope includes the
comparison summary and caveat.

Default/max `limit`: 4/8 rows.

## Pagination and selections

- `offset` and `limit` must be integers, not booleans; `offset >= 0` and
  `1 <= limit <=` the kind-specific maximum.
- `offset == total` is valid only when `total == 0`; otherwise an offset at or beyond the
  end is `invalid_request`. This prevents empty phantom pages.
- The caller follows only the returned integer `next_offset`; no server-side cursor or
  mutable session is needed because all inputs are checksum-pinned.
- Concatenating pages in offset order must reproduce the complete ordered selection with
  no gaps or duplicates.
- The limits bound one result envelope, not the model context window or the total study
  length. They may be revised only with a detail-version change and fixture byte tests.

The initial maxima were checked offline on an eight-year, 2,922-day fixture. Compact
representative rows measured 74 bytes for observations, 72 for missing dates, 53 for
largest-day rows, 150 for monthly rows, and 551–781 for full calendar pairs. Even using
the largest measured row plus 1,500 bytes of envelope overhead, the proposed maxima were
8,900, 8,700, 4,150, 5,100, and 7,748 bytes respectively. These are fixture measurements,
not universal size proofs; the runtime 24,000-byte check remains authoritative.

## State and approval semantics

This detail interface operates independently of report generation. Factual detail may be
read from `descriptive`, `awaiting_confirmation`, or `approved`
research states after all referenced evidence and approval linkage are verified. A pending
state therefore does not hide its underlying observations, but it also does not authorize
its proposed rules:

- the reader never evaluates criteria and never returns `criteria_evaluation`;
- `research.rules_evaluated` is always `false`;
- a pending response records `research.status: "awaiting_confirmation"` and limitation
  `"pending_rules_not_evaluated"`;
- report generation remains blocked by the existing confirmation rule;
- the interface has no approval operation and cannot revise or replace attachments.

Thus detail reading cannot turn a proposal into a decision or bypass host-owned approval.

## Availability and errors

| Condition | Result |
| --- | --- |
| Analyzed language and recorded detail exist | `available` page |
| Valid in-range selection has no matching missing dates | `available`, zero rows |
| Language exists but is unresolved, not collected, or collection failed | `unavailable: language_not_analyzed` |
| Required diagnostics attachment is absent | `unavailable: diagnostics_not_supplied` |
| Monthly arrays were not recorded | `unavailable: monthly_not_recorded` |
| Calendar methodology was not recorded | `unavailable: calendar_comparison_not_recorded` |
| Research status is pending | Facts available; `pending_rules_not_evaluated` limitation |
| Unknown language or kind | `invalid_request` |
| Only one of `start`/`end`, reversed dates, or a non-date value | `invalid_request` |
| Date outside baseline-start through current-end | `invalid_request` |
| `start`/`end` supplied for a kind that does not accept them | `invalid_request` |
| Invalid offset/limit type, value, or exhausted page | `invalid_request` |
| State, study, snapshot, attachment, source, or recorded result changed | Existing structured state/source/attachment integrity error |
| Valid requested page exceeds 24,000 UTF-8 bytes | `evidence_too_large`; retry with smaller limit |

A single-day range (`start == end`) is valid. A reversed range is invalid rather than an
ambiguous “empty range.” An empty filtered result is represented only when a valid range
contains no matching rows.

## Examples

### Available observations

```json
{
  "language": "cs",
  "kind": "observations",
  "status": "available",
  "unit": "views_per_day",
  "order": "date_ascending",
  "selection": {"start": "2026-07-01", "end": "2026-07-03"},
  "page": {"offset": 0, "limit": 50, "returned": 3, "total": 3, "next_offset": null},
  "items": [
    {"date": "2026-07-01", "views": 10, "observation": "observed", "study_period": "baseline"},
    {"date": "2026-07-02", "views": null, "observation": "missing", "study_period": "baseline"},
    {"date": "2026-07-03", "views": 0, "observation": "observed", "study_period": "current"}
  ]
}
```

The implementation adds the common research, study and verification fields omitted here
only to keep the example short.

### Unavailable diagnostic

```json
{
  "language": "pl",
  "kind": "largest_days",
  "status": "unavailable",
  "reason": "diagnostics_not_supplied",
  "page": {"offset": 0, "limit": 20, "returned": 0, "total": 0, "next_offset": null},
  "items": [],
  "verification": {"status": "source_verified_detail_absent", "source": "research_state", "raw_sha256": "…", "attachment_sha256": null}
}
```

### Invalid out-of-study range

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The requested detail range must be inside the pinned study window.",
    "details": {"allowed_start": "2026-07-01", "allowed_end": "2026-07-04"}
  }
}
```

## Verification seam

The implementation should expose only the single public function above. Internally it may
share a private verified context loader with report generation, but it must reuse
`read_research`/research-state linkage, `load_report_source`, `read_snapshot`,
`validate_response`, `analyze_series`, `run_diagnostics`, and
`compare_calendar_months` rather than duplicate their algorithms. Tests exercise the
public interface and CLI, including byte mutation, pagination joins, null/zero, ties,
leap days, partial months, pending states, and patched network/write calls.

Checksums bind exact bytes and support reproducibility; they do not prove user identity,
intent, or consent. “Recomputed and matched” means deterministic agreement with pinned
artifacts under the named method, not statistical confidence, audience size, demand, or
causality.
