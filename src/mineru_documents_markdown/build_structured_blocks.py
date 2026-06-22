#!/usr/bin/env python3
"""Build semantic Markdown and structured JSONL blocks from MinerU outputs."""

from __future__ import annotations

import argparse
import json
import re
import os
from pathlib import Path
from typing import Any

from .heading_candidates import block_id as make_block_id
from .heading_candidates import extract_heading_candidates, write_heading_candidates
from .heading_decisions import HeadingDecision, build_rule_decisions, split_heading_text, write_heading_decisions
from .llm_heading_assist import maybe_assist_decisions
from .section_tree import build_section_tree, write_section_tree
from .structure_utils import (
    classify_item_regions,
    classify_page_regions,
    heading_level_from_text,
    heading_key,
    image_caption,
    item_region_key,
    is_probable_body_section,
    is_probable_major_heading,
    is_probable_numbered_subsection,
    item_text,
    load_tasks,
    looks_like_toc_entry,
    markdown_file,
    non_heading_line_like,
    normalize_text,
    output_dirs,
    reference_caption_like,
    repeated_margin_texts,
    table_caption,
    iter_content_items,
)
from .toc_parser import parse_toc_tree, split_stuck_toc_line, toc_level_map, toc_path_map, write_toc_tree


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild semantic blocks from MinerU content lists.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument("--document", help="Only process one output directory by name.")
    parser.add_argument(
        "--semantic-scope",
        choices=("full", "body"),
        default="full",
        help="full preserves front/back matter; body keeps only body-region content.",
    )
    parser.add_argument(
        "--heading-strategy",
        choices=("rule", "llm", "hybrid"),
        default="rule",
        help="rule is local-only; llm sends all candidates; hybrid sends low-confidence candidates.",
    )
    parser.add_argument(
        "--llm-confidence-threshold",
        type=float,
        default=0.72,
        help="Hybrid mode sends rule decisions below this confidence to the LLM.",
    )
    parser.add_argument(
        "--candidate-min-score",
        type=float,
        default=0.18,
        help="Minimum local score for heading candidate extraction.",
    )
    parser.add_argument(
        "--llm-batch-size",
        type=int,
        default=20,
        help="Number of heading candidates per LLM request.",
    )
    parser.add_argument(
        "--heading-review-overrides",
        help="Optional DeepSeek/OpenAI review JSON with block-level heading decisions to apply.",
    )
    return parser.parse_args()


def load_heading_review_overrides(path: Path | None) -> dict[str, dict[str, HeadingDecision]]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Heading review override file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    overrides: dict[str, dict[str, HeadingDecision]] = {}
    for review in payload.get("reviews", []):
        if not isinstance(review, dict):
            continue
        candidate = review.get("candidate") or {}
        deepseek = review.get("deepseek") or {}
        document = str(candidate.get("document") or "")
        block_id = str(candidate.get("block_id") or "")
        if not document or not block_id:
            continue

        action = str(deepseek.get("action") or "keep_heading")
        if action not in {"keep_heading", "promote_to_heading", "demote_to_paragraph", "split_heading"}:
            action = "keep_heading"
        candidate_text = normalize_text(str(candidate.get("text") or ""))
        is_heading = bool(deepseek.get("is_heading", action != "demote_to_paragraph"))
        level = deepseek.get("level")
        if level is not None:
            try:
                level = max(1, min(int(level), 6))
            except (TypeError, ValueError):
                level = None
        confidence = deepseek.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(float(confidence), 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
        review_status = str(deepseek.get("review_status") or "")
        reason = str(deepseek.get("reason") or "Applied heading review override.")
        if review_status:
            reason = f"{review_status}: {reason}"
        heading_text = normalize_text(str(deepseek.get("heading_text") or ""))
        remaining_text = normalize_text(str(deepseek.get("remaining_text") or ""))
        if action == "demote_to_paragraph":
            heading_text = ""
            remaining_text = remaining_text or candidate_text
            is_heading = False
            level = None
        elif action == "keep_heading":
            heading_text = heading_text or candidate_text
            remaining_text = ""
            is_heading = True
        elif action == "split_heading":
            heading_text = heading_text or candidate_text
            is_heading = True

        overrides.setdefault(document, {})[block_id] = HeadingDecision(
            candidate_id=str(candidate.get("candidate_id") or block_id),
            block_id=block_id,
            action=action,
            is_heading=is_heading,
            heading_text=heading_text,
            remaining_text=remaining_text,
            level=level,
            parent_path=[],
            confidence=confidence,
            decision_source="deepseek_warn_review",
            reason=reason,
        )
    return overrides


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


def has_sentence_punctuation(text: str) -> bool:
    return bool(re.search(r"[，,。；;！？!?]", normalize_text(text)))


def strong_sentence_fragment(text: str) -> bool:
    clean = normalize_text(text)
    return len(clean) > 80 or bool(re.search(r"[。；;！？!?]", clean))


def fragment_heading_text(text: str) -> bool:
    clean = normalize_text(text)
    compact = re.sub(r"\s+", "", clean)
    if re.fullmatch(r"[\u4e00-\u9fff]", compact):
        return True
    return bool(len(compact) <= 6 and re.fullmatch(r"(?:[\u4e00-\u9fff]\s+){1,5}[\u4e00-\u9fff]", clean))


def merge_heading_text(left: str, right: str) -> str:
    left = normalize_text(left)
    right = normalize_text(right)
    if not left:
        return right
    if not right:
        return left
    if re.search(r"[\u4e00-\u9fff]$", left) and re.match(r"^[\u4e00-\u9fff]", right):
        return f"{left}{right}"
    return normalize_text(f"{left} {right}")


def close_vertical_neighbors(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_box = first.get("bbox")
    second_box = second.get("bbox")
    if not (isinstance(first_box, list) and isinstance(second_box, list)):
        return True
    if len(first_box) < 4 or len(second_box) < 4:
        return True
    try:
        first_left, first_top, first_right, first_bottom = [float(value) for value in first_box[:4]]
        second_left, second_top, second_right, _ = [float(value) for value in second_box[:4]]
    except (TypeError, ValueError):
        return True
    vertical_gap = second_top - first_bottom
    overlap = min(first_right, second_right) - max(first_left, second_left)
    width = max(first_right - first_left, second_right - second_left, 1.0)
    return -8 <= vertical_gap <= 32 and overlap / width >= 0.2


def can_merge_broken_heading(
    first: dict[str, Any],
    second: dict[str, Any],
    toc_levels: dict[str, int],
) -> tuple[bool, str]:
    first_item = first["item"]
    second_item = second["item"]
    if str(first_item.get("type") or "") not in {"text", "aside_text"}:
        return False, ""
    if str(second_item.get("type") or "") not in {"text", "aside_text"}:
        return False, ""
    if int(first["task_index"]) != int(second["task_index"]):
        return False, ""
    if int(first["absolute_page"]) != int(second["absolute_page"]):
        return False, ""
    first_text = normalize_text(item_text(first_item))
    second_text = normalize_text(item_text(second_item))
    if not first_text or not second_text:
        return False, ""
    if looks_like_toc_entry(first_text) or looks_like_toc_entry(second_text):
        return False, ""
    if has_sentence_punctuation(first_text) or has_sentence_punctuation(second_text):
        return False, ""
    if len(first_text) > 36 or len(second_text) > 42:
        return False, ""
    if not close_vertical_neighbors(first_item, second_item):
        return False, ""
    merged = merge_heading_text(first_text, second_text)
    if len(merged) > 72 or has_sentence_punctuation(merged):
        return False, ""
    if heading_key(merged) in toc_levels or heading_level_from_text(merged, toc_levels) is not None:
        return True, merged
    return False, ""


def merge_broken_heading_items(
    wrapped_items: list[dict[str, Any]],
    toc_levels: dict[str, int],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(wrapped_items):
        current = wrapped_items[index]
        if index + 1 >= len(wrapped_items):
            merged.append(current)
            break
        should_merge, merged_text = can_merge_broken_heading(current, wrapped_items[index + 1], toc_levels)
        if not should_merge:
            merged.append(current)
            index += 1
            continue
        next_item = wrapped_items[index + 1]
        new_wrapped = dict(current)
        new_item = dict(current["item"])
        new_item["text"] = merged_text
        new_item["merged_from_texts"] = [item_text(current["item"]), item_text(next_item["item"])]
        new_wrapped["item"] = new_item
        new_wrapped["merged_from_item_indices"] = [current["item_index"], next_item["item_index"]]
        merged.append(new_wrapped)
        index += 2
    return merged


def classify_block(
    item: dict[str, Any],
    region: str,
    repeated_margins: set[str],
    semantic_scope: str,
    toc_levels: dict[str, int],
) -> tuple[str, int | None, bool, bool, int | None]:
    item_type = str(item.get("type") or "unknown")
    text = normalize_text(item_text(item))
    include_region = semantic_scope == "full" or region == "body"
    if item_type in {"header", "footer", "page_number"} or text in repeated_margins:
        return item_type, None, False, False, None
    if region == "toc":
        return "toc", None, include_region, False, None

    toc_level = toc_levels.get(heading_key(text))
    if non_heading_line_like(text) and item_type in {"text", "aside_text", "list", "image"}:
        return "paragraph", None, include_region, region == "body", toc_level
    if looks_like_toc_entry(text) and item_type in {"text", "aside_text", "list"}:
        return "paragraph", None, include_region, False, toc_level
    if toc_level and item_type in {"text", "aside_text", "image"}:
        return "heading", toc_level, include_region, region == "body", toc_level
    pattern_level = heading_level_from_text(text, toc_levels)
    if pattern_level and item_type in {"text", "aside_text", "image"} and not strong_sentence_fragment(text):
        return "heading", pattern_level, include_region, region == "body", toc_level
    if is_probable_body_section(text) and item_type in {"text", "aside_text", "image"}:
        return "heading", 2, include_region, region == "body", toc_level
    if is_probable_numbered_subsection(text) and item_type in {"text", "aside_text", "image"}:
        return "heading", 3, include_region, region == "body", toc_level

    if item_type == "table":
        return "table", None, include_region, region == "body", toc_level
    if item_type == "image":
        return "image", None, include_region, region == "body", toc_level
    if item_type == "list":
        return "list", None, include_region, region == "body", toc_level
    if item_type in {"page_footnote", "ref_text"}:
        return item_type, None, include_region, region == "body", toc_level

    if item_type in {"text", "aside_text"}:
        if fragment_heading_text(text):
            return "paragraph", None, include_region, region == "body", toc_level
        if re.match(r"^\d+[.．、]\s*\S{2,30}$", text) and not has_sentence_punctuation(text):
            return "heading", 4, include_region, region == "body", toc_level
        if item.get("text_level") or is_probable_major_heading(text):
            return "heading", 1, include_region, region == "body", toc_level
        if re.match(r"^[一二三四五六七八九十]+[、.．]", text) and not strong_sentence_fragment(text):
            return "heading", 3, include_region, region == "body", toc_level
        if re.match(r"^（[一二三四五六七八九十]+）", text) and not strong_sentence_fragment(text):
            return "heading", 3, include_region, region == "body", toc_level
        return "paragraph", None, include_region, region == "body", toc_level
    return item_type, None, include_region, region == "body", toc_level


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


def normalized_lines(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    lines: list[str] = []
    for value in values:
        line = normalize_text(str(value))
        if line:
            lines.append(line)
    return lines


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def apply_decision(
    decision: HeadingDecision | None,
    block_type: str,
    heading_level: int | None,
    content: str,
) -> tuple[str, int | None, str, str, str, float | None, str]:
    if block_type in {"header", "footer", "page_number"}:
        return block_type, heading_level, content, "", "rule_fallback", None, ""
    if block_type == "list" and "\n" in content:
        return block_type, heading_level, content, "", "rule_fallback", None, ""
    if decision is None:
        return block_type, heading_level, content, "", "rule_fallback", None, ""
    if decision.action == "demote_to_paragraph":
        return (
            "paragraph",
            None,
            decision.remaining_text or content,
            "",
            decision.decision_source,
            decision.confidence,
            decision.reason,
        )
    if decision.action in {"keep_heading", "promote_to_heading", "split_heading"} and decision.is_heading:
        return (
            "heading",
            decision.level,
            decision.heading_text or content,
            decision.remaining_text,
            decision.decision_source,
            decision.confidence,
            decision.reason,
        )
    return block_type, heading_level, content, "", decision.decision_source, decision.confidence, decision.reason


def heading_diagnostics(blocks: list[dict[str, Any]], decisions: list[HeadingDecision]) -> dict[str, Any]:
    heading_levels: dict[str, int] = {}
    long_heading_samples: list[dict[str, Any]] = []
    split_count = 0
    demote_count = 0
    promote_count = 0
    low_confidence = 0
    h1_subsection_samples: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("block_type") != "heading":
            continue
        level = block.get("heading_level")
        heading_levels[str(level)] = heading_levels.get(str(level), 0) + 1
        text = str(block.get("text") or "")
        if len(text) > 80 or re.search(r"[，,。；;]", text):
            long_heading_samples.append({"page": block.get("page"), "text": text[:160]})
        compact = re.sub(r"\s+", "", text)
        if level == 1 and re.fullmatch(
            r"[（(]?[一二三四五六七八九十百\d]*[）)]?(症状|体征|常见并发症|症状与体征|临床症状|主要症状)",
            compact,
        ):
            h1_subsection_samples.append({"page": block.get("page"), "text": text[:160]})
    for decision in decisions:
        split_count += decision.action == "split_heading"
        demote_count += decision.action == "demote_to_paragraph"
        promote_count += decision.action == "promote_to_heading"
        low_confidence += decision.confidence < 0.6
    return {
        "heading_levels": heading_levels,
        "decision_count": len(decisions),
        "split_heading_count": split_count,
        "demote_to_paragraph_count": demote_count,
        "promote_to_heading_count": promote_count,
        "low_confidence_decision_count": low_confidence,
        "long_heading_samples": long_heading_samples[:30],
        "h1_subsection_samples": h1_subsection_samples[:30],
    }


def toc_semantic_lines(content: str, toc_levels: dict[str, int]) -> list[str] | None:
    rendered: list[str] = []
    matched = 0
    total = 0
    for raw_line in content.splitlines():
        for line in split_stuck_toc_line(raw_line):
            line = normalize_text(line)
            if not line:
                continue
            total += 1
            level = toc_levels.get(heading_key(line))
            if level:
                rendered.extend(["", f"{'#' * level} {normalize_heading_text(line)}", ""])
                matched += 1
            else:
                rendered.append(line)
    if total and matched / total >= 0.5:
        return toc_plain_lines(content)
    return None


def toc_plain_lines(content: str) -> list[str]:
    rendered: list[str] = []
    for raw_line in content.splitlines():
        for line in split_stuck_toc_line(raw_line):
            line = normalize_text(line)
            if not line:
                continue
            if line == "目录":
                rendered.extend(["", "**目录**", ""])
            else:
                rendered.append(f"- {line}")
    if rendered and rendered[-1] != "":
        rendered.append("")
    return rendered


def front_matter_heading_splits(
    content: str,
    heading_level: int,
    toc_levels: dict[str, int],
) -> list[tuple[str, int]]:
    parts = [normalize_text(part) for part in split_stuck_toc_line(content)]
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        return []
    resolved: list[tuple[str, int]] = []
    matched = 0
    for part in parts:
        level = toc_levels.get(heading_key(part))
        if level:
            matched += 1
        resolved.append((part, level or heading_level))
    if matched / len(parts) < 0.5:
        return []
    return resolved


def build_for_output_dir(
    out_dir: Path,
    semantic_scope: str = "full",
    heading_strategy: str = "rule",
    llm_confidence_threshold: float = 0.72,
    candidate_min_score: float = 0.18,
    llm_batch_size: int = 20,
    heading_review_overrides: dict[str, dict[str, HeadingDecision]] | None = None,
) -> tuple[int, int]:
    tasks = load_tasks(out_dir)
    raw_wrapped_items = list(iter_content_items(out_dir))
    page_regions = classify_page_regions(out_dir)
    repeated_margins = repeated_margin_texts(out_dir)
    toc_nodes = parse_toc_tree(raw_wrapped_items)
    toc_node_dicts = [node.to_dict() for node in toc_nodes]
    toc_levels = toc_level_map(toc_nodes)
    toc_paths = toc_path_map(toc_nodes)
    wrapped_items = merge_broken_heading_items(raw_wrapped_items, toc_levels)
    item_regions = classify_item_regions(wrapped_items)
    candidates = extract_heading_candidates(out_dir.name, wrapped_items, min_score=candidate_min_score)
    candidates = [
        candidate
        for candidate in candidates
        if item_regions.get((candidate.task_index, candidate.page, candidate.item_index)) != "toc"
    ]
    rule_decisions = build_rule_decisions(candidates, toc_levels, toc_paths)
    decisions = maybe_assist_decisions(
        out_dir.name,
        candidates,
        rule_decisions,
        toc_levels,
        toc_paths,
        toc_node_dicts,
        heading_strategy,
        out_dir / ".heading_llm_cache",
        threshold=llm_confidence_threshold,
        batch_size=llm_batch_size,
    )
    document_overrides = (heading_review_overrides or {}).get(out_dir.name, {})
    if document_overrides:
        decision_indexes = {decision.block_id: index for index, decision in enumerate(decisions)}
        for block_id, override in document_overrides.items():
            index = decision_indexes.get(block_id)
            if index is None:
                decisions.append(override)
            else:
                decisions[index] = override
    decisions_by_block = {decision.block_id: decision for decision in decisions}

    write_toc_tree(out_dir / "toc_tree.json", toc_nodes)
    write_heading_candidates(out_dir / "heading_candidates.jsonl", candidates)
    write_heading_decisions(out_dir / "heading_decisions.jsonl", decisions)

    structured_path = out_dir / "structured_blocks.jsonl"
    semantic_path = out_dir / semantic_markdown_name(out_dir)

    current_h1 = ""
    current_h2 = ""
    current_h3 = ""
    current_h4 = ""
    semantic_lines: list[str] = []
    structured_blocks: list[dict[str, Any]] = []
    block_count = 0
    semantic_count = 0

    with structured_path.open("w", encoding="utf-8") as jsonl:
        for wrapped in wrapped_items:
            item = wrapped["item"]
            page = int(wrapped["absolute_page"])
            region = item_regions.get(item_region_key(wrapped), page_regions.get(page, "body"))
            block_type, heading_level, include_in_semantic, recommended_for_rag, toc_level = classify_block(
                item,
                region,
                repeated_margins,
                semantic_scope,
                toc_levels,
            )
            content, extra = block_content(item, int(wrapped["task_index"]))
            original_content = content
            current_block_id = make_block_id(out_dir.name, int(wrapped["task_index"]), page, int(wrapped["item_index"]))
            decision = decisions_by_block.get(current_block_id) if include_in_semantic and region != "toc" else None
            if decision is None and region != "toc" and block_type == "heading":
                split = split_heading_text(content)
                if split:
                    heading_text, remaining = split
                    decision = HeadingDecision(
                        candidate_id=current_block_id,
                        block_id=current_block_id,
                        action="split_heading",
                        is_heading=True,
                        heading_text=heading_text,
                        remaining_text=remaining,
                        level=heading_level,
                        parent_path=[],
                        confidence=0.76,
                        decision_source="rule",
                        reason="Split a heading-like prefix from prose in a MinerU heading block.",
                    )
            (
                block_type,
                heading_level,
                content,
                remaining_text,
                decision_source,
                decision_confidence,
                decision_reason,
            ) = apply_decision(decision, block_type, heading_level, content)
            content_clean = normalize_text(content)
            if not content_clean and block_type not in {"image", "table"}:
                continue

            split_front_heading = (
                front_matter_heading_splits(content_clean, int(heading_level), toc_levels)
                if region == "front_matter" and block_type == "heading" and heading_level
                else []
            )
            if split_front_heading:
                for split_index, (split_content, split_level) in enumerate(split_front_heading, start=1):
                    heading_text = normalize_heading_text(split_content)
                    if split_level == 1:
                        current_h1 = heading_text
                        current_h2 = ""
                        current_h3 = ""
                        current_h4 = ""
                    elif split_level == 2:
                        current_h2 = heading_text
                        current_h3 = ""
                        current_h4 = ""
                    elif split_level == 3:
                        current_h3 = heading_text
                        current_h4 = ""
                    elif split_level == 4:
                        current_h4 = heading_text
                    section_path = [value for value in (current_h1, current_h2, current_h3, current_h4) if value]
                    split_block = {
                        "document": out_dir.name,
                        "block_id": f"{current_block_id}:split{split_index}",
                        "task_index": wrapped["task_index"],
                        "page_range": wrapped["page_range"],
                        "page": page,
                        "local_page_idx": wrapped["local_page_idx"],
                        "source_path": wrapped["source_path"],
                        "region": region,
                        "mineru_type": item.get("type"),
                        "block_type": "heading",
                        "heading_level": split_level,
                        "toc_heading_level": toc_levels.get(heading_key(split_content)),
                        "section_path": section_path,
                        "text": split_content,
                        "original_text": original_content,
                        "merged_from_item_indices": wrapped.get("merged_from_item_indices", []),
                        "remaining_text": "",
                        "bbox": item.get("bbox"),
                        "include_in_semantic": include_in_semantic,
                        "recommended_for_rag": recommended_for_rag,
                        "repair_action": "split_front_matter_heading",
                        "decision_source": "rule",
                        "confidence": max(decision_confidence or 0.0, 0.9),
                        "decision_reason": "Split OCR-stuck front matter heading using TOC entries.",
                        "image_table_candidate": False,
                        **extra,
                    }
                    jsonl.write(json.dumps(split_block, ensure_ascii=False) + "\n")
                    structured_blocks.append(split_block)
                    block_count += 1
                    if include_in_semantic:
                        semantic_lines.extend(["", f"{'#' * split_level} {normalize_heading_text(split_content)}", ""])
                        semantic_count += 1
                continue

            if block_type == "heading" and heading_level:
                heading_text = normalize_heading_text(content_clean)
                if heading_level == 1:
                    current_h1 = heading_text
                    current_h2 = ""
                    current_h3 = ""
                    current_h4 = ""
                elif heading_level == 2:
                    current_h2 = heading_text
                    current_h3 = ""
                    current_h4 = ""
                elif heading_level == 3:
                    current_h3 = heading_text
                    current_h4 = ""
                elif heading_level == 4:
                    current_h4 = heading_text

            section_path = [value for value in (current_h1, current_h2, current_h3, current_h4) if value]
            image_table_candidate = False
            if block_type == "image":
                probe = " ".join(str(value) for value in (content_clean, extra.get("image_caption", "")))
                image_table_candidate = any(keyword in probe for keyword in ("表", "项目", "剂量", "诊断", "评分"))

            block = {
                "document": out_dir.name,
                "block_id": current_block_id,
                "task_index": wrapped["task_index"],
                "page_range": wrapped["page_range"],
                "page": page,
                "local_page_idx": wrapped["local_page_idx"],
                "source_path": wrapped["source_path"],
                "region": region,
                "mineru_type": item.get("type"),
                "block_type": block_type,
                "heading_level": heading_level,
                "toc_heading_level": toc_level,
                "section_path": section_path,
                "text": content,
                "original_text": original_content,
                "merged_from_item_indices": wrapped.get("merged_from_item_indices", []),
                "remaining_text": remaining_text,
                "bbox": item.get("bbox"),
                "include_in_semantic": include_in_semantic,
                "recommended_for_rag": recommended_for_rag,
                "repair_action": decision.action if decision else "none",
                "decision_source": decision_source,
                "confidence": decision_confidence,
                "decision_reason": decision_reason,
                "image_table_candidate": image_table_candidate,
                **extra,
            }
            jsonl.write(json.dumps(block, ensure_ascii=False) + "\n")
            structured_blocks.append(block)
            block_count += 1

            if not include_in_semantic:
                continue
            if block_type == "toc":
                semantic_lines.extend(toc_plain_lines(content))
            elif block_type == "heading" and heading_level:
                toc_lines = toc_semantic_lines(content, toc_levels) if region == "front_matter" else None
                if toc_lines is not None:
                    semantic_lines.extend(toc_lines)
                else:
                    semantic_lines.extend(["", f"{'#' * heading_level} {normalize_heading_text(content_clean)}", ""])
                if remaining_text:
                    semantic_lines.extend([remaining_text.strip(), ""])
            elif block_type == "table":
                caption = extra.get("table_caption")
                if caption:
                    semantic_lines.extend(["", f"**表：{caption}**", ""])
                semantic_lines.extend(["", content.strip(), ""])
                for footnote in normalized_lines(extra.get("table_footnote")):
                    semantic_lines.append(footnote)
                if extra.get("table_footnote"):
                    semantic_lines.append("")
            elif block_type == "image":
                image_path = extra.get("image_path")
                caption = extra.get("image_caption")
                if caption:
                    semantic_lines.extend(["", f"**图：{caption}**"])
                if image_path:
                    semantic_lines.append(f"![]({image_path})")
                if content_clean:
                    semantic_lines.append(content_clean)
                for footnote in normalized_lines(extra.get("image_footnote")):
                    semantic_lines.append(footnote)
                semantic_lines.append("")
            elif block_type == "list":
                toc_lines = toc_semantic_lines(content, toc_levels) if region == "front_matter" else None
                if toc_lines is not None:
                    semantic_lines.extend(toc_lines)
                else:
                    for line in content.splitlines():
                        line = line.strip()
                        if line:
                            semantic_lines.append(f"- {line}")
                    semantic_lines.append("")
            else:
                toc_lines = toc_semantic_lines(content, toc_levels) if region == "front_matter" else None
                if toc_lines is not None:
                    semantic_lines.extend(toc_lines)
                else:
                    semantic_lines.extend([content.strip(), ""])
            semantic_count += 1

    section_tree_payload = build_section_tree(out_dir.name, structured_blocks, toc_node_dicts)
    write_section_tree(out_dir / "section_tree.json", section_tree_payload)

    diagnostics = heading_diagnostics(structured_blocks, decisions)
    diagnostics["toc_node_count"] = len(toc_nodes)
    diagnostics["section_tree_node_count"] = section_tree_payload["node_count"]
    diagnostics["heading_candidate_count"] = len(candidates)
    diagnostics["heading_strategy"] = heading_strategy
    (out_dir / "heading_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    semantic_text = "\n".join(semantic_lines)
    semantic_text = re.sub(r"\n{3,}", "\n\n", semantic_text).strip() + "\n"
    semantic_path.write_text(semantic_text, encoding="utf-8")
    return block_count, semantic_count


def main() -> int:
    load_dotenv()
    args = parse_args()
    heading_review_overrides = load_heading_review_overrides(
        Path(args.heading_review_overrides) if args.heading_review_overrides else None
    )
    dirs = output_dirs(Path(args.output_dir))
    if args.document:
        dirs = [path for path in dirs if path.name == args.document]
    if not dirs:
        print("No matching output directories found.")
        return 2

    total_blocks = 0
    total_semantic = 0
    for out_dir in dirs:
        blocks, semantic_blocks = build_for_output_dir(
            out_dir,
            args.semantic_scope,
            args.heading_strategy,
            args.llm_confidence_threshold,
            args.candidate_min_score,
            args.llm_batch_size,
            heading_review_overrides,
        )
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
