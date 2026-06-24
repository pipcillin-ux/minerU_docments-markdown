from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

from mineru_documents_markdown import mineru_batch_parse, run_pipeline
from mineru_documents_markdown.section_reasoning import engine as section_reasoning
from mineru_documents_markdown.semantic_rebuild import parse_args as parse_build_args
from mineru_documents_markdown.warn_review import parse_args as parse_warn_args


class CliConfigurationTests(unittest.TestCase):
    def test_cli_parsers_expose_stable_defaults(self) -> None:
        parsers = [
            (mineru_batch_parse.parse_args, "mineru-batch-parse"),
            (run_pipeline.parse_args, "mineru-run-pipeline"),
            (section_reasoning.parse_args, "mineru-section-reasoning"),
            (parse_build_args, "mineru-build-structured-blocks"),
            (parse_warn_args, "mineru-warn-review"),
        ]
        for parser, program in parsers:
            with self.subTest(program=program), patch("sys.argv", [program]):
                args = parser()
            self.assertIsInstance(args, argparse.Namespace)
        with patch("sys.argv", ["mineru-run-pipeline"]):
            self.assertEqual("generic", run_pipeline.parse_args().domain_profile)
        with patch("sys.argv", ["mineru-batch-parse"]):
            self.assertEqual(200, mineru_batch_parse.parse_args().chunk_size)

    def test_pipeline_command_builders_forward_domain_profile(self) -> None:
        args = argparse.Namespace(
            pdf=None,
            docs_dir="docs",
            chunk_size=60,
            max_upload_mb=None,
            api_base=None,
            model_version=None,
            language=None,
            resubmit_failed=False,
            semantic_scope="full",
            heading_strategy="rule",
            llm_confidence_threshold=0.72,
            candidate_min_score=0.18,
            llm_batch_size=20,
            domain_profile="tcm",
            review_context_radius=3,
            review_limit=None,
            force_review=False,
            section_reasoning_limit=None,
            section_reasoning_review_jobs=4,
            section_reasoning_min_confidence=0.86,
            section_reasoning_backup=True,
        )
        build = run_pipeline.build_command(args, output_dir=Path("output"), document="book")
        quality = run_pipeline.heading_quality_command(
            args,
            output_dir=Path("output"),
            fail_on="warn",
            document="book",
        )
        reasoning = run_pipeline.section_reasoning_command(
            args,
            output_dir=Path("output"),
            mode="adopt",
            document="book",
        )
        parse = run_pipeline.parse_command(args, Path("output"))

        self.assertIn("tcm", build)
        self.assertIn("tcm", quality)
        self.assertIn("tcm", reasoning)
        self.assertIn("--docs-dir", parse)
        self.assertIn("--adoption-backup", reasoning)

    def test_pipeline_small_helpers(self) -> None:
        args = argparse.Namespace(document="chosen", pdf=None)
        self.assertEqual("chosen", run_pipeline.document_name_from_args(args))
        self.assertIn("hello world", run_pipeline.cmd_text(["hello world"]))
        self.assertEqual(
            [run_pipeline.sys.executable, "-m", "example.module", "--flag"],
            run_pipeline.module_cmd("example.module", "--flag"),
        )


if __name__ == "__main__":
    unittest.main()
