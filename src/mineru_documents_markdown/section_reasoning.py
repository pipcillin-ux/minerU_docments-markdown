#!/usr/bin/env python3
"""Collect and optionally review section-tree reasoning candidates."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .build_structured_blocks import render_semantic_markdown
from .llm_heading_assist import api_config, cache_key, call_chat_completions
from .section_tree import attach_section_tree, clamp_level, end_block_for_range, toc_level_map, write_section_tree
from .structure_utils import heading_key, normalize_text, output_dirs


ALLOWED_ACTIONS = {
    "keep",
    "insert_child_section",
    "reparent_block",
    "merge_with_previous_section",
    "demote_to_paragraph",
    "uncertain",
}

SYSTEM_PROMPT = """You review local section-tree structure for a PDF-to-Markdown pipeline.
Return strict JSON only. Never rewrite source text. Use only the allowed action schema.
Prefer uncertain when evidence is insufficient."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect or review section-tree reasoning candidates.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument("--document", action="append", help="Only process this output document name.")
    parser.add_argument("--mode", choices=("collect", "report", "review", "apply"), default="collect")
    parser.add_argument("--limit", type=int, help="Global candidate/review limit.")
    parser.add_argument("--max-per-document", type=int, default=40)
    parser.add_argument("--max-per-type", type=int, default=12)
    parser.add_argument("--context-radius", type=int, default=3)
    parser.add_argument("--low-confidence-threshold", type=float, default=0.72)
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.82,
        help="Minimum LLM confidence required for apply mode.",
    )
    parser.add_argument("--force", action="store_true", help="Ignore cached LLM review responses.")
    return parser.parse_args()


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
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def short_text(value: Any, limit: int = 260) -> str:
    text = normalize_text(str(value or ""))
    return text[:limit]


def block_summary(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "page": block.get("page"),
        "block_type": block.get("block_type"),
        "heading_level": block.get("heading_level"),
        "tree_heading_level": block.get("tree_heading_level"),
        "section_id": block.get("section_id"),
        "tree_section_path": block.get("tree_section_path") or [],
        "text": short_text(block.get("text")),
    }


def section_summary(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "section_id": str(node.get("section_id") or ""),
        "title": short_text(node.get("title")),
        "level": node.get("level"),
        "path": node.get("path") or [],
        "start_page": node.get("start_page"),
        "end_page": node.get("end_page"),
        "source_block_id": str(node.get("source_block_id") or ""),
        "confidence": node.get("confidence"),
        "evidence": node.get("evidence") or [],
    }


def nearby_blocks(blocks: list[dict[str, Any]], index: int, radius: int) -> list[dict[str, Any]]:
    start = max(0, index - radius)
    end = min(len(blocks), index + radius + 1)
    return [block_summary(block) for block in blocks[start:end]]


def nearby_sections(nodes: list[dict[str, Any]], node_index: int | None, radius: int = 2) -> list[dict[str, Any]]:
    if node_index is None:
        return []
    start = max(0, node_index - radius)
    end = min(len(nodes), node_index + radius + 1)
    return [section_summary(node) for node in nodes[start:end]]


def toc_context(toc_nodes: list[dict[str, Any]], title_key: str, limit: int = 12) -> list[dict[str, Any]]:
    if not toc_nodes:
        return []
    matches = [
        node
        for node in toc_nodes
        if str(node.get("normalized_key") or heading_key(str(node.get("title") or ""))) == title_key
    ]
    if matches:
        selected = matches[:limit]
    else:
        selected = toc_nodes[:limit]
    return [
        {
            "title": short_text(node.get("title")),
            "level": node.get("level"),
            "path": node.get("path") or [],
            "page_hint": node.get("page_hint"),
            "document_order": node.get("document_order"),
        }
        for node in selected
    ]


def candidate_id(document: str, candidate_type: str, anchor: str) -> str:
    return f"{document}:{candidate_type}:{anchor}"


def add_candidate(
    candidates: list[dict[str, Any]],
    type_counts: Counter[str],
    seen: set[str],
    candidate: dict[str, Any],
    max_per_document: int,
    max_per_type: int,
) -> None:
    if len(candidates) >= max_per_document:
        return
    candidate_type = str(candidate.get("candidate_type") or "")
    if type_counts[candidate_type] >= max_per_type:
        return
    cid = str(candidate.get("candidate_id") or "")
    if not cid or cid in seen:
        return
    seen.add(cid)
    candidates.append(candidate)
    type_counts[candidate_type] += 1


def make_block_candidate(
    document: str,
    candidate_type: str,
    block: dict[str, Any],
    blocks: list[dict[str, Any]],
    block_index: int,
    section: dict[str, Any] | None,
    section_index: int | None,
    nodes: list[dict[str, Any]],
    toc_nodes: list[dict[str, Any]],
    context_radius: int,
    signals: list[str],
) -> dict[str, Any]:
    block_id = str(block.get("block_id") or f"index_{block_index}")
    section_path = block.get("tree_section_path") or []
    title_key = heading_key(str(block.get("text") or ""))
    return {
        "candidate_id": candidate_id(document, candidate_type, block_id),
        "document": document,
        "candidate_type": candidate_type,
        "fallback_action": "keep",
        "signals": signals,
        "current_block": block_summary(block),
        "current_section": section_summary(section or {}),
        "nearby_blocks": nearby_blocks(blocks, block_index, context_radius),
        "nearby_sections": nearby_sections(nodes, section_index),
        "toc_context": toc_context(toc_nodes, title_key or heading_key(" ".join(str(value) for value in section_path))),
        "question": section_question(candidate_type),
    }


def make_section_candidate(
    document: str,
    candidate_type: str,
    node: dict[str, Any],
    nodes: list[dict[str, Any]],
    node_index: int,
    blocks_by_id: dict[str, dict[str, Any]],
    toc_nodes: list[dict[str, Any]],
    signals: list[str],
) -> dict[str, Any]:
    section_id = str(node.get("section_id") or f"node_{node_index}")
    source_block = blocks_by_id.get(str(node.get("source_block_id") or ""), {})
    return {
        "candidate_id": candidate_id(document, candidate_type, section_id),
        "document": document,
        "candidate_type": candidate_type,
        "fallback_action": "keep",
        "signals": signals,
        "current_block": block_summary(source_block),
        "current_section": section_summary(node),
        "nearby_blocks": [],
        "nearby_sections": nearby_sections(nodes, node_index),
        "toc_context": toc_context(toc_nodes, str(node.get("normalized_key") or heading_key(str(node.get("title") or "")))),
        "question": section_question(candidate_type),
    }


def section_question(candidate_type: str) -> str:
    questions = {
        "local_heading_under_tree_node": "Should this local body heading become a child section under the current section?",
        "toc_node_unanchored_or_weak": "Is this TOC-backed section correctly anchored to the body text?",
        "repeated_title_boundary": "Does this repeated title belong under the current parent section?",
        "cross_page_boundary": "Does the nearby text belong to this section boundary?",
        "low_confidence_tree_node": "Is this low-confidence section node structurally valid?",
    }
    return questions.get(candidate_type, "Review this local section structure.")


def collect_candidates_for_document(
    out_dir: Path,
    *,
    context_radius: int,
    max_per_document: int,
    max_per_type: int,
    low_confidence_threshold: float,
) -> list[dict[str, Any]]:
    document = out_dir.name
    blocks = load_jsonl(out_dir / "structured_blocks.jsonl")
    section_payload = load_json(out_dir / "section_tree.json", {"nodes": []})
    toc_payload = load_json(out_dir / "toc_tree.json", {"nodes": []})
    if not blocks:
        return []

    nodes = list(section_payload.get("nodes") or [])
    toc_nodes = list(toc_payload.get("nodes") or [])
    node_by_id = {str(node.get("section_id") or ""): node for node in nodes}
    node_index_by_id = {str(node.get("section_id") or ""): index for index, node in enumerate(nodes)}
    blocks_by_id = {str(block.get("block_id") or ""): block for block in blocks}
    title_counts = Counter(str(node.get("normalized_key") or heading_key(str(node.get("title") or ""))) for node in nodes)
    candidates: list[dict[str, Any]] = []
    type_counts: Counter[str] = Counter()
    seen: set[str] = set()

    for index, block in enumerate(blocks):
        if block.get("region") != "body" or block.get("block_type") != "heading":
            continue
        if block.get("include_in_semantic") is False:
            continue
        path = block.get("tree_section_path") or []
        text_key = heading_key(str(block.get("text") or ""))
        path_key = heading_key(str(path[-1])) if path else ""
        if text_key and path_key and text_key != path_key:
            section_id = str(block.get("section_id") or "")
            add_candidate(
                candidates,
                type_counts,
                seen,
                make_block_candidate(
                    document,
                    "local_heading_under_tree_node",
                    block,
                    blocks,
                    index,
                    node_by_id.get(section_id),
                    node_index_by_id.get(section_id),
                    nodes,
                    toc_nodes,
                    context_radius,
                    ["body_heading", "text_differs_from_assigned_section_title"],
                ),
                max_per_document,
                max_per_type,
            )

    for node_index, node in enumerate(nodes):
        title_key = str(node.get("normalized_key") or heading_key(str(node.get("title") or "")))
        source_block = blocks_by_id.get(str(node.get("source_block_id") or ""))
        source_key = heading_key(str(source_block.get("text") or "")) if source_block else ""
        signals: list[str] = []
        if not source_block:
            signals.append("missing_source_block")
        elif title_key and source_key and title_key != source_key:
            signals.append("source_text_mismatch")
        if signals:
            add_candidate(
                candidates,
                type_counts,
                seen,
                make_section_candidate(
                    document,
                    "toc_node_unanchored_or_weak",
                    node,
                    nodes,
                    node_index,
                    blocks_by_id,
                    toc_nodes,
                    signals,
                ),
                max_per_document,
                max_per_type,
            )
        if title_key and title_counts[title_key] >= 3:
            add_candidate(
                candidates,
                type_counts,
                seen,
                make_section_candidate(
                    document,
                    "repeated_title_boundary",
                    node,
                    nodes,
                    node_index,
                    blocks_by_id,
                    toc_nodes,
                    [f"repeated_title_count:{title_counts[title_key]}"],
                ),
                max_per_document,
                max_per_type,
            )
        start_page = node.get("start_page")
        end_page = node.get("end_page")
        if isinstance(start_page, int) and isinstance(end_page, int) and end_page > start_page:
            add_candidate(
                candidates,
                type_counts,
                seen,
                make_section_candidate(
                    document,
                    "cross_page_boundary",
                    node,
                    nodes,
                    node_index,
                    blocks_by_id,
                    toc_nodes,
                    [f"page_span:{start_page}-{end_page}"],
                ),
                max_per_document,
                max_per_type,
            )
        try:
            confidence = float(node.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence and confidence < low_confidence_threshold:
            add_candidate(
                candidates,
                type_counts,
                seen,
                make_section_candidate(
                    document,
                    "low_confidence_tree_node",
                    node,
                    nodes,
                    node_index,
                    blocks_by_id,
                    toc_nodes,
                    [f"confidence:{confidence:.4f}"],
                ),
                max_per_document,
                max_per_type,
            )

    return candidates


def candidate_path(out_dir: Path) -> Path:
    return out_dir / "section_reasoning_candidates.jsonl"


def decision_path(out_dir: Path) -> Path:
    return out_dir / "section_reasoning_decisions.jsonl"


def report_path(out_dir: Path) -> Path:
    return out_dir / "section_reasoning_report.md"


def apply_report_path(out_dir: Path) -> Path:
    return out_dir / "section_reasoning_apply_report.md"


def reasoned_section_tree_path(out_dir: Path) -> Path:
    return out_dir / "section_tree.reasoned.json"


def reasoned_structured_blocks_path(out_dir: Path) -> Path:
    return out_dir / "structured_blocks.reasoned.jsonl"


def reasoned_semantic_path(out_dir: Path) -> Path:
    preferred = out_dir / f"{out_dir.name}.semantic.md"
    if preferred.exists():
        name = preferred.name
    else:
        matches = sorted(path.name for path in out_dir.glob("*.semantic.md"))
        name = matches[0] if matches else f"{out_dir.name}.semantic.md"
    if name.endswith(".semantic.md"):
        name = f"{name[: -len('.semantic.md')]}.semantic.reasoned.md"
    else:
        name = f"{out_dir.name}.semantic.reasoned.md"
    return out_dir / name


def collect_mode(args: argparse.Namespace) -> list[Path]:
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    written: list[Path] = []
    total = 0
    for out_dir in output_dirs(root):
        if document_names and out_dir.name not in document_names:
            continue
        if args.limit is not None and total >= args.limit:
            break
        remaining = None if args.limit is None else max(0, args.limit - total)
        candidates = collect_candidates_for_document(
            out_dir,
            context_radius=max(0, args.context_radius),
            max_per_document=max(1, args.max_per_document),
            max_per_type=max(1, args.max_per_type),
            low_confidence_threshold=args.low_confidence_threshold,
        )
        if remaining is not None:
            candidates = candidates[:remaining]
        write_jsonl(candidate_path(out_dir), candidates)
        write_report(out_dir, candidates, load_jsonl(decision_path(out_dir)))
        written.append(candidate_path(out_dir))
        total += len(candidates)
        print(f"[OK] {out_dir.name} | section_reasoning_candidates {len(candidates)}")
    print(f"Section reasoning collect complete: {total} candidate(s).")
    return written


def load_all_candidates(root: Path, document_names: set[str] | None, limit: int | None = None) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    for out_dir in output_dirs(root):
        if document_names and out_dir.name not in document_names:
            continue
        for candidate in load_jsonl(candidate_path(out_dir)):
            rows.append((out_dir, candidate))
            if limit is not None and len(rows) >= limit:
                return rows
    return rows


def review_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "Review one local section-tree reasoning candidate.",
        "candidate": candidate,
        "instructions": [
            "Choose the best allowed action for this local structure issue.",
            "Use insert_child_section only when the block is clearly a real subsection under current_section.",
            "Use reparent_block only when the current block belongs under a different nearby section.",
            "Use demote_to_paragraph when the current block is not a heading/section cue.",
            "Use keep when the existing section tree assignment is acceptable.",
            "Use uncertain when local evidence is insufficient.",
            "Never rewrite source text.",
        ],
        "schema": {
            "candidate_id": "string",
            "action": "keep | insert_child_section | reparent_block | merge_with_previous_section | demote_to_paragraph | uncertain",
            "target_parent_id": "string or empty",
            "title": "string or empty",
            "level": "integer 1-6 or null",
            "confidence": "number 0-1",
            "reason": "short explanation",
        },
    }


def unwrap_decision(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("decision"), dict):
        return data["decision"]
    decisions = data.get("decisions")
    if isinstance(decisions, list) and decisions and isinstance(decisions[0], dict):
        return decisions[0]
    return data


def clamp_confidence(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def normalize_decision(data: dict[str, Any], candidate: dict[str, Any], input_hash: str) -> dict[str, Any]:
    raw = unwrap_decision(data)
    action = str(raw.get("action") or "uncertain")
    if action not in ALLOWED_ACTIONS:
        action = "uncertain"
    level = raw.get("level")
    if level is not None:
        try:
            level = max(1, min(int(level), 6))
        except (TypeError, ValueError):
            level = None
    candidate_id_value = str(raw.get("candidate_id") or candidate.get("candidate_id") or "")
    if candidate_id_value != str(candidate.get("candidate_id") or ""):
        action = "uncertain"
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "document": str(candidate.get("document") or ""),
        "candidate_type": str(candidate.get("candidate_type") or ""),
        "decision_source": "llm_section_reasoning",
        "input_hash": input_hash,
        "action": action,
        "target_parent_id": str(raw.get("target_parent_id") or ""),
        "title": short_text(raw.get("title")),
        "level": level,
        "confidence": clamp_confidence(raw.get("confidence")),
        "reason": str(raw.get("reason") or "LLM section reasoning decision."),
        "fallback_action": str(candidate.get("fallback_action") or "keep"),
    }


def uncertain_decision(candidate: dict[str, Any], input_hash: str, reason: str) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "document": str(candidate.get("document") or ""),
        "candidate_type": str(candidate.get("candidate_type") or ""),
        "decision_source": "llm_section_reasoning_fallback",
        "input_hash": input_hash,
        "action": "uncertain",
        "target_parent_id": "",
        "title": "",
        "level": None,
        "confidence": 0.0,
        "reason": reason,
        "fallback_action": str(candidate.get("fallback_action") or "keep"),
    }


def review_candidate(out_dir: Path, candidate: dict[str, Any], force: bool) -> dict[str, Any]:
    payload = review_payload(candidate)
    input_hash = cache_key(payload)
    cache_dir = out_dir / ".section_reasoning_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{input_hash}.json"
    try:
        if path.exists() and not force:
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = call_chat_completions(payload, system_prompt=SYSTEM_PROMPT)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return normalize_decision(data, candidate, input_hash)
    except Exception as exc:  # noqa: BLE001 - review failures must fall back to audited uncertainty.
        return uncertain_decision(candidate, input_hash, f"LLM review failed: {exc}")


def review_mode(args: argparse.Namespace) -> int:
    load_dotenv()
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    candidates = load_all_candidates(root, document_names, args.limit)
    if not candidates:
        collect_mode(args)
        candidates = load_all_candidates(root, document_names, args.limit)
    api_key, _, _ = api_config()
    if candidates and not api_key:
        print("DEEPSEEK_API_KEY or OPENAI_API_KEY is required for section reasoning review.")
        return 2

    by_dir: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    for index, (out_dir, candidate) in enumerate(candidates, start=1):
        print(f"[{index}/{len(candidates)}] Reviewing {candidate.get('document')} {candidate.get('candidate_type')}")
        by_dir[out_dir].append(review_candidate(out_dir, candidate, args.force))

    for out_dir, decisions in by_dir.items():
        write_jsonl(decision_path(out_dir), decisions)
        write_report(out_dir, load_jsonl(candidate_path(out_dir)), decisions)
        print(f"[OK] {out_dir.name} | section_reasoning_decisions {len(decisions)}")
    print(f"Section reasoning review complete: {sum(len(value) for value in by_dir.values())} decision(s).")
    return 0


def report_mode(args: argparse.Namespace) -> int:
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    total_candidates = 0
    total_decisions = 0
    for out_dir in output_dirs(root):
        if document_names and out_dir.name not in document_names:
            continue
        candidates = load_jsonl(candidate_path(out_dir))
        decisions = load_jsonl(decision_path(out_dir))
        write_report(out_dir, candidates, decisions)
        total_candidates += len(candidates)
        total_decisions += len(decisions)
        print(f"[OK] {out_dir.name} | candidates {len(candidates)} | decisions {len(decisions)}")
    print(f"Section reasoning reports complete: {total_candidates} candidate(s), {total_decisions} decision(s).")
    return 0


def block_index_by_id(blocks: list[dict[str, Any]]) -> dict[str, int]:
    return {str(block.get("block_id") or ""): index for index, block in enumerate(blocks)}


def block_page(block: dict[str, Any]) -> int | None:
    try:
        page = int(block.get("page"))
    except (TypeError, ValueError):
        return None
    return page if page >= 0 else None


def next_reasoned_section_id(existing: set[str], counter: int) -> tuple[str, int]:
    while True:
        section_id = f"llm_sec_{counter:06d}"
        counter += 1
        if section_id not in existing:
            existing.add(section_id)
            return section_id, counter


def normalized_node_copy(node: dict[str, Any]) -> dict[str, Any]:
    copied = dict(node)
    copied["parent_path"] = list(node.get("parent_path") or [])
    copied["path"] = list(node.get("path") or [])
    copied["evidence"] = list(node.get("evidence") or [])
    return copied


def make_inserted_node(
    *,
    section_id: str,
    parent: dict[str, Any],
    block: dict[str, Any],
    title: str,
    level: int,
    decision: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    parent_path = list(parent.get("path") or [])
    block_id = str(block.get("block_id") or "")
    confidence = clamp_confidence(decision.get("confidence"))
    evidence = [
        "llm_section_reasoning",
        "action:insert_child_section",
        f"candidate:{candidate.get('candidate_id')}",
    ]
    return {
        "section_id": section_id,
        "title": title,
        "normalized_key": heading_key(title),
        "level": level,
        "parent_id": str(parent.get("section_id") or ""),
        "parent_path": parent_path,
        "path": [*parent_path, title],
        "document_order": 0,
        "start_page": block_page(block),
        "end_page": block_page(block),
        "start_block_id": block_id,
        "end_block_id": block_id,
        "source_block_id": block_id,
        "source_heading_level": clamp_level(block.get("heading_level")),
        "toc_heading_level": clamp_level(block.get("toc_heading_level")),
        "region": str(block.get("region") or ""),
        "confidence": round(confidence, 4),
        "evidence": evidence,
        "reasoning_candidate_id": str(candidate.get("candidate_id") or ""),
        "reasoning_action": "insert_child_section",
        "reasoning_source": str(decision.get("decision_source") or "llm_section_reasoning"),
        "reasoning_reason": str(decision.get("reason") or ""),
    }


def node_start_index(node: dict[str, Any], indexes: dict[str, int]) -> int | None:
    start = indexes.get(str(node.get("start_block_id") or ""))
    if start is None:
        start = indexes.get(str(node.get("source_block_id") or ""))
    return start


def recompute_reasoned_ranges(nodes: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexes = block_index_by_id(blocks)
    original_order = {id(node): index for index, node in enumerate(nodes)}

    def sort_key(node: dict[str, Any]) -> tuple[int, int, int]:
        start = node_start_index(node, indexes)
        if start is None:
            start = len(blocks) + int(node.get("document_order") or 0)
        return (start, int(node.get("level") or 99), original_order[id(node)])

    sorted_nodes = sorted(nodes, key=sort_key)
    start_indexes = [node_start_index(node, indexes) for node in sorted_nodes]

    for order, node in enumerate(sorted_nodes, start=1):
        node["document_order"] = order

    for index, node in enumerate(sorted_nodes):
        start_index = start_indexes[index]
        if start_index is None:
            continue
        level = clamp_level(node.get("level")) or 6
        end_index = len(blocks) - 1
        for next_index in range(index + 1, len(sorted_nodes)):
            next_start_index = start_indexes[next_index]
            if next_start_index is None or next_start_index <= start_index:
                continue
            next_level = clamp_level(sorted_nodes[next_index].get("level")) or 6
            if next_level <= level:
                end_index = max(start_index, next_start_index - 1)
                break
        end_block = end_block_for_range(blocks, start_index, end_index)
        node["end_block_id"] = str(end_block.get("block_id") or node.get("start_block_id") or "")
        node["end_page"] = block_page(end_block) or node.get("start_page")

    return sorted_nodes


def apply_decisions_for_document(
    out_dir: Path,
    *,
    min_confidence: float,
) -> dict[str, Any]:
    blocks = load_jsonl(out_dir / "structured_blocks.jsonl")
    section_payload = load_json(out_dir / "section_tree.json", {"nodes": []})
    toc_payload = load_json(out_dir / "toc_tree.json", {"nodes": []})
    candidates = load_jsonl(candidate_path(out_dir))
    decisions = load_jsonl(decision_path(out_dir))

    if not blocks:
        return {"status": "skipped", "reason": "structured_blocks.jsonl is missing or empty.", "applied": []}

    original_nodes = [normalized_node_copy(node) for node in section_payload.get("nodes") or []]
    node_by_id = {str(node.get("section_id") or ""): node for node in original_nodes}
    candidates_by_id = {str(candidate.get("candidate_id") or ""): candidate for candidate in candidates}
    blocks_by_id = {str(block.get("block_id") or ""): block for block in blocks}
    existing_ids = set(node_by_id)
    inserted_nodes: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    next_id_counter = 1

    for decision in decisions:
        candidate_id_value = str(decision.get("candidate_id") or "")
        action = str(decision.get("action") or "")
        confidence = clamp_confidence(decision.get("confidence"))
        if action != "insert_child_section":
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "unsupported_action"})
            continue
        if confidence < min_confidence:
            rejected.append(
                {
                    "candidate_id": candidate_id_value,
                    "action": action,
                    "confidence": confidence,
                    "reason": "below_min_confidence",
                }
            )
            continue

        candidate = candidates_by_id.get(candidate_id_value)
        if candidate is None:
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "missing_candidate"})
            continue
        target_parent_id = str(decision.get("target_parent_id") or "")
        parent = node_by_id.get(target_parent_id)
        if parent is None:
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "missing_target_parent"})
            continue
        parent_level = clamp_level(parent.get("level"))
        if parent_level is None or parent_level >= 6:
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "invalid_parent_level"})
            continue
        current_block = candidate.get("current_block") or {}
        block = blocks_by_id.get(str(current_block.get("block_id") or ""))
        if block is None:
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "missing_source_block"})
            continue
        if block.get("region") != "body" or block.get("include_in_semantic") is False:
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "non_body_or_excluded_block"})
            continue

        title = short_text(decision.get("title")) or short_text(block.get("text"))
        if not title:
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "empty_title"})
            continue

        level = clamp_level(decision.get("level")) or parent_level + 1
        if level <= parent_level:
            level = parent_level + 1
        level = min(level, 6)
        duplicate = any(
            str(node.get("parent_id") or "") == target_parent_id
            and str(node.get("source_block_id") or "") == str(block.get("block_id") or "")
            and str(node.get("normalized_key") or heading_key(str(node.get("title") or ""))) == heading_key(title)
            for node in [*original_nodes, *inserted_nodes]
        )
        if duplicate:
            rejected.append({"candidate_id": candidate_id_value, "action": action, "reason": "duplicate_child"})
            continue

        section_id, next_id_counter = next_reasoned_section_id(existing_ids, next_id_counter)
        inserted = make_inserted_node(
            section_id=section_id,
            parent=parent,
            block=block,
            title=title,
            level=level,
            decision=decision,
            candidate=candidate,
        )
        inserted_nodes.append(inserted)
        node_by_id[section_id] = inserted
        applied.append(
            {
                "candidate_id": candidate_id_value,
                "section_id": section_id,
                "parent_id": target_parent_id,
                "title": title,
                "level": level,
                "confidence": confidence,
            }
        )

    reasoned_nodes = recompute_reasoned_ranges([*original_nodes, *inserted_nodes], blocks)
    reasoned_payload = {
        **section_payload,
        "document": str(section_payload.get("document") or out_dir.name),
        "version": section_payload.get("version"),
        "source": f"{section_payload.get('source') or 'section_tree'}+llm_reasoned",
        "reasoning_version": 1,
        "reasoning_source": "llm_section_reasoning_sidecar",
        "reasoning_min_confidence": min_confidence,
        "applied_decision_count": len(applied),
        "node_count": len(reasoned_nodes),
        "nodes": reasoned_nodes,
    }

    reasoned_blocks = attach_section_tree(blocks, reasoned_payload)
    toc_levels = toc_level_map(list(toc_payload.get("nodes") or []))
    semantic_text, semantic_count = render_semantic_markdown(reasoned_blocks, toc_levels)

    write_section_tree(reasoned_section_tree_path(out_dir), reasoned_payload)
    write_jsonl(reasoned_structured_blocks_path(out_dir), reasoned_blocks)
    reasoned_semantic_path(out_dir).write_text(semantic_text, encoding="utf-8")

    return {
        "status": "ok",
        "applied": applied,
        "rejected": rejected,
        "semantic_count": semantic_count,
        "outputs": [
            reasoned_section_tree_path(out_dir).name,
            reasoned_structured_blocks_path(out_dir).name,
            reasoned_semantic_path(out_dir).name,
        ],
    }


def write_apply_report(out_dir: Path, result: dict[str, Any]) -> None:
    lines = [
        f"# {out_dir.name} 章节推理应用报告",
        "",
        f"- Generated at: {datetime.now(UTC).isoformat()}",
        f"- Status: {result.get('status')}",
        f"- Applied: {len(result.get('applied') or [])}",
        f"- Rejected: {len(result.get('rejected') or [])}",
        "",
        "## Outputs",
        "",
    ]
    for output in result.get("outputs") or []:
        lines.append(f"- `{output}`")
    if not result.get("outputs"):
        lines.append("- No reasoned sidecar outputs.")

    lines.extend(["", "## Applied Decisions", ""])
    applied = result.get("applied") or []
    if applied:
        for item in applied:
            lines.append(
                "- "
                f"`{item.get('section_id')}` "
                f"parent=`{item.get('parent_id')}` "
                f"level={item.get('level')} "
                f"confidence={item.get('confidence')}: {item.get('title')}"
            )
    else:
        lines.append("- No decisions were applied.")

    lines.extend(["", "## Rejected Decisions", ""])
    rejected = result.get("rejected") or []
    if rejected:
        for item in rejected[:120]:
            lines.append(
                "- "
                f"`{item.get('candidate_id')}` "
                f"action=`{item.get('action')}` "
                f"reason=`{item.get('reason')}`"
            )
    else:
        lines.append("- No decisions were rejected.")
    apply_report_path(out_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_mode(args: argparse.Namespace) -> int:
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    total_applied = 0
    processed = 0
    for out_dir in output_dirs(root):
        if document_names and out_dir.name not in document_names:
            continue
        decisions = load_jsonl(decision_path(out_dir))
        if not decisions:
            continue
        result = apply_decisions_for_document(out_dir, min_confidence=max(0.0, min(args.min_confidence, 1.0)))
        write_apply_report(out_dir, result)
        applied_count = len(result.get("applied") or [])
        total_applied += applied_count
        processed += 1
        print(f"[OK] {out_dir.name} | applied {applied_count} | status {result.get('status')}")
    print(f"Section reasoning apply complete: {total_applied} decision(s) applied across {processed} document(s).")
    return 0


def write_report(out_dir: Path, candidates: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> None:
    candidate_counts = Counter(str(candidate.get("candidate_type") or "") for candidate in candidates)
    decision_counts = Counter(str(decision.get("action") or "") for decision in decisions)
    lines = [
        f"# {out_dir.name} 章节语义推理报告",
        "",
        f"- Generated at: {datetime.now(UTC).isoformat()}",
        f"- Candidates: {len(candidates)}",
        f"- Decisions: {len(decisions)}",
        "",
        "## Candidate Types",
        "",
    ]
    if candidate_counts:
        for key, count in sorted(candidate_counts.items()):
            lines.append(f"- `{key}`: {count}")
    else:
        lines.append("- No section reasoning candidates.")
    lines.extend(["", "## Decision Actions", ""])
    if decision_counts:
        for key, count in sorted(decision_counts.items()):
            lines.append(f"- `{key}`: {count}")
    else:
        lines.append("- No LLM decisions.")
    lines.extend(["", "## Samples", ""])
    for candidate in candidates[:80]:
        current_block = candidate.get("current_block") or {}
        current_section = candidate.get("current_section") or {}
        path = " > ".join(str(value) for value in current_section.get("path") or [])
        lines.append(
            "- "
            f"`{candidate.get('candidate_type')}` "
            f"p.{current_block.get('page')} "
            f"`{current_block.get('block_id') or current_section.get('section_id')}`"
        )
        if path:
            lines.append(f"  - Section: {path}")
        if current_block.get("text"):
            lines.append(f"  - Block: {current_block.get('text')}")
        signals = ", ".join(str(value) for value in candidate.get("signals") or [])
        if signals:
            lines.append(f"  - Signals: {signals}")
        lines.append(f"  - Question: {candidate.get('question')}")
    report_path(out_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.mode == "collect":
        collect_mode(args)
        return 0
    if args.mode == "review":
        return review_mode(args)
    if args.mode == "apply":
        return apply_mode(args)
    return report_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
