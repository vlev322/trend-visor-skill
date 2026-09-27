# Error codes

All commands print JSON with `status: "error"` and an `error.code`/`error.message`
on failure. Exit code 2 means invalid input (fix arguments before retrying);
exit code 1 means the operation ran but failed or produced a partial result;
exit code 0 means it completed, including a `null`/`not_computed` result.

| Code | What it means | What to do |
| --- | --- | --- |
| `invalid_request` / `invalid_arguments` | Bad or missing argument, malformed date, or a combination that isn't allowed (e.g. `--offline` with `--refresh`). | Fix the input using the message and `--help`. |
| `invalid_response` | Wikimedia returned something the validator could not trust (bad JSON shape, mismatched dimensions, duplicate rows). | Not evidence about the topic; report the failure. |
| `data_unavailable` | Pageviews HTTP 404. Can mean zero views or the series not loaded yet. | Do not report as "no interest"; note the ambiguity. |
| `rate_limited` | Wikimedia asked to slow down (HTTP 429). | Respect `Retry-After`; no automatic retry is made. |
| `network_error` / `http_error` | The HTTP request failed or returned an unexpected status. | Not evidence about the topic; report the failure. |
| `entity_unavailable` | The selected Wikidata item is missing or a redirect. | Re-search or ask the user for a corrected ID. |
| `resolution_error` | The saved resolution file is missing, unreadable, or internally inconsistent. | Re-run `resolve` and save a fresh file. |
| `cache_miss` | `--offline` requested a snapshot that isn't cached for this exact request. | Drop `--offline` or run without it to fetch. |
| `collection_stopped` | An earlier rate limit/service error stopped further requests in the same `study` run. | Respect the earlier error; don't force more requests in the same run. |
| `snapshot_error` / `storage_error` | A snapshot directory is missing, unreadable, or its checksums don't match. | Point to a valid snapshot directory; do not hand-edit snapshot files. |
| `study_source_error` | A `study --from-study` follow-up couldn't reuse the parent snapshot for the requested dates. | Check the dates fall inside the parent study's collected window. |
| `report_source_error` | The referenced study file isn't a valid, readable study JSON. | Point `--study` at an actual saved `study` output. |
| `evidence_too_large` | The requested report page exceeds the response size bound. | Lower `--limit`, or ask for one language at a time via `--offset`. |
| `output_exists` | The output path already exists (JSON/Markdown/PNG/PDF are never overwritten). | Choose a new path. |
| `artifact_write_error` / `artifact_too_large` / `chart_write_error` | Filesystem, permission, or size problem while saving a result. | Check the destination path, permissions, and disk space. |
| `pdf_one_page_not_eligible` | The report has too many languages, or too much text, to fit one A4 PDF page. | Use the Markdown report, or narrow the languages/question/criteria. |
| `missing_dependency` | An operation needs the `charts` extra (matplotlib) and it isn't installed. | Run `uv sync --locked --extra charts`, then use `.venv/bin/python`. |
