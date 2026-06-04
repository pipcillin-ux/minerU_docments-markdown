#!/usr/bin/env python3
"""Create document profiles and structure-drift diagnostics for MinerU outputs."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from structure_utils import (
    classify_page_regions,
    compact_heading_text,
    content_list_path,
    image_caption,
    item_text,
    load_tasks,
    markdown_file,
    markdown_headings,
    normalize_text,
    output_dirs,
    parse_page_range,
    repeated_margin_texts,
    table_caption,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Profile MinerU output directories.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    return parser.parse_args()


def chunk_page_count(out_dir: Path, task: dict[str, Any]) -> int:
    chunk_path = out_dir / str(task.get("chunk_path") or "")
    if not chunk_path.exists():
        return 0
    return len(PdfReader(str(chunk_path)).pages)


def heading_diagnostics(out_dir: Path) -> dict[str, Any]:
    headings = markdown_headings(out_dir)
    levels = Counter(level for level, _ in headings)
    toc_like_headings = [text for _, text in headings if re.search(r"\s\d{1,4}$", text.strip())]
    consecutive_top = 0
    max_consecutive_top = 0
    for level, _ in headings:
        if level == 1:
            consecutive_top += 1
        else:
            max_consecutive_top = max(max_consecutive_top, consecutive_top)
            consecutive_top = 0
    max_consecutive_top = max(max_consecutive_top, consecutive_top)
    first_level_ratio = (levels[1] / len(headings)) if headings else 0
    return {
        "heading_count": len(headings),
        "heading_levels": dict(sorted(levels.items())),
        "first_level_ratio": round(first_level_ratio, 4),
        "toc_like_heading_count": len(toc_like_headings),
        "toc_like_heading_samples": toc_like_headings[:20],
        "max_consecutive_h1": max_consecutive_top,
    }


def profile_output_dir(out_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    tasks = load_tasks(out_dir)
    item_counts: Counter[str] = Counter()
    page_counts: Counter[int] = Counter()
    text_level_counts: Counter[str] = Counter()
    table_pages: set[int] = set()
    image_pages: set[int] = set()
    image_table_candidates: list[dict[str, Any]] = []
    table_captions: list[str] = []
    image_captions: list[str] = []
    content_paths: list[str] = []

    for task in tasks:
        path = content_list_path(out_dir, task)
        if path:
            content_paths.append(str(path.relative_to(out_dir)))

    from structure_utils import iter_content_items

    for wrapped in iter_content_items(out_dir):
        item = wrapped["item"]
        item_type = str(item.get("type") or "unknown")
        absolute_page = int(wrapped["absolute_page"])
        item_counts[item_type] += 1
        if normalize_text(item_text(item)):
            page_counts[absolute_page] += 1
        if "text_level" in item:
            text_level_counts[str(item.get("text_level"))] += 1
        if item_type == "table":
            table_pages.add(absolute_page)
            caption = table_caption(item)
            if caption:
                table_captions.append(caption)
        if item_type == "image":
            image_pages.add(absolute_page)
            caption = image_caption(item)
            if caption:
                image_captions.append(caption)
            text = normalize_text(item_text(item) + " " + caption)
            if any(keyword in text for keyword in ("表", "项目", "剂量", "分型", "诊断", "评分")):
                image_table_candidates.append(
                    {
                        "page": absolute_page,
                        "caption": caption,
                        "text": text[:120],
                    }
                )

    total_pages = sum(chunk_page_count(out_dir, task) for task in tasks)
    page_ranges = [str(task.get("page_range")) for task in tasks]
    sorted_ranges = [parse_page_range(value) for value in page_ranges]
    range_issues: list[str] = []
    expected_start = 1
    for start, end in sorted_ranges:
        if start != expected_start:
            range_issues.append(f"expected page {expected_start}, found range {start}-{end}")
        expected_start = end + 1

    regions = classify_page_regions(out_dir)
    low_content_pages = [page for page in range(1, total_pages + 1) if page_counts.get(page, 0) <= 2]
    repeated_margins = sorted(repeated_margin_texts(out_dir))
    headings = heading_diagnostics(out_dir)
    md_path = markdown_file(out_dir)

    diagnostics: dict[str, Any] = {
        **headings,
        "range_issues": range_issues,
        "repeated_margin_text_count": len(repeated_margins),
        "repeated_margin_text_samples": repeated_margins[:20],
        "low_content_page_count": len(low_content_pages),
        "low_content_pages": low_content_pages[:100],
        "front_matter_pages": [page for page, region in sorted(regions.items()) if region == "front_matter"],
        "back_matter_pages": [page for page, region in sorted(regions.items()) if region == "back_matter"],
        "image_table_candidate_count": len(image_table_candidates),
        "image_table_candidates": image_table_candidates[:50],
        "table_caption_samples": table_captions[:30],
        "image_caption_samples": image_captions[:30],
    }

    issues: list[str] = []
    if range_issues:
        issues.extend(range_issues)
    if headings["heading_count"] and headings["first_level_ratio"] > 0.9 and headings["heading_count"] > 20:
        issues.append("most Markdown headings are H1; heading hierarchy may be flattened")
    if headings["toc_like_heading_count"] > 15:
        issues.append("many TOC-like lines are Markdown headings; TOC may pollute body structure")
    if headings["max_consecutive_h1"] > 20:
        issues.append("long consecutive H1 sequence detected")
    if len(repeated_margins) > 20:
        issues.append("many repeated margin texts detected")
    if len(image_table_candidates) > 0:
        issues.append("image/table candidates need human review")
    if item_counts.get("table", 0) == 0 and len(image_pages) > 20:
        issues.append("no structured table blocks but many images; tables may be image-only")

    risk = "OK"
    if issues:
        risk = "WARN"
    if range_issues or not md_path or any(task.get("status") != "done" for task in tasks):
        risk = "FAIL"

    profile: dict[str, Any] = {
        "document": out_dir.name,
        "risk": risk,
        "task_count": len(tasks),
        "done_task_count": sum(1 for task in tasks if task.get("status") == "done"),
        "page_count": total_pages,
        "page_ranges": page_ranges,
        "markdown_file": md_path.name if md_path else None,
        "markdown_bytes": md_path.stat().st_size if md_path else 0,
        "content_list_files": content_paths,
        "item_counts": dict(sorted(item_counts.items())),
        "text_level_counts": dict(sorted(text_level_counts.items())),
        "table_pages": sorted(table_pages),
        "image_pages": sorted(image_pages),
        "region_counts": dict(Counter(regions.values())),
    }
    diagnostics["risk"] = risk
    diagnostics["issues"] = issues
    return profile, diagnostics, issues


def write_quality_report(out_dir: Path, profile: dict[str, Any], diagnostics: dict[str, Any]) -> None:
    lines = [
        f"# {profile['document']} 质量报告",
        "",
        f"- 风险等级：{profile['risk']}",
        f"- 页数：{profile['page_count']}",
        f"- 分块任务：{profile['done_task_count']}/{profile['task_count']}",
        f"- Markdown：{profile['markdown_file']} ({profile['markdown_bytes']} bytes)",
        f"- 表格块：{profile['item_counts'].get('table', 0)}",
        f"- 图片块：{profile['item_counts'].get('image', 0)}",
        f"- 疑似图片型表格：{diagnostics['image_table_candidate_count']}",
        f"- 标题数量：{diagnostics['heading_count']}",
        f"- H1 占比：{diagnostics['first_level_ratio']}",
        "",
        "## 问题",
        "",
    ]
    issues = diagnostics.get("issues") or []
    if issues:
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("- 未发现结构偏移风险。")
    lines.extend(["", "## 目录/正文区域候选", ""])
    lines.append(f"- front_matter_pages: {diagnostics['front_matter_pages'][:50]}")
    lines.append(f"- back_matter_pages: {diagnostics['back_matter_pages'][:50]}")
    lines.extend(["", "## 重复页眉页脚候选", ""])
    samples = diagnostics.get("repeated_margin_text_samples") or []
    if samples:
        lines.extend(f"- {sample}" for sample in samples)
    else:
        lines.append("- 无")
    out_dir.joinpath("quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_dir)
    dirs = output_dirs(output_root)
    if not dirs:
        print(f"No output directories with tasks.json found under {output_root}")
        return 2

    rows: list[dict[str, Any]] = []
    total_issues = 0
    for out_dir in dirs:
        profile, diagnostics, issues = profile_output_dir(out_dir)
        out_dir.joinpath("document_profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        out_dir.joinpath("structure_diagnostics.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        write_quality_report(out_dir, profile, diagnostics)
        total_issues += len(issues)
        rows.append(
            {
                "document": profile["document"],
                "risk": profile["risk"],
                "pages": profile["page_count"],
                "tasks": profile["task_count"],
                "markdown_bytes": profile["markdown_bytes"],
                "tables": profile["item_counts"].get("table", 0),
                "images": profile["item_counts"].get("image", 0),
                "headings": diagnostics["heading_count"],
                "h1_ratio": diagnostics["first_level_ratio"],
                "issues": "; ".join(issues),
            }
        )
        print(
            f"[{profile['risk']}] {profile['document']} | pages {profile['page_count']} | "
            f"tables {profile['item_counts'].get('table', 0)} | "
            f"images {profile['item_counts'].get('image', 0)} | issues {len(issues)}"
        )

    summary_path = output_root / "document_profiles_summary.csv"
    with summary_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    report_lines = ["# PDF 结构质量总览", ""]
    for row in rows:
        report_lines.append(f"- [{row['risk']}] {row['document']}：{row['issues'] or '无明显问题'}")
    (output_root / "quality_report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"Profile complete: {len(rows)} documents, {total_issues} diagnostic issue(s).")
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
