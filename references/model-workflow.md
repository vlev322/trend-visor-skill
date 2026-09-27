# General model workflow

Use this runner when a model should choose operations on a saved discovery or
research state. Topics, languages and periods come from that state, not a named
evaluation profile. It supports the current one-topic/two-period study format;
unsupported requests require clarification, not a fabricated method.

## Host setup and first request

1. Save the user's question with [discovery begin](discovery-workflow.md), optionally
   supplying a complete scope. An existing [research state](research-state.md) is
   also a valid starting point. Keep the exact file and SHA256.
2. From the skill directory, inspect `python -m tools.model_eval.workflow --help`.
   Select `--kind discovery` or `--kind research`, `--state FILE SHA256`, and a new
   `--output-dir`. Use an empty directory outside snapshots. Existing artifacts
   are never overwritten. The runner does not accept historical `live.json` files.
3. Start with `--dry-run`: it returns the prepared messages/tool schema, current
  pause status and context estimate, without writes, credentials, SDK imports or
  HTTP. The estimate is a UTF-8 byte upper proxy, not a tokenizer count; see
  [context preflight](context-preflight.md). A dry run does not verify provider
  capacity or model competence.
4. Only after checking the specific model/provider capacity and tokenizer assumptions,
  resource budget and output reserve, replace `--dry-run` with `--run-model` and pass
  `--context-window-tokens N --context-source "checked reference"`. Live calls are
  blocked without both. The per-request estimate also reserves max output tokens and
  margin; an estimated over-capacity request is not sent. The evaluation extra supplies the existing
   OpenAI-compatible client. Credentials stay in the local `.env`/environment using
   `TREND_VISOR_LLM_BASE_URL`, `TREND_VISOR_LLM_MODEL`, `TREND_VISOR_LLM_API_KEY`.
   Never put secrets in state, question, instructions or command arguments.
5. Network permissions are separate: `--allow-lookup` permits Wikidata/MediaWiki
   metadata requests; `--allow-collection` permits pageview requests after article
   approval. Either requires a real descriptive `--user-agent`. Without collection
   permission, `collect` uses exact cache only. Permission for the model endpoint
   does not imply permission for Wikimedia. The model cannot change these flags.

The model receives exactly one function, `wikipedia_workflow`, with the currently
allowed operations. Runtime validation enforces operation-specific fields even
though the provider schema is not a strict discriminated union. One response must
contain one tool call, no accompanying prose. Invalid JSON, duplicate keys, lossy
numeric decoding, refusal, truncation, extra fields and unknown operations stop
the run; no repair, automatic retry or provider fallback occurs.

## Operations and boundaries

Every call supplies `operation` and exactly the extra fields listed below. Paths,
checksums, cache, date arguments for analysis and output filenames are host-owned.
The provider schema itself includes only fields used by the currently allowed
operations; runtime validation remains authoritative for exact operation-specific
fields and rejects extras.

| Operation | Extra fields | Boundary |
| --- | --- | --- |
| `clarify` | `question` | Save the model's question and pause. Its prose requires review. |
| `set_scope` | `scope` | Complete discovery scope per discovery reference; retains original question/criteria, clears previous selection. Never guess missing intent. |
| `search` | none | Ready discovery state, metadata permission required. |
| `next_page` | none | Saved search with another page, metadata permission required. |
| `resolve` | `entity` | An ID from the current saved search page; then pause for review. |
| `collect` | none | Host-approved articles/dates; offline unless host permits pageview HTTP. |
| `evidence` | none (an optional legacy `offset` echo is accepted only when it exactly matches the host cursor) | Return the next verified language summary in requested order; the host advances the cursor and exposes the next page only while one remains. The echo never selects/skips a page. All-language count stays explicit. |
| `detail` | `language`, `detail_kind`; optionally `start`, `end`, `offset`, `limit` | One bounded, verified evidence-detail page from the active research state. `observations` and `missing_dates` require inclusive dates; other kinds use the recorded study/attachment scope. Follow `page.next_offset` without changing the other selection fields. |
| `analyze` | `language`, `top_days`, `trim_days` | Descriptive research only; use its exact snapshot/periods. Explicit positive day counts, at most 36525. |
| `chart` | `language` | Descriptive research only; save and verify PNG plus JSON sidecar. Requires charts extra. |
| `propose_rules` | `rules`, `match` | [Numeric criteria contract](report-criteria.md); pause without evaluation. |
| `report` | none | Generate the verified deterministic Markdown/JSON, not model-written factual prose. |

There is **no approval, arbitrary file, shell or arbitrary URL operation**. No
missing-value cap can be invented by this adapter. Existing explicitly supplied
attachments may contain one and retain its limitations. These are dispatcher
boundaries, not a sandbox against a host with unrestricted filesystem access.

`detail` is read-only and uses the host-pinned active research reference; the model
cannot supply a state path, snapshot, attachment, checksum or URL. Each page is
verified by the core detail reader and saved as that call's immutable result artifact.
Only the latest page and compact completed-action descriptors (`detail_kind`, range,
offset, total and `next_offset`) enter the next request; earlier daily/monthly rows are
not accumulated in prompt history. Detail paging does not collect or analyze again,
change state/approval, or evaluate proposed rules. Pending proposal states pause before
the model receives any tool operation.

Prepare diagnostic/chart attachments before proposing rules. Approved research
allows evidence/report/clarification only: adding attachments would clear approval.
Before proposing recommendations, consult [the support and reliability matrix](report-intents.md)
and [the numeric criteria contract](report-criteria.md). For changed diagnostics or scope,
the host explicitly revises the core state, preserving relevant attachment/criterion lists as documented in the state API.
The adapter refuses a second attachment of the same type/language rather than
searching scenario settings for a favorable outcome.

For unresolved textual criteria, a proposal must preserve every description
verbatim and in the same order, one rule per original condition. Its syntax is
validated before creating a descriptive revision with cleared text slots and a
pending rule state. The original revision remains linked and unchanged; failure
leaves the previous active reference intact. Verbatim descriptions do **not** prove
the numerical interpretation is faithful: the human must review metric, threshold,
parameters and `all`/`any`. Unsupported or composite conditions need clarification.

## Pauses and resume

- `awaiting_confirmation`: show the exact pinned proposal using `discovery show`
  or `research show`. For discovery, show all mappings, excerpts/issues, dates,
  methodology and criteria. No matched article means clarify instead of approve.
  For rules, show every original condition alongside its proposed numerical form.
- Only the host, after an actual affirmative reply, invokes the corresponding
  core `approve` command with the pending-state checksum and actual user reply.
  A negative or ambiguous reply is not routed to approve. SHA256 pins bytes; it
  is not proof of identity or consent.
- Resume with the new approved state/checksum and a **new run directory**. Starting
  on a pending state pauses with zero model calls, even with `--run-model`.
- `needs_user_input`: review the question, collect the answer and explicitly revise
  the core state/question/scope/criteria before a new run. The runner does not
  parse arbitrary chat replies as approval and does not import a whole transcript.
- `collection_incomplete`: technical collection failures, including cache misses,
  pause immediately. Saved study and research references plus failure counts
  remain available. No automatic re-collection. The host can inspect the research
  state for a partial report or explicitly revise the discovery for a later run.
- `failed` or `budget_exhausted`: inspect the audit and active reference, then
  decide explicitly whether to revise/resume. No silent extra model request.
- `completed`: deterministic report generation succeeded; inspect the Markdown
  and PNG. This is **not** human acceptance, validated free-form model reasoning,
  a market recommendation, a statistical fit review, or PDF completion.

The call budget is per invocation (default 12, allowed 1–40), not cumulative across
host-created runs. Core discovery also retains its 20-operation lineage counter.
The host owns the global budget and active-state registry. Deliberately resuming an
old state can repeat requests; there is no exactly-once or cross-process lock.
Metadata search is at most one HTTP request; resolve at most two plus language
count; collection at most one per matched article and may reuse cache. Core
service-limit stops still apply. These are bounds, not measured HTTP counters.

## Context and audit

Each model request contains a compact mandatory system policy, only the reference
bundle selected for the current state/allowed operations, the current revalidated
state, allowed actions, the latest result and compact completed-action descriptors.
Discovery loads `discovery-workflow.md`. Research loads `research-state.md` and
`report-intents.md`; it loads `report-criteria.md` only when rule proposals are
available or already pinned rules need interpretation, and `evidence-details.md`
only when `detail` is available. The full user-facing `SKILL.md` and host-only
`model-workflow.md` are not injected into model prompts: their mandatory safety
rules are summarized in the short `PROMPT` and enforced by the dispatcher/schema.
After discovery collection hands off to research, the next turn selects the research
bundle. Every event records the exact loaded reference names, SHA256 values and
UTF-8 byte lengths; dry-run prints the same manifest. At the 2026-09-26 measurement,
the previous unconditional seven-file bundle was 130,270 UTF-8 bytes; the selected
discovery procedure is 10,254 bytes and the maximal descriptive research bundle
(state, intent, criteria and detail references) is 40,373 bytes, a 69.01% reduction
in reference bytes versus that baseline. This excludes the compact policy, actual
state/result, tool schema and serialization overhead, and is not a token measurement.
Earlier tool outputs are not appended. Detail progress adds only kind/range/total/next offset—not prior daily rows.
Raw daily/monthly arrays remain in snapshots; diagnostic results and PNG sidecars
remain on disk. Final reports cover **all** study rows in original order, not merely
pages the model viewed.

Evidence and detail pages retain the existing 24000-byte envelope check. This is not a token
budget. The action list is bounded by the run budget; the latest result may still
include resolution excerpts. In-memory action progress is not a new trusted resume
format; resumable evidence/approval lives in the pinned core states.

`workflow.json` records initial/active references, instruction hashes, permissions,
call count, operation attempts, elapsed times and events referencing each immutable
request, normalized model response and operation result. Each event records its own
`context_preflight`; a request exceeding the declared estimate fails before the model
callback. CLI live calls also record non-secret provider/model/output-limit/timeout
settings and the host-declared context window/source. Requests are saved before
calling the provider. Provider errors preserve a structured failure, not raw errors
or credentials; ordinary valid normalized responses are retained unchanged.

`request_metrics` aggregates actual instrumented client attempts by `model`, `metadata`
and `pageviews`, with successes/errors, available HTTP statuses, measured client
latency and body bytes read by the application. Model SDK response-body wire bytes are
not exposed by the SDK; a null status on success is not replaced with a fabricated
200. `cache` counters distinguish exact hits, misses, refresh bypasses and lookup
errors. Operation-level errors and per-language partial collection outcomes remain
separate from transport success. One recorder attempt means one client invocation;
urllib-internal redirects are not counted individually.

Per-event `usage` is the provider-reported value or null. `request_utf8_bytes` counts
the runner's serialized audit envelope, not tokens or exact SDK wire framing. The
implemented request estimate is a UTF-8 byte upper proxy with explicit tokenizer and
hidden-framing assumptions, output reserve and margin—not a model-specific tokenizer
count or provider guarantee. The CLI blocks live calls without a host-checked
capacity/source declaration; the stated 150000 is not a default or independent
verification. Tariff-based costs remain unknown until a dated provider rate/source is supplied.
**10000 tokens remains soft guidance, never a failure gate.**

`usage_summary` reports a token total only when every model call supplied that exact
field. A known subtotal and count are retained separately; missing usage is not zero.
`cost_summary` is explicitly `unknown_not_configured` until a dated, sourced tariff
and currency are available; no price is inferred from usage alone.

An optional caller-supplied estimate can be enabled with all five flags:
`--input-rate-per-million`, `--output-rate-per-million`, `--tariff-currency`,
`--tariff-date` and `--tariff-source`. Rates are nonnegative decimals per million
reported prompt/completion tokens. The runner uses decimal arithmetic and computes
an estimate only when all calls have complete provider usage. It does not fetch or
authenticate the tariff; cached-token discounts, reasoning/image charges, taxes and
other provider-specific line items are excluded unless the supplied rates account
for them. Partial tariff input is rejected; missing usage or tariff remains `null`,
not zero.

Each artifact is independently atomic; a multi-file action is not a transaction.
A crash or disk failure can leave completed sidecars without a final log. Keep
them for inspection; do not silently continue or overwrite. The adapter is
sequential and uses existing analysis/chart CLI entry points in-process; it is
not a concurrent service.

## Verification status

Offline tests cover independent synthetic topic discovery, separate simulated host
approvals, two-language collection, diagnostics, PNG, criteria and report;
permissions/path injection, malformed responses, numeric precision, failure
preservation, compact action progress, no-extras dry-run and mocked SDK transport.
These are not real Wikipedia/model observations or human approvals. General live
acceptance, long-history model/context metrics and richer selective detail remain
separate work. Historical fixed-profile `live` and final-only replay tests remain
unchanged. PDF follows accepted text, not this implementation milestone.