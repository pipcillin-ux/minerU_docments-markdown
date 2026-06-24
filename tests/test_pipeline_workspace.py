from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mineru_documents_markdown import run_pipeline
from mineru_documents_markdown.pipeline_workspace import (
    WorkspaceError,
    normalize_root_report_paths,
    prepare_workspace,
    publish_workspace,
    validate_workspace_paths,
)


def pipeline_args(output_dir: Path, work_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        pdf=None,
        docs_dir="docs",
        output_dir=str(output_dir),
        work_dir=str(work_dir),
        fresh_work=False,
        document=None,
        token=None,
        chunk_size=60,
        max_upload_mb=None,
        api_base=None,
        model_version=None,
        language=None,
        resubmit_failed=False,
        skip_parse=True,
        skip_review=True,
        semantic_scope="full",
        heading_strategy="rule",
        llm_confidence_threshold=0.72,
        candidate_min_score=0.18,
        llm_batch_size=20,
        repair_warn_with="none",
        review_output=None,
        heading_review_overrides=None,
        review_context_radius=3,
        review_limit=None,
        force_review=False,
        section_reasoning="none",
        skip_section_reasoning=False,
        section_reasoning_limit=None,
        section_reasoning_review_jobs=1,
        section_reasoning_min_confidence=None,
        section_reasoning_backup=False,
        fail_on="warn",
    )


class PipelineWorkspaceTests(unittest.TestCase):
    def test_seeded_workspace_is_isolated_from_published_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            output.mkdir()
            output.joinpath("result.txt").write_text("published", encoding="utf-8")

            status = prepare_workspace(output, work, skip_parse=True, fresh_work=False)
            work.joinpath("result.txt").write_text("staged", encoding="utf-8")

            self.assertTrue(status.startswith("seeded:"))
            self.assertEqual("published", output.joinpath("result.txt").read_text(encoding="utf-8"))
            self.assertEqual("staged", work.joinpath("result.txt").read_text(encoding="utf-8"))

    def test_existing_workspace_is_resumed_and_fresh_work_reseeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            output.mkdir()
            output.joinpath("result.txt").write_text("published", encoding="utf-8")
            work.mkdir()
            work.joinpath("resume.txt").write_text("resume", encoding="utf-8")

            self.assertEqual(
                "resumed",
                prepare_workspace(output, work, skip_parse=True, fresh_work=False),
            )
            self.assertTrue(work.joinpath("resume.txt").exists())

            status = prepare_workspace(output, work, skip_parse=True, fresh_work=True)
            self.assertTrue(status.startswith("seeded:"))
            self.assertFalse(work.joinpath("resume.txt").exists())
            self.assertEqual("published", work.joinpath("result.txt").read_text(encoding="utf-8"))

    def test_publish_replaces_output_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            output.mkdir()
            work.mkdir()
            output.joinpath("result.txt").write_text("old", encoding="utf-8")
            work.joinpath("result.txt").write_text("new", encoding="utf-8")

            publish_workspace(output, work)

            self.assertEqual("new", output.joinpath("result.txt").read_text(encoding="utf-8"))
            self.assertFalse(work.exists())

    def test_publish_failure_rolls_back_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            output.mkdir()
            work.mkdir()
            output.joinpath("result.txt").write_text("old", encoding="utf-8")
            work.joinpath("result.txt").write_text("new", encoding="utf-8")
            calls = 0

            def fail_second_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated publish failure")
                os.replace(source, destination)

            with self.assertRaises(WorkspaceError):
                publish_workspace(output, work, replace=fail_second_replace)

            self.assertEqual("old", output.joinpath("result.txt").read_text(encoding="utf-8"))
            self.assertEqual("new", work.joinpath("result.txt").read_text(encoding="utf-8"))

    def test_publish_interrupt_rolls_back_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            output.mkdir()
            work.mkdir()
            output.joinpath("result.txt").write_text("old", encoding="utf-8")
            work.joinpath("result.txt").write_text("new", encoding="utf-8")
            calls = 0

            def interrupt_second_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt
                os.replace(source, destination)

            with self.assertRaises(KeyboardInterrupt):
                publish_workspace(output, work, replace=interrupt_second_replace)

            self.assertEqual("old", output.joinpath("result.txt").read_text(encoding="utf-8"))
            self.assertEqual("new", work.joinpath("result.txt").read_text(encoding="utf-8"))

    def test_nested_workspace_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output"
            with self.assertRaises(WorkspaceError):
                validate_workspace_paths(output, output / ".work")

    def test_root_report_paths_are_normalized_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            work.mkdir()
            report = work / "summary.md"
            report.write_text(f"Output root: {work.resolve()}\n", encoding="utf-8")

            changed = normalize_root_report_paths(work, output)

            self.assertEqual(1, changed)
            self.assertEqual(f"Output root: {output.resolve()}\n", report.read_text(encoding="utf-8"))

    def test_pipeline_failure_keeps_output_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            output.mkdir()
            output.joinpath("result.txt").write_text("published", encoding="utf-8")
            args = pipeline_args(output, work)

            def fail_stage(name: str, command: list[str]) -> None:
                work.joinpath("partial.txt").write_text(name, encoding="utf-8")
                raise run_pipeline.PipelineError(name, 7)

            with patch.object(run_pipeline, "parse_args", return_value=args):
                with patch.object(run_pipeline, "run_stage", side_effect=fail_stage):
                    self.assertEqual(7, run_pipeline.main())

            self.assertEqual("published", output.joinpath("result.txt").read_text(encoding="utf-8"))
            self.assertFalse(output.joinpath("partial.txt").exists())
            self.assertTrue(work.joinpath("partial.txt").exists())

    def test_pipeline_publishes_only_after_all_stages_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            work = root / ".output.pipeline-work"
            output.mkdir()
            output.joinpath("result.txt").write_text("published", encoding="utf-8")
            args = pipeline_args(output, work)
            seen_stages: list[str] = []

            def pass_stage(name: str, command: list[str]) -> None:
                seen_stages.append(name)
                if "--output-dir" in command:
                    value = command[command.index("--output-dir") + 1]
                    self.assertEqual(work.resolve(), Path(value).resolve())
                work.joinpath("validated.txt").write_text(name, encoding="utf-8")

            with patch.object(run_pipeline, "parse_args", return_value=args):
                with patch.object(run_pipeline, "run_stage", side_effect=pass_stage):
                    self.assertEqual(0, run_pipeline.main())

            self.assertIn("Final output validation", seen_stages)
            self.assertTrue(output.joinpath("validated.txt").exists())
            self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()
