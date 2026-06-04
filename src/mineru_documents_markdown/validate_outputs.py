#!/usr/bin/env python3
"""Validate MinerU batch parsing outputs.

This checks the local pipeline invariants: PDF chunks match task page ranges,
merged Markdown contains every rewritten part Markdown, and image references
resolve to local assets.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from pypdf import PdfReader

from .mineru_batch_parse import rewrite_markdown_links
from .structure_utils import markdown_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate merged MinerU Markdown outputs.")
    parser.add_argument("--output-dir", default="output", help="Root output directory to validate.")
    return parser.parse_args()


def expected_pages(page_range: str) -> int:
    start, end = [int(value) for value in page_range.split("-", 1)]
    return end - start + 1


def validate_output_dir(out_dir: Path) -> tuple[dict[str, int | str], list[str]]:
    issues: list[str] = []
    task_file = out_dir / "tasks.json"
    tasks = json.loads(task_file.read_text(encoding="utf-8"))
    merged_md = markdown_file(out_dir)

    if merged_md is None:
        issues.append("missing merged markdown")
        md_text = ""
        md_size = 0
    else:
        md_text = merged_md.read_text(encoding="utf-8", errors="replace")
        md_size = merged_md.stat().st_size

    markers = re.findall(r"<!-- MinerU batch: pages ([^>]+) -->", md_text)
    if len(markers) != len(tasks):
        issues.append(f"batch marker count {len(markers)} != task count {len(tasks)}")

    done_tasks = 0
    total_chunk_pages = 0
    for task in tasks:
        page_range = str(task.get("page_range", ""))
        if task.get("status") == "done":
            done_tasks += 1
        else:
            issues.append(f"{page_range}: status is {task.get('status')}")

        chunk_path = out_dir / str(task.get("chunk_path") or "")
        if not chunk_path.exists():
            issues.append(f"{page_range}: missing chunk PDF")
        else:
            chunk_pages = len(PdfReader(str(chunk_path)).pages)
            total_chunk_pages += chunk_pages
            expected = expected_pages(page_range)
            if chunk_pages != expected:
                issues.append(f"{page_range}: chunk pages {chunk_pages} != expected {expected}")

        part_md = out_dir / str(task.get("md_path") or "")
        if not part_md.exists():
            issues.append(f"{page_range}: missing part markdown")
        else:
            rewritten = rewrite_markdown_links(
                part_md.read_text(encoding="utf-8", errors="replace"),
                part_md.parent,
                Path("assets") / f"part_{int(task['index']):03d}",
            ).strip()
            if rewritten and rewritten not in md_text:
                issues.append(f"{page_range}: rewritten part markdown not found in merged markdown")

    image_links = re.findall(r"!\[[^\]]*]\(([^)#?]+)", md_text)
    missing_images = [link for link in image_links if not (out_dir / link).exists()]
    if missing_images:
        issues.append(f"missing image refs: {len(missing_images)}; first={missing_images[0]}")

    summary: dict[str, int | str] = {
        "name": out_dir.name,
        "tasks": len(tasks),
        "done": done_tasks,
        "chunk_pages": total_chunk_pages,
        "markdown_bytes": md_size,
        "image_refs": len(image_links),
        "issues": len(issues),
    }
    return summary, issues


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_dir)
    task_files = sorted(output_root.glob("*/tasks.json"))
    if not task_files:
        print(f"No tasks.json files found under {output_root}")
        return 2

    total_issues = 0
    print(f"Validating {len(task_files)} output directories under {output_root}")
    for task_file in task_files:
        out_dir = task_file.parent
        summary, issues = validate_output_dir(out_dir)
        total_issues += len(issues)
        print(
            f"[{'OK' if not issues else 'FAIL'}] {summary['name']} | "
            f"tasks {summary['done']}/{summary['tasks']} | "
            f"chunk_pages {summary['chunk_pages']} | "
            f"md {summary['markdown_bytes']} bytes | "
            f"images {summary['image_refs']}"
        )
        for issue in issues:
            print(f"  - {issue}")

    print(f"Validation complete: {len(task_files)} directories checked, {total_issues} issue(s).")
    return 1 if total_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
