# Model request context preflight

This reference describes the offline estimator and the host's live-run gate. It
measures the actual visible workflow payload on every turn; it does not claim an
exact tokenizer count or independently discover provider limits.

## What is measured

`tools/model_eval/context_budget.py::estimate_request_context` serializes, as
compact UTF-8 JSON, the complete visible chat request fields sent by the current
OpenAI-compatible client:

- `model` identifier when known (or the maximum 200 printable ASCII bytes allowed
  by `ModelConfig` as a conservative dry-run reserve, without reading local `.env`);
- every system/user message, including loaded skill/references, current state,
  latest result, and compact completed-action descriptors;
- the current function/tool schema and `tool_choice`;
- request temperature and `max_tokens` field.

The UTF-8 byte count is recorded as `input_token_upper_estimate`, not divided by
four and not presented as measured tokens. It is conservative for tokenizers that
use byte fallback because each token consumes one or more bytes. It is **not a
universal mathematical guarantee for an arbitrary provider tokenizer**. Provider
hidden system content or framing may not be visible. The result records these
assumptions on every preflight. This repository currently has no compatible
model-specific tokenizer package or configured tokenizer mapping.

The output reserve is the CLI's `--max-tokens`. A declared context window also
reserves extra slack: by default `max(256 tokens, ceil(5% of the window))`; the
host can override it with `--context-margin-tokens`, but never below 256 tokens.
The allowance covers likely
framing/tokenizer differences but cannot prove their size. The calculation is:

`visible UTF-8 byte upper estimate + output reserve + margin <= host-declared context window`.

The 10,000-token optimization target is reported as a boolean signal only. It is
never used as a rejection threshold. A measured visible request over 10,000 can
still fit and proceed when the declared window estimate allows it.

## Dry run and live gate

`--dry-run` remains read-only, imports no SDK, and reads no `.env`. Its result now
includes `context_preflight`:

- without a declared window and source, status is `capacity_unverified`;
- with both, status is `fits_declared_capacity_estimate` or
  `exceeds_declared_capacity_estimate`;
- if no explicit `--model` was supplied, dry-run does not read local config and
  reserves the maximum valid 200-byte model ID; the configured ID itself remains unknown.

A live `--run-model` requires both:

```text
--context-window-tokens N --context-source "host-checked model/provider reference"
```

The source should identify the provider/model documentation and the host's check
of both context capacity and tokenizer assumptions. The program records this text;
it does not fetch or independently authenticate it. A live request whose estimated
input, output reserve and margin exceed the declared capacity fails before calling
the model callback. Each successful preflight is saved in that call's workflow audit.
Pending states still stop before model calls and need no context declaration.

## Audit interpretation and limitations

Each `workflow.json` event's `context_preflight` contains method/version, input
bytes and token estimate, output reserve, margin, declared capacity/source,
estimated total and whether the soft guideline was exceeded. The runner's existing
`request_utf8_bytes` separately measures its audit envelope; it is not a token
count or exact HTTP wire size. `workflow.json` top-level policy records method,
capacity declaration and margin policy. Provider usage is recorded separately and
does not retroactively prove tokenizer accuracy; calibration against usage is part
of this live-metrics step.

`fits_declared_capacity_estimate` means only that this documented conservative
proxy plus caller-chosen reserves fits the declared number. It does not guarantee
provider acceptance, account for hidden prompt text, or replace a live usage check.
If tokenizer behavior, model ID, context window, or framing reserve is unknown,
keep the preflight explicitly unverified and do not make a capacity guarantee.
