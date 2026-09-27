# PDF export contract — step 5.1 (design only)

**Status:** contract prepared 2026-09-27. No PDF has been rendered, no package
installed, and no font downloaded or copied. The user explicitly chose to defer
the incomplete synthetic 4.4 acceptance and allow **only** this preparatory 5.1
contract. This is a scoped exception to the normal 4.5 → 5.1 dependency; it does
not authorize step 5.2 implementation/rendering or the 5.3 model run.

## Purpose and authority

PDF is a presentation of an already verified, user-accepted report. It is not a
new analysis, model interpretation, summary rewrite, or data source. The source
of truth remains the exact report produced from a pinned `research_state` and its
verified study/snapshot/attachments. Rendering must be entirely offline: no LLM,
Wikimedia, external image/font fetch, or modification of study, report, chart,
snapshot, or research-state files.

For implementation, the public seam should stay narrow: accept an exact report
JSON artifact reference (path + SHA256) and a new PDF output path. Before drawing,
verify the report JSON bytes and rebuild the deterministic report from its exact
saved research-state reference using `research_report`; require the regenerated
report ID and displayed content to match. The research/report verifier remains
authoritative for study, raw snapshot, diagnostics and chart checksums. Reject a
missing/mismatched source or unsupported report rather than repairing it.

The renderer is not a human-approval service. The host may invoke it only after
the exact report text has been accepted and the relevant plan gates are satisfied.
The user accepted the 4.2 and 4.3 reviewed Markdown reports on 2026-09-27, but
4.4 remains partial/deferred. The user has **not** authorized PDF rendering. A
future implementation/run needs a separate explicit go-ahead after those gates
are reconciled. No `--approved` boolean in a CLI command can substitute for that
host decision.

## V1 one-page layout

**Page:** A4 portrait, white/light neutral background, high-contrast ink-friendly
palette, fixed outer margins, selectable PDF text, embedded font. No information
is encoded only by color. Use a visual hierarchy rather than shrinking all content
to force a fit.

1. **Header and scope:** report title; the exact user question; baseline/current
   inclusive dates; `as_of` and safety lag; requested language edition(s).
2. **Result block:** for every report row, show exact article title and project,
   baseline/current mean daily views with units, descriptive percent change only
   when `computed`, and each period's observed/expected/missing coverage. Preserve
   `unknown`, `unavailable`, zero, and incomplete-coverage reasons as distinct
   statuses; never manufacture a percentage or turn missing into zero.
3. **Chart:** include the existing checksum-verified PNG for each displayed
   language when supplied, preserving aspect ratio and its reported date windows.
   Do not redraw, smooth, recrop data, or generate an image from model prose. If a
   chart was not supplied, state that plainly instead of substituting one.
4. **Sensitivity:** when verified diagnostics are attached, include the named
   scenario, its exact parameters and result. Label them exploratory descriptive
   checks, not a confidence interval, correction, or statistical inference. If
   absent, say “not supplied/not assessed.”
5. **Next check:** include each row's verified next-check recommendation/reason;
   do not infer causal explanation, audience priority, or product demand.
6. **Limitations and source:** retain the relevant report limitations: views are
   events rather than unique people; exact article/title scope; missing-data and
   bot-classification caveats; descriptive/nonseasonal/noncausal status; and no
   statistical-confidence claim. Show a clickable source URL, retrieval timestamp,
   and a readable raw checksum prefix. The complete SHA256 and exact report/study/
   chart provenance belong in the companion manifest and remain linked to the
   report JSON.

V1 eligibility is content-based, not “fit at any cost.” Target the accepted small
report: at most three requested-language rows, at most one chart per row, and
available space for all required text at **at least 8.5 pt body size** (source
footer may be 7.5 pt). All requested rows and all material limitations must fit.
If the report exceeds the supported layout, return a structured
`pdf_one_page_not_eligible` result before publishing anything. Do not omit
languages, limitations, unavailable outcomes, criteria decisions, or sources.
An extended/multi-page appendix or an executive-summary mode needs its own
explicit content contract and user approval; it is not silently selected.

## Rendering and file contract

- Reuse the report's exact existing chart PNG; do not alter its contents. Raster
  resolution and legibility are checked at page scale in 5.2.
- Create a **new** `.pdf` path; never overwrite an existing PDF, source artifact,
  or symlink. Publish atomically. Keep PDF outside snapshot directories.
- Create a companion JSON manifest binding PDF SHA256 to report ID/version and
  report JSON SHA256, research-state/study/resolution references, chart PNG
  hashes, renderer/package version, font file hashes/license identifier, page
  count/media box, and creation timestamp. Use portable relative artifact paths
  where the export root permits; never embed workspace-private absolute paths in
  visible PDF text or PDF metadata.
- Set PDF metadata to a neutral title/creator and the report ID; do not place
  secrets, full chat transcripts, machine-local paths, or arbitrary user data in
  document metadata.
- Rendering performs zero HTTP/LLM calls and never changes input artifacts.

## Renderer and font decision

- **Renderer:** ReportLab PDF Toolkit, proposed optional dependency
  `reportlab==5.0.1` under a future `pdf` extra; keep the base installation
  dependency-free. PyPI's 5.0.1 metadata labels the package BSD-licensed and lists
  a bundled `LICENSE`; the exact LICENSE file from the pinned distribution must
  be retained/checked when the extra is added. Official ReportLab docs describe
  the open-source toolkit as pure Python since 4.0, UTF-8/Unicode input, and
  embedded Unicode TrueType font support. This is appropriate for offline text,
  vector layout, and reuse of a PNG chart without a browser or system PDF binary.
- **Font:** static Noto Sans Regular + Bold TTFs from the official Noto Fonts
  source (not a machine-installed font). Google Fonts metadata identifies Noto Sans
  version 2.015, OFL, and Cyrillic/Cyrillic-Ext subsets; the static upstream archive
  lists `NotoSans-Regular.ttf` and `NotoSans-Bold.ttf`. The font software is under
  SIL Open Font License 1.1, which explicitly permits bundling and embedding.
  Bundle its license/copyright notice. At 5.2, pin the exact source commit, files,
  and SHA256s and run a glyph-coverage check over Ukrainian strings before use.
- **Dependency state:** this 5.1 decision does not modify `pyproject.toml` or
  `uv.lock`, install ReportLab, or fetch font files. At 5.2, add the optional extra
  and lock its full dependency closure. The existing `charts` extra stays separate;
  a report without chart attachments must still be renderable when the PDF extra
  alone is installed.

## Acceptance tests designed for 5.2

Verified report fields and textual extraction match; no missing/zero/status
conflation; chart provenance/checksum preserved; Ukrainian glyphs embedded and
extractable; links/long titles wrap; output is exactly one A4 page for the accepted
small report; minimum type size maintained; oversized input fails without output;
multi-language rows and limitations are never dropped; no overwrite or snapshot
mutation; corrupt report/chart/font/checksum fails closed; network and model
clients are patched to raise if called; manifest hashes bind exact inputs/output.
Separate visual review must inspect cropping, font legibility, chart scaling,
contrast, whitespace and line collisions. Text extraction and page-count tests do
not replace that visual check.

## Primary sources checked 2026-09-27

- [ReportLab on PyPI (machine-readable project/license metadata)](https://pypi.org/pypi/reportlab/json)
- [ReportLab open-source installation](https://docs.reportlab.com/install/open_source_installation/)
- [ReportLab user guide: fonts, Unicode and TrueType embedding](https://docs.reportlab.com/reportlab/userguide/ch3_fonts/)
- [Google Fonts Noto Sans metadata (version, license, Cyrillic subsets)](https://github.com/google/fonts/blob/main/ofl/notosans/METADATA.pb)
- [Noto Sans description (Latin/Cyrillic/Greek scripts)](https://github.com/google/fonts/blob/main/ofl/notosans/DESCRIPTION.en_us.html)
- [Noto Sans OFL 1.1 text](https://github.com/google/fonts/blob/main/ofl/notosans/OFL.txt)
- [Noto static TTF source directory](https://github.com/notofonts/noto-fonts/tree/main/hinted/ttf/NotoSans)
- [Noto Fonts repository license](https://github.com/notofonts/noto-fonts/blob/main/LICENSE)

**Not done in 5.1:** no renderer code, dependency/lock change, font download, PDF,
PNG modification, or external request beyond the cited documentation retrieval.
Step 5.2 and PDF output remain explicitly unauthorized.