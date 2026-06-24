from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mineru_documents_markdown.section_reasoning import (
    ancestor_effective_end_index,
    block_index_by_id,
    build_reasoned_candidate_for_document,
    collect_candidates_for_document,
    natural_node_end_indexes,
    recompute_reasoned_ranges,
)


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def block(
    block_id: str,
    text: str,
    *,
    block_type: str = "heading",
    heading_level: int | None = None,
    section_id: str = "",
) -> dict[str, object]:
    return {
        "block_id": block_id,
        "text": text,
        "original_text": text,
        "remaining_text": "",
        "block_type": block_type,
        "heading_level": heading_level,
        "region": "body",
        "include_in_semantic": True,
        "page": 1,
        "section_id": section_id,
        "tree_section_path": ["父章节"],
    }


def node(
    section_id: str,
    title: str,
    level: int,
    start_block_id: str,
    end_block_id: str,
    *,
    parent_id: str = "",
    order: int = 1,
) -> dict[str, object]:
    parent_path = ["父章节"] if parent_id else []
    return {
        "section_id": section_id,
        "title": title,
        "normalized_key": title,
        "level": level,
        "parent_id": parent_id,
        "parent_path": parent_path,
        "path": [*parent_path, title],
        "document_order": order,
        "start_page": 1,
        "end_page": 1,
        "start_block_id": start_block_id,
        "end_block_id": end_block_id,
        "source_block_id": start_block_id,
        "source_heading_level": level,
        "toc_heading_level": level,
        "region": "body",
        "confidence": 0.9,
        "evidence": [],
    }


class SectionReasoningRangeTests(unittest.TestCase):
    def test_collect_skips_heading_already_anchored_by_section_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            blocks = [block("b1", "子章节", heading_level=2, section_id="sec_1")]
            write_jsonl(out_dir / "structured_blocks.jsonl", blocks)
            write_json(
                out_dir / "section_tree.json",
                {"nodes": [node("sec_1", "父章节", 1, "b1", "b1")]},
            )
            write_json(out_dir / "toc_tree.json", {"nodes": []})

            candidates = collect_candidates_for_document(
                out_dir,
                context_radius=1,
                max_per_document=20,
                max_per_type=10,
                low_confidence_threshold=0.72,
            )

            self.assertFalse(
                any(item.get("candidate_type") == "local_heading_under_tree_node" for item in candidates)
            )

    def test_existing_source_anchor_is_rejected_without_range_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            blocks = [
                block("b1", "父章节", heading_level=1, section_id="sec_1"),
                block("b2", "已有子章节", heading_level=2, section_id="sec_2"),
                block("b3", "正文", block_type="paragraph", section_id="sec_2"),
            ]
            nodes = [
                node("sec_1", "父章节", 1, "b1", "b3", order=1),
                node("sec_2", "已有子章节", 2, "b2", "b3", parent_id="sec_1", order=2),
            ]
            write_jsonl(out_dir / "structured_blocks.jsonl", blocks)
            write_json(out_dir / "section_tree.json", {"document": "test", "nodes": nodes})
            write_json(out_dir / "toc_tree.json", {"nodes": []})
            write_jsonl(
                out_dir / "section_reasoning_candidates.jsonl",
                [
                    {
                        "candidate_id": "candidate-1",
                        "current_block": {"block_id": "b2", "text": "已有子章节"},
                    }
                ],
            )
            write_jsonl(
                out_dir / "section_reasoning_decisions.jsonl",
                [
                    {
                        "candidate_id": "candidate-1",
                        "action": "insert_child_section",
                        "target_parent_id": "sec_1",
                        "title": "已有子章节",
                        "level": 2,
                        "confidence": 0.95,
                        "decision_source": "llm_section_reasoning",
                    }
                ],
            )

            result = build_reasoned_candidate_for_document(
                out_dir,
                min_confidence=0.86,
                require_llm_source=True,
            )

            self.assertEqual([], result["applied"])
            self.assertEqual("source_already_section_node", result["rejected"][0]["reason"])
            self.assertEqual(nodes, result["section_payload"]["nodes"])

    def test_insert_extends_only_parent_chain_to_natural_boundary(self) -> None:
        blocks = [
            block("b1", "父章节", heading_level=2, section_id="sec_1"),
            block("b2", "父章节正文", block_type="paragraph", section_id="sec_1"),
            block("b3", "新增子章节", heading_level=3),
            block("b4", "子章节正文", block_type="paragraph"),
            block("b5", "下一节", heading_level=2, section_id="sec_2"),
        ]
        parent = node("sec_1", "父章节", 2, "b1", "b1", order=1)
        sibling = node("sec_2", "下一节", 2, "b5", "b5", order=2)
        inserted = node(
            "llm_sec_000001",
            "新增子章节",
            3,
            "b3",
            "b3",
            parent_id="sec_1",
            order=0,
        )
        inserted["reasoning_action"] = "insert_child_section"

        result = recompute_reasoned_ranges([parent, sibling], [inserted], blocks)
        result_by_id = {item["section_id"]: item for item in result}

        self.assertEqual("b4", result_by_id["llm_sec_000001"]["end_block_id"])
        self.assertEqual("b4", result_by_id["sec_1"]["end_block_id"])
        self.assertEqual("b5", result_by_id["sec_2"]["end_block_id"])

    def test_existing_valid_range_can_widen_inferred_toc_boundary(self) -> None:
        blocks = [
            block("b1", "父章节", heading_level=2, section_id="sec_1"),
            block("b2", "目录骨架中的相邻节点", heading_level=2, section_id="sec_2"),
            block("b3", "正文", block_type="paragraph", section_id="sec_1"),
            block("b4", "真实子章节", heading_level=3, section_id="sec_1"),
            block("b5", "子章节正文", block_type="paragraph", section_id="sec_1"),
        ]
        parent = node("sec_1", "父章节", 2, "b1", "b5", order=1)
        adjacent = node("sec_2", "相邻节点", 2, "b2", "b2", order=2)
        nodes = [parent, adjacent]
        indexes = block_index_by_id(blocks)

        boundary = ancestor_effective_end_index(
            parent,
            node_by_id={"sec_1": parent, "sec_2": adjacent},
            natural_ends=natural_node_end_indexes(nodes, blocks),
            indexes=indexes,
        )

        self.assertEqual(indexes["b5"], boundary)


if __name__ == "__main__":
    unittest.main()
