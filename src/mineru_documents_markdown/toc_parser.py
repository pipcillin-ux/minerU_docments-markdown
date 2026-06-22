"""Parse a best-effort TOC tree from MinerU content blocks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .structure_utils import (
    BODY_SECTION_TITLES,
    classify_item_regions,
    heading_level_from_text,
    heading_key,
    item_region_key,
    is_probable_body_section,
    is_probable_major_heading,
    is_probable_numbered_subsection,
    item_text,
    looks_like_toc_entry,
    looks_like_toc_text,
    normalize_text,
    strip_toc_page_number,
)


KNOWN_SECTION_KEYS = {heading_key(value) for value in BODY_SECTION_TITLES}


@dataclass
class TocNode:
    node_id: str
    title: str
    normalized_key: str
    level: int
    page_hint: int | None
    parent_path: list[str]
    document_order: int
    source_page: int
    source_item_index: int
    source_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "normalized_key": self.normalized_key,
            "level": self.level,
            "page_hint": self.page_hint,
            "parent_path": self.parent_path,
            "document_order": self.document_order,
            "source_page": self.source_page,
            "source_item_index": self.source_item_index,
            "source_text": self.source_text,
        }


def page_hint(text: str) -> int | None:
    match = re.search(r"(\d{1,4})\s*$", normalize_text(text))
    return int(match.group(1)) if match else None


def split_stuck_toc_line(line: str) -> list[str]:
    """Split OCR-stuck TOC lines such as '实验室... 23诊断要点 23'."""
    line = normalize_text(line)
    if not line:
        return []
    keys = sorted(KNOWN_SECTION_KEYS, key=len, reverse=True)
    pattern = "|".join(re.escape(key) for key in keys)
    if not pattern:
        return [line]
    compact = re.sub(r"\s+", "", line)
    matches = list(re.finditer(rf"(?:{pattern})\d{{1,4}}", compact))
    if len(matches) <= 1:
        matches = list(
            re.finditer(
                r"[（(][一二三四五六七八九十百\d]+[）)][^（）()]{1,40}?\d{1,4}",
                compact,
            )
        )
    if len(matches) <= 1:
        return [line]
    parts: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(compact)
        piece = compact[start:end]
        if piece:
            parts.append(piece)
    return parts or [line]


def iter_toc_lines(wrapped_items: list[dict[str, Any]], max_page: int = 50) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    regions = classify_item_regions(wrapped_items)
    for wrapped in wrapped_items:
        page = int(wrapped["absolute_page"])
        if page > max_page:
            continue
        if regions.get(item_region_key(wrapped)) != "toc":
            continue
        item = wrapped["item"]
        text = item_text(item)
        if not text:
            continue
        for raw_line in text.splitlines():
            for line in split_stuck_toc_line(raw_line):
                clean = normalize_text(line)
                if not clean:
                    continue
                if clean == "目录":
                    continue
                if looks_like_toc_entry(clean) or is_probable_body_section(clean) or is_probable_numbered_subsection(clean):
                    lines.append(
                        {
                            "line": clean,
                            "page": page,
                            "item_index": int(wrapped["item_index"]),
                            "text_level": item.get("text_level"),
                            "mineru_type": item.get("type"),
                        }
                    )
    return lines


def infer_toc_level(line: str, mineru_level: Any, current_level: int | None) -> int | None:
    title = strip_toc_page_number(line)
    key = heading_key(title)
    if not key:
        return None
    pattern_level = heading_level_from_text(title)
    if pattern_level is not None:
        return pattern_level
    if key in KNOWN_SECTION_KEYS:
        return 2
    if is_probable_numbered_subsection(title):
        return 3
    if re.match(r"^第[一二三四五六七八九十百千万\d]+[章节篇部分编卷]", title):
        return 1
    if isinstance(mineru_level, int) and mineru_level <= 1 and looks_like_toc_text(line):
        return 1
    if is_probable_major_heading(title) and looks_like_toc_text(line):
        return 1
    if current_level == 1 and looks_like_toc_text(line):
        return 2
    return None


def parse_toc_tree(wrapped_items: list[dict[str, Any]]) -> list[TocNode]:
    nodes: list[TocNode] = []
    stack: list[TocNode] = []
    toc_lines = iter_toc_lines(wrapped_items)
    for line_info in toc_lines:
        title = strip_toc_page_number(line_info["line"])
        key = heading_key(title)
        if not key:
            continue
        level = infer_toc_level(line_info["line"], line_info.get("text_level"), stack[-1].level if stack else None)
        if level is None:
            continue
        while stack and stack[-1].level >= level:
            stack.pop()
        parent_path = [node.title for node in stack]
        node = TocNode(
            node_id=f"toc_{len(nodes) + 1:05d}",
            title=title,
            normalized_key=key,
            level=level,
            page_hint=page_hint(line_info["line"]),
            parent_path=parent_path,
            document_order=len(nodes) + 1,
            source_page=int(line_info["page"]),
            source_item_index=int(line_info["item_index"]),
            source_text=line_info["line"],
        )
        nodes.append(node)
        stack.append(node)
    return nodes


def toc_level_map(nodes: list[TocNode]) -> dict[str, int]:
    levels: dict[str, int] = {}
    for node in nodes:
        levels.setdefault(node.normalized_key, node.level)
    return levels


def toc_path_map(nodes: list[TocNode]) -> dict[str, list[str]]:
    paths: dict[str, list[str]] = {}
    for node in nodes:
        paths.setdefault(node.normalized_key, [*node.parent_path, node.title])
    return paths


def write_toc_tree(path: Path, nodes: list[TocNode]) -> None:
    payload = {
        "node_count": len(nodes),
        "nodes": [node.to_dict() for node in nodes],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
