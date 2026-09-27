# Explicit criteria for follow-up research

Use this contract when the user wants audiences screened by measurable, agreed
criteria. It evaluates the existing study's language rows without sorting,
weights, a market score, or a claim of statistical superiority.

## Workflow

1. Clarify what the user means by promising: absolute traffic, descriptive change,
   data coverage, or a particular sensitivity scenario. There are no default
   thresholds. A term such as “reliable growth” is not itself a numeric rule.
2. Show the user the metric, units, operator, threshold, scenario parameters and
   whether all conditions or any condition are required. Explain unavailable
   values and the distinction between descriptive evidence and product demand.
   Obtain agreement before using the rule for an actual recommendation.
3. Save the exact rule document below in a new JSON file using the reviewed
   study checksum and the report question. Python callers can use
   `save_json_artifact`; otherwise hash the exact saved bytes. Do not invent an
   approval flag or treat model-authored agreement as user approval.
4. Add `--criteria-rules "<rules.json>" "<rules-file-sha256>"` to the usual
   `report` command. For sensitivity rules, also provide the corresponding pinned
   `--analysis-result` attachments. No network, model call or new statistical fit
   is performed by rule evaluation.
5. Read each row's `criteria_evaluation` and its reasons, alongside the original
   analysis and limitations. Share the Markdown with its evidence-based
   explanations, not just the selected subset. The report still requires review.

Do not combine `--criterion` with `--criteria-rules`: unstructured conditions
must not silently disappear when structured screening is added. Keep using
`--criterion` when an intention cannot yet be represented; the report then stays
`criteria_require_review`, not automatically selected. Unsupported business,
causal or inferential conditions require clarification or further research,
not translation into a convenient but different proxy.

## Document contract (criteria_version 1)

All listed fields are required; extra fields are rejected. Maximum file size is
65536 bytes. Duplicate JSON keys and non-finite constants are rejected.

| Top-level field | Value |
| --- | --- |
| `criteria_version` | Integer `1`, not boolean |
| `study_sha256` | Exact checksum of the saved study used by `report` |
| `question` | Exact same question string as `report --question` |
| `match` | `all` or `any`, explicitly chosen |
| `rules` | Array of 1–5 rule objects |

| Rule field | Value |
| --- | --- |
| `id` | Unique lowercase identifier: `[a-z][a-z0-9_]{0,39}` |
| `description` | Nonempty single-line user-facing description, at most 200 characters |
| `metric` | One of the metrics below; no arbitrary expression or field path |
| `operator` | `gt` (>), `gte` (≥), `lt` (<), or `lte` (≤) |
| `threshold` | Explicit finite JSON number with absolute value at most 10^15; no string/boolean |
| `parameters` | Empty object for base metrics; exactly the required day-count field for a sensitivity metric |

Threshold literals that lose their decimal value during JSON float decoding are
rejected, not rounded. This includes underflow to zero and excessive precision
such as `1.00000000000000001`. Supply a representable, reviewed value instead of
silently changing the original threshold.

## Supported metrics

| Metric | Unit / exact meaning | Availability |
| --- | --- | --- |
| `baseline_mean_daily_views` | Views/day: baseline observed sum divided by days | Baseline must be fully observed |
| `current_mean_daily_views` | Views/day: current observed sum divided by days | Current must be fully observed |
| `change_percent` | Within-language percentage change of full-period daily means | Both periods complete and baseline mean positive |
| `baseline_coverage_percent` | 100 × observed days / expected baseline days | Available for an analyzed row, including zero observed days |
| `current_coverage_percent` | 100 × observed days / expected current days | Available for an analyzed row, including zero observed days |
| `change_without_top_days_percent` | Relative change after excluding the largest days separately in each period | Verified diagnostics required; `parameters` must contain only `top_days` |
| `change_after_trim_percent` | Relative change for fixed-edge-trimmed periods | Verified diagnostics required; `parameters` must contain only `trim_days` |

Scenario day counts are integers from 1 to 1000000 and must match the attached
analysis's corresponding parameter. A missing attachment, mismatch or unavailable
scenario yields `undetermined` with a reason. Other diagnostic parameters are not
used by these two metrics. A trimmed result can be available while the original
comparison remains unavailable: it is a different window, not a repair.

The coverage metrics concern recorded dates, not a confidence level or overall
measurement accuracy. For mean metrics, an observed-day mean from a partial
period is deliberately not substituted for a full-period mean. An observed zero
is known; no observations, unmatched articles and collection failures stay unknown.

## Decisions and evidence

- Per-rule outcome: `matches`, `does_not_match`, or `undetermined`.
- `all`: any definite failure means `does_not_match`; otherwise any unknown
  means `undetermined`; only all matches yield `matches`.
- `any`: any match means `matches`; otherwise any unknown means `undetermined`;
  only all failures yield `does_not_match`.
- Every individual check remains present even if another one determines the
  combined outcome. A match is a candidate for follow-up under this rule, not a
  launch recommendation. Failure is not evidence that an audience does not exist.

Checks use exact rational values derived from verified sums and day counts.
`value` is displayed to six decimals; `exact_value` retains numerator and
denominator. Threshold decisions do not use the rounded display. Each check cites
its `evidence_id`; sensitivity checks also carry `analysis_sha256` (null when no
diagnostic artifact was supplied). The Markdown states metric, operator, threshold,
value, result and any unavailable reason or scenario parameter.

`prioritization` becomes `evaluated`, with the rules, combination mode and counts
over **all** study rows. Counts are not a ranking and remain global on a paginated
evidence response. Per-row decisions remain in requested-language order. Without
rules, previous `needs_criteria` / `criteria_require_review` behavior is unchanged.

## Repeatability and limits

The rule checksum enters `report_id` and `audit.criteria_rules`. Changing the
rule bytes, study or question requires a new matching document/checksum and a new
report, preserving earlier results. Re-evaluation of the same saved data is
offline and does not change the data or previous report. A supplied checksum binds
bytes, not proof of who approved them or whether a business threshold is sensible.

Malformed, changed, unsupported or context-mismatched documents fail with
`criteria_rules_error` before any output is published. Invalid checksum syntax or
mixing structured and unstructured criteria returns `invalid_request`.
No expressions, weights, hidden scores, threshold optimization or inference about
product demand are implemented. More complex criteria need a separately agreed
extension; the agent must not claim this small contract covers every user intention.
