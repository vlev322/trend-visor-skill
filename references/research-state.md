# Saved research context and criteria approval

Use this workflow for follow-up questions after a saved, confirmed `study`,
when the agent must retain the exact question, evidence and criteria proposal
without copying the entire chat or daily data into every model request.
It is provider-neutral and offline. It does not perform topic discovery,
collect new data, interpret arbitrary user intent, or call a model.
For the earlier question/search/article-approval phase, use the persistent
discovery workflow linked from `SKILL.md`; its `collect` action returns a
standard research-state reference ready for this workflow.

## Host responsibility

The host asks the user and determines whether the actual reply confirms the
shown proposal. Keep `research approve` out of the model's autonomous tool
allowlist. Only the host invokes it after an explicit human confirmation.
The reply is recorded verbatim, not parsed as a yes/no command by this library.
A negative reply must never be routed to `approve` merely because it is text.
Do not record secrets or an entire chat as `--user-reply`.

Checksums and the saved approval record bind bytes and context, not a person's
identity. They do not protect against a party that can deliberately rewrite all
files/checksums, invoke host commands or bypass this workflow using the existing
direct `report --criteria-rules` interface. That direct interface is retained
for callers that independently obtain and enforce approval. State-based reports
must not silently fall back to it when a pending proposal blocks reporting.

## Start from existing evidence

Run `python3 -m tools.pageviews research start --study "<study.json>" "<study-sha256>" --question "<question>" --output "<new-state.json>"`.

- The question is single-line text up to 1000 characters. The command validates
  the saved study, resolution, exact snapshots and any supplied attachments.
- Optionally record up to five unresolved `--criterion` strings (200 characters
  each). These are intentions awaiting clarification, not hidden numeric rules.
- Explicit `--analysis-result FILE SHA256` and `--chart-result FILE SHA256` pairs
  preserve the same attachments supported by `report`; their bytes and scope are
  verified. PNG verification requires the charts extra; other operations use the
  standard library and no model credentials.
- Output is a new immutable state JSON. The CLI returns its path/checksum,
  `research_id`, revision, status, question, study scope, attachment counts and
  `next_action`. Retain the state reference outside the model's chat context.
- Initial status is `descriptive`. With textual criteria, `next_action` is
  `clarify_criteria`; otherwise it is `report_or_revise`.

Use a new path under a local output directory such as `assets/reports/`.
There is no automatic “latest” state or shared mutable session file.

## Propose rules without evaluating candidates

After discussing measurable criteria, prepare a rule document using the
criteria contract linked from `SKILL.md`. Its study SHA and question must match
the saved context exactly.

Run `python3 -m tools.pageviews research propose --state "<state.json>" "<state-sha256>" --rules "<rules.json>" "<rules-sha256>" --output "<new-proposal.json>"`.

This creates `awaiting_confirmation`, with `next_action` equal to
`ask_user_to_confirm_exact_state`. The summary exposes the full proposed rule
objects and study scope for review, but does not evaluate which languages match.
Show the user each metric, unit, threshold, operator, scenario settings and
all/any combination. Use the **proposal state checksum**, not the rules-file
checksum, when asking the host to bind the confirmation.

Unresolved textual criteria block a proposal rather than disappearing. First
use `revise` to explicitly replace or remove them after clarification. Replacing
rules on an already approved state also creates a new pending proposal; the old
approval never carries over to the replacement.

## Confirm separately, then report

After human review, the host runs
`python3 -m tools.pageviews research approve --state "<proposal.json>" "<proposal-sha256>" --confirm-sha256 "<same-proposal-sha256>" --user-reply "<actual non-secret confirmation>" --output "<new-approved-state.json>"`.

The reply must be nonempty single-line UTF-8 text of at most 2000 characters.
Approval requires a pending state and the exact confirmation checksum. The new
state records that checksum and reply. On later reads, it rechecks the exact
pending parent, matching research ID, revision, inputs and rules, as well as all
current evidence files. Changing only the approved state's inputs and rehashing
it does not preserve a valid link to the reviewed proposal.

Run `python3 -m tools.pageviews report --research-state "<approved-state.json>" "<state-sha256>" --output "<new-report.json>" --markdown "<new-report.md>"`.

- Pending states return `confirmation_required` before output publication.
- Descriptive states can produce reports without structured rule evaluation.
- Approved states reuse the exact pinned rules and evidence. The result includes
  `research_state` provenance in full JSON and compact stdout.
- Input overrides such as `--question`, `--study`, `--criterion`,
  `--criteria-rules` or attachment flags cannot accompany `--research-state`.
  Output paths and evidence `--offset` / `--limit` remain selectable.
- Omit output flags when inspecting another evidence page. No state or input
  file is modified, and no download occurs. Report outputs are separate artifacts,
  not automatically appended to the saved state's history.

## Inspect, revise, or reject a proposal

Run `python3 -m tools.pageviews research show --state "<state.json>" "<state-sha256>"` to reload and validate the current scope/proposal without writing anything.

Run `python3 -m tools.pageviews research revise --state "<state.json>" "<state-sha256>" --question "<updated-question>" --output "<new-revision.json>"` for a related question or to discard a proposal.

Every revision clears rules and approval, even when the question text stays the
same. Only the study reference is retained by default. Textual criteria and
analysis/chart attachment lists are **replaced**, not merged: explicitly pass
all that remain relevant. This conservative rule prevents stale attachments or
conditions from being silently reused after a scope change.

An optional `--study FILE SHA256` selects a different saved study, including
new periods, languages or data. Obtain the usual article/scope confirmation
before producing that study; changing state is not permission to collect data.
A future rules proposal must match the new question and study. There is no
automatic rebinding, refetch, latest-cache substitution or approval inheritance.

## Storage and failure contract

State version 1 contains only `state_version`, `research_id`, `revision`,
`status`, `inputs`, `rules`, `approval` and `parent`. Inputs contain the study
reference, question, textual criteria and explicit analysis/chart references.
References are absolute paths plus lowercase SHA256; states are at most 65536
bytes. Full daily data, results, chat transcripts and nested history are not
stored inside each revision. Parent is a single locator, so repeated follow-ups
do not grow the current state linearly with chat length.

Preserve old states for audit. Normal reads do not recursively replay every
ancestor. Approved states require their exact pending parent; other parent
references are lineage metadata, not proof that the entire history was audited.
Current evidence is always revalidated. If it is corrupted or unavailable,
restore the exact artifacts or explicitly create a new context after review;
the command does not repair or silently replace them.

Malformed/changed state JSON yields `research_state_error`. Invalid new inputs
use `invalid_request`; invalid transitions or checksums supplied as confirmation
use `confirmation_required`. Evidence errors retain their existing source,
attachment, criteria or missing-dependency codes. Existing outputs and snapshot
paths remain protected by the shared artifact publisher. Publication and CLI
summary reading are separate: a concurrent filesystem change after a successful
save may leave a valid saved state even if the final summary cannot be read.

This is a tested post-study continuation interface, not a complete agent run.
Pre-study persistence is implemented separately by `discovery`. Host model
integration, token-budget measurements and
live evaluation on independent topics remain separate acceptance steps.
