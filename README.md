# Wikipedia-pageviews-trends

An [Agent Skill](https://agentskills.io/specification) that compares interest
in a topic across Wikipedia language editions using Wikimedia's public daily
pageviews, and produces a chart plus a Markdown report an agent can share with
a B2C team deciding which topic or language audience to invest in next.

The behavioral contract for an agent using this skill lives in
[SKILL.md](SKILL.md) and [references/](references); this file is for a human
maintaining the code.

## Installation

- **Use this repo as-is**: `git clone` it and open the folder in VS Code —
  Copilot auto-discovers the skill via
  [.github/skills/trend-visor/](.github/skills/trend-visor). Then
  `cp .env.example .env` (fill in `TREND_VISOR_USER_AGENT`) and
  `uv sync --locked --extra charts`.
- **Add it to a different project**: run
  [scripts/package-skill.sh](scripts/package-skill.sh) `<other-project>/.github/skills/trend-visor`
  and commit the result there (this repo's own skill folder is symlinked, so
  it can't just be copied as-is).

### `uv: command not found`

`uv` isn't part of Python; install it once with
`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`) and
open a new shell. If you'd rather not install anything extra, skip `uv`
entirely: plain `python3 -m tools.pageviews <command>` works for everything
except `chart`/`--chart-dir`/`--pdf` (those need `pip install matplotlib==3.11.2`
in whatever Python you're using).

## Install and run tests

```
uv sync --locked --extra charts   # matplotlib for PNG charts; needs network once
.venv/bin/python -m unittest discover -s tests -v
```

Without the `charts` extra, `python3 -m unittest discover -s tests` still runs;
chart-dependent tests are skipped, not failed. See [SKILL.md](SKILL.md) for the
actual commands (`search`, `resolve`, `study`, `report`, ...).

## How this was built and checked

The implementation was written with AI assistance and simplified in a second
pass after a review found the first version overbuilt for the task (a
model-evaluation harness, discovery/research state machines, and SHA256
confirmation on every hand-off had grown larger than the actual data-analysis
code). That simplification is preserved as the `pre-simplify` git tag if the
earlier, more defensive version is ever needed for reference.

Verification for both passes combined:

- **Unit tests** (`tests/`) mock every Wikimedia HTTP call and check the
  validation, caching, calendar/diagnostic math, and report text against
  hand-computed expected values — this is what caught most AI-introduced bugs
  (e.g. treating a missing day as zero, or letting a cache key ignore a
  request field).
- **Manual spot-checks** against the public
  [pageviews.wmcloud.org](https://pageviews.wmcloud.org/) tool for a few
  real articles, to confirm the collected numbers matched what Wikimedia
  actually reports before trusting the descriptive-statistics code.
- **Real model runs** on a live topic ("interest in artificial intelligence,
  Ukrainian Wikipedia, 2024 vs 2025") using an actual OpenRouter model over the
  real `search`/`resolve`/`study` CLI, including a no-HTTP follow-up on a
  narrower date range. The accepted reports and charts from that run are kept
  in [evals/history/](evals/history/) as evidence, since the tooling that
  produced them (`tools/model_eval`) was removed in the simplification pass.
  Mistakes the model actually made along the way — and how they were fixed —
  are listed below rather than papered over:
  - The model occasionally added prose text alongside a tool call instead of
    only the tool call; the adapter now rejects that instead of guessing intent.
  - The model sometimes serialized a numeric argument (e.g. a page offset) as
    a quoted string; the schema and validation were tightened to reject that
    rather than silently coerce it.
  - A response was truncated mid-tool-call when the output token budget was
    too small for a larger page of evidence; the fix was a larger reserved
    budget and smaller result pages, not blindly retrying.
- **A real 2-language run** ("compare growth of intermittent fasting interest
  in Polish and Czech Wikipedia over the last two years", the task's own
  example) on the free OpenRouter model `inclusionai/ling-3.0-flash-fin:free`
  after the simplification, using the harness in
  [evals/run_live.py](evals/run_live.py) over the real, current CLI. The
  accepted report/chart are kept in
  [evals/history/fasting-pl-cs-live/](evals/history/fasting-pl-cs-live/). This
  is real data: `Q1666254` ("intermittent fasting") has no Polish Wikipedia
  sitelink, and the model correctly stopped and asked instead of silently
  substituting a different Polish article — exactly the behavior
  [SKILL.md](SKILL.md) requires. It then correctly reported the Czech
  article's descriptive numbers, including a `null` change because the
  current period had 3 missing days (`incomplete_coverage`), and the chart
  visibly shows the baseline mean was inflated by a few large single-day
  spikes rather than sustained growth. Real problems this run surfaced:
  - **A real code bug, fixed:** `report`'s default evidence page exceeded the
    24000-byte cap for a single analyzed language, because the calendar
    year-over-year comparison embedded every individual month-pair
    (`calendar["pairs"]`) instead of just the summary counts actually used by
    the report text. Fixed in `reports.py` to keep only `calendar["summary"]`
    in evidence, with a regression test
    (`test_a_two_year_study_still_fits_the_default_evidence_page`).
  - **A free-model limitation, documented rather than fixed:** this specific
    free reasoning model repeatedly exhausted its entire completion budget on
    hidden reasoning tokens before writing any visible answer
    (`finish_reason: "length"` with empty `content`), and separately got stuck
    calling `analyze`/`chart` with the wrong path format five times in a row
    without adapting. Capping the model's reasoning-token budget
    (`"reasoning": {"max_tokens": 1024}`) and keeping the adapter instructions
    ("keep reasoning brief") reduced but did not eliminate this; a stronger or
    non-free model should be markedly more reliable here. This is a model
    capability limit, not something the skill's CLI can paper over.

## Known limitations (by design, not oversight)

- Descriptive only: no forecast, no statistical significance test, and no
  slope/trend model. An earlier version had an optional HAC-corrected linear
  trend fit; it was removed because a plain year-over-year calendar
  comparison plus spike sensitivity answers "how much should I trust this
  change" more legibly for a small model and a non-technical reader.
- A topic can now map to one Wikidata item **per language** (`study` accepts
  a repeated `--resolution`, merged by language), for the common case where a
  topic has no single cross-language item (e.g. `no_sitelink`). There is
  still no automatic merging of multiple articles for the *same* language, or
  of redirects/renames history.
- One-page PDF export (`report --pdf`) supports at most 6 languages and a
  bounded amount of text; it refuses instead of dropping/overlapping content
  when a report doesn't fit (the chart shrinks first). Larger studies use the
  Markdown report instead. The PDF always draws its own vector overview chart
  from the pinned snapshots (monthly means; indexed to baseline = 100 when
  several languages are compared), so it no longer depends on `--chart-dir`.
- Wikimedia's per-article API omits zero-view days entirely rather than
  returning `views: 0` (verified directly against the live API for this
  project: a single-day request for a known zero-view day returns HTTP 404,
  and a multi-day request silently skips that date). `study`/`report` count
  every such day as an observed 0 (`assumed_zero_days`); an earlier version
  of this codebase treated it as unknown and blocked the whole comparison,
  which was wrong for the common case of a low-traffic article or language.

## Roadmap

Rough order, each a self-contained increment:

1. **Normalize by language-edition size.** Absolute pageviews aren't
   comparable across wikis of very different overall traffic; add an
   optional per-language baseline (e.g. the wiki's total daily views) so a
   "which language is relatively more interested" comparison is defensible.
2. **Redirects and page-move history.** A topic's pageviews can be split
   across an old and a new title; detect and optionally combine them instead
   of only following the current sitelink.
3. **Multi-page PDF** for studies with more than 6 languages, reusing the
   same overview chart and key-metrics table.
4. **Batch collection for many languages** (30–50+) with progress reporting
   and partial-failure handling, instead of one request per language inline.
5. **An explicit, opt-in trend model** (e.g. Theil–Sen on monthly means)
   reintroduced only if real usage shows the calendar/spike checks aren't
   enough, with the same "descriptive vs. inferential" separation the
   original HAC model had.
