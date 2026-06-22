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

This installs 6 CLI commands:

```text
mineru-batch-parse
mineru-validate-outputs
mineru-profile-documents
mineru-build-structured-blocks
mineru-heading-quality
mineru-run-pipeline
```

Put your MinerU token in `.env`:

```text
mineru_api_token=YOUR_TOKEN
```

The script also accepts `--token` or `MINERU_TOKEN`.

## Input

Put PDFs under:

```text
docs/
```

By default, every `*.pdf` in `docs/` is processed.

## One-Command Pipeline

The formal command reads PDFs from `docs/`, writes outputs under `output/`, and
runs parse, diagnostics, semantic rebuild, WARN repair, and final validation:

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --chunk-size 60 \
  --resubmit-failed \
  --repair-warn-with deepseek \
  --fail-on warn
```

The pipeline runs:

```text
parse -> validate -> profile -> semantic rebuild -> heading quality
      -> DeepSeek WARN review -> targeted rebuild -> final quality/validate
```

`--fail-on warn` enforces a final `0 FAIL / 0 WARN` target. Use
`--fail-on fail` when WARN items should remain reviewable but non-blocking.
DeepSeek WARN repair requires `DEEPSEEK_API_KEY` or `deepseek_api_key` in
`.env` when WARN items need LLM review.

If `output/` already contains parsed documents, rerun only diagnostics,
semantic rebuild, repair, and validation:

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --skip-parse \
  --repair-warn-with deepseek \
  --fail-on warn
```

To reuse an existing review/override file during rebuild:

```bash
.venv/bin/mineru-run-pipeline \
  --skip-parse \
  --heading-review-overrides output/docs_warn_deepseek_review.json \
  --skip-review
```

To avoid DeepSeek/OpenAI-compatible review calls and only run local rules:

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --skip-parse \
  --repair-warn-with none \
  --fail-on fail
```

DeepSeek review reads `DEEPSEEK_API_KEY` or `deepseek_api_key` from the
environment or `.env`. It writes review overrides to:

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

The Markdown file name is cleaned from the PDF name:

- Leading numeric IDs are removed.
- Trailing numeric IDs are removed.
- Duplicate suffixes like `(1)` are removed.
- Repeated spaces are collapsed.

Example:

```text
docs/骨伤科专病中医临床诊治_13773573.pdf
```

becomes:

```text
output/骨伤科专病中医临床诊治/骨伤科专病中医临床诊治.md
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
.venv/bin/mineru-profile-documents
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
.venv/bin/mineru-build-structured-blocks
```

This writes:

```text
output/<document-name>/toc_tree.json
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
  heading level, section path, bbox, table, image metadata, and heading decision
  audit fields. The `recommended_for_rag` field marks body blocks that are
  better suited for RAG.
- `toc_tree.json`: best-effort table-of-contents tree with parent paths and
  page hints.
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
- Broken headings: split chapter titles can be conservatively merged, while
  glued "heading + prose" or "heading + table/figure reference tail" text is
  split back into heading and body content.
- Non-heading demotion: index-like TOC rows, CIP/cataloging lines, references,
  numeric chart/OCR fragments, table/figure reference tails, and review
  questions are excluded from body heading-level decisions.
- Quality gates: `mineru-heading-quality` checks TOC leakage, TOC entries in the
  body outline, heading-level jumps, and same-pattern sibling inconsistencies.
  The current repository target is `0 FAIL / 0 WARN` across the full corpus.

To build body-only semantic Markdown:

```bash
.venv/bin/mineru-build-structured-blocks --semantic-scope body
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

For large documents, keep requests small:

```bash
.venv/bin/mineru-build-structured-blocks \
  --heading-strategy hybrid \
  --llm-confidence-threshold 0.6 \
  --llm-batch-size 5
```

Run hardened heading quality checks:

```bash
.venv/bin/mineru-heading-quality
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
  --heading-strategy hybrid \
  --llm-confidence-threshold 0.6 \
  --llm-batch-size 5 \
  --fail-on fail
```

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
