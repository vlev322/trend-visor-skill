# Interpretation: what the numbers mean and don't mean

This file backs the short rules in `SKILL.md`. Read it when you need the exact
formulas, status meanings, or want to double-check a claim before writing it.

## `resolve` target status

| Status | Meaning |
| --- | --- |
| `matched` | An article links directly to the Wikidata item with no redirect/disambiguation issue. |
| `no_sitelink` | This wiki has no linked article. Not proof of no interest. Keep the language visible, and ask before dropping it; you can also `search --language <that language>` and, if a fitting item exists, `resolve` it for just that language and pass **both** saved resolution files to `study --resolution A --resolution B` (repeat the flag; do not run two separate studies). |
| `needs_review` | Redirect, disambiguation, or an identity mismatch. Inspect `issues`/`redirects` before using it. |
| `page_missing` / `unsupported_language` / `unavailable_project` | The title, language code, or wiki is not usable as queried. |
| `check_failed` / `not_checked` | A request failed or was skipped after an earlier failure; not evidence about the topic. |

## `study` per-language status

| Status | Meaning |
| --- | --- |
| `analyzed` | A snapshot was collected/reused and compared over the two periods. |
| `not_collected` | Resolution was not `matched`; see the original resolve status. |
| `collection_failed` | A fetch or cache read failed; the error code is preserved. |

Wikimedia's per-article API **omits days with zero views** instead of
returning `views: 0` (confirmed directly against the live API: a single-day
request for a zero-view day returns HTTP 404, and a multi-day request silently
skips that date). `study` and `report` therefore count every day the API did
not report as an observed 0 (`coverage.assumed_zero_days`), and a true API
`views: 0` row is kept separately as `coverage.explicit_zero_days`. The
exception: if a snapshot has **no explicit row anywhere** in its collected
window, that is treated as too suspicious to assume real zero traffic (e.g. a
mismatched or unpublished title), and the comparison stays `not_computed`
(`incomplete_coverage`) instead of being silently zero-filled.

`comparison.status: computed` requires a positive baseline mean; otherwise it
is `not_computed` with a `reason` (`zero_baseline`, or `incomplete_coverage`
only for that no-data-anywhere case above).

`change_percent = 100 × (current_mean − baseline_mean) / baseline_mean`, using
daily means over **every day in each period** (explicit rows plus assumed
zeros). It answers "how did this one article's average daily views change
between these two exact periods", nothing about unique visitors, purchase
intent, or a country's population — and nothing about the rest of the language
edition's traffic, which can rise or fall for unrelated reasons.

### `--months N` period alignment

Instead of four explicit dates, `study --months N` aligns to full calendar
months ending at the safety cutoff (`--as-of` minus `--lag-days` minus one
day). For `N<=12` the baseline is the same N months one year earlier; for
`N>12` the baseline is the N months immediately before the current period
(there usually isn't a full extra year of history to compare against). This
cannot be combined with the four explicit date flags or with `--from-study`.

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
| "N days in this window had no reported traffic (counted as 0)" | Treating `assumed_zero_days` as a data-quality problem to be excluded or apologized for — it is Wikimedia's normal reporting behavior |

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
