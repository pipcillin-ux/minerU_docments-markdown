"""Build a body section tree from semantic heading blocks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .structure_utils import heading_key, heading_level_from_text, normalize_text


TREE_VERSION = 1


@dataclass
class SectionNode:
    section_id: str
    title: str
    normalized_key: str
    level: int
    parent_id: str | None
    parent_path: list[str]
    path: list[str]
    document_order: int
    start_page: int | None
    end_page: int | None
    start_block_id: str
    end_block_id: str
    source_block_id: str
    source_heading_level: int | None
    toc_heading_level: int | None
    region: str
    confidence: float
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "normalized_key": self.normalized_key,
            "level": self.level,
            "parent_id": self.parent_id,
            "parent_path": self.parent_path,
            "path": self.path,
            "document_order": self.document_order,
            "start_page": self.start_page,
            "end_page": self.end_page,
            "start_block_id": self.start_block_id,
            "end_block_id": self.end_block_id,
            "source_block_id": self.source_block_id,
            "source_heading_level": self.source_heading_level,
            "toc_heading_level": self.toc_heading_level,
            "region": self.region,
            "confidence": round(self.confidence, 4),
            "evidence": self.evidence,
        }


def clamp_level(value: Any) -> int | None:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    if level < 1 or level > 6:
        return None
    return level


def toc_level_map(toc_nodes: list[dict[str, Any]]) -> dict[str, int]:
    levels: dict[str, int] = {}
    for node in toc_nodes:
        key = str(node.get("normalized_key") or "")
        level = clamp_level(node.get("level"))
        if key and level is not None:
            levels.setdefault(key, level)
    return levels


def heading_pattern(text: str) -> str:
    clean = normalize_text(text)
    if re.match(r"^第[一二三四五六七八九十百千万\d]+[章篇编部卷]", clean):
        return "chapter"
    if re.match(r"^第[一二三四五六七八九十百千万\d]+节", clean):
        return "section"
    if re.match(r"^[一二三四五六七八九十百]+[、.．]", clean):
        return "chinese_numbered"
    if re.match(r"^[（(][一二三四五六七八九十百\d]+[）)]", clean):
        return "paren_numbered"
    if re.match(r"^\d+(?:\.\d+)*[.．、]", clean):
        return "arabic_numbered"
    return "plain"


def infer_tree_level(block: dict[str, Any], toc_levels: dict[str, int]) -> tuple[int | None, list[str], float]:
    text = normalize_text(str(block.get("text") or ""))
    key = heading_key(text)
    source_level = clamp_level(block.get("heading_level"))
    toc_level = clamp_level(block.get("toc_heading_level"))
    if toc_level is None and key in toc_levels:
        toc_level = toc_levels[key]
    pattern_level = heading_level_from_text(text, toc_levels if toc_level is not None else None)
    pattern_level = clamp_level(pattern_level)

    evidence: list[str] = []
    confidence = 0.54
    level: int | None = None

    if toc_level is not None:
        level = toc_level
        evidence.append("toc_match")
        confidence += 0.24
    elif pattern_level is not None:
        level = pattern_level
        evidence.append("numbering_pattern")
        confidence += 0.2
    elif source_level is not None:
        level = source_level
        evidence.append("source_heading_level")
        confidence += 0.12

    if source_level is not None and level is not None:
        if source_level == level:
            evidence.append("source_level_agrees")
            confidence += 0.08
        elif abs(source_level - level) <= 1:
            evidence.append("source_level_nearby")
            confidence += 0.03

    pattern = heading_pattern(text)
    if pattern != "plain":
        evidence.append(f"pattern:{pattern}")
        confidence += 0.04

    region = str(block.get("region") or "")
    if region == "body":
        evidence.append("body_region")
        confidence += 0.04
    elif region == "toc":
        confidence -= 0.3

    return level, evidence, max(0.0, min(confidence, 1.0))


def heading_blocks(blocks: list[dict[str, Any]], toc_nodes: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], int, list[str], float]]:
    toc_levels = toc_level_map(toc_nodes)
    resolved: list[tuple[int, dict[str, Any], int, list[str], float]] = []
    for index, block in enumerate(blocks):
        if block.get("block_type") != "heading":
            continue
        if block.get("region") != "body":
            continue
        if block.get("include_in_semantic") is False:
            continue
        text = normalize_text(str(block.get("text") or ""))
        if not text:
            continue
        level, evidence, confidence = infer_tree_level(block, toc_levels)
        if level is None:
            continue
        resolved.append((index, block, level, evidence, confidence))
    return trim_to_body_tree_start(resolved)


def credible_tree_start(block: dict[str, Any], level: int, evidence: list[str]) -> bool:
    text = normalize_text(str(block.get("text") or ""))
    if level != 1:
        return False
    if "toc_match" in evidence:
        return True
    if text in {"绪论", "导论", "概论", "总论"}:
        return True
    return bool(re.match(r"^第[一二三四五六七八九十百千万\d]+[章篇编部卷]", text))


def trim_to_body_tree_start(
    resolved: list[tuple[int, dict[str, Any], int, list[str], float]],
) -> list[tuple[int, dict[str, Any], int, list[str], float]]:
    for index, (_, block, level, evidence, _) in enumerate(resolved):
        if credible_tree_start(block, level, evidence):
            return resolved[index:]
    return resolved


def end_block_for_range(blocks: list[dict[str, Any]], start_index: int, end_index: int) -> dict[str, Any]:
    end_index = max(start_index, min(end_index, len(blocks) - 1))
    for index in range(end_index, start_index - 1, -1):
        block = blocks[index]
        if block.get("include_in_semantic") is False:
            continue
        if block.get("region") == "toc":
            continue
        if str(block.get("block_id") or ""):
            return block
    return blocks[start_index]


def should_use_toc_backbone(
    candidates: list[tuple[int, dict[str, Any], int, list[str], float]],
    toc_nodes: list[dict[str, Any]],
) -> bool:
    if not toc_nodes:
        return False
    if len(candidates) >= 12 and len(candidates) >= int(len(toc_nodes) * 0.15):
        return False
    return len(candidates) < max(8, int(len(toc_nodes) * 0.05))


def max_block_page(blocks: list[dict[str, Any]]) -> int | None:
    pages = [clamp_page(block.get("page")) for block in blocks]
    pages = [page for page in pages if page is not None]
    return max(pages) if pages else None


def nearest_block_id_for_page(blocks: list[dict[str, Any]], page: int | None, title_key: str = "") -> str:
    if page is None:
        return ""
    fallback = ""
    for block in blocks:
        block_page = clamp_page(block.get("page"))
        if block_page is None or block_page < page:
            continue
        if block.get("region") == "toc":
            continue
        if not fallback:
            fallback = str(block.get("block_id") or "")
        if title_key and heading_key(str(block.get("text") or "")) == title_key:
            return str(block.get("block_id") or "")
    return fallback


def build_section_tree_from_toc(document: str, blocks: list[dict[str, Any]], toc_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[SectionNode] = []
    stack: list[SectionNode] = []
    max_page = max_block_page(blocks)
    ordered_toc = sorted(toc_nodes, key=lambda node: int(node.get("document_order") or 0))

    for order, toc_node in enumerate(ordered_toc, start=1):
        title = normalize_text(str(toc_node.get("title") or ""))
        if not title:
            continue
        level = clamp_level(toc_node.get("level")) or 1
        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        parent_path = list(parent.path) if parent else []
        start_page = clamp_page(toc_node.get("page_hint"))
        source_block_id = nearest_block_id_for_page(blocks, start_page, str(toc_node.get("normalized_key") or ""))
        node = SectionNode(
            section_id=f"sec_{len(nodes) + 1:06d}",
            title=title,
            normalized_key=str(toc_node.get("normalized_key") or heading_key(title)),
            level=level,
            parent_id=parent.section_id if parent else None,
            parent_path=parent_path,
            path=[*parent_path, title],
            document_order=len(nodes) + 1,
            start_page=start_page,
            end_page=start_page,
            start_block_id=source_block_id,
            end_block_id=source_block_id,
            source_block_id=source_block_id,
            source_heading_level=None,
            toc_heading_level=level,
            region="body",
            confidence=0.82,
            evidence=["toc_backbone"],
        )
        nodes.append(node)
        stack.append(node)

    for index, node in enumerate(nodes):
        end_page = max_page or node.start_page
        for next_index in range(index + 1, len(nodes)):
            next_node = nodes[next_index]
            if next_node.level <= node.level:
                next_start = next_node.start_page
                if isinstance(next_start, int) and isinstance(node.start_page, int):
                    end_page = max(node.start_page, next_start - 1)
                else:
                    end_page = node.start_page
                break
        node.end_page = end_page
        node.end_block_id = nearest_block_id_for_page(blocks, end_page, "") or node.start_block_id

    return {
        "document": document,
        "version": TREE_VERSION,
        "source": "toc_backbone",
        "node_count": len(nodes),
        "nodes": [node.to_dict() for node in nodes],
    }


def build_section_tree_from_headings(
    document: str,
    blocks: list[dict[str, Any]],
    candidates: list[tuple[int, dict[str, Any], int, list[str], float]],
) -> dict[str, Any]:
    nodes: list[SectionNode] = []
    stack: list[SectionNode] = []
    node_indexes: list[int] = []

    for order, (block_index, block, level, evidence, confidence) in enumerate(candidates, start=1):
        text = normalize_text(str(block.get("text") or ""))
        block_id = str(block.get("block_id") or "")
        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        if parent is None and level > 1:
            evidence = [*evidence, "root_without_parent"]
            confidence = max(0.0, confidence - 0.08)
        parent_path = list(parent.path) if parent else []
        node = SectionNode(
            section_id=f"sec_{order:06d}",
            title=text,
            normalized_key=heading_key(text),
            level=level,
            parent_id=parent.section_id if parent else None,
            parent_path=parent_path,
            path=[*parent_path, text],
            document_order=order,
            start_page=clamp_page(block.get("page")),
            end_page=clamp_page(block.get("page")),
            start_block_id=block_id,
            end_block_id=block_id,
            source_block_id=block_id,
            source_heading_level=clamp_level(block.get("heading_level")),
            toc_heading_level=clamp_level(block.get("toc_heading_level")),
            region=str(block.get("region") or ""),
            confidence=confidence,
            evidence=evidence,
        )
        nodes.append(node)
        node_indexes.append(block_index)
        stack.append(node)

    for index, node in enumerate(nodes):
        current_block_index = node_indexes[index]
        end_index = len(blocks) - 1
        for next_index in range(index + 1, len(nodes)):
            if nodes[next_index].level <= node.level:
                end_index = max(current_block_index, node_indexes[next_index] - 1)
                break
        end_block = end_block_for_range(blocks, current_block_index, end_index)
        node.end_block_id = str(end_block.get("block_id") or node.start_block_id)
        node.end_page = clamp_page(end_block.get("page")) or node.start_page

    payload = {
        "document": document,
        "version": TREE_VERSION,
        "source": "body_headings",
        "node_count": len(nodes),
        "nodes": [node.to_dict() for node in nodes],
    }
    return payload


def build_section_tree(document: str, blocks: list[dict[str, Any]], toc_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = heading_blocks(blocks, toc_nodes)
    if should_use_toc_backbone(candidates, toc_nodes):
        return build_section_tree_from_toc(document, blocks, toc_nodes)
    return build_section_tree_from_headings(document, blocks, candidates)


def clamp_page(value: Any) -> int | None:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page >= 0 else None


def write_section_tree(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
