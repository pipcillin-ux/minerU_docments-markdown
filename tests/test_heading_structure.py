from __future__ import annotations

import unittest

from mineru_documents_markdown.build_structured_blocks import heading_stack_path, update_heading_stack
from mineru_documents_markdown.heading_quality import check_structured_blocks


def heading(block_id: str, level: int | None, text: str, region: str = "body") -> dict[str, object]:
    return {
        "block_id": block_id,
        "block_type": "heading",
        "heading_level": level,
        "text": text,
        "region": region,
        "page": 1,
    }


class HeadingStructureTests(unittest.TestCase):
    def test_heading_stack_tracks_h1_through_h6_and_clears_descendants(self) -> None:
        stack = [""] * 6
        for level in range(1, 7):
            stack = update_heading_stack(stack, level, f"H{level}")
        self.assertEqual(["H1", "H2", "H3", "H4", "H5", "H6"], heading_stack_path(stack))

        stack = update_heading_stack(stack, 3, "H3-new")
        self.assertEqual(["H1", "H2", "H3-new"], heading_stack_path(stack))

    def test_invalid_semantic_heading_level_is_fail(self) -> None:
        issues = check_structured_blocks([heading("bad", None, "Invalid")], {"nodes": []})
        self.assertIn("semantic_heading_invalid_level", {issue.code for issue in issues})
        self.assertIn("FAIL", {issue.severity for issue in issues})

    def test_demoted_paragraph_does_not_hide_heading_jump(self) -> None:
        blocks = [
            heading("h1", 1, "Root"),
            {
                "block_id": "paragraph",
                "block_type": "paragraph",
                "heading_level": None,
                "text": "Demoted heading text",
                "region": "body",
                "page": 1,
            },
            heading("h4", 4, "Deep"),
        ]
        issues = check_structured_blocks(blocks, {"nodes": []})
        self.assertIn("heading_level_jump", {issue.code for issue in issues})

    def test_cross_region_heading_does_not_create_false_jump(self) -> None:
        blocks = [
            heading("front", 1, "Front", region="front_matter"),
            heading("body", 4, "Body", region="body"),
        ]
        issues = check_structured_blocks(blocks, {"nodes": []})
        self.assertNotIn("heading_level_jump", {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()
