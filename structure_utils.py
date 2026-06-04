#!/usr/bin/env python3
"""Shared helpers for profiling and rebuilding MinerU outputs."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


BODY_SECTION_TITLES = {
    "病因病机",
    "临床表现",
    "实验室和其他辅助检查",
    "诊断要点",
    "鉴别诊断",
    "治疗",
    "医案精选",
    "名家名医论坛",
    "难点与对策",
    "经验与体会",
    "预后与转归",
    "预防与调护",
    "现代研究",
    "评述与展望",
}

FRONT_MATTER_PATTERNS = (
    "目录",
    "前言",
    "编写说明",
    "出版者的话",
    "图书在版编目",
    "编委会",
    "主编",
    "版权",
)

BACK_MATTER_PATTERNS = ("参考文献", "附录", "索引", "后记")


def output_dirs(output_root: Path) -> list[Path]:
    return sorted(path.parent for path in output_root.glob("*/tasks.json"))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_tasks(out_dir: Path) -> list[dict[str, Any]]:
    return load_json(out_dir / "tasks.json")


def parse_page_range(page_range: str) -> tuple[int, int]:
    start, end = page_range.split("-", 1)
    return int(start), int(end)


def content_list_path(out_dir: Path, task: dict[str, Any]) -> Path | None:
    md_path = task.get("md_path")
    if md_path:
        extracted_dir = (out_dir / str(md_path)).parent
        candidates = sorted(path for path in extracted_dir.glob("*_content_list.json") if "_v2" not in path.name)
        if candidates:
            return candidates[0]
    part_dir = out_dir / "parts" / f"part_{int(task['index']):03d}_{task['page_range']}"
    candidates = sorted(path for path in part_dir.glob("extracted/*_content_list.json") if "_v2" not in path.name)
    return candidates[0] if candidates else None


def iter_content_items(out_dir: Path) -> Iterable[dict[str, Any]]:
    for task in load_tasks(out_dir):
        path = content_list_path(out_dir, task)
        if path is None:
            continue
        start, _ = parse_page_range(str(task["page_range"]))
        for item_index, item in enumerate(load_json(path)):
            page_idx = int(item.get("page_idx", 0))
            yield {
                "task": task,
                "task_index": int(task["index"]),
                "page_range": str(task["page_range"]),
                "source_path": str(path.relative_to(out_dir)),
                "item_index": item_index,
                "absolute_page": start + page_idx,
                "local_page_idx": page_idx,
                "item": item,
            }


def item_text(item: dict[str, Any]) -> str:
    if isinstance(item.get("text"), str):
        return item["text"]
    if isinstance(item.get("list_items"), list):
        return "\n".join(str(value) for value in item["list_items"])
    if isinstance(item.get("table_body"), str):
        return item["table_body"]
    if isinstance(item.get("content"), str):
        return item["content"]
    return ""


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def compact_heading_text(text: str) -> str:
    text = normalize_text(text)
    return re.sub(r"\s+", "", text)


def markdown_file(out_dir: Path) -> Path | None:
    candidates = sorted(
        path
        for path in out_dir.glob("*.md")
        if not path.name.endswith(".semantic.md") and path.name != "quality_report.md"
    )
    return candidates[0] if candidates else None


def markdown_headings(out_dir: Path) -> list[tuple[int, str]]:
    path = markdown_file(out_dir)
    if path is None:
        return []
    headings: list[tuple[int, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((len(match.group(1)), match.group(2)))
    return headings


def looks_like_toc_text(text: str) -> bool:
    clean = normalize_text(text)
    if clean == "目录" or clean.endswith("目录"):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        numbered = sum(1 for line in lines if re.search(r"\s\d{1,4}$", line))
        return numbered / len(lines) >= 0.45
    return bool(re.search(r".{2,30}\s+\d{1,4}$", clean))


def page_texts(out_dir: Path) -> dict[int, list[str]]:
    pages: dict[int, list[str]] = {}
    for wrapped in iter_content_items(out_dir):
        text = normalize_text(item_text(wrapped["item"]))
        if text:
            pages.setdefault(int(wrapped["absolute_page"]), []).append(text)
    return pages


def classify_page_regions(out_dir: Path) -> dict[int, str]:
    pages = page_texts(out_dir)
    max_page = max(pages) if pages else 0
    toc_pages = {
        page
        for page, texts in pages.items()
        if page <= 50
        for text in texts
        if looks_like_toc_text(text) or text == "目录"
    }
    toc_end = max(toc_pages) if toc_pages else 0
    regions: dict[int, str] = {}
    for page, texts in pages.items():
        joined = " ".join(texts[:40])
        if page <= toc_end or (page <= 15 and any(pattern in joined for pattern in FRONT_MATTER_PATTERNS)):
            regions[page] = "front_matter"
        elif page >= int(max_page * 0.75) and any(pattern in joined for pattern in BACK_MATTER_PATTERNS):
            regions[page] = "back_matter"
        else:
            regions[page] = "body"
    return regions


def repeated_margin_texts(out_dir: Path, min_repeats: int = 3) -> set[str]:
    counts: Counter[str] = Counter()
    for wrapped in iter_content_items(out_dir):
        item = wrapped["item"]
        if item.get("type") not in {"header", "footer", "page_number"}:
            continue
        text = normalize_text(item_text(item))
        if text:
            counts[text] += 1
    return {text for text, count in counts.items() if count >= min_repeats}


def table_caption(item: dict[str, Any]) -> str:
    captions = item.get("table_caption")
    if isinstance(captions, list):
        return normalize_text(" ".join(str(value) for value in captions))
    return normalize_text(str(captions or ""))


def image_caption(item: dict[str, Any]) -> str:
    captions = item.get("image_caption")
    if isinstance(captions, list):
        return normalize_text(" ".join(str(value) for value in captions))
    return normalize_text(str(captions or ""))


def is_probable_body_section(text: str) -> bool:
    clean = compact_heading_text(text)
    return clean in {compact_heading_text(value) for value in BODY_SECTION_TITLES}


def is_probable_major_heading(text: str) -> bool:
    clean = compact_heading_text(text)
    if not clean or len(clean) > 26:
        return False
    if is_probable_body_section(text):
        return False
    if clean in {"目录", "前言", "编写说明"}:
        return False
    return any(keyword in clean for keyword in ("病", "症", "骨折", "脱位", "损伤", "综合征", "炎", "癌", "瘤"))
