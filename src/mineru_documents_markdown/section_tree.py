"""Build a body section tree from semantic heading blocks."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .defaults import KNOWN_SECTION_CONFIDENCE

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
    return resolved


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
    first_toc_major_key = ""
    for node in sorted(toc_nodes, key=lambda value: int(value.get("document_order") or 0)):
        if clamp_level(node.get("level")) == 1:
            first_toc_major_key = str(node.get("normalized_key") or "")
            break
    if first_toc_major_key:
        trimmed = trim_to_body_tree_start(candidates)
        first_candidate_key = heading_key(str(trimmed[0][1].get("text") or "")) if trimmed else ""
        if first_candidate_key != first_toc_major_key:
            return True
    if len(candidates) >= 12 and len(candidates) >= int(len(toc_nodes) * 0.15):
        return False
    return len(candidates) < max(8, int(len(toc_nodes) * 0.05))


def max_block_page(blocks: list[dict[str, Any]]) -> int | None:
    pages = [clamp_page(block.get("page")) for block in blocks]
    pages = [page for page in pages if page is not None]
    return max(pages) if pages else None


def body_heading_anchors(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if block.get("block_type") != "heading" or block.get("region") != "body":
            continue
        if block.get("include_in_semantic") is False:
            continue
        page = clamp_page(block.get("page"))
        key = heading_key(str(block.get("text") or ""))
        block_id = str(block.get("block_id") or "")
        if page is None or not key or not block_id:
            continue
        anchors.append({"index": index, "page": page, "key": key, "block_id": block_id})
    return anchors


def estimate_page_offset(blocks: list[dict[str, Any]], toc_nodes: list[dict[str, Any]]) -> int | None:
    headings = body_heading_anchors(blocks)

    last_index = -1
    offsets: list[int] = []
    for node in sorted(toc_nodes, key=lambda value: int(value.get("document_order") or 0)):
        page_hint = clamp_page(node.get("page_hint"))
        key = str(node.get("normalized_key") or "")
        if page_hint is None or not key:
            continue
        match = None
        for heading in headings:
            if int(heading["index"]) > last_index and heading["key"] == key:
                match = heading
                break
        if match is None:
            continue
        last_index = int(match["index"])
        offsets.append(int(match["page"]) - page_hint)

    if not offsets:
        return None
    offset, count = Counter(offsets).most_common(1)[0]
    if count < 3:
        return None
    return offset


def nearby_heading_block_id(
    anchors: list[dict[str, Any]],
    page: int | None,
    title_key: str,
) -> str:
    if page is None or not title_key:
        return ""
    matches = [anchor for anchor in anchors if anchor["key"] == title_key]
    if not matches:
        return ""
    near_matches = [anchor for anchor in matches if abs(int(anchor["page"]) - page) <= 3]
    if near_matches:
        best = min(near_matches, key=lambda anchor: (abs(int(anchor["page"]) - page), int(anchor["index"])))
        return str(best["block_id"])
    if len(matches) == 1 and abs(int(matches[0]["page"]) - page) <= 8:
        return str(matches[0]["block_id"])
    return ""


def nearest_block_id_for_page(blocks: list[dict[str, Any]], page: int | None) -> str:
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
    return fallback


def last_block_id_for_page(blocks: list[dict[str, Any]], page: int | None) -> str:
    if page is None:
        return ""
    fallback = ""
    for block in blocks:
        block_page = clamp_page(block.get("page"))
        if block_page is None:
            continue
        if block_page > page:
            break
        if block.get("region") == "toc":
            continue
        fallback = str(block.get("block_id") or fallback)
    return fallback


def build_section_tree_from_toc(document: str, blocks: list[dict[str, Any]], toc_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[SectionNode] = []
    stack: list[SectionNode] = []
    max_page = max_block_page(blocks)
    page_offset = estimate_page_offset(blocks, toc_nodes)
    ordered_toc = sorted(toc_nodes, key=lambda node: int(node.get("document_order") or 0))
    heading_anchors = body_heading_anchors(blocks)
    block_by_id = {str(block.get("block_id") or ""): block for block in blocks}

    for order, toc_node in enumerate(ordered_toc, start=1):
        title = normalize_text(str(toc_node.get("title") or ""))
        if not title:
            continue
        level = clamp_level(toc_node.get("level")) or 1
        page_hint = clamp_page(toc_node.get("page_hint"))
        start_page = page_hint + page_offset if page_hint is not None and page_offset is not None else page_hint
        title_key = str(toc_node.get("normalized_key") or heading_key(title))
        source_block_id = nearby_heading_block_id(heading_anchors, start_page, title_key)
        if not source_block_id:
            source_block_id = nearest_block_id_for_page(blocks, start_page)
        source_block = block_by_id.get(source_block_id)
        source_page = clamp_page(source_block.get("page")) if source_block else None
        if source_page is not None:
            start_page = source_page
        if max_page is not None and start_page is not None and start_page > max_page and not source_block_id:
            continue

        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        parent_path = list(parent.path) if parent else []
        node = SectionNode(
            section_id=f"sec_{len(nodes) + 1:06d}",
            title=title,
            normalized_key=title_key,
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
            confidence=KNOWN_SECTION_CONFIDENCE,
            evidence=["toc_backbone", f"page_offset:{page_offset}"] if page_offset is not None else ["toc_backbone"],
        )
        nodes.append(node)
        stack.append(node)

    block_index_by_id = {str(block.get("block_id") or ""): index for index, block in enumerate(blocks)}
    for index, node in enumerate(nodes):
        start_index = block_index_by_id.get(node.start_block_id)
        if start_index is not None:
            end_index = len(blocks) - 1
            for next_index in range(index + 1, len(nodes)):
                next_node = nodes[next_index]
                if next_node.level > node.level:
                    continue
                next_start_index = block_index_by_id.get(next_node.start_block_id)
                if next_start_index is not None and next_start_index > start_index:
                    end_index = max(start_index, next_start_index - 1)
                    break
            end_block = end_block_for_range(blocks, start_index, end_index)
            node.end_block_id = str(end_block.get("block_id") or node.start_block_id)
            node.end_page = clamp_page(end_block.get("page")) or node.start_page
            continue

        end_page = max(max_page or node.start_page or 0, node.start_page or 0) or node.start_page
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
        node.end_block_id = last_block_id_for_page(blocks, end_page) or node.start_block_id

    return {
        "document": document,
        "version": TREE_VERSION,
        "source": "toc_backbone",
        "page_offset": page_offset,
        "node_count": len(nodes),
        "nodes": [node.to_dict() for node in nodes],
    }


def build_section_tree_from_headings(
    document: str,
    blocks: list[dict[str, Any]],
    candidates: list[tuple[int, dict[str, Any], int, list[str], float]],
) -> dict[str, Any]:
    candidates = trim_to_body_tree_start(candidates)
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


def node_ranges(section_payload: dict[str, Any], blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    block_index_by_id = {str(block.get("block_id") or ""): index for index, block in enumerate(blocks)}
    ranges: list[dict[str, Any]] = []
    for node in section_payload.get("nodes") or []:
        start_index = block_index_by_id.get(str(node.get("start_block_id") or ""))
        if start_index is None:
            start_index = block_index_by_id.get(str(node.get("source_block_id") or ""))
        end_index = block_index_by_id.get(str(node.get("end_block_id") or ""))
        ranges.append(
            {
                "node": node,
                "start_index": start_index,
                "end_index": end_index,
                "start_page": clamp_page(node.get("start_page")),
                "end_page": clamp_page(node.get("end_page")),
            }
        )
    return ranges


def best_section_for_block(
    block: dict[str, Any],
    block_index: int,
    ranges: list[dict[str, Any]],
) -> dict[str, Any] | None:
    page = clamp_page(block.get("page"))
    index_matches: list[dict[str, Any]] = []
    page_matches: list[dict[str, Any]] = []
    for item in ranges:
        node = item["node"]
        start_index = item.get("start_index")
        end_index = item.get("end_index")
        if isinstance(start_index, int):
            resolved_end = end_index if isinstance(end_index, int) else start_index
            if start_index <= block_index <= resolved_end:
                index_matches.append(node)
                continue
        start_page = item.get("start_page")
        end_page = item.get("end_page")
        if page is not None and isinstance(start_page, int):
            resolved_end_page = end_page if isinstance(end_page, int) else start_page
            if start_page <= page <= resolved_end_page and (
                not isinstance(start_index, int) or start_index <= block_index
            ):
                page_matches.append(node)

    matches = index_matches or page_matches
    if not matches:
        return None
    return max(
        matches,
        key=lambda node: (
            int(node.get("level") or 0),
            int(node.get("document_order") or 0),
        ),
    )


def section_fields(node: dict[str, Any] | None, source: str) -> dict[str, Any]:
    if node is None:
        return {
            "section_id": "",
            "section_parent_id": "",
            "tree_section_path": [],
            "tree_heading_level": None,
            "tree_section_source": source,
            "tree_section_confidence": None,
        }
    return {
        "section_id": str(node.get("section_id") or ""),
        "section_parent_id": str(node.get("parent_id") or ""),
        "tree_section_path": list(node.get("path") or []),
        "tree_heading_level": clamp_level(node.get("level")),
        "tree_section_source": source,
        "tree_section_confidence": node.get("confidence"),
    }


def attach_section_tree(blocks: list[dict[str, Any]], section_payload: dict[str, Any]) -> list[dict[str, Any]]:
    source = str(section_payload.get("source") or "")
    ranges = node_ranges(section_payload, blocks)
    attached: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        node = None
        if block.get("region") == "body" and block.get("include_in_semantic") is not False:
            node = best_section_for_block(block, index, ranges)
        attached.append({**block, **section_fields(node, source)})
    return attached


def clamp_page(value: Any) -> int | None:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page >= 0 else None


def write_section_tree(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
