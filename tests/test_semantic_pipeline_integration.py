from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from mineru_documents_markdown import heading_quality, profile_documents, regression_fixtures, validate_outputs
from mineru_documents_markdown.domain_profiles import load_domain_profile
from mineru_documents_markdown.io_utils import load_jsonl, write_jsonl
from mineru_documents_markdown.section_reasoning import engine as section_reasoning
from mineru_documents_markdown.semantic_rebuild import build_for_output_dir


def write_pdf(path: Path, page_count: int) -> None:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=300, height=400)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)


def create_mineru_fixture(root: Path) -> tuple[Path, Path]:
    output_root = root / "output"
    out_dir = output_root / "sample"
    extracted = out_dir / "parts" / "part_001_1-3" / "extracted"
    images = extracted / "images"
    images.mkdir(parents=True)
    images.joinpath("chart.png").write_bytes(b"png")

    part_markdown = "# 第一章 总论\n\n正文内容。\n\n![chart](images/chart.png)\n"
    part_path = extracted / "sample.md"
    part_path.write_text(part_markdown, encoding="utf-8")
    out_dir.joinpath("assets", "part_001", "images").mkdir(parents=True)
    out_dir.joinpath("assets", "part_001", "images", "chart.png").write_bytes(b"png")

    items = [
        {"type": "text", "text": "目录", "page_idx": 0},
        {"type": "text", "text": "第一章 总论 2", "page_idx": 0},
        {"type": "text", "text": "概述2诊断要点3", "page_idx": 0},
        {"type": "text", "text": "第一章 总论", "text_level": 1, "page_idx": 1},
        {
            "type": "text",
            "text": "本章介绍疾病的基本概念、临床特点和诊疗原则。",
            "page_idx": 1,
        },
        {"type": "text", "text": "概述", "page_idx": 1},
        {"type": "text", "text": "这里是概述正文，包含足够的文字用于正文识别。", "page_idx": 1},
        {"type": "text", "text": "（一）症状", "page_idx": 1},
        {"type": "text", "text": "常见症状包括疼痛、乏力以及活动受限。", "page_idx": 1},
        {
            "type": "table",
            "table_caption": ["诊断评分表"],
            "table_body": "| 项目 | 评分 |\n|---|---|\n| 症状 | 2 |",
            "page_idx": 1,
        },
        {
            "type": "image",
            "img_path": "images/chart.png",
            "image_caption": ["诊断评分表"],
            "content": "该图展示诊断评分项目，用于说明不同指标的对应关系。",
            "page_idx": 1,
        },
        {"type": "text", "text": "第二章 治疗", "text_level": 1, "page_idx": 2},
        {"type": "text", "text": "治疗应结合病情、体质和检查结果综合制定。", "page_idx": 2},
        {"type": "text", "text": "治疗", "page_idx": 2},
        {"type": "list", "list_items": ["一般治疗", "药物治疗"], "page_idx": 2},
    ]
    extracted.joinpath("sample_content_list.json").write_text(
        json.dumps(items, ensure_ascii=False),
        encoding="utf-8",
    )

    write_pdf(out_dir / "chunks" / "part_001_1-3.pdf", 3)
    tasks = [
        {
            "index": 1,
            "page_range": "1-3",
            "status": "done",
            "chunk_path": "chunks/part_001_1-3.pdf",
            "md_path": "parts/part_001_1-3/extracted/sample.md",
            "error": None,
        }
    ]
    out_dir.joinpath("tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    merged = (
        "<!-- MinerU batch: pages 1-3 -->\n\n"
        "# 第一章 总论\n\n正文内容。\n\n![chart](assets/part_001/images/chart.png)\n"
    )
    out_dir.joinpath("sample.md").write_text(merged, encoding="utf-8")
    return output_root, out_dir


class SemanticPipelineIntegrationTests(unittest.TestCase):
    def test_end_to_end_rebuild_diagnostics_and_reasoning_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root, out_dir = create_mineru_fixture(Path(temp_dir))
            profile = load_domain_profile("tcm")

            validation, validation_issues = validate_outputs.validate_output_dir(out_dir)
            self.assertEqual(0, validation["issues"])
            self.assertEqual([], validation_issues)

            blocks, semantic_blocks = build_for_output_dir(out_dir, domain_profile=profile)
            self.assertGreater(blocks, 10)
            self.assertGreater(semantic_blocks, 5)
            structured = load_jsonl(out_dir / "structured_blocks.jsonl")
            self.assertTrue(any(block.get("image_table_candidate") for block in structured))
            self.assertTrue(any(block.get("section_id") for block in structured if block.get("region") == "body"))

            document_profile, diagnostics, _ = profile_documents.profile_output_dir(out_dir, profile)
            self.assertEqual("sample", document_profile["document"])
            self.assertGreaterEqual(diagnostics["image_table_candidate_count"], 1)

            summary, issues = heading_quality.quality_for_output_dir(out_dir, profile)
            self.assertIn(summary["status"], {"OK", "WARN", "FAIL"})
            heading_quality.write_markdown_report(out_dir, summary, issues)

            samples = regression_fixtures.collect_document_samples(out_dir, 4)
            self.assertTrue(samples)

            collect_args = argparse.Namespace(
                output_dir=str(output_root),
                document=None,
                limit=None,
                max_per_document=40,
                max_per_type=12,
                context_radius=3,
                low_confidence_threshold=0.72,
            )
            written = section_reasoning.collect_mode(collect_args)
            self.assertEqual([out_dir / "section_reasoning_candidates.jsonl"], written)
            candidates = load_jsonl(section_reasoning.candidate_path(out_dir))
            self.assertTrue(candidates)
            write_jsonl(
                section_reasoning.decision_path(out_dir),
                [
                    {
                        "candidate_id": candidates[0]["candidate_id"],
                        "action": "keep",
                        "confidence": 0.99,
                        "decision_source": "test",
                    }
                ],
            )

            applied = section_reasoning.apply_decisions_for_document(
                out_dir,
                min_confidence=0.86,
                domain_profile=profile,
            )
            self.assertEqual("ok", applied["status"])
            self.assertEqual([], applied["applied"])
            section_reasoning.write_apply_report(out_dir, applied)

            adopted = section_reasoning.adopt_decisions_for_document(
                out_dir,
                min_confidence=0.86,
                target="main",
                domain_profile=profile,
            )
            self.assertEqual("no_adoptable_decisions", adopted["status"])

            summary_args = argparse.Namespace(
                output_dir=str(output_root),
                document=None,
                min_confidence=0.86,
                mode="summary",
            )
            self.assertEqual(0, section_reasoning.summary_mode(summary_args))
            self.assertTrue((output_root / "section_reasoning_summary.csv").exists())

    def test_command_mains_on_synthetic_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root, _ = create_mineru_fixture(Path(temp_dir))
            fixtures_dir = output_root / "fixtures"

            commands = [
                (
                    validate_outputs.main,
                    ["mineru-validate-outputs", "--output-dir", str(output_root)],
                ),
                (
                    profile_documents.main,
                    [
                        "mineru-profile-documents",
                        "--output-dir",
                        str(output_root),
                        "--domain-profile",
                        "tcm",
                    ],
                ),
            ]
            for main, argv in commands:
                with self.subTest(command=argv[0]), patch("sys.argv", argv):
                    self.assertEqual(0, main())

            build_for_output_dir(output_root / "sample", domain_profile=load_domain_profile("tcm"))
            with patch(
                "sys.argv",
                [
                    "mineru-heading-quality",
                    "--output-dir",
                    str(output_root),
                    "--domain-profile",
                    "tcm",
                    "--fail-on",
                    "none",
                ],
            ):
                self.assertEqual(0, heading_quality.main())
            with patch(
                "sys.argv",
                [
                    "mineru-build-regression-fixtures",
                    "--output-dir",
                    str(output_root),
                    "--fixtures-dir",
                    str(fixtures_dir),
                ],
            ):
                self.assertEqual(0, regression_fixtures.main())


if __name__ == "__main__":
    unittest.main()
