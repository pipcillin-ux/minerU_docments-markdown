#!/usr/bin/env python3
"""Collect, review, summarize, and apply section-tree reasoning candidates."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ..build_structured_blocks import render_semantic_markdown
from ..defaults import (
    LLM_REVIEW_CONFIDENCE_THRESHOLD,
    SECTION_REASONING_ADOPT_CONFIDENCE,
    SECTION_REASONING_APPLY_CONFIDENCE,
)
from ..domain_profiles import DomainProfile, load_domain_profile
from ..heading_quality import quality_for_output_dir, write_markdown_report
from ..io_utils import load_dotenv, load_json, load_jsonl, write_jsonl
from ..llm_heading_assist import api_config, cache_key, call_chat_completions
from ..section_tree import attach_section_tree, clamp_level, end_block_for_range, toc_level_map, write_section_tree
from ..structure_utils import heading_key, normalize_text, output_dirs

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
    parser = argparse.ArgumentParser(
        description="Collect, review, summarize, or apply section-tree reasoning candidates."
    )
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument("--document", action="append", help="Only process this output document name.")
    parser.add_argument(
        "--domain-profile",
        default="generic",
        help="Domain profile name (generic/tcm) or a TOML profile path.",
    )
    parser.add_argument(
        "--mode", choices=("collect", "report", "summary", "review", "apply", "adopt"), default="collect"
    )
    parser.add_argument(
        "--target",
        choices=("sidecar", "main"),
        default="sidecar",
        help="adopt target. main is required before primary outputs are overwritten.",
    )
    parser.add_argument("--limit", type=int, help="Global candidate/review limit.")
    parser.add_argument("--max-per-document", type=int, default=40)
    parser.add_argument("--max-per-type", type=int, default=12)
    parser.add_argument("--context-radius", type=int, default=3)
    parser.add_argument(
        "--low-confidence-threshold",
        type=float,
        default=LLM_REVIEW_CONFIDENCE_THRESHOLD,
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        help="Minimum LLM confidence. Defaults to 0.82 for apply and 0.86 for summary/adopt.",
    )
    parser.add_argument("--adoption-backup", action="store_true", help="Write .pre-adopt backup files.")
    parser.add_argument("--force", action="store_true", help="Ignore cached LLM review responses.")
    parser.add_argument(
        "--review-jobs",
        type=int,
        default=1,
        help="Parallel LLM review workers. Decisions are still merged serially.",
    )
    return parser.parse_args()


def short_text(value: Any, limit: int = 260) -> str:
    text = cast(str, normalize_text(str(value or "")))
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
        "toc_context": toc_context(
            toc_nodes, str(node.get("normalized_key") or heading_key(str(node.get("title") or "")))
        ),
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
    anchored_block_ids = {
        str(node.get("source_block_id") or "") for node in nodes if str(node.get("source_block_id") or "")
    }
    title_counts = Counter(
        str(node.get("normalized_key") or heading_key(str(node.get("title") or ""))) for node in nodes
    )
    candidates: list[dict[str, Any]] = []
    type_counts: Counter[str] = Counter()
    seen: set[str] = set()

    for index, block in enumerate(blocks):
        if block.get("region") != "body" or block.get("block_type") != "heading":
            continue
        if block.get("include_in_semantic") is False:
            continue
        if str(block.get("block_id") or "") in anchored_block_ids:
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


def adoption_report_path(out_dir: Path) -> Path:
    return out_dir / "section_reasoning_adoption_report.md"


def summary_csv_path(root: Path) -> Path:
    return root / "section_reasoning_summary.csv"


def summary_report_path(root: Path) -> Path:
    return root / "section_reasoning_summary.md"


def main_section_tree_path(out_dir: Path) -> Path:
    return out_dir / "section_tree.json"


def main_structured_blocks_path(out_dir: Path) -> Path:
    return out_dir / "structured_blocks.jsonl"


def main_semantic_path(out_dir: Path) -> Path:
    preferred = out_dir / f"{out_dir.name}.semantic.md"
    if preferred.exists():
        return preferred
    matches = sorted(path for path in out_dir.glob("*.semantic.md") if ".reasoned." not in path.name)
    return matches[0] if matches else preferred


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


def reviewed_candidate_ids(out_dir: Path) -> set[str]:
    return {str(decision.get("candidate_id") or "") for decision in load_jsonl(decision_path(out_dir))}


def load_all_candidates(
    root: Path,
    document_names: set[str] | None,
    limit: int | None = None,
    *,
    skip_reviewed: bool = False,
) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    for out_dir in output_dirs(root):
        if document_names and out_dir.name not in document_names:
            continue
        reviewed_ids = reviewed_candidate_ids(out_dir) if skip_reviewed else set()
        for candidate in load_jsonl(candidate_path(out_dir)):
            if str(candidate.get("candidate_id") or "") in reviewed_ids:
                continue
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
        return cast(dict[str, Any], data["decision"])
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


def merge_decisions(
    existing: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for decision in existing:
        candidate_id_value = str(decision.get("candidate_id") or "")
        if candidate_id_value:
            by_id[candidate_id_value] = decision
    for decision in updates:
        candidate_id_value = str(decision.get("candidate_id") or "")
        if candidate_id_value:
            by_id[candidate_id_value] = decision

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_id_value = str(candidate.get("candidate_id") or "")
        if candidate_id_value in by_id:
            merged.append(by_id[candidate_id_value])
            seen.add(candidate_id_value)
    for decision in [*existing, *updates]:
        candidate_id_value = str(decision.get("candidate_id") or "")
        if candidate_id_value and candidate_id_value not in seen:
            merged.append(decision)
            seen.add(candidate_id_value)
    return merged


def review_mode(args: argparse.Namespace) -> int:
    load_dotenv()
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    candidates = load_all_candidates(root, document_names, args.limit, skip_reviewed=not args.force)
    if not candidates:
        collect_mode(args)
        candidates = load_all_candidates(root, document_names, args.limit, skip_reviewed=not args.force)
    if not candidates:
        print("No section reasoning candidates need review.")
        return 0
    api_key, _, _ = api_config()
    if candidates and not api_key:
        print("DEEPSEEK_API_KEY or OPENAI_API_KEY is required for section reasoning review.")
        return 2

    by_dir: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    jobs = max(1, int(args.review_jobs or 1))
    ordered_results: list[tuple[Path, dict[str, Any]] | None] = [None] * len(candidates)
    if jobs == 1:
        for index, (out_dir, candidate) in enumerate(candidates, start=1):
            print(
                f"[{index}/{len(candidates)}] Reviewing {candidate.get('document')} {candidate.get('candidate_type')}"
            )
            ordered_results[index - 1] = (out_dir, review_candidate(out_dir, candidate, args.force))
    else:
        print(f"Reviewing {len(candidates)} candidate(s) with {jobs} worker(s).")
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            future_map = {
                executor.submit(review_candidate, out_dir, candidate, args.force): (index, out_dir, candidate)
                for index, (out_dir, candidate) in enumerate(candidates, start=1)
            }
            for done_count, future in enumerate(as_completed(future_map), start=1):
                index, out_dir, candidate = future_map[future]
                try:
                    decision = future.result()
                except Exception as exc:  # noqa: BLE001 - defensive fallback; review_candidate normally catches.
                    input_hash = cache_key(review_payload(candidate))
                    decision = uncertain_decision(candidate, input_hash, f"Parallel LLM review failed: {exc}")
                ordered_results[index - 1] = (out_dir, decision)
                print(
                    f"[{done_count}/{len(candidates)}] Reviewed "
                    f"{candidate.get('document')} {candidate.get('candidate_type')}"
                )

    for item in ordered_results:
        if item is None:
            continue
        out_dir, decision = item
        by_dir[out_dir].append(decision)

    for out_dir, decisions in by_dir.items():
        existing = load_jsonl(decision_path(out_dir))
        candidates_for_dir = load_jsonl(candidate_path(out_dir))
        merged = merge_decisions(existing, decisions, candidates_for_dir)
        write_jsonl(decision_path(out_dir), merged)
        write_report(out_dir, candidates_for_dir, merged)
        print(f"[OK] {out_dir.name} | new decisions {len(decisions)} | total decisions {len(merged)}")
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


def format_counter(counter: Counter[str]) -> str:
    return "; ".join(f"{key}:{count}" for key, count in sorted(counter.items()) if key)


def markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def parse_report_bullet(path: Path, label: str) -> str:
    if not path.exists():
        return ""
    prefix = f"- {label}:"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def parse_report_int(path: Path, label: str) -> int:
    raw = parse_report_bullet(path, label)
    try:
        return int(raw)
    except ValueError:
        return 0


def reasoning_node_count(path: Path) -> int:
    payload = load_json(path, {"nodes": []})
    total = 0
    for node in payload.get("nodes") or []:
        source = str(node.get("reasoning_source") or "")
        evidence = {str(value) for value in node.get("evidence") or []}
        if source == "llm_section_reasoning" or "llm_section_reasoning" in evidence:
            total += 1
    return total


def high_confidence_insert_count(decisions: list[dict[str, Any]], min_confidence: float) -> int:
    return sum(
        1
        for decision in decisions
        if str(decision.get("decision_source") or "") == "llm_section_reasoning"
        and str(decision.get("action") or "") == "insert_child_section"
        and clamp_confidence(decision.get("confidence")) >= min_confidence
    )


def summarize_document(out_dir: Path, *, min_confidence: float) -> dict[str, Any]:
    candidates = load_jsonl(candidate_path(out_dir))
    decisions = load_jsonl(decision_path(out_dir))
    current_candidate_ids = {str(candidate.get("candidate_id") or "") for candidate in candidates}
    current_decisions = [
        decision for decision in decisions if str(decision.get("candidate_id") or "") in current_candidate_ids
    ]
    orphan_decision_count = len(decisions) - len(current_decisions)
    candidate_counts = Counter(str(candidate.get("candidate_type") or "") for candidate in candidates)
    decision_counts = Counter(str(decision.get("action") or "") for decision in current_decisions)
    adoption_report = adoption_report_path(out_dir)

    adoption_ready = 0
    adoption_check_rejected = 0
    adoption_check_status = ""
    adoption_structural_issues = 0
    if decisions:
        result = build_reasoned_candidate_for_document(
            out_dir,
            min_confidence=min_confidence,
            require_llm_source=True,
        )
        adoption_check_status = str(result.get("status") or "")
        applied_count = len(result.get("applied") or [])
        adoption_check_rejected = len(result.get("rejected") or [])
        if applied_count:
            structural_issues = validate_reasoned_adoption(out_dir, result)
            adoption_structural_issues = len(structural_issues)
            if structural_issues:
                adoption_check_status = "structural_blocked"
            else:
                adoption_ready = applied_count

    main_reasoning_nodes = reasoning_node_count(main_section_tree_path(out_dir))
    sidecar_reasoning_nodes = reasoning_node_count(reasoned_section_tree_path(out_dir))
    decision_count = len(current_decisions)
    candidate_count = len(candidates)
    review_needed = candidate_count > decision_count
    main_adoption_pending = adoption_ready > 0

    return {
        "document": out_dir.name,
        "candidates": candidate_count,
        "decisions": decision_count,
        "orphan_decisions": orphan_decision_count,
        "candidate_types": format_counter(candidate_counts),
        "decision_actions": format_counter(decision_counts),
        "high_confidence_insert_decisions": high_confidence_insert_count(current_decisions, min_confidence),
        "adoption_check_status": adoption_check_status,
        "adoption_ready": adoption_ready,
        "adoption_check_rejected": adoption_check_rejected,
        "adoption_structural_issues": adoption_structural_issues,
        "main_reasoning_nodes": main_reasoning_nodes,
        "sidecar_reasoning_nodes": sidecar_reasoning_nodes,
        "adoption_report_status": parse_report_bullet(adoption_report, "Status"),
        "adoption_report_adopted": parse_report_int(adoption_report, "Adopted"),
        "adoption_report_rejected": parse_report_int(adoption_report, "Rejected"),
        "adoption_rolled_back": parse_report_bullet(adoption_report, "Rolled back"),
        "review_needed": review_needed,
        "main_adoption_pending": main_adoption_pending,
        "_candidate_counts": candidate_counts,
        "_decision_counts": decision_counts,
    }


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    public_fields = [
        "document",
        "candidates",
        "decisions",
        "orphan_decisions",
        "candidate_types",
        "decision_actions",
        "high_confidence_insert_decisions",
        "adoption_check_status",
        "adoption_ready",
        "adoption_check_rejected",
        "adoption_structural_issues",
        "main_reasoning_nodes",
        "sidecar_reasoning_nodes",
        "adoption_report_status",
        "adoption_report_adopted",
        "adoption_report_rejected",
        "adoption_rolled_back",
        "review_needed",
        "main_adoption_pending",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=public_fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in public_fields})


def summary_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = None) -> list[str]:
    selected = rows if limit is None else rows[:limit]
    if not selected:
        return ["- None."]
    header = "| " + " | ".join(label for label, _ in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, divider]
    for row in selected:
        values = [markdown_cell(row.get(key)) for _, key in columns]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def write_summary_report(path: Path, rows: list[dict[str, Any]], *, min_confidence: float) -> None:
    total_candidates = sum(int(row.get("candidates") or 0) for row in rows)
    total_decisions = sum(int(row.get("decisions") or 0) for row in rows)
    total_orphans = sum(int(row.get("orphan_decisions") or 0) for row in rows)
    total_high_conf = sum(int(row.get("high_confidence_insert_decisions") or 0) for row in rows)
    total_ready = sum(int(row.get("adoption_ready") or 0) for row in rows)
    total_structural_issues = sum(int(row.get("adoption_structural_issues") or 0) for row in rows)
    total_main_nodes = sum(int(row.get("main_reasoning_nodes") or 0) for row in rows)
    review_queue = [row for row in rows if row.get("review_needed")]
    adoption_queue = [row for row in rows if int(row.get("adoption_ready") or 0) > 0]
    adopted_rows = [row for row in rows if int(row.get("main_reasoning_nodes") or 0) > 0]
    blocked_rows = [
        row
        for row in rows
        if int(row.get("high_confidence_insert_decisions") or 0) > 0
        and int(row.get("adoption_ready") or 0) == 0
        and int(row.get("main_reasoning_nodes") or 0) == 0
    ]
    active_rows = [
        row
        for row in rows
        if int(row.get("candidates") or 0)
        or int(row.get("decisions") or 0)
        or int(row.get("main_reasoning_nodes") or 0)
        or int(row.get("sidecar_reasoning_nodes") or 0)
    ]

    lines = [
        "# Section Reasoning Corpus Summary",
        "",
        f"- Generated at: {datetime.now(UTC).isoformat()}",
        f"- Output root: `{path.parent}`",
        f"- Minimum confidence: {min_confidence:.2f}",
        f"- Documents scanned: {len(rows)}",
        f"- Candidates: {total_candidates}",
        f"- Decisions: {total_decisions}",
        f"- Orphan decisions: {total_orphans}",
        f"- High-confidence insert decisions: {total_high_conf}",
        f"- Adoption-ready decisions: {total_ready}",
        f"- Adoption structural issues: {total_structural_issues}",
        f"- Main-output LLM reasoning nodes: {total_main_nodes}",
        "",
        "## Recommended Next Commands",
        "",
        "```bash",
        ".venv/bin/mineru-section-reasoning --mode collect",
        ".venv/bin/mineru-section-reasoning --mode summary --min-confidence 0.86",
        ".venv/bin/mineru-section-reasoning --mode review --limit 20",
        ".venv/bin/mineru-section-reasoning --mode adopt --target main --min-confidence 0.86",
        "```",
        "",
        "## Review Queue",
        "",
    ]
    review_queue = sorted(
        review_queue, key=lambda row: int(row.get("candidates") or 0) - int(row.get("decisions") or 0), reverse=True
    )
    lines.extend(
        summary_table(
            review_queue,
            [
                ("Document", "document"),
                ("Candidates", "candidates"),
                ("Decisions", "decisions"),
                ("Orphans", "orphan_decisions"),
                ("Candidate Types", "candidate_types"),
            ],
            limit=30,
        )
    )
    lines.extend(["", "## Adoption Queue", ""])
    adoption_queue = sorted(adoption_queue, key=lambda row: int(row.get("adoption_ready") or 0), reverse=True)
    lines.extend(
        summary_table(
            adoption_queue,
            [
                ("Document", "document"),
                ("Ready", "adoption_ready"),
                ("Structural Issues", "adoption_structural_issues"),
                ("Rejected By Check", "adoption_check_rejected"),
                ("Actions", "decision_actions"),
            ],
            limit=30,
        )
    )
    lines.extend(["", "## Already Adopted In Main Outputs", ""])
    lines.extend(
        summary_table(
            adopted_rows,
            [
                ("Document", "document"),
                ("Main Nodes", "main_reasoning_nodes"),
                ("Report Status", "adoption_report_status"),
                ("Report Adopted", "adoption_report_adopted"),
            ],
            limit=30,
        )
    )
    lines.extend(["", "## High-Confidence But Not Adoptable", ""])
    lines.extend(
        summary_table(
            blocked_rows,
            [
                ("Document", "document"),
                ("High-Confidence", "high_confidence_insert_decisions"),
                ("Check Status", "adoption_check_status"),
                ("Structural Issues", "adoption_structural_issues"),
                ("Rejected By Check", "adoption_check_rejected"),
                ("Actions", "decision_actions"),
            ],
            limit=30,
        )
    )
    lines.extend(["", "## Per-Document Status", ""])
    lines.extend(
        summary_table(
            active_rows,
            [
                ("Document", "document"),
                ("Candidates", "candidates"),
                ("Decisions", "decisions"),
                ("Orphans", "orphan_decisions"),
                ("High-Conf", "high_confidence_insert_decisions"),
                ("Ready", "adoption_ready"),
                ("Structural Issues", "adoption_structural_issues"),
                ("Main Nodes", "main_reasoning_nodes"),
                ("Candidate Types", "candidate_types"),
                ("Actions", "decision_actions"),
            ],
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summary_mode(args: argparse.Namespace) -> int:
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    min_confidence = confidence_threshold(args)
    rows = [
        summarize_document(out_dir, min_confidence=min_confidence)
        for out_dir in output_dirs(root)
        if not document_names or out_dir.name in document_names
    ]
    write_summary_csv(summary_csv_path(root), rows)
    write_summary_report(summary_report_path(root), rows, min_confidence=min_confidence)
    print(
        "Section reasoning summary complete: "
        f"{len(rows)} document(s), "
        f"{sum(int(row.get('candidates') or 0) for row in rows)} candidate(s), "
        f"{sum(int(row.get('decisions') or 0) for row in rows)} decision(s)."
    )
    print(f"Wrote {summary_csv_path(root)}")
    print(f"Wrote {summary_report_path(root)}")
    return 0


def confidence_threshold(args: argparse.Namespace) -> float:
    if args.min_confidence is not None:
        return max(0.0, min(float(args.min_confidence), 1.0))
    if args.mode in {"adopt", "summary"}:
        return SECTION_REASONING_ADOPT_CONFIDENCE
    return SECTION_REASONING_APPLY_CONFIDENCE


def block_index_by_id(blocks: list[dict[str, Any]]) -> dict[str, int]:
    return {str(block.get("block_id") or ""): index for index, block in enumerate(blocks)}


def block_page(block: dict[str, Any]) -> int | None:
    value = block.get("page")
    if value is None:
        return None
    try:
        page = int(value)
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


def natural_node_end_indexes(
    nodes: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> dict[str, int]:
    indexes = block_index_by_id(blocks)
    original_order = {id(node): index for index, node in enumerate(nodes)}

    def sort_key(node: dict[str, Any]) -> tuple[int, int, int]:
        start = node_start_index(node, indexes)
        if start is None:
            start = len(blocks) + int(node.get("document_order") or 0)
        return (start, int(node.get("level") or 99), original_order[id(node)])

    sorted_nodes = sorted(nodes, key=sort_key)
    start_indexes = [node_start_index(node, indexes) for node in sorted_nodes]
    natural_ends: dict[str, int] = {}

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
        section_id = str(node.get("section_id") or "")
        if section_id:
            natural_ends[section_id] = end_index

    return natural_ends


def ancestor_effective_end_index(
    parent: dict[str, Any],
    *,
    node_by_id: dict[str, dict[str, Any]],
    natural_ends: dict[str, int],
    indexes: dict[str, int],
) -> int | None:
    boundaries: list[int] = []
    current: dict[str, Any] | None = parent
    visited: set[str] = set()
    while current is not None:
        section_id = str(current.get("section_id") or "")
        if not section_id or section_id in visited:
            break
        visited.add(section_id)
        inferred_boundary = natural_ends.get(section_id)
        existing_boundary = indexes.get(str(current.get("end_block_id") or ""))
        available_boundaries = [boundary for boundary in (inferred_boundary, existing_boundary) if boundary is not None]
        if available_boundaries:
            boundaries.append(max(available_boundaries))
        current = node_by_id.get(str(current.get("parent_id") or ""))
    return min(boundaries) if boundaries else None


def set_node_end_index(
    node: dict[str, Any],
    blocks: list[dict[str, Any]],
    start_index: int,
    end_index: int,
) -> None:
    end_block = end_block_for_range(blocks, start_index, end_index)
    node["end_block_id"] = str(end_block.get("block_id") or node.get("start_block_id") or "")
    node["end_page"] = block_page(end_block) or node.get("start_page")


def recompute_reasoned_ranges(
    original_nodes: list[dict[str, Any]],
    inserted_nodes: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not inserted_nodes:
        return original_nodes

    indexes = block_index_by_id(blocks)
    nodes = [*original_nodes, *inserted_nodes]
    original_order = {id(node): index for index, node in enumerate(nodes)}
    node_by_id = {str(node.get("section_id") or ""): node for node in nodes}
    natural_ends = natural_node_end_indexes(original_nodes, blocks)

    def sort_key(node: dict[str, Any]) -> tuple[int, int, int]:
        start = node_start_index(node, indexes)
        if start is None:
            start = len(blocks) + int(node.get("document_order") or 0)
        return (start, int(node.get("level") or 99), original_order[id(node)])

    sorted_nodes = sorted(nodes, key=sort_key)
    start_indexes = [node_start_index(node, indexes) for node in sorted_nodes]
    sorted_index_by_id = {
        str(node.get("section_id") or ""): index
        for index, node in enumerate(sorted_nodes)
        if str(node.get("section_id") or "")
    }

    for order, node in enumerate(sorted_nodes, start=1):
        node["document_order"] = order

    for node in inserted_nodes:
        section_id = str(node.get("section_id") or "")
        sorted_index = sorted_index_by_id.get(section_id)
        if sorted_index is None:
            continue
        start_index = start_indexes[sorted_index]
        if start_index is None:
            continue
        level = clamp_level(node.get("level")) or 6
        parent = node_by_id.get(str(node.get("parent_id") or ""))
        parent_boundary = (
            ancestor_effective_end_index(
                parent,
                node_by_id=node_by_id,
                natural_ends=natural_ends,
                indexes=indexes,
            )
            if parent is not None
            else None
        )
        end_index = parent_boundary if parent_boundary is not None else len(blocks) - 1

        for next_index in range(sorted_index + 1, len(sorted_nodes)):
            next_start_index = start_indexes[next_index]
            if next_start_index is None or next_start_index <= start_index:
                continue
            next_level = clamp_level(sorted_nodes[next_index].get("level")) or 6
            if next_level <= level:
                end_index = min(end_index, max(start_index, next_start_index - 1))
                break

        for block_index in range(start_index + 1, end_index + 1):
            block = blocks[block_index]
            if block.get("region") != "body" or block.get("include_in_semantic") is False:
                continue
            if block.get("block_type") != "heading":
                continue
            local_level = clamp_level(block.get("heading_level"))
            if local_level is not None and local_level <= level:
                end_index = max(start_index, block_index - 1)
                break
        set_node_end_index(node, blocks, start_index, end_index)

        required_end = indexes.get(str(node.get("end_block_id") or ""), start_index)
        current = parent
        visited: set[str] = set()
        while current is not None:
            current_id = str(current.get("section_id") or "")
            if not current_id or current_id in visited:
                break
            visited.add(current_id)
            current_start = node_start_index(current, indexes)
            if current_start is None:
                break
            current_end = indexes.get(str(current.get("end_block_id") or ""), current_start)
            allowed_end = max(natural_ends.get(current_id, current_end), current_end)
            target_end = min(max(current_end, required_end), allowed_end)
            if target_end > current_end:
                set_node_end_index(current, blocks, current_start, target_end)
                current_end = indexes.get(str(current.get("end_block_id") or ""), target_end)
            required_end = max(required_end, current_end)
            current = node_by_id.get(str(current.get("parent_id") or ""))

    return sorted_nodes


def build_reasoned_candidate_for_document(
    out_dir: Path,
    *,
    min_confidence: float,
    require_llm_source: bool = False,
    domain_profile: DomainProfile | None = None,
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
    block_indexes = block_index_by_id(blocks)
    natural_ends = natural_node_end_indexes(original_nodes, blocks)
    source_anchor_by_block = {
        str(node.get("source_block_id") or ""): str(node.get("section_id") or "")
        for node in original_nodes
        if str(node.get("source_block_id") or "")
    }
    existing_ids = set(node_by_id)
    inserted_nodes: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    next_id_counter = 1

    for decision in decisions:
        candidate_id_value = str(decision.get("candidate_id") or "")
        action = str(decision.get("action") or "")
        confidence = clamp_confidence(decision.get("confidence"))
        if require_llm_source and str(decision.get("decision_source") or "") != "llm_section_reasoning":
            rejected.append(
                {
                    "candidate_id": candidate_id_value,
                    "action": action,
                    "reason": "non_llm_decision_source",
                }
            )
            continue
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
        if short_text(current_block.get("text")) and short_text(current_block.get("text")) != short_text(
            block.get("text")
        ):
            rejected.append(
                {"candidate_id": candidate_id_value, "action": action, "reason": "candidate_block_text_changed"}
            )
            continue
        if block.get("region") != "body" or block.get("include_in_semantic") is False:
            rejected.append(
                {"candidate_id": candidate_id_value, "action": action, "reason": "non_body_or_excluded_block"}
            )
            continue
        block_id = str(block.get("block_id") or "")
        if block_id in source_anchor_by_block:
            rejected.append(
                {
                    "candidate_id": candidate_id_value,
                    "action": action,
                    "reason": "source_already_section_node",
                    "existing_section_id": source_anchor_by_block[block_id],
                }
            )
            continue

        source_index = block_indexes.get(block_id)
        parent_start = node_start_index(parent, block_indexes)
        parent_boundary = ancestor_effective_end_index(
            parent,
            node_by_id=node_by_id,
            natural_ends=natural_ends,
            indexes=block_indexes,
        )
        if (
            source_index is None
            or parent_start is None
            or parent_boundary is None
            or source_index <= parent_start
            or source_index > parent_boundary
        ):
            rejected.append(
                {
                    "candidate_id": candidate_id_value,
                    "action": action,
                    "reason": "source_outside_parent_natural_boundary",
                }
            )
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
        source_anchor_by_block[block_id] = section_id
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

    reasoned_nodes = recompute_reasoned_ranges(original_nodes, inserted_nodes, blocks)
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
    semantic_text, semantic_count = render_semantic_markdown(
        reasoned_blocks,
        toc_levels,
        domain_profile,
    )

    return {
        "status": "ok",
        "applied": applied,
        "rejected": rejected,
        "semantic_count": semantic_count,
        "section_payload": reasoned_payload,
        "structured_blocks": reasoned_blocks,
        "semantic_text": semantic_text,
        "outputs": [
            reasoned_section_tree_path(out_dir).name,
            reasoned_structured_blocks_path(out_dir).name,
            reasoned_semantic_path(out_dir).name,
        ],
    }


def write_reasoned_sidecars(out_dir: Path, result: dict[str, Any]) -> None:
    write_section_tree(reasoned_section_tree_path(out_dir), result["section_payload"])
    write_jsonl(reasoned_structured_blocks_path(out_dir), result["structured_blocks"])
    reasoned_semantic_path(out_dir).write_text(str(result["semantic_text"]), encoding="utf-8")


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"section_payload", "structured_blocks", "semantic_text"}
    }


def apply_decisions_for_document(
    out_dir: Path,
    *,
    min_confidence: float,
    domain_profile: DomainProfile | None = None,
) -> dict[str, Any]:
    result = build_reasoned_candidate_for_document(
        out_dir,
        min_confidence=min_confidence,
        domain_profile=domain_profile,
    )
    if result.get("status") == "ok":
        write_reasoned_sidecars(out_dir, result)
    return public_result(result)


def main_output_paths(out_dir: Path) -> list[Path]:
    return [main_section_tree_path(out_dir), main_structured_blocks_path(out_dir), main_semantic_path(out_dir)]


def read_output_snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    snapshot: dict[Path, bytes | None] = {}
    for path in paths:
        snapshot[path] = path.read_bytes() if path.exists() else None
    return snapshot


def restore_output_snapshot(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(content)


def write_pre_adopt_backups(snapshot: dict[Path, bytes | None]) -> list[str]:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    written: list[str] = []
    for path, content in snapshot.items():
        if content is None:
            continue
        backup_path = path.with_name(f"{path.name}.pre-adopt-{timestamp}.bak")
        backup_path.write_bytes(content)
        written.append(backup_path.name)
    return written


def validate_text_unchanged(original_blocks: list[dict[str, Any]], reasoned_blocks: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    if len(original_blocks) != len(reasoned_blocks):
        return [f"structured_block_count_changed:{len(original_blocks)}->{len(reasoned_blocks)}"]
    original_by_id = {str(block.get("block_id") or ""): block for block in original_blocks}
    for block in reasoned_blocks:
        block_id = str(block.get("block_id") or "")
        original = original_by_id.get(block_id)
        if original is None:
            issues.append(f"new_or_unknown_block:{block_id}")
            continue
        for key in ("text", "original_text", "remaining_text"):
            if str(original.get(key) or "") != str(block.get(key) or ""):
                issues.append(f"block_{key}_changed:{block_id}")
                break
    return issues


def structure_issues_for_payload(section_payload: dict[str, Any], blocks: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    block_indexes = block_index_by_id(blocks)
    nodes = list(section_payload.get("nodes") or [])
    node_ids = [str(node.get("section_id") or "") for node in nodes]
    if len(set(node_ids)) != len(node_ids):
        issues.append("duplicate_section_id")
    node_by_id = {str(node.get("section_id") or ""): node for node in nodes}
    ranges: dict[str, tuple[int, int]] = {}
    previous_order = 0
    previous_start = -1

    for node in nodes:
        section_id = str(node.get("section_id") or "")
        if not section_id:
            issues.append("empty_section_id")
            continue
        order = int(node.get("document_order") or 0)
        if order <= previous_order:
            issues.append(f"document_order_not_increasing:{section_id}")
        previous_order = order
        level = clamp_level(node.get("level"))
        if level is None:
            issues.append(f"invalid_level:{section_id}")
        if str(node.get("region") or "") != "body":
            issues.append(f"non_body_section_node:{section_id}")
        start = node_start_index(node, block_indexes)
        end = block_indexes.get(str(node.get("end_block_id") or ""))
        if start is None:
            issues.append(f"missing_start_block:{section_id}")
            continue
        if end is None:
            issues.append(f"missing_end_block:{section_id}")
            continue
        if end < start:
            issues.append(f"reversed_range:{section_id}")
        if start < previous_start:
            issues.append(f"document_order_before_block_order:{section_id}")
        previous_start = max(previous_start, start)
        ranges[section_id] = (start, end)

    for node in nodes:
        section_id = str(node.get("section_id") or "")
        parent_id = str(node.get("parent_id") or "")
        if not parent_id:
            continue
        parent = node_by_id.get(parent_id)
        if parent is None:
            issues.append(f"missing_parent:{section_id}->{parent_id}")
            continue
        child_range = ranges.get(section_id)
        parent_range = ranges.get(parent_id)
        if (
            child_range
            and parent_range
            and not (parent_range[0] <= child_range[0] <= child_range[1] <= parent_range[1])
        ):
            issues.append(f"child_range_outside_parent:{section_id}->{parent_id}")
        parent_level = clamp_level(parent.get("level"))
        child_level = clamp_level(node.get("level"))
        if parent_level is not None and child_level is not None and child_level <= parent_level:
            issues.append(f"child_level_not_below_parent:{section_id}->{parent_id}")
    return issues


def validate_reasoned_adoption(out_dir: Path, result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not result.get("applied"):
        issues.append("no_decisions_applied")
    section_payload = result.get("section_payload") or {}
    reasoned_blocks = list(result.get("structured_blocks") or [])
    original_blocks = load_jsonl(main_structured_blocks_path(out_dir))
    original_payload = load_json(main_section_tree_path(out_dir), {"nodes": []})
    issues.extend(validate_text_unchanged(original_blocks, reasoned_blocks))

    base_structure_issues = set(structure_issues_for_payload(original_payload, original_blocks))
    reasoned_structure_issues = structure_issues_for_payload(section_payload, reasoned_blocks)
    issues.extend(
        f"new_structure_issue:{issue}" for issue in reasoned_structure_issues if issue not in base_structure_issues
    )

    node_by_id = {str(node.get("section_id") or ""): node for node in section_payload.get("nodes") or []}
    block_indexes = block_index_by_id(reasoned_blocks)
    node_ranges_by_id: dict[str, tuple[int, int]] = {}
    for node in section_payload.get("nodes") or []:
        section_id = str(node.get("section_id") or "")
        start = node_start_index(node, block_indexes)
        end = block_indexes.get(str(node.get("end_block_id") or ""))
        if section_id and start is not None and end is not None:
            node_ranges_by_id[section_id] = (start, end)

    for item in result.get("applied") or []:
        section_id = str(item.get("section_id") or "")
        node = node_by_id.get(section_id)
        if node is None:
            issues.append(f"applied_node_missing:{section_id}")
            continue
        source_block_id = str(node.get("source_block_id") or "")
        block = next((value for value in reasoned_blocks if str(value.get("block_id") or "") == source_block_id), None)
        if block is None:
            issues.append(f"applied_source_block_missing:{section_id}")
            continue
        if block.get("region") != "body" or block.get("include_in_semantic") is False:
            issues.append(f"applied_source_not_body_semantic:{section_id}")
        if block.get("section_id") != section_id:
            issues.append(f"applied_block_not_attached:{section_id}")
        node_range = node_ranges_by_id.get(section_id)
        if node_range:
            for index, assigned_block in enumerate(reasoned_blocks):
                if assigned_block.get("section_id") == section_id and not (node_range[0] <= index <= node_range[1]):
                    issues.append(f"applied_assignment_outside_range:{section_id}:{assigned_block.get('block_id')}")
                    break
                if (
                    node_range[0] < index <= node_range[1]
                    and assigned_block.get("section_id") == section_id
                    and assigned_block.get("block_type") == "heading"
                    and assigned_block.get("region") == "body"
                ):
                    local_level = clamp_level(assigned_block.get("heading_level"))
                    node_level = clamp_level(node.get("level"))
                    if local_level is not None and node_level is not None and local_level <= node_level:
                        issues.append(
                            f"applied_node_contains_peer_heading:{section_id}:{assigned_block.get('block_id')}"
                        )
                        break
    return issues


def write_main_outputs(out_dir: Path, result: dict[str, Any]) -> None:
    write_section_tree(main_section_tree_path(out_dir), result["section_payload"])
    write_jsonl(main_structured_blocks_path(out_dir), result["structured_blocks"])
    main_semantic_path(out_dir).write_text(str(result["semantic_text"]), encoding="utf-8")


def adoption_quality_result(
    out_dir: Path,
    domain_profile: DomainProfile | None = None,
) -> dict[str, Any]:
    summary, issues = quality_for_output_dir(out_dir, domain_profile)
    out_dir.joinpath("heading_quality.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown_report(out_dir, summary, issues)
    return cast(dict[str, Any], summary)


def write_adoption_report(out_dir: Path, result: dict[str, Any]) -> None:
    lines = [
        f"# {out_dir.name} 章节推理采纳报告",
        "",
        f"- Generated at: {datetime.now(UTC).isoformat()}",
        f"- Status: {result.get('status')}",
        f"- Target: {result.get('target')}",
        f"- Adopted: {len(result.get('adopted') or [])}",
        f"- Rejected: {len(result.get('rejected') or [])}",
        f"- Rolled back: {bool(result.get('rolled_back'))}",
        "",
        "## Quality Gate",
        "",
    ]
    quality = result.get("quality") or {}
    if quality:
        counts = quality.get("issue_counts") or {}
        lines.extend(
            [
                f"- Status: {quality.get('status')}",
                f"- FAIL: {counts.get('FAIL', 0)}",
                f"- WARN: {counts.get('WARN', 0)}",
                f"- INFO: {counts.get('INFO', 0)}",
            ]
        )
    else:
        lines.append("- Not run.")
    structural_issues = result.get("structural_issues") or []
    lines.extend(["", "## Structural Gate", ""])
    if structural_issues:
        for issue in structural_issues:
            lines.append(f"- `{issue}`")
    else:
        lines.append("- Passed.")

    lines.extend(["", "## Adopted Decisions", ""])
    adopted = result.get("adopted") or []
    if adopted:
        for item in adopted:
            lines.append(
                "- "
                f"`{item.get('section_id')}` parent=`{item.get('parent_id')}` "
                f"level={item.get('level')} confidence={item.get('confidence')}: {item.get('title')}"
            )
    else:
        lines.append("- No decisions were adopted.")

    lines.extend(["", "## Rejected Decisions", ""])
    rejected = result.get("rejected") or []
    if rejected:
        for item in rejected[:120]:
            lines.append(f"- `{item.get('candidate_id')}` action=`{item.get('action')}` reason=`{item.get('reason')}`")
    else:
        lines.append("- No decisions were rejected.")

    backups = result.get("backups") or []
    if backups:
        lines.extend(["", "## Backups", ""])
        for backup in backups:
            lines.append(f"- `{backup}`")
    adoption_report_path(out_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def adopt_decisions_for_document(
    out_dir: Path,
    *,
    min_confidence: float,
    target: str,
    write_backups: bool = False,
    domain_profile: DomainProfile | None = None,
) -> dict[str, Any]:
    result = build_reasoned_candidate_for_document(
        out_dir,
        min_confidence=min_confidence,
        require_llm_source=True,
        domain_profile=domain_profile,
    )
    if result.get("status") != "ok":
        adoption_result = {**public_result(result), "target": target, "adopted": [], "rolled_back": False}
        write_adoption_report(out_dir, adoption_result)
        return adoption_result

    write_reasoned_sidecars(out_dir, result)
    if not result.get("applied"):
        adoption_result = {
            **public_result(result),
            "target": target,
            "adopted": [],
            "structural_issues": [],
            "rolled_back": False,
            "quality": {},
            "backups": [],
            "status": "no_adoptable_decisions",
        }
        write_adoption_report(out_dir, adoption_result)
        return adoption_result

    structural_issues = validate_reasoned_adoption(out_dir, result)
    adoption_result = {
        **public_result(result),
        "target": target,
        "adopted": [],
        "structural_issues": structural_issues,
        "rolled_back": False,
        "quality": {},
        "backups": [],
    }
    if structural_issues:
        adoption_result["status"] = "rejected"
        write_adoption_report(out_dir, adoption_result)
        return adoption_result
    if target != "main":
        adoption_result["status"] = "sidecar_ready"
        write_adoption_report(out_dir, adoption_result)
        return adoption_result

    paths = main_output_paths(out_dir)
    snapshot = read_output_snapshot(paths)
    if write_backups:
        adoption_result["backups"] = write_pre_adopt_backups(snapshot)
    try:
        write_main_outputs(out_dir, result)
        quality = adoption_quality_result(out_dir, domain_profile)
        adoption_result["quality"] = {
            "status": quality.get("status"),
            "issue_counts": quality.get("issue_counts") or {},
        }
        counts = quality.get("issue_counts") or {}
        if counts.get("FAIL", 0) or counts.get("WARN", 0):
            restore_output_snapshot(snapshot)
            adoption_quality_result(out_dir, domain_profile)
            adoption_result["status"] = "rolled_back"
            adoption_result["rolled_back"] = True
            adoption_result["structural_issues"] = [
                *structural_issues,
                f"quality_gate_failed:FAIL={counts.get('FAIL', 0)},WARN={counts.get('WARN', 0)}",
            ]
            write_adoption_report(out_dir, adoption_result)
            return adoption_result
    except Exception as exc:  # noqa: BLE001 - failed adoption must restore primary outputs.
        restore_output_snapshot(snapshot)
        adoption_quality_result(out_dir, domain_profile)
        adoption_result["status"] = "rolled_back"
        adoption_result["rolled_back"] = True
        adoption_result["structural_issues"] = [*structural_issues, f"adoption_write_failed:{exc}"]
        write_adoption_report(out_dir, adoption_result)
        return adoption_result

    adoption_result["status"] = "adopted"
    adoption_result["adopted"] = list(result.get("applied") or [])
    write_adoption_report(out_dir, adoption_result)
    return adoption_result


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
            lines.append(f"- `{item.get('candidate_id')}` action=`{item.get('action')}` reason=`{item.get('reason')}`")
    else:
        lines.append("- No decisions were rejected.")
    apply_report_path(out_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_mode(args: argparse.Namespace) -> int:
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    total_applied = 0
    processed = 0
    min_confidence = confidence_threshold(args)
    domain_profile = load_domain_profile(getattr(args, "domain_profile", "generic"))
    for out_dir in output_dirs(root):
        if document_names and out_dir.name not in document_names:
            continue
        decisions = load_jsonl(decision_path(out_dir))
        if not decisions:
            continue
        result = apply_decisions_for_document(
            out_dir,
            min_confidence=min_confidence,
            domain_profile=domain_profile,
        )
        write_apply_report(out_dir, result)
        applied_count = len(result.get("applied") or [])
        total_applied += applied_count
        processed += 1
        print(f"[OK] {out_dir.name} | applied {applied_count} | status {result.get('status')}")
    print(f"Section reasoning apply complete: {total_applied} decision(s) applied across {processed} document(s).")
    return 0


def adopt_mode(args: argparse.Namespace) -> int:
    root = Path(args.output_dir)
    document_names = set(args.document) if args.document else None
    total_adopted = 0
    processed = 0
    failed = 0
    min_confidence = confidence_threshold(args)
    domain_profile = load_domain_profile(getattr(args, "domain_profile", "generic"))
    for out_dir in output_dirs(root):
        if document_names and out_dir.name not in document_names:
            continue
        decisions = load_jsonl(decision_path(out_dir))
        if not decisions:
            continue
        result = adopt_decisions_for_document(
            out_dir,
            min_confidence=min_confidence,
            target=args.target,
            write_backups=args.adoption_backup,
            domain_profile=domain_profile,
        )
        adopted_count = len(result.get("adopted") or [])
        total_adopted += adopted_count
        processed += 1
        if result.get("status") == "rolled_back":
            failed += 1
        print(f"[OK] {out_dir.name} | adopted {adopted_count} | status {result.get('status')}")
    print(f"Section reasoning adopt complete: {total_adopted} decision(s) adopted across {processed} document(s).")
    return 1 if failed else 0


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
    if args.mode == "summary":
        return summary_mode(args)
    if args.mode == "review":
        return review_mode(args)
    if args.mode == "apply":
        return apply_mode(args)
    if args.mode == "adopt":
        return adopt_mode(args)
    return report_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
