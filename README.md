# MinerU PDF Batch Parser

[中文说明](README.zh-CN.md)

Use MinerU API to parse long PDF documents into Markdown. The scripts handle
MinerU's page, file-size, and rate limits by splitting PDFs into chunks,
uploading them in batches, polling results, and merging each document back into
one Markdown file.

## Setup

Create the project virtual environment and install dependencies:

```bash
cd /Users/piperacillin/code/python_code/pdf
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

This installs 8 CLI commands:

```text
mineru-batch-parse
mineru-validate-outputs
mineru-profile-documents
mineru-build-structured-blocks
mineru-heading-quality
mineru-build-regression-fixtures
mineru-section-reasoning
mineru-run-pipeline
```

Put your MinerU token in `.env`:

```text
mineru_api_token=YOUR_TOKEN
```

`MINERU_TOKEN` in the process environment takes precedence over `.env`.
Tokens are intentionally not accepted as CLI arguments so they cannot leak
through process listings.

## Input

Put PDFs under:

```text
docs/
```

By default, every `*.pdf` in `docs/` is processed.

## One-Command Pipeline

The formal command reads PDFs from `docs/` and runs parse, diagnostics,
semantic rebuild, WARN repair, and final validation in an isolated workspace.
Only validated results are published under `output/`:

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --domain-profile tcm \
  --chunk-size 60 \
  --resubmit-failed \
  --repair-warn-with deepseek \
  --fail-on warn
```

The pipeline runs:

```text
parse -> diagnose -> rebuild -> repair -> validate -> publish
```

The default workspace is `.output.pipeline-work` beside `output/`. On macOS it
is initialized with an APFS copy-on-write clone when `--skip-parse` is used, so
the published output remains unchanged without making a second physical copy
of the full corpus. Every subprocess writes to the workspace, including
diagnostic reports, review files, section reasoning sidecars, and quality
reports.

If a stage fails or the process is interrupted, `output/` is unchanged and the
workspace is retained. Run the same command again to resume. Add
`--fresh-work` to discard the retained workspace and start again. After all
quality gates pass, the workspace replaces `output/` with rollback protection.

`--fail-on warn` enforces a final `0 FAIL / 0 WARN` target. Use
`--fail-on fail` when WARN items should remain reviewable but non-blocking.
DeepSeek WARN repair requires `DEEPSEEK_API_KEY` or `deepseek_api_key` in
`.env` when WARN items need LLM review.

If `output/` already contains parsed documents, `--skip-parse` clones that
published snapshot into the workspace, then reruns diagnostics, semantic
rebuild, repair, validation, and publication:

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --domain-profile tcm \
  --skip-parse \
  --repair-warn-with deepseek \
  --fail-on warn
```

To reuse an existing review/override file during rebuild:

```bash
.venv/bin/mineru-run-pipeline \
  --skip-parse \
  --domain-profile tcm \
  --heading-review-overrides output/docs_warn_deepseek_review.json \
  --skip-review
```

To avoid DeepSeek/OpenAI-compatible review calls and only run local rules:

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --domain-profile tcm \
  --skip-parse \
  --repair-warn-with none \
  --fail-on fail
```

To adopt already-reviewed high-confidence section reasoning decisions into the
main outputs:

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --domain-profile tcm \
  --skip-parse \
  --heading-review-overrides output/docs_warn_deepseek_review.json \
  --skip-review \
  --section-reasoning adopt \
  --section-reasoning-min-confidence 0.86 \
  --fail-on warn
```

`adopt` first generates reasoned candidates, then promotes only decisions that
pass deterministic structural checks and a post-adoption heading-quality gate.
If the gate fails, the primary `section_tree.json`, `structured_blocks.jsonl`,
and `<document-name>.semantic.md` files are restored.
The existing heading-review overrides are applied during the rebuild so the
pipeline can pass the pre-adoption quality gate without another API call.
Whenever `--section-reasoning collect|review|apply|adopt` is enabled, the
pipeline also refreshes `output/section_reasoning_summary.csv` and
`output/section_reasoning_summary.md`.

DeepSeek review reads `DEEPSEEK_API_KEY` or `deepseek_api_key` from the
environment or `.env`. Review overrides are created in the workspace and become
visible at these paths only after publication:

```text
output/heading_warn_deepseek_review.json
output/heading_warn_deepseek_review.md
```

## Batch Parse All PDFs

Run:

```bash
.venv/bin/mineru-batch-parse
```

The script will:

1. Count PDF pages.
2. Split each PDF into chunks of up to 200 pages.
3. Keep each uploaded chunk under MinerU's 200 MB file limit.
4. Upload chunks through MinerU signed upload URLs.
5. Poll MinerU batch results.
6. Download result zip files.
7. Merge chunk Markdown files into one Markdown file per PDF.
8. Rewrite image links to local `assets/part_xxx/` paths.

## Dry Run

Preview page ranges without uploading anything:

```bash
.venv/bin/mineru-batch-parse --dry-run
```

## Single PDF

Process one PDF:

```bash
.venv/bin/mineru-batch-parse \
  --pdf docs/example.pdf \
  --out output/example
```

When `--pdf` is omitted, `--out` is treated as the output root directory.
When `--pdf` is provided, `--out` is the output directory for that one PDF.
PDF stems are preserved exactly as output directory/file names. Batch mode
stops before writing anything when two input names would collide
case-insensitively.

## Resume Or Retry

The script writes `tasks.json` in each output directory, so it can resume after
interruption.

Submit missing tasks only:

```bash
.venv/bin/mineru-batch-parse --submit-only
```

Retry failed tasks:

```bash
.venv/bin/mineru-batch-parse --resubmit-failed
```

## Rate Limits

MinerU limits used by the script:

- Submit/upload APIs: 50 files per minute.
- Result APIs: 1000 requests per minute.
- Daily upload limit: 5000 files per user.
- HTML daily upload limit: 100 files, not used here.

Defaults:

```bash
--submit-files-per-minute 50
--result-requests-per-minute 1000
--daily-upload-file-limit 5000
--chunk-size 200
--max-upload-mb 200
```

If a chunk exceeds 200 MB, rerun with a smaller page chunk size:

```bash
.venv/bin/mineru-batch-parse --chunk-size 100 --resubmit-failed
```

## Output

For each PDF, output is written to:

```text
output/<document-name>/
  <document-name>.md
  tasks.json
  chunks/
  parts/
  assets/
```

The output directory and Markdown file preserve the complete PDF stem. Batch
mode computes every destination before creating document outputs and aborts on
case-insensitive or Unicode-normalized collisions.

Example:

```text
docs/1_2_3.pdf
```

becomes:

```text
output/1_2_3/1_2_3.md
```

## Validate Outputs

Run:

```bash
.venv/bin/mineru-validate-outputs
```

The validator prints one line per output directory and checks:

- All tasks are `done`.
- PDF chunk page counts match `page_range`.
- The merged Markdown contains every chunk Markdown after image path rewrite.
- All image references point to existing files.

Example final line:

```text
Validation complete: 16 directories checked, 0 issue(s).
```

## Structure Diagnostics And Semantic Rebuild

MinerU Markdown is a candidate parse, not the final semantic structure. Complex
PDFs may flatten heading levels, mix TOC entries into the body, include repeated
headers/footers, degrade tables into plain text, or leave table-like images for
manual review.

Generate document profiles and structure diagnostics:

```bash
.venv/bin/mineru-profile-documents --domain-profile tcm
```

This writes:

```text
output/document_profiles_summary.csv
output/quality_report.md
output/<document-name>/document_profile.json
output/<document-name>/structure_diagnostics.json
output/<document-name>/quality_report.md
```

Build structured blocks and semantic Markdown:

```bash
.venv/bin/mineru-build-structured-blocks --domain-profile tcm
```

This writes:

```text
output/<document-name>/toc_tree.json
output/<document-name>/section_tree.json
output/<document-name>/heading_candidates.jsonl
output/<document-name>/heading_decisions.jsonl
output/<document-name>/heading_diagnostics.json
output/<document-name>/structured_blocks.jsonl
output/<document-name>/<document-name>.semantic.md
```

Recommended usage:

- `<document-name>.md`: original merged MinerU Markdown for traceability.
- `<document-name>.semantic.md`: rebuilt full-document structure. By default it
  preserves front matter, body, references, and appendices, and only drops
  repeated noise such as headers, footers, and page numbers.
- `structured_blocks.jsonl`: machine-readable blocks with page, chunk, type,
  heading level, legacy section path, section tree fields, bbox, table, image
  metadata, and heading decision audit fields. The `section_id`,
  `tree_section_path`, `tree_heading_level`, `tree_section_source`, and
  `tree_section_confidence` fields attach body blocks to the reconstructed
  tree. The `recommended_for_rag` field marks body blocks that are better
  suited for RAG.
- `toc_tree.json`: best-effort table-of-contents tree with parent paths and
  page hints.
- `section_tree.json`: sidecar body section tree. It prefers body headings for
  parent/child recovery and falls back to the TOC backbone when body headings
  are too sparse. It is used to attach each body block to a stable section path;
  semantic Markdown body headings are rendered against this tree.
- `heading_candidates.jsonl`: local heading candidates with layout and text
  signals.
- `heading_decisions.jsonl`: audited heading repair decisions. Actions include
  `keep_heading`, `promote_to_heading`, `demote_to_paragraph`, and
  `split_heading`.
- `heading_diagnostics.json`: semantic heading quality metrics.

### Current Structure Repairs

The semantic rebuild now handles the main structure-drift cases explicitly:

- TOC boundaries: items are classified as `front_matter`, `toc`, `body`, or
  `back_matter`. TOC regions are rendered as plain `**目录**` blocks instead of
  Markdown headings, and TOC blocks are not recommended for RAG body chunks.
- Heading levels: `toc_tree.json` and numbering patterns drive level inference.
  Same-pattern sibling headings are checked for consistency so one logical
  level does not drift between H1/H2/H3.
- Section tree: `section_tree.json` records the long-term body parent/child
  structure and `structured_blocks.jsonl` is backfilled with tree assignment
  fields. Semantic Markdown body heading levels are rendered from the tree:
  headings that match a section node use the node level, while local headings
  inside a section are constrained below their assigned tree parent.
- Broken headings: split chapter titles can be conservatively merged, while
  glued "heading + prose" or "heading + table/figure reference tail" text is
  split back into heading and body content.
- Non-heading demotion: index-like TOC rows, CIP/cataloging lines, references,
  numeric chart/OCR fragments, table/figure reference tails, review questions,
  and long numbered prose/list items are excluded from body heading-level
  decisions.
- Quality gates: `mineru-heading-quality` checks TOC leakage, TOC entries in the
  body outline, heading-level jumps, and same-pattern sibling inconsistencies.
  The current repository target is `0 FAIL / 0 WARN` across the full corpus.

### Domain Profiles

The parser defaults to `--domain-profile generic`. Generic mode uses only
layout, numbering, syntax, TOC, and section-tree signals. It does not enable
medical section names, clinical subsection terms, or Chinese image-table
keywords.

The current textbook corpus uses the built-in TCM profile:

```bash
.venv/bin/mineru-run-pipeline --domain-profile tcm ...
```

All structure commands accept the same profile value:

```text
generic
tcm
/absolute/or/relative/path/to/custom-profile.toml
```

Built-in profiles live under `src/mineru_documents_markdown/domains/`.
`domain_profiles.py` validates custom TOML files and exposes one immutable
profile object to profiling, TOC parsing, semantic rebuild, quality checks, and
section-reasoning adoption.

The semantic rebuild implementation is split by responsibility:

```text
semantic_rebuild.py       orchestration and block reconstruction
semantic_render.py        Markdown rendering
semantic_state.py         H1-H6 section-path state
semantic_diagnostics.py   rebuilt-heading diagnostics
section_reasoning/        collect/review/apply/adopt/summary/report CLI modules
```

To build body-only semantic Markdown:

```bash
.venv/bin/mineru-build-structured-blocks --domain-profile tcm --semantic-scope body
```

Heading strategy options:

```bash
# Local-only and reproducible. This is the default.
.venv/bin/mineru-build-structured-blocks --heading-strategy rule

# Send every heading candidate to an OpenAI-compatible LLM.
.venv/bin/mineru-build-structured-blocks --heading-strategy llm

# Use rules for high-confidence candidates and LLM for low-confidence ones.
.venv/bin/mineru-build-structured-blocks --heading-strategy hybrid
```

DeepSeek can be used as the optional LLM assist layer:

```bash
export DEEPSEEK_API_KEY="..."
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
.venv/bin/mineru-build-structured-blocks --heading-strategy hybrid
```

The LLM is only given heading candidates plus small local context windows. It is
not asked to rewrite the full document. If the API key is missing, times out, or
returns invalid JSON, the builder falls back to local rule decisions.

The optional section-reasoning LLM pass handles local tree conflicts, such as
repeated subsection titles, TOC/body disagreement, or missing explicit
headings. It uses small context windows, schema-validated actions, cached
outputs, and quality gates; it does not rewrite source text or replace the
deterministic pipeline.

Collect local section-reasoning candidates without calling an LLM:

```bash
.venv/bin/mineru-section-reasoning --domain-profile tcm --mode collect --limit 200
.venv/bin/mineru-section-reasoning --domain-profile tcm --mode report
.venv/bin/mineru-section-reasoning --domain-profile tcm --mode summary --min-confidence 0.86
```

`summary` is read-only. It aggregates the corpus-level review/adoption state so
large batches can be triaged before spending LLM calls:

```text
output/section_reasoning_summary.csv
output/section_reasoning_summary.md
```

The summary reports candidate counts, reviewed decisions, high-confidence
insert decisions, structurally adoption-ready decisions, orphan audit decisions,
main-output LLM reasoning nodes, documents still waiting for review, and
documents already adopted into the main outputs.

Review those candidates with DeepSeek/OpenAI-compatible JSON responses:

```bash
.venv/bin/mineru-section-reasoning --domain-profile tcm --mode review --limit 80 --review-jobs 4
```

Review mode is incremental by default: it skips candidates already present in
`section_reasoning_decisions.jsonl`, merges new decisions into the existing
decision file, and only re-reviews cached candidates when `--force` is passed.
`--review-jobs` parallelizes only the API review calls; candidate selection and
decision-file writes remain serial to avoid duplicate or racy updates.

Collect/review writes sidecar audit files only:

```text
output/<document-name>/section_reasoning_candidates.jsonl
output/<document-name>/section_reasoning_decisions.jsonl
output/<document-name>/section_reasoning_report.md
```

Apply high-confidence reviewed decisions to reasoned sidecar outputs:

```bash
.venv/bin/mineru-section-reasoning --domain-profile tcm --mode apply --min-confidence 0.86
```

Apply mode is deliberately conservative today: it only materializes
`insert_child_section` decisions that pass the confidence threshold, then
reattaches blocks from the original main outputs. Candidate collection skips
body headings that already anchor any section node, and apply rejects stale
decisions for those blocks with `source_already_section_node`.

Range updates are tree-local: the inserted node is bounded by peer headings and
the effective parent envelope, while only its parent/ancestor chain may be
extended. Existing validated ranges can widen a TOC-inferred boundary, but no
ancestor may be crossed. If no node is inserted, the original ranges are kept
unchanged. Apply does not overwrite `section_tree.json`,
`structured_blocks.jsonl`, or `<document-name>.semantic.md`.

```text
output/<document-name>/section_tree.reasoned.json
output/<document-name>/structured_blocks.reasoned.jsonl
output/<document-name>/<document-name>.semantic.reasoned.md
output/<document-name>/section_reasoning_apply_report.md
```

Adopt high-confidence decisions into the main outputs:

```bash
.venv/bin/mineru-section-reasoning \
  --domain-profile tcm \
  --mode adopt \
  --target main \
  --min-confidence 0.86
```

Adopt mode currently promotes only `insert_child_section` decisions from
`llm_section_reasoning`. It writes `section_reasoning_adoption_report.md`,
checks that source text is unchanged, prevents new section-range defects, and
rolls back if the adopted document produces FAIL/WARN heading-quality issues.
Rerunning adopt is idempotent for documents that have no new adoptable
decisions.

Run the focused range-regression tests with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

For large documents, keep requests small:

```bash
.venv/bin/mineru-build-structured-blocks \
  --heading-strategy hybrid \
  --llm-confidence-threshold 0.6 \
  --llm-batch-size 5
```

Run hardened heading quality checks:

```bash
.venv/bin/mineru-heading-quality --domain-profile tcm
```

Quality status meanings:

- `FAIL`: definite structural defects, such as stuck TOC headings, missing
  semantic headings, or broken decision JSON. Strict pipelines should stop.
- `WARN`: structural risks that need review, such as sentence-like headings,
  sibling level inconsistencies, or heading level jumps. These are good targets
  for DeepSeek/hybrid review or manual spot checks.
- `INFO`: non-blocking audit notes, such as TOC nodes not found in body
  headings.

This writes:

```text
output/heading_quality_summary.csv
output/<document-name>/heading_quality.json
output/<document-name>/heading_quality.md
```

Use the formal pipeline for a stricter workflow:

```bash
.venv/bin/mineru-run-pipeline \
  --skip-parse \
  --domain-profile tcm \
  --heading-strategy hybrid \
  --llm-confidence-threshold 0.6 \
  --llm-batch-size 5 \
  --fail-on fail
```

Build compact regression fixtures from the current corpus:

```bash
.venv/bin/mineru-build-regression-fixtures
```

This writes representative structure samples to:

```text
output/regression_fixtures/structure_regression_samples.json
output/regression_fixtures/structure_regression_samples.md
```

The fixtures cover TOC/body boundaries, TOC-backed and body-heading section
trees, block-to-section attachment, split/merged headings, numbered prose
demotion, and tree-driven Markdown heading rendering.

## Notes

- The project uses a standard `src/` layout. Core code lives under
  `src/mineru_documents_markdown/`.
- Top-level `mineru_batch_parse.py`, `validate_outputs.py`,
  `profile_documents.py`, and `build_structured_blocks.py` are compatibility
  wrappers, so the previous commands still work.
- `--url` can be used for a single public PDF URL, but local upload mode is the
  default and recommended for this project.
- Real parsing uploads PDF content to MinerU. Use `--dry-run` if you only want
  to inspect page ranges.

## Contributors

- piperacillin
- Codex
