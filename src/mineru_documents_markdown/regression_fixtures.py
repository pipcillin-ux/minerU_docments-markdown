#!/usr/bin/env python3
"""Build compact regression fixtures from rebuilt MinerU outputs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .build_structured_blocks import semantic_heading_level
from .io_utils import load_json, load_jsonl
from .structure_utils import is_numbered_prose_fragment, normalize_text, output_dirs


CATEGORY_TITLES = {
    "toc_boundary": "TOC/body boundary",
    "toc_backbone_tree": "TOC-backbone section tree",
    "body_heading_tree": "Body-heading section tree",
    "block_section_attachment": "Body block section attachment",
    "split_heading": "Split heading repair",
    "merged_broken_heading": "Merged broken heading",
    "numbered_prose_demoted": "Numbered prose demotion",
    "decision_demoted": "Demoted heading decision",
    "tree_render_heading": "Tree-driven Markdown heading level",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build structure regression fixtures from output/.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument(
        "--fixtures-dir",
        default="output/regression_fixtures",
        help="Directory for regression fixture JSON/Markdown reports.",
    )
    parser.add_argument("--max-per-category", type=int, default=8, help="Maximum samples per category.")
    return parser.parse_args()


def short_text(value: Any, limit: int = 220) -> str:
    text = normalize_text(str(value or ""))
    return text[:limit]


def context(blocks: list[dict[str, Any]], index: int, radius: int = 2) -> tuple[list[str], list[str]]:
    before: list[str] = []
    after: list[str] = []
    for block in blocks[max(0, index - radius) : index]:
        text = short_text(block.get("text"), 160)
        if text:
            before.append(text)
    for block in blocks[index + 1 : index + 1 + radius]:
        text = short_text(block.get("text"), 160)
        if text:
            after.append(text)
    return before, after


def block_index_by_id(blocks: list[dict[str, Any]]) -> dict[str, int]:
    return {str(block.get("block_id") or ""): index for index, block in enumerate(blocks)}


def make_block_sample(
    category: str,
    document: str,
    blocks: list[dict[str, Any]],
    index: int,
    expected: str,
    evidence: list[str],
) -> dict[str, Any]:
    block = blocks[index]
    before, after = context(blocks, index)
    return {
        "category": category,
        "document": document,
        "page": block.get("page"),
        "block_id": str(block.get("block_id") or ""),
        "block_type": block.get("block_type"),
        "region": block.get("region"),
        "heading_level": block.get("heading_level"),
        "tree_heading_level": block.get("tree_heading_level"),
        "section_id": block.get("section_id"),
        "tree_section_path": block.get("tree_section_path") or [],
        "text": short_text(block.get("text")),
        "expected": expected,
        "evidence": evidence,
        "context_before": before,
        "context_after": after,
    }


def make_tree_sample(
    category: str,
    document: str,
    node: dict[str, Any],
    expected: str,
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "category": category,
        "document": document,
        "page": node.get("start_page"),
        "block_id": str(node.get("source_block_id") or ""),
        "section_id": str(node.get("section_id") or ""),
        "level": node.get("level"),
        "title": short_text(node.get("title")),
        "tree_section_path": node.get("path") or [],
        "expected": expected,
        "evidence": evidence,
    }


def add_sample(
    samples: list[dict[str, Any]],
    counts: Counter[str],
    seen: set[tuple[str, str, str]],
    sample: dict[str, Any],
    max_per_category: int,
) -> None:
    category = str(sample.get("category") or "")
    if counts[category] >= max_per_category:
        return
    key = (category, str(sample.get("document") or ""), str(sample.get("block_id") or sample.get("section_id") or ""))
    if key in seen:
        return
    seen.add(key)
    samples.append(sample)
    counts[category] += 1


def collect_document_samples(out_dir: Path, max_per_category: int) -> list[dict[str, Any]]:
    blocks = load_jsonl(out_dir / "structured_blocks.jsonl")
    decisions = load_jsonl(out_dir / "heading_decisions.jsonl")
    section_tree = load_json(out_dir / "section_tree.json", {"nodes": []})
    if not blocks:
        return []

    document = out_dir.name
    samples: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str, str]] = set()
    index_by_id = block_index_by_id(blocks)

    for index, block in enumerate(blocks):
        if block.get("region") == "body" and block.get("include_in_semantic") is not False:
            add_sample(
                samples,
                counts,
                seen,
                make_block_sample(
                    "toc_boundary",
                    document,
                    blocks,
                    index,
                    "First semantic body block should start after front matter/TOC pollution.",
                    ["region:body", "include_in_semantic"],
                ),
                max_per_category,
            )
            break

    nodes = list(section_tree.get("nodes") or [])
    tree_source = str(section_tree.get("source") or "")
    if tree_source == "toc_backbone" and nodes:
        add_sample(
            samples,
            counts,
            seen,
            make_tree_sample(
                "toc_backbone_tree",
                document,
                nodes[0],
                "Sparse or shifted body headings should still produce a stable TOC-backed tree.",
                ["section_tree.source:toc_backbone", f"page_offset:{section_tree.get('page_offset')}"],
            ),
            max_per_category,
        )
    if tree_source == "body_headings" and nodes:
        add_sample(
            samples,
            counts,
            seen,
            make_tree_sample(
                "body_heading_tree",
                document,
                nodes[0],
                "Dense body headings should be used directly as the section tree.",
                ["section_tree.source:body_headings"],
            ),
            max_per_category,
        )

    for index, block in enumerate(blocks):
        if block.get("region") != "body" or block.get("include_in_semantic") is False:
            continue
        if block.get("block_type") != "heading" and block.get("section_id") and block.get("tree_section_path"):
            add_sample(
                samples,
                counts,
                seen,
                make_block_sample(
                    "block_section_attachment",
                    document,
                    blocks,
                    index,
                    "Body content should attach to the deepest covering section tree node.",
                    ["section_id", "tree_section_path"],
                ),
                max_per_category,
            )
            break

    for index, block in enumerate(blocks):
        action = str(block.get("repair_action") or "")
        if action in {"split_heading", "split_front_matter_heading"} or block.get("remaining_text"):
            add_sample(
                samples,
                counts,
                seen,
                make_block_sample(
                    "split_heading",
                    document,
                    blocks,
                    index,
                    "Heading-like prefix should be separated from glued prose.",
                    [f"repair_action:{action}", "remaining_text" if block.get("remaining_text") else "split_block"],
                ),
                max_per_category,
            )
            break

    for index, block in enumerate(blocks):
        if block.get("merged_from_item_indices"):
            add_sample(
                samples,
                counts,
                seen,
                make_block_sample(
                    "merged_broken_heading",
                    document,
                    blocks,
                    index,
                    "Conservatively merged broken heading lines should remain one heading block.",
                    ["merged_from_item_indices"],
                ),
                max_per_category,
            )
            break

    for index, block in enumerate(blocks):
        if block.get("block_type") == "paragraph" and is_numbered_prose_fragment(str(block.get("text") or "")):
            add_sample(
                samples,
                counts,
                seen,
                make_block_sample(
                    "numbered_prose_demoted",
                    document,
                    blocks,
                    index,
                    "Long numbered prose/list item should remain paragraph, not Markdown heading.",
                    ["is_numbered_prose_fragment", "block_type:paragraph"],
                ),
                max_per_category,
            )
            break

    for decision in decisions:
        if decision.get("action") != "demote_to_paragraph":
            continue
        block_id = str(decision.get("block_id") or "")
        index = index_by_id.get(block_id)
        if index is None:
            continue
        add_sample(
            samples,
            counts,
            seen,
            make_block_sample(
                "decision_demoted",
                document,
                blocks,
                index,
                "Audited heading candidate should be demoted to paragraph.",
                [str(decision.get("decision_source") or ""), short_text(decision.get("reason"), 120)],
            ),
            max_per_category,
        )
        break

    for index, block in enumerate(blocks):
        if block.get("region") != "body" or block.get("block_type") != "heading":
            continue
        render_level = semantic_heading_level(block)
        local_level = block.get("heading_level")
        if render_level and local_level and int(local_level) != render_level:
            sample = make_block_sample(
                "tree_render_heading",
                document,
                blocks,
                index,
                f"Semantic Markdown should render this body heading as H{render_level}.",
                [f"local_heading_level:{local_level}", f"render_heading_level:{render_level}"],
            )
            sample["render_heading_level"] = render_level
            add_sample(samples, counts, seen, sample, max_per_category)
            break

    return samples


def write_markdown_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Structure Regression Fixtures",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Documents scanned: {payload['documents_scanned']}",
        f"- Samples: {len(payload['samples'])}",
        "",
        "## Category Counts",
        "",
    ]
    for category, title in CATEGORY_TITLES.items():
        count = payload["category_counts"].get(category, 0)
        lines.append(f"- `{category}`: {count} - {title}")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in payload["samples"]:
        grouped.setdefault(str(sample.get("category") or ""), []).append(sample)
    for category, title in CATEGORY_TITLES.items():
        items = grouped.get(category, [])
        if not items:
            continue
        lines.extend(["", f"## {title}", ""])
        for sample in items:
            path_text = " > ".join(str(value) for value in sample.get("tree_section_path") or [])
            lines.append(
                f"- `{sample.get('document')}` p.{sample.get('page')} "
                f"`{sample.get('block_id') or sample.get('section_id')}`"
            )
            if path_text:
                lines.append(f"  - Path: {path_text}")
            if sample.get("text") or sample.get("title"):
                lines.append(f"  - Text: {sample.get('text') or sample.get('title')}")
            lines.append(f"  - Expected: {sample.get('expected')}")
            evidence = ", ".join(str(value) for value in sample.get("evidence") or [] if value)
            if evidence:
                lines.append(f"  - Evidence: {evidence}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = Path(args.output_dir)
    fixtures_dir = Path(args.fixtures_dir)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    collected: list[dict[str, Any]] = []
    for out_dir in output_dirs(root):
        collected.extend(collect_document_samples(out_dir, args.max_per_category))

    samples: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str, str]] = set()
    for sample in collected:
        add_sample(samples, counts, seen, sample, args.max_per_category)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "output_dir": str(root),
        "documents_scanned": len(output_dirs(root)),
        "category_counts": {category: counts.get(category, 0) for category in CATEGORY_TITLES},
        "samples": samples,
    }
    json_path = fixtures_dir / "structure_regression_samples.json"
    md_path = fixtures_dir / "structure_regression_samples.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown_report(md_path, payload)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Collected {len(samples)} samples across {len(output_dirs(root))} documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
