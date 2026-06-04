#!/usr/bin/env python3
"""Build semantic Markdown and structured JSONL blocks from MinerU outputs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from structure_utils import (
    classify_page_regions,
    image_caption,
    is_probable_body_section,
    is_probable_major_heading,
    item_text,
    load_tasks,
    markdown_file,
    normalize_text,
    output_dirs,
    repeated_margin_texts,
    table_caption,
    iter_content_items,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild semantic blocks from MinerU content lists.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument("--document", help="Only process one output directory by name.")
    return parser.parse_args()


def semantic_markdown_name(out_dir: Path) -> str:
    source_md = markdown_file(out_dir)
    if source_md:
        return source_md.with_suffix(".semantic.md").name
    return f"{out_dir.name}.semantic.md"


def image_markdown_path(task_index: int, raw_path: str) -> str:
    return (Path("assets") / f"part_{task_index:03d}" / raw_path).as_posix()


def normalize_heading_text(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\s+", "", text) if len(text) <= 20 else text
    return text


def classify_block(item: dict[str, Any], region: str, repeated_margins: set[str]) -> tuple[str, int | None, bool]:
    item_type = str(item.get("type") or "unknown")
    text = normalize_text(item_text(item))
    if item_type in {"header", "footer", "page_number"} or text in repeated_margins:
        return item_type, None, False
    if item_type == "table":
        return "table", None, region == "body"
    if item_type == "image":
        return "image", None, region == "body"
    if item_type == "list":
        return "list", None, region == "body"
    if item_type in {"page_footnote", "ref_text"}:
        return item_type, None, region == "body"

    if item_type in {"text", "aside_text"}:
        if region != "body":
            return item_type, None, False
        if is_probable_body_section(text):
            return "heading", 2, True
        if item.get("text_level") or is_probable_major_heading(text):
            return "heading", 1, True
        if re.match(r"^[一二三四五六七八九十]+[、.．]", text):
            return "heading", 3, True
        if re.match(r"^（[一二三四五六七八九十]+）", text):
            return "heading", 3, True
        return "paragraph", None, True
    return item_type, None, region == "body"


def block_content(item: dict[str, Any], task_index: int) -> tuple[str, dict[str, Any]]:
    item_type = str(item.get("type") or "unknown")
    extra: dict[str, Any] = {}
    if item_type == "table":
        extra["table_caption"] = table_caption(item)
        extra["table_footnote"] = item.get("table_footnote") or []
        return str(item.get("table_body") or ""), extra
    if item_type == "image":
        raw_path = str(item.get("img_path") or "")
        extra["image_caption"] = image_caption(item)
        extra["image_footnote"] = item.get("image_footnote") or []
        extra["image_path"] = image_markdown_path(task_index, raw_path) if raw_path else ""
        extra["image_sub_type"] = item.get("sub_type")
        return normalize_text(item.get("content") or extra["image_caption"]), extra
    if item_type == "list" and isinstance(item.get("list_items"), list):
        return "\n".join(str(value) for value in item["list_items"]), extra
    return item_text(item), extra


def build_for_output_dir(out_dir: Path) -> tuple[int, int]:
    tasks = load_tasks(out_dir)
    regions = classify_page_regions(out_dir)
    repeated_margins = repeated_margin_texts(out_dir)
    structured_path = out_dir / "structured_blocks.jsonl"
    semantic_path = out_dir / semantic_markdown_name(out_dir)

    current_h1 = ""
    current_h2 = ""
    current_h3 = ""
    semantic_lines: list[str] = []
    block_count = 0
    semantic_count = 0

    with structured_path.open("w", encoding="utf-8") as jsonl:
        for wrapped in iter_content_items(out_dir):
            item = wrapped["item"]
            page = int(wrapped["absolute_page"])
            region = regions.get(page, "body")
            block_type, heading_level, include_in_semantic = classify_block(item, region, repeated_margins)
            content, extra = block_content(item, int(wrapped["task_index"]))
            content_clean = normalize_text(content)
            if not content_clean and block_type not in {"image", "table"}:
                continue

            if block_type == "heading":
                heading_text = normalize_heading_text(content_clean)
                if heading_level == 1:
                    current_h1 = heading_text
                    current_h2 = ""
                    current_h3 = ""
                elif heading_level == 2:
                    current_h2 = heading_text
                    current_h3 = ""
                elif heading_level == 3:
                    current_h3 = heading_text

            section_path = [value for value in (current_h1, current_h2, current_h3) if value]
            image_table_candidate = False
            if block_type == "image":
                probe = " ".join(str(value) for value in (content_clean, extra.get("image_caption", "")))
                image_table_candidate = any(keyword in probe for keyword in ("表", "项目", "剂量", "诊断", "评分"))

            block = {
                "document": out_dir.name,
                "block_id": f"{out_dir.name}:{wrapped['task_index']}:{page}:{wrapped['item_index']}",
                "task_index": wrapped["task_index"],
                "page_range": wrapped["page_range"],
                "page": page,
                "local_page_idx": wrapped["local_page_idx"],
                "source_path": wrapped["source_path"],
                "region": region,
                "mineru_type": item.get("type"),
                "block_type": block_type,
                "heading_level": heading_level,
                "section_path": section_path,
                "text": content,
                "bbox": item.get("bbox"),
                "include_in_semantic": include_in_semantic,
                "image_table_candidate": image_table_candidate,
                **extra,
            }
            jsonl.write(json.dumps(block, ensure_ascii=False) + "\n")
            block_count += 1

            if not include_in_semantic:
                continue
            if block_type == "heading" and heading_level:
                semantic_lines.extend(["", f"{'#' * heading_level} {normalize_heading_text(content_clean)}", ""])
            elif block_type == "table":
                caption = extra.get("table_caption")
                if caption:
                    semantic_lines.extend(["", f"**表：{caption}**", ""])
                semantic_lines.extend(["", content.strip(), ""])
            elif block_type == "image":
                image_path = extra.get("image_path")
                caption = extra.get("image_caption")
                if caption:
                    semantic_lines.extend(["", f"**图：{caption}**"])
                if image_path:
                    semantic_lines.append(f"![]({image_path})")
                if content_clean:
                    semantic_lines.append(content_clean)
                semantic_lines.append("")
            elif block_type == "list":
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        semantic_lines.append(f"- {line}")
                semantic_lines.append("")
            else:
                semantic_lines.extend([content.strip(), ""])
            semantic_count += 1

    semantic_text = "\n".join(semantic_lines)
    semantic_text = re.sub(r"\n{3,}", "\n\n", semantic_text).strip() + "\n"
    semantic_path.write_text(semantic_text, encoding="utf-8")
    return block_count, semantic_count


def main() -> int:
    args = parse_args()
    dirs = output_dirs(Path(args.output_dir))
    if args.document:
        dirs = [path for path in dirs if path.name == args.document]
    if not dirs:
        print("No matching output directories found.")
        return 2

    total_blocks = 0
    total_semantic = 0
    for out_dir in dirs:
        blocks, semantic_blocks = build_for_output_dir(out_dir)
        total_blocks += blocks
        total_semantic += semantic_blocks
        print(
            f"[OK] {out_dir.name} | structured_blocks {blocks} | "
            f"semantic_blocks {semantic_blocks} | {semantic_markdown_name(out_dir)}"
        )
    print(f"Structured rebuild complete: {len(dirs)} documents, {total_blocks} blocks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
