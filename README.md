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

Verification for passes combined:

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
