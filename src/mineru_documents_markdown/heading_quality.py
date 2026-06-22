#!/usr/bin/env python3
"""Quality checks for rebuilt heading structures."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .structure_utils import heading_key, looks_like_toc_entry, normalize_text, output_dirs
from .toc_parser import split_stuck_toc_line


IssueSeverity = str


@dataclass
class HeadingIssue:
    code: str
    severity: IssueSeverity
    message: str
    page: int | None = None
    text: str = ""
    block_id: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "page": self.page,
            "text": self.text,
            "block_id": self.block_id,
            "suggestion": self.suggestion,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check semantic heading quality for MinerU outputs.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument("--document", help="Only check one output directory by name.")
    parser.add_argument(
        "--fail-on",
        choices=("none", "warn", "fail"),
        default="none",
        help="Exit non-zero when issues at or above this severity are found.",
    )
    return parser.parse_args()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_invalid_json": line[:200]})
    return rows


def looks_like_stuck_toc(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    return bool(re.search(r"\d", clean) and len(split_stuck_toc_line(clean)) > 1)


def is_sentence_heading(text: str) -> bool:
    clean = normalize_text(text)
    if len(clean) > 90:
        return True
    return bool(re.search(r"[，,。；;！？!?]", clean))


def is_actionable_sentence_heading(block: dict[str, Any], text: str) -> bool:
    clean = normalize_text(text)
    if (
        block.get("decision_source") == "deepseek_warn_review"
        and block.get("repair_action") in {"keep_heading", "promote_to_heading"}
    ):
        return False
    if block.get("toc_heading_level"):
        return False
    if str(block.get("decision_reason") or "").startswith("The candidate matches a TOC node"):
        return False
    if re.match(r"^难点[一二三四五六七八九十百\d]+[:：]", clean):
        return False
    if re.match(r"^[（(][一二三四五六七八九十百\d]+[）)].{1,44}$", clean):
        return False
    if re.match(r"^\d+[.．、]\s*.{1,48}$", clean) and not re.search(r"[。；;！？!?]", clean):
        return False
    if "——" in clean and len(clean) <= 60:
        return False
    return is_sentence_heading(clean)


def is_question_like_heading(text: str) -> bool:
    clean = normalize_text(text)
    return bool(re.search(r"[?？]\s*$", clean))


def is_strict_clinical_subsection(text: str) -> bool:
    clean = normalize_text(text)
    compact = re.sub(r"\s+", "", clean)
    clinical_terms = r"(症状|体征|常见并发症|症状与体征|临床症状|主要症状)"
    if re.fullmatch(clinical_terms, compact):
        return True
    return bool(re.fullmatch(r"[（(]?[一二三四五六七八九十百\d]+[）)]" + clinical_terms, compact))


def is_fragment_heading(block: dict[str, Any], text: str) -> bool:
    if block.get("toc_heading_level"):
        return False
    clean = normalize_text(text)
    compact = re.sub(r"\s+", "", clean)
    if not compact:
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]", compact):
        return True
    return bool(len(compact) <= 6 and re.fullmatch(r"(?:[\u4e00-\u9fff]\s+){1,5}[\u4e00-\u9fff]", clean))


def title_without_page(text: str) -> str:
    return re.sub(r"\s*\d{1,4}\s*$", "", normalize_text(text)).strip()


def issue_counts(issues: list[HeadingIssue]) -> dict[str, int]:
    counts = Counter(issue.severity for issue in issues)
    return {"FAIL": counts.get("FAIL", 0), "WARN": counts.get("WARN", 0), "INFO": counts.get("INFO", 0)}


def check_toc_tree(out_dir: Path, toc_payload: dict[str, Any]) -> list[HeadingIssue]:
    issues: list[HeadingIssue] = []
    nodes = toc_payload.get("nodes") or []
    if not nodes:
        return [
            HeadingIssue(
                code="toc_missing",
                severity="WARN",
                message="No toc_tree.json nodes were found.",
                suggestion="Run mineru-build-structured-blocks before heading quality checks.",
            )
        ]
    for node in nodes:
        source_text = str(node.get("source_text") or "")
        title = str(node.get("title") or "")
        if looks_like_stuck_toc(source_text) and not looks_like_stuck_toc(title):
            continue
        if looks_like_stuck_toc(title):
            issues.append(
                HeadingIssue(
                    code="toc_stuck_item",
                    severity="FAIL",
                    message="TOC node still appears to contain multiple stuck entries.",
                    page=node.get("source_page"),
                    text=title,
                    suggestion="Split the TOC line into separate title/page nodes.",
                )
            )
        if node.get("level") not in {1, 2, 3, 4, 5, 6}:
            issues.append(
                HeadingIssue(
                    code="toc_invalid_level",
                    severity="FAIL",
                    message="TOC node has an invalid heading level.",
                    page=node.get("source_page"),
                    text=title,
                    suggestion="Rebuild toc_tree.json and inspect level inference.",
                )
            )
    return issues


def check_structured_blocks(blocks: list[dict[str, Any]], toc_payload: dict[str, Any]) -> list[HeadingIssue]:
    issues: list[HeadingIssue] = []
    headings = [block for block in blocks if block.get("block_type") == "heading"]
    if not headings:
        return [
            HeadingIssue(
                code="semantic_heading_missing",
                severity="FAIL",
                message="No semantic headings were found in structured_blocks.jsonl.",
                suggestion="Rebuild semantic blocks and inspect heading decisions.",
            )
        ]

    previous_level: int | None = None
    previous_region = ""
    heading_keys = {heading_key(str(block.get("text") or "")) for block in headings}
    for block in headings:
        text = normalize_text(str(block.get("text") or ""))
        level = block.get("heading_level")
        page = block.get("page")
        region = str(block.get("region") or "")
        block_id = str(block.get("block_id") or "")
        if region == "toc":
            issues.append(
                HeadingIssue(
                    code="toc_heading_leak",
                    severity="FAIL",
                    message="A TOC-region block leaked into the semantic heading outline.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Render TOC entries as a separate TOC block, not Markdown headings.",
                )
            )
        if looks_like_toc_entry(text):
            issues.append(
                HeadingIssue(
                    code="toc_item_in_body_outline",
                    severity="FAIL",
                    message="Heading looks like a TOC entry with a page number.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Keep page-numbered TOC entries outside the body heading outline.",
                )
            )
        if looks_like_stuck_toc(text):
            issues.append(
                HeadingIssue(
                    code="semantic_stuck_heading",
                    severity="FAIL",
                    message="Semantic heading appears to contain multiple stuck TOC/headings.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Split the heading into separate headings before rendering semantic.md.",
                )
            )
        if level == 1 and re.match(r"^[（(][一二三四五六七八九十百\d]+[）)]", text):
            issues.append(
                HeadingIssue(
                    code="subsection_as_h1",
                    severity="FAIL",
                    message="Numbered subsection was promoted to H1.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Use TOC/context to demote this heading to H3/H4.",
                )
            )
        if level == 1 and is_strict_clinical_subsection(text):
            issues.append(
                HeadingIssue(
                    code="clinical_subsection_as_h1",
                    severity="FAIL",
                    message="Clinical subsection-like heading was promoted to H1.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Demote symptoms/signs/complications under their parent section.",
                )
            )
        if region == "body" and is_fragment_heading(block, text):
            issues.append(
                HeadingIssue(
                    code="fragment_heading",
                    severity="WARN",
                    message="Heading looks like a short OCR/layout fragment.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Review neighboring blocks and merge or demote this fragment.",
                )
            )
        if is_actionable_sentence_heading(block, text):
            issues.append(
                HeadingIssue(
                    code="sentence_like_heading",
                    severity="WARN",
                    message="Heading looks like a long sentence or prose fragment.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Review for split_heading or demote_to_paragraph.",
                )
            )
        if (
            region == "body"
            and previous_region == "body"
            and isinstance(level, int)
            and previous_level is not None
            and level - previous_level > 2
            and not re.fullmatch(r"\d{1,4}", text)
        ):
            issues.append(
                HeadingIssue(
                    code="heading_level_jump",
                    severity="INFO",
                    message="Heading level jumps by more than two levels.",
                    page=page,
                    text=text[:180],
                    block_id=block_id,
                    suggestion="Inspect parent path and neighboring headings.",
                )
            )
        if isinstance(level, int):
            previous_level = level
            previous_region = region

    toc_nodes = toc_payload.get("nodes") or []
    for node in toc_nodes:
        key = heading_key(str(node.get("title") or ""))
        if key and key not in heading_keys:
            issues.append(
                HeadingIssue(
                    code="toc_node_unmatched",
                    severity="INFO",
                    message="TOC node was not found as a semantic heading.",
                    page=node.get("source_page"),
                    text=str(node.get("title") or "")[:180],
                    suggestion="This can be normal for front matter, but repeated misses indicate TOC/body mismatch.",
                )
            )
    return issues


def check_decisions(decisions: list[dict[str, Any]]) -> list[HeadingIssue]:
    issues: list[HeadingIssue] = []
    for decision in decisions:
        if decision.get("_invalid_json"):
            issues.append(
                HeadingIssue(
                    code="decision_invalid_json",
                    severity="FAIL",
                    message="Invalid JSON line in heading_decisions.jsonl.",
                    text=str(decision.get("_invalid_json") or "")[:180],
                    suggestion="Rebuild heading decisions.",
                )
            )
            continue
        confidence = float(decision.get("confidence") or 0)
        action = str(decision.get("action") or "")
        if confidence < 0.45:
            issues.append(
                HeadingIssue(
                    code="low_confidence_decision",
                    severity="INFO",
                    message="Heading decision confidence is low.",
                    text=str(decision.get("heading_text") or decision.get("remaining_text") or "")[:180],
                    block_id=str(decision.get("block_id") or ""),
                    suggestion="Review manually or rerun with --heading-strategy hybrid.",
                )
            )
        if action == "split_heading" and not decision.get("remaining_text"):
            issues.append(
                HeadingIssue(
                    code="empty_split_remaining",
                    severity="WARN",
                    message="split_heading decision has no remaining_text.",
                    text=str(decision.get("heading_text") or "")[:180],
                    block_id=str(decision.get("block_id") or ""),
                    suggestion="Use keep_heading unless there is prose after the heading.",
                )
            )
    return issues


def check_section_tree(section_payload: dict[str, Any], blocks: list[dict[str, Any]]) -> list[HeadingIssue]:
    issues: list[HeadingIssue] = []
    if section_payload.get("_missing"):
        return [
            HeadingIssue(
                code="section_tree_missing",
                severity="WARN",
                message="section_tree.json was not found.",
                suggestion="Run mineru-build-structured-blocks to generate the section tree.",
            )
        ]
    if section_payload.get("_invalid"):
        return [
            HeadingIssue(
                code="section_tree_invalid_json",
                severity="FAIL",
                message="section_tree.json could not be parsed as JSON.",
                suggestion="Rebuild semantic blocks and section_tree.json.",
            )
        ]

    nodes = section_payload.get("nodes") or []
    tree_source = str(section_payload.get("source") or "")
    if section_payload.get("node_count") != len(nodes):
        issues.append(
            HeadingIssue(
                code="section_tree_node_count_mismatch",
                severity="FAIL",
                message="section_tree.json node_count does not match the nodes array length.",
                suggestion="Rebuild section_tree.json.",
            )
        )

    block_index_by_id = {str(block.get("block_id") or ""): index for index, block in enumerate(blocks)}
    first_tree_index: int | None = None
    for node in nodes:
        source_block_id = str(node.get("source_block_id") or "")
        block_index = block_index_by_id.get(source_block_id)
        if block_index is not None:
            first_tree_index = block_index if first_tree_index is None else min(first_tree_index, block_index)
    body_headings = []
    for index, block in enumerate(blocks):
        if (
            block.get("block_type") == "heading"
            and block.get("region") == "body"
            and block.get("include_in_semantic") is not False
            and (first_tree_index is None or index >= first_tree_index)
        ):
            body_headings.append(block)
    if body_headings and not nodes:
        issues.append(
            HeadingIssue(
                code="section_tree_empty",
                severity="FAIL",
                message="Body headings exist but section_tree.json has no nodes.",
                suggestion="Inspect section tree construction inputs.",
            )
        )

    ids: set[str] = set()
    duplicate_ids: set[str] = set()
    node_by_id: dict[str, dict[str, Any]] = {}
    block_ids = set(block_index_by_id)
    for node in nodes:
        section_id = str(node.get("section_id") or "")
        if not section_id:
            issues.append(
                HeadingIssue(
                    code="section_tree_missing_id",
                    severity="FAIL",
                    message="Section tree node is missing section_id.",
                    text=str(node.get("title") or "")[:180],
                    suggestion="Rebuild section_tree.json with stable node IDs.",
                )
            )
            continue
        if section_id in ids:
            duplicate_ids.add(section_id)
        ids.add(section_id)
        node_by_id[section_id] = node

    for section_id in sorted(duplicate_ids):
        issues.append(
            HeadingIssue(
                code="section_tree_duplicate_id",
                severity="FAIL",
                message="section_tree.json contains duplicate section_id values.",
                text=section_id,
                suggestion="Regenerate section IDs from document order.",
            )
        )

    for node in nodes:
        section_id = str(node.get("section_id") or "")
        title = str(node.get("title") or "")
        try:
            level = int(node.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        if level < 1 or level > 6:
            issues.append(
                HeadingIssue(
                    code="section_tree_invalid_level",
                    severity="FAIL",
                    message="Section tree node has an invalid level.",
                    page=node.get("start_page"),
                    text=title[:180],
                    block_id=str(node.get("source_block_id") or ""),
                    suggestion="Clamp section tree levels to H1-H6.",
                )
            )
        if str(node.get("region") or "") == "toc":
            issues.append(
                HeadingIssue(
                    code="toc_region_in_section_tree",
                    severity="FAIL",
                    message="TOC-region heading entered the body section tree.",
                    page=node.get("start_page"),
                    text=title[:180],
                    block_id=str(node.get("source_block_id") or ""),
                    suggestion="Exclude TOC region blocks before section tree construction.",
                )
            )
        parent_id = node.get("parent_id")
        if parent_id:
            parent = node_by_id.get(str(parent_id))
            if parent is None:
                issues.append(
                    HeadingIssue(
                        code="section_tree_orphan_node",
                        severity="FAIL",
                        message="Section tree node references a missing parent_id.",
                        page=node.get("start_page"),
                        text=title[:180],
                        block_id=str(node.get("source_block_id") or ""),
                        suggestion="Choose parents from existing earlier section nodes.",
                    )
                )
            else:
                try:
                    parent_level = int(parent.get("level") or 0)
                except (TypeError, ValueError):
                    parent_level = 0
                if parent_level >= level:
                    issues.append(
                        HeadingIssue(
                            code="section_tree_level_order_invalid",
                            severity="FAIL",
                            message="Child section level is not deeper than its parent.",
                            page=node.get("start_page"),
                            text=title[:180],
                            block_id=str(node.get("source_block_id") or ""),
                            suggestion="Pop the section stack until the parent level is shallower.",
                        )
                    )
        start_page = node.get("start_page")
        end_page = node.get("end_page")
        if isinstance(start_page, int) and isinstance(end_page, int) and end_page < start_page:
            issues.append(
                HeadingIssue(
                    code="section_tree_page_range_invalid",
                    severity="FAIL",
                    message="Section tree node has an invalid page range.",
                    page=start_page,
                    text=title[:180],
                    block_id=str(node.get("source_block_id") or ""),
                    suggestion="Recompute section end pages from the next same-or-higher level heading.",
                )
            )
        source_block_id = str(node.get("source_block_id") or "")
        if source_block_id and source_block_id not in block_ids:
            issues.append(
                HeadingIssue(
                    code="section_tree_source_block_missing",
                    severity="FAIL",
                    message="Section tree node points to a block_id not found in structured_blocks.jsonl.",
                    page=node.get("start_page"),
                    text=title[:180],
                    block_id=source_block_id,
                    suggestion="Build the section tree from the same structured block list that is written to disk.",
                )
            )

    for node in nodes:
        section_id = str(node.get("section_id") or "")
        seen: set[str] = set()
        parent_id = str(node.get("parent_id") or "")
        while parent_id:
            if parent_id in seen or parent_id == section_id:
                issues.append(
                    HeadingIssue(
                        code="section_tree_cycle",
                        severity="FAIL",
                        message="Section tree contains a parent cycle.",
                        page=node.get("start_page"),
                        text=str(node.get("title") or "")[:180],
                        block_id=str(node.get("source_block_id") or ""),
                        suggestion="Rebuild parent links from document-order stack state.",
                    )
                )
                break
            seen.add(parent_id)
            parent = node_by_id.get(parent_id)
            if not parent:
                break
            parent_id = str(parent.get("parent_id") or "")

    first_tree_page: int | None = None
    for node in nodes:
        start_page = node.get("start_page")
        if isinstance(start_page, int):
            first_tree_page = start_page if first_tree_page is None else min(first_tree_page, start_page)

    for index, block in enumerate(blocks):
        if block.get("region") != "body" or block.get("include_in_semantic") is False:
            continue
        if first_tree_index is not None and index < first_tree_index:
            continue
        page = block.get("page")
        if first_tree_index is None and isinstance(page, int) and isinstance(first_tree_page, int) and page < first_tree_page:
            continue
        section_id = str(block.get("section_id") or "")
        block_id = str(block.get("block_id") or "")
        if not section_id:
            issues.append(
                HeadingIssue(
                    code="body_block_without_section",
                    severity="FAIL",
                    message="Body block was not attached to a section tree node.",
                    page=block.get("page"),
                    text=str(block.get("text") or "")[:180],
                    block_id=block_id,
                    suggestion="Attach body blocks to the deepest section_tree node covering their block/page range.",
                )
            )
            continue
        if section_id not in ids:
            issues.append(
                HeadingIssue(
                    code="body_block_invalid_section_id",
                    severity="FAIL",
                    message="Body block references a section_id not present in section_tree.json.",
                    page=block.get("page"),
                    text=str(block.get("text") or "")[:180],
                    block_id=block_id,
                    suggestion="Reattach structured blocks after rebuilding section_tree.json.",
                )
            )
        if not block.get("tree_section_path"):
            issues.append(
                HeadingIssue(
                    code="body_block_empty_tree_path",
                    severity="FAIL",
                    message="Body block has a section_id but no tree_section_path.",
                    page=block.get("page"),
                    text=str(block.get("text") or "")[:180],
                    block_id=block_id,
                    suggestion="Copy the matched section node path into tree_section_path.",
                )
            )

    if tree_source == "body_headings":
        section_source_blocks = {str(node.get("source_block_id") or "") for node in nodes}
        for block in body_headings:
            block_id = str(block.get("block_id") or "")
            if block_id and block_id not in section_source_blocks:
                issues.append(
                    HeadingIssue(
                        code="heading_not_in_section_tree",
                        severity="FAIL",
                        message="Body heading was not represented in section_tree.json.",
                        page=block.get("page"),
                        text=str(block.get("text") or "")[:180],
                        block_id=block_id,
                        suggestion="Use all body semantic headings as section tree nodes in Phase 1.",
                    )
                )
    return issues


def check_sibling_consistency(blocks: list[dict[str, Any]]) -> list[HeadingIssue]:
    issues: list[HeadingIssue] = []
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        if block.get("block_type") != "heading":
            continue
        if block.get("region") != "body":
            continue
        path = tuple(str(value) for value in (block.get("section_path") or [])[:-1])
        grouped[path].append(block)
    for parent_path, siblings in grouped.items():
        if len(siblings) < 4:
            continue
        levels = Counter(int(block.get("heading_level") or 0) for block in siblings)
        if len(levels) <= 1:
            continue
        dominant, dominant_count = levels.most_common(1)[0]
        if dominant_count / len(siblings) < 0.75:
            continue
        for block in siblings:
            level = int(block.get("heading_level") or 0)
            if level == dominant or level == 0:
                continue
            issues.append(
                HeadingIssue(
                    code="sibling_level_inconsistent",
                    severity="INFO",
                    message="Sibling heading level differs from the dominant level under the same parent.",
                    page=block.get("page"),
                    text=str(block.get("text") or "")[:180],
                    block_id=str(block.get("block_id") or ""),
                    suggestion=f"Parent path {' > '.join(parent_path) or '(root)'} mostly uses H{dominant}.",
                )
            )
    pattern_grouped: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        if block.get("block_type") != "heading" or block.get("region") != "body":
            continue
        text = normalize_text(str(block.get("text") or ""))
        if is_question_like_heading(text):
            continue
        pattern = ""
        if re.match(r"^第[一二三四五六七八九十百千万\d]+[章篇编部卷]", text):
            pattern = "chapter"
        elif re.match(r"^第[一二三四五六七八九十百千万\d]+节", text):
            pattern = "section"
        elif re.match(r"^[一二三四五六七八九十百]+[、.．]", text):
            pattern = "chinese_numbered"
        elif re.match(r"^[（(][一二三四五六七八九十百\d]+[）)]", text):
            pattern = "paren_numbered"
        elif re.match(r"^\d+(?:\.\d+)*[.．、]", text):
            pattern = "arabic_numbered"
        if not pattern:
            continue
        parent = tuple(str(value) for value in (block.get("section_path") or [])[:-1])
        pattern_grouped[(parent, pattern)].append(block)
    for (parent_path, pattern), siblings in pattern_grouped.items():
        if len(siblings) < 3:
            continue
        levels = Counter(int(block.get("heading_level") or 0) for block in siblings)
        if len(levels) <= 1:
            continue
        dominant, dominant_count = levels.most_common(1)[0]
        if dominant_count / len(siblings) < 0.7:
            continue
        for block in siblings:
            level = int(block.get("heading_level") or 0)
            if level == dominant or level == 0:
                continue
            issues.append(
                HeadingIssue(
                    code="same_pattern_sibling_level_inconsistent",
                    severity="WARN",
                    message="Same-pattern sibling heading level differs from the dominant level.",
                    page=block.get("page"),
                    text=str(block.get("text") or "")[:180],
                    block_id=str(block.get("block_id") or ""),
                    suggestion=(
                        f"Parent path {' > '.join(parent_path) or '(root)'} mostly uses "
                        f"H{dominant} for {pattern} headings."
                    ),
                )
            )
    return issues


def quality_for_output_dir(out_dir: Path) -> tuple[dict[str, Any], list[HeadingIssue]]:
    toc_payload = load_json(out_dir / "toc_tree.json", {"nodes": []})
    section_tree_path = out_dir / "section_tree.json"
    section_payload = load_json(
        section_tree_path,
        {"_invalid": True},
    ) if section_tree_path.exists() else {"_missing": True}
    blocks = load_jsonl(out_dir / "structured_blocks.jsonl")
    decisions = load_jsonl(out_dir / "heading_decisions.jsonl")
    issues: list[HeadingIssue] = []
    issues.extend(check_toc_tree(out_dir, toc_payload))
    issues.extend(check_structured_blocks(blocks, toc_payload))
    issues.extend(check_decisions(decisions))
    issues.extend(check_section_tree(section_payload, blocks))
    issues.extend(check_sibling_consistency(blocks))
    counts = issue_counts(issues)
    status = "OK"
    if counts["WARN"]:
        status = "WARN"
    if counts["FAIL"]:
        status = "FAIL"
    summary = {
        "document": out_dir.name,
        "status": status,
        "toc_node_count": len(toc_payload.get("nodes") or []),
        "section_tree_node_count": len(section_payload.get("nodes") or []),
        "structured_block_count": len(blocks),
        "heading_decision_count": len(decisions),
        "issue_counts": counts,
        "issues": [issue.to_dict() for issue in issues],
    }
    return summary, issues


def write_markdown_report(out_dir: Path, summary: dict[str, Any], issues: list[HeadingIssue]) -> None:
    lines = [
        f"# {summary['document']} 标题质量报告",
        "",
        f"- 状态：{summary['status']}",
        f"- 目录节点：{summary['toc_node_count']}",
        f"- 章节树节点：{summary['section_tree_node_count']}",
        f"- 结构块：{summary['structured_block_count']}",
        f"- 标题决策：{summary['heading_decision_count']}",
        f"- FAIL：{summary['issue_counts']['FAIL']}",
        f"- WARN：{summary['issue_counts']['WARN']}",
        f"- INFO：{summary['issue_counts']['INFO']}",
        "",
        "## 问题",
        "",
    ]
    if not issues:
        lines.append("- 未发现标题质量问题。")
    else:
        for issue in issues[:200]:
            location = f" 第 {issue.page} 页" if issue.page else ""
            text = f"：{issue.text}" if issue.text else ""
            lines.append(f"- [{issue.severity}] `{issue.code}`{location} - {issue.message}{text}")
            if issue.suggestion:
                lines.append(f"  建议：{issue.suggestion}")
    out_dir.joinpath("heading_quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_dir)
    dirs = output_dirs(output_root)
    if args.document:
        dirs = [path for path in dirs if path.name == args.document]
    if not dirs:
        print("No matching output directories found.")
        return 2

    rows: list[dict[str, Any]] = []
    fail_documents = 0
    warn_documents = 0
    for out_dir in dirs:
        summary, issues = quality_for_output_dir(out_dir)
        out_dir.joinpath("heading_quality.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        write_markdown_report(out_dir, summary, issues)
        counts = summary["issue_counts"]
        fail_documents += summary["status"] == "FAIL"
        warn_documents += summary["status"] == "WARN"
        rows.append(
            {
                "document": summary["document"],
                "status": summary["status"],
                "toc_nodes": summary["toc_node_count"],
                "section_tree_nodes": summary["section_tree_node_count"],
                "structured_blocks": summary["structured_block_count"],
                "heading_decisions": summary["heading_decision_count"],
                "fail": counts["FAIL"],
                "warn": counts["WARN"],
                "info": counts["INFO"],
            }
        )
        print(
            f"[{summary['status']}] {summary['document']} | "
            f"FAIL {counts['FAIL']} | WARN {counts['WARN']} | INFO {counts['INFO']}"
        )

    summary_path = output_root / "heading_quality_summary.csv"
    with summary_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Heading quality complete: {len(rows)} documents, {fail_documents} FAIL, {warn_documents} WARN.")
    print(f"Wrote {summary_path}")

    if args.fail_on == "fail" and fail_documents:
        return 1
    if args.fail_on == "warn" and (fail_documents or warn_documents):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
