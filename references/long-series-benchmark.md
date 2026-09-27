# Offline long-series workflow benchmark

**Run:** 2026-09-26, Python 3.14.5, local `.venv`, deterministic synthetic fixtures.
**Reproduce:** `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m tests.test_long_series_workflow`.
The same setup is asserted by `tests.test_long_series_workflow.LongSeriesWorkflowTests`.
No Wikimedia/model HTTP is enabled. Model decisions are fixed callback fixtures,
not real model runs or human acceptance.

## Measurements

Each case has three synthetic language rows, daily values with explicit zeroes,
unknown days and recurring spikes, monthly summaries, calendar comparisons, and
this same sequence of eight callbacks: two summary pages, two observation pages,
missing dates, monthly detail, calendar detail and report. Independent Python sums
check the saved baseline/current observed totals. Detail rows are paginated; raw
arrays are never included in summary messages. Request sizes are the existing
UTF-8 byte-based context estimator—not tokenizer token counts.

| Years | Days/language | Daily rows processed | Model callbacks | Max request estimate (bytes) | Sum request estimates (bytes) | Max result artifact (bytes) | Snapshot artifacts total (bytes) | Workflow artifacts total (bytes) | Report JSON / Markdown (bytes) | Elapsed (s) | >10K soft signal | Wikimedia HTTP | Model usage |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | :---: |
| 1 | 365 | 1,095 | 8 | 53,316 | 410,891 | 7,288 | 286,912 | 535,923 | 21,091 / 7,241 | 0.782 | yes | 0 | unknown |
| 2 | 730 | 2,190 | 8 | 56,785 | 414,757 | 11,402 | 570,206 | 545,989 | 21,715 / 7,787 | 1.460 | yes | 0 | unknown |
| 5 | 1,826 | 5,478 | 8 | 56,778 | 414,882 | 11,393 | 1,421,310 | 546,281 | 21,727 / 7,793 | 3.974 | yes | 0 | unknown |
| 8 | 2,922 | 8,766 | 8 | 56,788 | 415,021 | 11,389 | 2,272,401 | 546,637 | 21,766 / 7,817 | 6.971 | yes | 0 | unknown |

The maximum visible request estimate changes by 3,472 bytes from 1 to 8 years,
while processed daily rows grow 8×; this measured run stays dominated by fixed
policy/references and one bounded result page rather than transmitting full raw
series. The per-request estimate exceeds the 10,000-token soft-efficiency signal
in every case; this is explicitly **not** a failure or gate. Its bytes do not
establish a token count or model-window fit.

All measured request/result/report/snapshot artifacts are below the existing
10 MiB individual artifact/response caps. Separate tests exercise the actual
10 MiB boundary: a response and JSON artifact at exactly 10,485,760 bytes are
accepted; one byte above is rejected before publishing/returning. This does not
measure behavior for an entire multi-file workflow whose aggregate files exceed
10 MiB; limits apply per artifact/response, not to total disk usage.

## Interpretation and limitations

- The callback run validates core calculations, state transitions, pagination,
  per-turn context measurements, report creation, file sizes and no-Wikimedia-HTTP
  behavior on pinned synthetic data.
- It does not establish performance of a real model, actual provider tokens,
  hidden framing, provider usage, cost, production hardware or user-facing latency.
- `model_usage: unknown` is intentional because callbacks supply no provider usage;
  it must not be displayed as zero.
- Three rows demonstrate multi-language mechanics; they do not represent three
  independent markets or audiences. Topics/articles remain separate confirmed scope.
- Measured wall time is one local run, not a service-level guarantee. Re-running
  the command can produce different elapsed times and byte sizes if instruction
  content or serialization changes; the test invariants, not the table's timings,
  are the stable contract.
