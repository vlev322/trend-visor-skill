# Live model acceptance card — 4.2/4.3 text accepted; 4.4 synthetic acceptance partial

**Status:** bounded 4.1 model-only capability probe **passed** on 2026-09-27.
The approved Ukrainian-only pilot completed 4.2 and one user-confirmed 4.3
period-split follow-up using only the pinned snapshot. Both deterministic reports
were independently reviewed offline and explicitly accepted by the user on
2026-09-27. The separate 4.4 synthetic live-model pass remains partial, so the
aggregate 4.5 acceptance gate is not yet closed.
The previous unauthenticated GET to the old provider `/v1` is historical and says
nothing about OpenRouter.

## Confirmed scope and collected article

- User question (2026-09-26): “скажи чи був спад тренду теми ші в українській
  вікіпедії”. User selected **Ukrainian only**; do not silently add English or
  another edition. Interpret “trend” only as a descriptive comparison of daily
  means, not statistical significance, causality or a forecast.
- User-confirmed baseline/current dates: `2024-01-01..2024-12-31` and
  `2025-01-01..2025-12-31`, `as_of=2026-09-26`, safety lag 7. These are the two
  latest complete calendar years selected for this question.
- User explicitly confirmed collection with “так” on 2026-09-27 for Wikidata `Q11660`,
  Ukrainian Wikipedia article **«Штучний інтелект»**, page ID `1739`, namespace 0,
  direct sitelink with no redirects. The preview excerpt describes artificial
  intelligence as a branch of computer science. This is a broad topic article;
  pageviews would measure views of this exact article title, not all AI-related
  articles or total interest in AI. Host recorded the approval against the exact
  discovery proposal checksum before collection.
- This one-language question does not satisfy the plan's minimum 2+ eligible-language
  acceptance case. That remains a separate required scenario; do not add a language
  to this question without the user's scope change.
- Follow-up (2026-09-27): user selected and confirmed baseline
  `2024-03-01..2024-05-30` against current `2025-03-01..2025-05-30`; same article,
  same `uk` edition and same already-collected daily snapshot. No new HTTP was
  authorized or performed. Numeric rule criteria remain **not selected**; each
  later rule proposal still needs its own review and approval.

## Provider, context and run limits

- Provider/model preference (user, 2026-09-26): **OpenRouter**, exact requested
  variant `inclusionai/ling-3.0-flash-fin:free`. On 2026-09-27 the non-secret
  config was checked and corrected to `https://openrouter.ai/api/v1` and the exact
  namespaced ID. The user said setup was ready; the key value was never read or
  recorded. Credential rotation is treated as user-confirmed, not independently
  verifiable from this workspace. OpenRouter's [Chat Completions API](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion.md)
  is OpenAI-compatible. The bounded exact-ID tool-call probe verified one successful
  response through this route and the existing adapter; future availability and the
  underlying provider endpoint remain unknown.
- Public OpenRouter [model catalog](https://openrouter.ai/api/v1/models), checked
  2026-09-27, lists the exact `:free` ID as a separate entry. That entry reports
  `pricing.prompt=0`, `pricing.completion=0`, `context_length=262144`,
  `top_provider.context_length=262144`, `top_provider.max_completion_tokens=32768`,
  and `tools` plus `tool_choice` among supported parameters. These are **catalog
  declarations**, not observed behavior or a guarantee that a free endpoint is
  currently available to this account. OpenRouter's [free-variant documentation](https://openrouter.ai/docs/guides/routing/model-variants/free.md)
  says a `:free` variant has its own catalog entry, price, context and endpoints, and
  warns that rate limits/availability may differ from the paid variant.
- The catalog's `canonical_slug` for the selected variant points to the base model
  `inclusionai/ling-3.0-flash-fin`; the routable `id` is the requested
  `inclusionai/ling-3.0-flash-fin:free`. Do not silently remove `:free` or fall back
  to the paid model. OpenRouter documents that a free variant must be explicitly
  listed and that variants can be queried through its model API. The one-call probe
  requested and returned the exact `:free` model ID and a valid tool call. The
  underlying provider endpoint selected by OpenRouter is still unknown.
- The probe demonstrated one successful tool-call response through the existing
  adapter using its `tools` / `tool_choice=auto` request shape. The returned harmless
  function was **not executed**. This is a bounded protocol check, not a guarantee of
  future route availability, rate limits or workflow factual quality.
- Effective endpoint context ceiling, account rate limits and continued availability
  remain **unverified**. The probe was tiny; no large-context stress test was attempted.
- Context: OpenRouter's exact free catalog entry declares **262144 tokens**. The
  user's prior **150000-token** figure is a user-provided target, not a verified
  endpoint limit. Any `--context-window-tokens`/`--context-source` declaration for
  preflight must identify that public catalog entry and label the value as
  catalog-based, not endpoint-confirmed. Local preflight uses UTF-8 bytes as a
  conditional upper estimate, not an exact model tokenizer count; output reserve
  defaults to 2048, margin defaults to `max(256, ceil(5% of the declared window))`.
- Previous provider/model (`https://llm.ho.1024soft.com/v1`, `qwen3-coder-next`)
  is **historical and no longer intended**. Its unauthenticated `/v1` GET returned
  404; it did not probe a completion and is not evidence about OpenRouter or this
  Ling route. Upstream Qwen model-card/parser facts are superseded for the chosen
  provider/model.
- Spend preference (user, 2026-09-26): observe actual consumption in initial runs
  rather than set a planned spend cap. User selected **40 calls per invocation**,
  the CLI hard maximum; retries are disabled, and no dollar ceiling is enforced.
  This cap does not persist across invocations.
- Additional spend statement (user, 2026-09-27): the OpenRouter account/key has a
  **$0 spend limit**. This account setting was not independently verified and is not
  enforced by this local runner; treat it as user-reported protection, not as a
  substitute for using the exact `:free` ID, credential rotation, or route checks.
- Cross-run active-state/spend ledger: **not implemented**, because step 2.3 was
  explicitly deferred at the user's request. Historical states/runs can be replayed;
  no global exactly-once or cumulative budget guarantee is made.
- Tariff: unknown until a dated source/rates are confirmed. Optional input/output
  rates yield an estimate only with complete provider-reported token usage. Missing
  usage or tariff remains unknown, not zero. The runner does not enforce a hard
  dollar ceiling during a call.
- Stop conditions: context estimate exceeds the declared window; provider/model or
  tools mismatch; user call/time/spend cap reached; any request is ambiguous;
  article/title/scope needs human review; rate limit or service error; model usage or
  tariff remains outside the approved exposure; any checksum/source mismatch. No
  automatic retry, fallback or model/provider switch.

## Permission gates

Permission categories selected by user (2026-09-26), subject to final scope and
provider/context preflight:

1. Model endpoint requests to the selected provider/model.
2. Metadata HTTP for topic search and language/article resolution (at most one
   search request per search operation and at most two plus language-count requests
   per resolve; service limits still apply).
3. Pageview HTTP **only after** exact candidate articles, languages and dates have
   been shown and approved. Maximum one request per matched article; exact cache may
   be reused. Offline mode remains possible if online collection is not approved.

The user separately selected metadata HTTP for 4.2. Pageview permission still does
not cover unseen articles: show the exact candidate and obtain separate confirmation
before collection. The host must record the actual response and article decision.

## Current evidence / gaps

- Offline synthetic workflow, follow-up details, reports, usage parsing and context
  preflight are regression-tested. This is not model competence or live acceptance.
- 8-year synthetic benchmark: three languages, 8 model callbacks in a deterministic
  mock, maximum visible byte estimate 56,788, Wikimedia HTTP 0. The callback did not
  return provider usage, so token totals/cost are unknown. See
  [long-series benchmark](long-series-benchmark.md).
- Live capability probe (2026-09-27): one model HTTP attempt; successful response,
  `finish_reason=tool_calls`, one exact `capability_probe` call, returned model ID
  `inclusionai/ling-3.0-flash-fin:free`. Usage: 319 prompt + 41 completion = 360
  total tokens. Latency: 1,178.911 ms. Success HTTP status/body bytes are not exposed
  by the SDK adapter. Preflight: 779 input UTF-8 bytes, 14,015 estimated total
  tokens including a 128-token output reserve and 13,108-token margin, against the
  catalog-declared 262,144-token context. No topic metadata, Wikimedia/pageview HTTP,
  retry, fallback, host tool execution or durable probe artifact. Actual billed amount
  is unknown: catalog rates are zero, but the adapter did not retain provider cost and
  the user's $0 account cap was not independently verified.
- 4.2 discovery and collection: the initial short query `ШІ` returned five
  irrelevant candidates, so an immutable scope revision changed only the query to
  `штучний інтелект`; language `uk`, dates, as-of and lag were preserved. The search
  matched `Q11660` («штучний інтелект», «розділ інформатики»); resolve matched it
  directly to `uk.wikipedia.org`, article «Штучний інтелект», page 1739, no redirect.
  The user explicitly replied «так» to this exact article and periods. Host recorded
  the approval in `state-approved.json` (SHA256
  `90e0980fc009db43d286e3a45e3be81e30b04b1f83158a67cc67a300b2313138`).
- Pageview request (2026-09-27T05:53:09Z) returned HTTP 200 for only that title and
  2024-01-01..2025-12-31, `all-access/user`, daily UTC. `cache_hit=false`;
  one request, no retry. 731/731 days observed, missing=0, explicit zeros=0.
  Study SHA256 `c9d18d10b94ae8bffc8d512c41468589a737cd7da0d4d06aadd7b16ae69a6698`;
  raw SHA256 `4ead755874cbd696ee10ebbbe24fd90c6520656e2a52cb229bb9820c17b811bb`.
- Descriptive result: 2024 total 144,905; mean 395.915301/day (366/366).
  2025 total 112,312; mean 307.704110/day (365/365). Change in mean daily views:
  **−22.280319%**. No seasonal adjustment, statistical inference, confidence
  interval, causal explanation, or product-demand inference was performed.
- Sensitivity checks (separate, not cumulative corrections): remove top 3 days
  within each period → −22.944528%; trim 7 days from each edge → −22.552340%.
  Neither scenario is a statistical confidence measure or a corrected estimate.
  PNG: `assets/reports/live-2026-09-27-ai-uk-4.2/pageviews-uk.png` (SHA256
  `244bfbce42a0ce4aff9ff0089e2bda752235fc7218e0b77303b5967d2c94aafe`), visually
  reviewed; unsmoothed daily lines and both period coverages are shown.
- Final deterministic report: `assets/reports/live-2026-09-27-ai-uk-4.2/report-final.md`
  (SHA256 `01aae17c97c7984b8e85d0b118e4bd3f4b15868ac76021ceb52dfd654d35907d`)
  and JSON SHA256 `4af3f9ec6d02497d66aa428f79387a015ebb5dd4d8a2284c06a1f0e7cc0a7a3e`.
  Report verification recomputed the descriptive results and diagnostics, matched
  the chart checksum/metadata/source, and made 0 network requests. Full evidence,
  model requests/responses, state lineage and metrics are in the same report folder
  and summarized in `REMAINING_PLAN.md`.
- During 4.2 an invalid operation-specific tool argument and a later assistant
  narration-plus-tool response were safely rejected before their respective
  metadata operations. The schema/prompt guard was fixed, the two stale schema
  assertions were updated, 31 targeted tests pass, and the full suite passes:
  479 tests, 0 failures/errors/skips.
- 4.2 live model accounting: 6 model attempts in total, including two safely
  rejected protocol responses; 34,279 reported tokens (30,623 prompt + 3,656
  completion), no automatic retries/fallback. The completed research turn used
  one model request (11,257 + 430 = 11,687 tokens) and no Wikimedia HTTP. The
  accumulated model tariff/billed amount remains unknown; usage and the catalog's
  `:free` price declaration are not an invoice.
- The Ukrainian-only text report is generated, but the user has not yet accepted
  its wording/content (step 4.5). This does not satisfy the separate minimum-2-
  language acceptance case; do not expand this approved scope without new consent.
- 4.3 follow-up: the user selected `2024-03-01..2024-05-30` vs
  `2025-03-01..2025-05-30` and confirmed the year-over-year interpretation. A new
  `study_version=2` was derived from the exact verified parent study/snapshot;
  it records the literal reply and retains the real wider HTTP request window and
  raw SHA. It accepts no collection override and rejects out-of-parent-window dates.
  Analysis/chart use no HTTP. Results: baseline 42,585 total / 467.967033 per day;
  current 32,825 / 360.714286 per day; change **−22.918868%**, 91/91 observed in
  each period. Top-3 exclusion: −23.714123%; 7-day edge trim: −23.176503%. These are
  descriptive comparisons/sensitivity scenarios, not statistical inference.
- Follow-up report: `assets/reports/live-2026-09-27-ai-uk-4.2/followup-2024-03-01_2024-05-30-vs-2025-03-01_2025-05-30/workflow/action-001-report.md`
  (SHA256 `c07f94eea4f28bcdcf66b3849f6b5ad6019c088b7d9cf53a918ee8da221950db`);
  JSON SHA256 `d240ea9ee0d722636e58cce666d5523f10982da88a057d5d38e67aad5b7f5d28`.
  Report recalculated descriptive/diagnostic values, verified chart bytes/source,
  and recorded 0 report-time network requests. Its one model turn used 11,403 prompt
  + 411 completion = 11,814 tokens; no metadata/pageview HTTP; actual cost unknown.
- Derived-study implementation added `study --from-study`, v2 parent/raw/scope
  validation, and tamper/out-of-range regression tests. The latest full suite passes:
  485 tests, 0 failures/errors/skips. Step 4.3 is complete for the approved
  follow-up. The separate 2+ language acceptance remains unperformed.
- Offline 4.5 text review found that the generic report suggested selecting
  language priorities even when only one language was requested. The report now
  says cross-language prioritization is not applicable to a single-language study;
  multi-language and structured-criteria behavior is unchanged. Added a regression
  test. Main reviewed report:
  `assets/reports/live-2026-09-27-ai-uk-4.2/report-final-reviewed.md` (SHA256
  `5a945c737b375ca7eb2715585fd79b03af816db3d335a8aeb90681b3241825bb`), JSON SHA256
  `ef4df3070a0499e99874303061ba26cf299446b373fa44d953ba89daa2c3a2f1`. Follow-up
  reviewed report:
  `assets/reports/live-2026-09-27-ai-uk-4.2/followup-2024-03-01_2024-05-30-vs-2025-03-01_2025-05-30/report-final-reviewed.md`
  (SHA256 `58f790408429682f6edd707dd70d63e9eb53a37f29f76cd5175114b6334d3eff`),
  JSON SHA256 `2e5c47e5ad579ffd48b9bc720333a7b307ff1e0b5818f1637c34bbc0cd6e6266`.
  Previous report artifacts remain unchanged. Full suite: 485 tests, 0 failures,
  errors or skips. These are offline machine checks; final content acceptance is
  still yours to give.
- 4.4 was attempted on a separate 8-year synthetic fixture (not live Wikipedia
  observations): 8 model calls in total under the previously stated cap, all with
  reported usage (97,687 prompt + 10,871 completion = 108,558 tokens), 8 model HTTP
  successes, and Wikimedia metadata/pageview HTTP=0. The final two calls succeeded:
  one host-paged summary and one 50-row daily detail page; null/missing and explicit
  zero remained distinct. A deterministic report was then generated offline and an
  independent fixture oracle matched observed sums for `cs/pl/en`. The workflow
  stopped at its two-call cap before a model report turn or remaining detail kinds.
  The user declined increasing the cap; therefore 4.4 remains **partial/deferred**,
  not accepted. The offline report is at
  `assets/reports/live-2026-09-27-ai-uk-4.2/synthetic-8y-4.4/report-final.md`
  (SHA256 `b2a88995f9b5606b2426b1b78a12031df6846ac321c95c25d0bdb0f710c34212`);
  full audit is summarized in `REMAINING_PLAN.md`.
- The user accepted the 4.2/4.3 report texts. Per the user's explicit decision,
  4.4 was deferred and only the 5.1 preparatory contract was authorized; see
  `references/pdf-contract.md`. No renderer/dependency/font was installed and no
  PDF was generated. Step 5.2/rendering remains blocked until separate permission
  and the remaining acceptance-gate decision.

## Explicit approval record and pending collection decision

- User's question: recorded; Ukrainian only. Dates confirmed: 2024 vs 2025 full years.
- Exact provider/model: OpenRouter `inclusionai/ling-3.0-flash-fin:free`; exact-ID
  tool-call probe **passed once**. Underlying provider endpoint and future routing
  remain unknown; no paid/base-model fallback.
- Catalog reports context 262144, prompt/completion price zero, and `tools` /
  `tool_choice` support; the one live response verified the tool-call protocol.
- Context window/source and tokenizer/framing check: **catalog-based declaration only**.
  This is not a measured endpoint limit; the user's earlier 150000 is not
  independently confirmed.
- Per-run spend preference: observe actual usage without a planned dollar cap;
  implementation maximum 40 calls, output default 2048, timeout default 60 seconds.
- Model HTTP permission: selected and exercised by the bounded 4.1 probe and
  subsequent limited 4.2 workflow calls.
- Metadata HTTP permission: selected for 4.2 topic/article discovery
- Pageview HTTP permission: granted for the exact `uk` article/2024-vs-2025 periods
  after review, then used once successfully (HTTP 200).
- User explicitly approved launch/unblocking on 2026-09-27, with the previously
  selected Ukrainian-only scope, 40-call invocation maximum, and reported $0 account
  spend limit. This approval does not waive the safety/configuration gates below.
- Human article decision: explicit «так» recorded in `state-approved.json`, bound
  to the proposal state checksum; it authorizes only this title, edition and dates.
- Text acceptance: **accepted for 4.2 and 4.3** by the user reply «Приймаю обидва
  звіти» on 2026-09-27, covering `report-final-reviewed.md` and the 4.3
  `report-final-reviewed.md` linked above. This does not mark synthetic step 4.4
  complete or close the aggregate 4.5 gate. The article approval authorized the original data
  collection; the follow-up periods were separately confirmed in chat and used only
  the existing pin. Further criteria/scope changes, additional model runs, languages,
  articles, collections, or PDF work are not authorized by those decisions alone.

Security note: an OpenRouter credential was accidentally included in a user-supplied
snippet earlier. Its value is intentionally not reproduced or stored. The user
reported local setup complete; the probe was executed using the key loaded locally
from ignored `.env`, without reading or exposing its value. Never send replacement
keys in chat.
