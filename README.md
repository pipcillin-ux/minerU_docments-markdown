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

## Batch Parse All PDFs

Run:

```bash
.venv/bin/python mineru_batch_parse.py
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
.venv/bin/python mineru_batch_parse.py --dry-run
```

## Single PDF

Process one PDF:

```bash
.venv/bin/python mineru_batch_parse.py \
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
.venv/bin/python mineru_batch_parse.py --submit-only
```

Retry failed tasks:

```bash
.venv/bin/python mineru_batch_parse.py --resubmit-failed
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
.venv/bin/python mineru_batch_parse.py --chunk-size 100 --resubmit-failed
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
.venv/bin/python validate_outputs.py
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
.venv/bin/python profile_documents.py
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
.venv/bin/python build_structured_blocks.py
```

This writes:

```text
output/<document-name>/structured_blocks.jsonl
output/<document-name>/<document-name>.semantic.md
```

Recommended usage:

- `<document-name>.md`: original merged MinerU Markdown for traceability.
- `<document-name>.semantic.md`: rule-rebuilt structure for RAG and extraction.
- `structured_blocks.jsonl`: machine-readable blocks with page, chunk, type,
  heading level, section path, bbox, table, and image metadata.

## Notes

- `--url` can be used for a single public PDF URL, but local upload mode is the
  default and recommended for this project.
- Real parsing uploads PDF content to MinerU. Use `--dry-run` if you only want
  to inspect page ranges.

## Contributors

- piperacillin
- Codex
