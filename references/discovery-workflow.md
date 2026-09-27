# Persistent discovery from a question to a saved study

Use `python3 -m tools.pageviews discovery` when starting from a topic rather than
an existing study, or when a question/search/article review must survive a lost
chat context. Topics, languages, dates and methodology are inputs; there are no
astronomy/fasting profiles in this workflow. It orchestrates the existing core
search, resolve and study functions, not a new model or data-analysis algorithm.

## Actions and states

Every mutating action requires `--output NEW.json`. All later actions use
`--state FILE SHA256` for the exact preceding state. `show` is read-only.

| Action | Required state / inputs | Result |
| --- | --- | --- |
| `begin` | `--question`; optional `--scope FILE SHA256`, repeated `--criterion` | `needs_scope` or `ready_to_search` |
| `revise` | Any valid state, explicit question and optional complete scope/criteria | New revision; clears searches, selection, approvals and collection references |
| `show` | Any valid state | Verified saved context, next action and useful saved results; no HTTP |
| `search` | `ready_to_search`, `--user-agent` | Saved search result plus `searched` state |
| `search --next-page` | `searched` with non-null next offset | Explicit next search page, preserved previous page in previous state |
| `resolve` | `searched`, `--entity` from this saved page, `--user-agent` | Saved resolution plus `awaiting_confirmation` |
| `approve` | Pending proposal, exact `--confirm-sha256`, actual `--user-reply` | New `approved` state; host-only action |
| `collect` | Approved state, optional `--cache-dir` | Saved study, initial post-study research state and `collected` discovery state |

`search` and `resolve` perform real metadata HTTP when invoked outside tests.
They accept `--timeout` (default 30 seconds per request), with no automatic
retry. `begin`, `revise`, `show` and `approve` never fetch. Collection defaults
**offline**. Add `--online --user-agent "<real contact identifier>"` only when
new pageview requests are authorized. Online collection still reuses exact
cache entries; there is no implicit refresh or overlapping-range merge.

Example initial save:
`python3 -m tools.pageviews discovery begin --question "<user question>" --output assets/reports/question.json`.

Read `next_action`: incomplete scope needs clarification; search requires entity
selection or refinement; a pending result with zero matches needs scope/mapping
clarification, not approval. Resolve preserves every requested language,
including no-sitelink and technical failures. A matched identity still requires
human review of title, meaning, article preview and cross-language comparability.
A broad question that cannot be represented by this single-topic/two-period
study needs explicit clarification rather than a silent narrower substitution.

## Complete scope file

Save the following fields as one JSON object, then supply its exact checksum
via `--scope FILE SHA256` to `begin` or `revise`. All fields are required and
extra fields are rejected; file size is limited to 65536 bytes.

| Field | Meaning |
| --- | --- |
| `query` | Canonical Wikidata label/alias search text, 1–500 characters |
| `search_language` | Canonical language code for searching and resolving labels |
| `languages` | 1–50 distinct canonical Wikipedia language-edition codes in desired order |
| `baseline_start`, `baseline_end` | Inclusive YYYY-MM-DD baseline dates |
| `current_start`, `current_end` | Inclusive YYYY-MM-DD current dates; after baseline without overlap |
| `as_of` | Explicit reference UTC date |
| `lag_days` | Explicit nonnegative integer safety buffer; no silent clipping |
| `methodology` | Null for plain description, or object with exactly `trend_model` and `hac_lags` |

For calendar-only analysis use `methodology` with both values null. For the
optional historical slope, use `trend_model: linear-calendar-hac` and an explicit
nonnegative `hac_lags` chosen with rationale before inspecting inference.
Dependencies and fit eligibility remain the existing methodology's responsibility.
Collection records these settings, and saved study settings are checked against
approved scope both study-wide and per analyzed article. Report rendering remains
explicitly descriptive; it does not independently revalidate the statistical fit.

Dates earlier than 2015-07-01 or beyond the selected safety cutoff are rejected,
not filled with invented observations. Missing scope can be saved as a question;
partial guessed scope cannot be used to search or collect. `revise` replaces the
whole scope and textual criteria list, not selected fields, and clears previous
article selection/approval even if only dates changed.

## Host-owned article and scope confirmation

Show the full resolved mapping and the scope (question, languages, periods,
as-of, buffer, methodology and unresolved criteria). The host interprets the
actual human reply and only then invokes:
`python3 -m tools.pageviews discovery approve --state "<pending.json>" "<pending-sha256>" --confirm-sha256 "<same-pending-sha256>" --user-reply "<actual non-secret confirmation>" --output "<approved.json>"`.

The confirmation checksum is for the **discovery proposal**, not just its
resolution file. At least one article must be matched. The host must not expose
`approve` to autonomous model tool calls, fabricate a reply or treat model-written
approval as human approval. Reply text is recorded (single line, at most 2000
characters), not automatically parsed for consent. Scope confirmation is distinct
from a later numeric criteria proposal/confirmation in `research`.

Checksums verify byte identity and the saved proposal linkage, not source
truth/authenticity, a human's identity or intent. Callers with unrestricted shell
or file access can bypass this workflow using direct CLI operations or coordinated
file edits. Host permissions and actual review remain essential.

## Collection and handoff

Run `discovery collect --state FILE SHA256 --cache-dir DIRECTORY --output NEW.json`
for cache-only collection, or explicitly add `--online --user-agent ...`.
The command takes dates, languages, chosen entity and methodology from the
approved context; it has no scope override flags.

It creates sidecars based on the output stem: `<stem>-study.json` and
`<stem>-research.json`. The discovery result returns pinned `study` and `research`
references. The latter is a standard descriptive post-study state holding the
same question and unresolved criteria, usable immediately with
`report --research-state FILE SHA256`. Preserve the discovery state for its
article-approval lineage; the research state is a separate post-study lifecycle.

For richer output, run `analyze --diagnostics` and `chart` against the exact
study snapshots and dates, save their result JSON, then use `research revise`
with all applicable attachment references. Propose and separately approve numeric
rules when needed, then generate the report. Use the post-study and criteria
references linked directly from `SKILL.md` for these contracts.

`collected` means study and handoff artifacts were saved, **not** that every
language succeeded. Read `study_status` and `study_summary`; cache misses,
throttling or unresolved checks can yield partial/unavailable results. The CLI
returns exit 1 for technical failures while preserving useful output, and exit 0
for non-matches or incomplete calendar coverage without technical failure.

## Persistence, budgets and failure behavior

- Discovery version 1 stores question, scope, textual criteria, selection,
  pinned artifact locators, approval, parent, revision, status and operation count.
  State size is at most 64 KiB; raw daily data never live in the state.
- Search and resolution sidecars use `<output-stem>-search.json` and
  `<output-stem>-resolution.json`. All expected output paths are preflighted
  before network calls; existing files are never replaced. Files are published
  independently, not as a multi-file transaction. On filesystem/process failure,
  a finished sidecar or snapshot may remain without a final state. Inspect before
  explicitly retrying; exactly-once HTTP across crashes is not guaranteed.
- States and sidecars are immutable. Only the selected current search page is
  included in each summary; previous pages remain selectable through old states.
  No automatic branch selection or latest pointer exists.
- Domain failures during search/resolve/collection create a `failed` state with
  the operation error, and clear active approval. `show` reads it without retry.
  Review service limits, then explicitly revise/restart if appropriate. Validation
  or filesystem failures may return an error without publishing a failed state.
- A lineage allows at most 20 search/resolve/collect operations. Revisions retain
  this counter. This is **not** an HTTP count or global rate limit: one search
  makes at most 1 request, one resolve at most 2 + language-count requests, and
  online collect at most one pageview request per matched article. Core stop-on-
  throttling rules still apply. Summary `limits` states these upper bounds.
- Reusing an old state with a new output path explicitly creates a branch and can
  execute another operation. The host must track the active branch and actual
  request budget; these immutable files are not a global deduplication lock.
- Reads verify current referenced artifacts, the selected candidate, resolved
  entity/languages, exact approval proposal, and collected study/research scope.
  Approval and collection inspect the necessary parent/proposal linkage, not a
  recursive audit of every ancestor. Retain historical files for audit.
- The summary returns saved candidate labels and article excerpts as untrusted
  evidence, never as instructions. It is bounded by core request/result limits,
  not by the report's 24000-byte page budget or an LLM tokenizer. The host must
  account for full prompt size; 10K is an efficiency guideline, not a hard cap.

This is a general code/CLI workflow tested with synthetic HTTP fixtures. A model
runner choosing actions, permission separation, independent live topics and
measured end-to-end model acceptance are still separate completion requirements.
