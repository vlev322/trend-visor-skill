# Live model evidence (archived before simplification)

These reports were produced and human-accepted on 2026-09-27 using the
pre-simplify `tools/model_eval` live workflow (tag `pre-simplify`), before the
tooling that generated them was removed. They document that an earlier version
of the skill produced a correct, reviewed Ukrainian report and a valid PNG for
a real Wikipedia article, and that a no-HTTP follow-up on a narrower date range
reused the same pinned snapshot.

- `4.2-uk-ai/`: initial study of the "artificial intelligence" topic in
  Ukrainian Wikipedia, 2024 vs 2025 full years. Model: OpenRouter
  `inclusionai/ling-3.0-flash-fin:free`. 6 model calls total across discovery
  and research; 1 pageview HTTP request; observed change -22.28%
  (399→308 mean daily views), 731/731 days observed, 0 missing.
- `4.3-uk-ai-followup/`: same confirmed article, narrower baseline/current
  window (2024-03-01..05-30 vs 2025-03-01..05-30), created via
  `study --from-study` with 0 Wikimedia HTTP requests, reusing the exact
  pinned snapshot from 4.2. Observed change -22.92%.

These are historical artifacts, not a re-runnable test suite. The rebuilt
skill must be re-validated against fresh scenarios (see the plan's "реальний
прогін" phase), including a 2+ language case which these runs do not cover.
