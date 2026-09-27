# Interpretation: what the numbers mean and don't mean

This file backs the short rules in `SKILL.md`. Read it when you need the exact
formulas, status meanings, or want to double-check a claim before writing it.

## `resolve` target status

| Status | Meaning |
| --- | --- |
| `matched` | An article links directly to the Wikidata item with no redirect/disambiguation issue. |
| `no_sitelink` | This wiki has no linked article. Not proof of no interest — keep the language visible, ask before dropping it. |
| `needs_review` | Redirect, disambiguation, or an identity mismatch. Inspect `issues`/`redirects` before using it. |
| `page_missing` / `unsupported_language` / `unavailable_project` | The title, language code, or wiki is not usable as queried. |
| `check_failed` / `not_checked` | A request failed or was skipped after an earlier failure; not evidence about the topic. |

## `study` per-language status

| Status | Meaning |
| --- | --- |
| `analyzed` | A snapshot was collected/reused and compared over the two periods. |
| `not_collected` | Resolution was not `matched`; see the original resolve status. |
| `collection_failed` | A fetch or cache read failed; the error code is preserved. |

`comparison.status: computed` requires full coverage in both periods and a
positive baseline mean; otherwise it is `not_computed` with a `reason`
(`incomplete_coverage` or `zero_baseline`). A missing day is `views: null`,
never `0`. An observed `0` is a real recorded zero.

`change_percent = 100 × (current_mean − baseline_mean) / baseline_mean`, using
daily means over **observed** days in each period. It answers "how did this one
article's average daily views change between these two exact periods", nothing
about unique visitors, purchase intent, or a country's population.

## What `report` computes for every analyzed language

- **Descriptive comparison** — the numbers above, always shown even when null.
- **Calendar consistency** (`calendar_analysis.compare_calendar_months`) —
  pairs each current month with the same calendar month one year earlier
  inside the baseline, and reports how many pairs came out higher/lower/equal
  and how many were excluded (partial month, missing data, no partner month).
  This checks whether a change holds up month-by-month; it does not adjust for
  weekday mix or moving holidays, and it is not a statistical test.
- **Sensitivity diagnostics** (`diagnostics.run_diagnostics`, fixed at
  `top_days=3`, `trim_days=7`) — the change recomputed after excluding each
  period's 3 largest days, and again after trimming 7 days from each window
  edge. If the recomputed change disagrees sharply with the original, a few
  spikes are likely driving the result; say so plainly. These are exploratory
  scenarios, not corrections, confidence intervals, or proof of a bot/event.
- **Chart** (optional, needs the `charts` extra and `--chart-dir`) — one PNG
  per analyzed language with daily markers, missing-day gaps shown as gaps
  (never smoothed to zero), and the two periods on a shared linear scale.

## Supported vs. unsupported conclusions

| Can say | Cannot say |
| --- | --- |
| "Views for article X in language Y changed by N% between these two periods" | "Interest in the topic changed by N%" (an article's scope may not match the topic; the page itself may have been edited or renamed) |
| "This change is (or isn't) consistent across matched calendar months" | "This change is statistically significant" — no test is run |
| "Excluding the 3 biggest days changes the result to M%" | "The spike was caused by X" — the data does not say why a day was large |
| "Language Y has more/less relative growth than language Z" | "Language Y is a better market" — views ≠ audience size, purchase intent, or population; different wikis are not normalized against each other |
| "No article was found for language Y" | Substituting a broader/related article for Y without the user's explicit approval |

A follow-up rules file, ranking formula, or "confidence score" is out of scope:
apply the user's own stated `--criterion` text using your own judgment against
the numbers above, and say plainly when a request needs a metric this skill
does not compute.

## Follow-ups without repeating work

- Same resolution, new or additional periods that fall **inside** an
  already-collected outer window → `study --from-study <existing-study.json>`
  with the new dates. Zero Wikimedia requests; it re-reads the pinned snapshot.
- Same resolution, a period outside the previously collected window, or you
  want a fresh pull → run `study --resolution ...` again with the exact same
  request fields (project, dates, `as_of`, `lag-days`); an exact repeat reuses
  the cached snapshot automatically. `--refresh` forces a new download while
  keeping the old snapshot.
- New topic or new language → `search`/`resolve` again; never guess an entity
  ID or reuse a resolution for a different topic.
