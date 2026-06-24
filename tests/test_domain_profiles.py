from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mineru_documents_markdown.domain_profiles import (
    load_domain_profile,
    strict_subsection_like,
)
from mineru_documents_markdown.semantic_rebuild import classify_block
from mineru_documents_markdown.toc_parser import split_stuck_toc_line


class DomainProfileTests(unittest.TestCase):
    def test_generic_profile_does_not_enable_tcm_rules(self) -> None:
        generic = load_domain_profile("generic")
        tcm = load_domain_profile("tcm")
        item = {"type": "text", "text": "概述"}

        generic_type, generic_level, *_ = classify_block(
            item,
            "body",
            set(),
            "full",
            {},
            generic,
        )
        tcm_type, tcm_level, *_ = classify_block(
            item,
            "body",
            set(),
            "full",
            {},
            tcm,
        )

        self.assertEqual(("paragraph", None), (generic_type, generic_level))
        self.assertEqual(("heading", 2), (tcm_type, tcm_level))
        self.assertFalse(strict_subsection_like("症状", generic))
        self.assertTrue(strict_subsection_like("（一）症状", tcm))

    def test_tcm_profile_splits_stuck_known_sections(self) -> None:
        generic = load_domain_profile("generic")
        tcm = load_domain_profile("tcm")
        line = "诊断要点23治疗24"

        self.assertEqual([line], split_stuck_toc_line(line, generic))
        self.assertEqual(["诊断要点23", "治疗24"], split_stuck_toc_line(line, tcm))

    def test_custom_toml_profile_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legal.toml"
            path.write_text(
                "\n".join(
                    [
                        "[profile]",
                        'name = "legal"',
                        'body_section_titles = ["裁判理由"]',
                        'strict_subsection_terms = ["争议焦点"]',
                        'image_table_keywords = ["附表"]',
                        'major_heading_keywords = ["判决"]',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            profile = load_domain_profile(path)

        self.assertEqual("legal", profile.name)
        self.assertEqual(("裁判理由",), profile.body_section_titles)
        self.assertEqual(("附表",), profile.image_table_keywords)


if __name__ == "__main__":
    unittest.main()
