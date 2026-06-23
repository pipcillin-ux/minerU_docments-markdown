#!/usr/bin/env python3
"""Run the full MinerU parse, diagnose, rebuild, and repair pipeline."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from .mineru_batch_parse import output_document_dir


class PipelineError(RuntimeError):
    def __init__(self, stage: str, returncode: int) -> None:
        super().__init__(f"{stage} failed with exit code {returncode}.")
        self.stage = stage
        self.returncode = returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full MinerU PDF-to-semantic-Markdown pipeline.")
    parser.add_argument("--pdf", help="Process one PDF. Output is placed under --output-dir/<document-name>.")
    parser.add_argument("--docs-dir", default="docs", help="PDF directory used when --pdf is omitted.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument("--document", help="Only rebuild/review/check this existing output document name.")
    parser.add_argument("--token", help="MinerU API token passed to mineru-batch-parse.")
    parser.add_argument("--chunk-size", type=int, default=60, help="PDF pages per MinerU chunk.")
    parser.add_argument("--max-upload-mb", type=int, help="Maximum MinerU upload chunk size in MB.")
    parser.add_argument("--api-base", help="MinerU API base URL.")
    parser.add_argument("--model-version", help="MinerU model version.")
    parser.add_argument("--language", help="MinerU document language.")
    parser.add_argument("--resubmit-failed", action="store_true", help="Resubmit failed MinerU tasks.")
    parser.add_argument("--skip-parse", action="store_true", help="Skip MinerU parsing and use existing output.")
    parser.add_argument("--skip-review", action="store_true", help="Skip DeepSeek WARN review even if enabled.")
    parser.add_argument(
        "--semantic-scope",
        choices=("full", "body"),
        default="full",
        help="Semantic Markdown scope passed to mineru-build-structured-blocks.",
    )
    parser.add_argument(
        "--heading-strategy",
        choices=("rule", "llm", "hybrid"),
        default="rule",
        help="Initial heading decision strategy.",
    )
    parser.add_argument("--llm-confidence-threshold", type=float, default=0.72)
    parser.add_argument("--candidate-min-score", type=float, default=0.18)
    parser.add_argument("--llm-batch-size", type=int, default=20)
    parser.add_argument(
        "--repair-warn-with",
        choices=("none", "deepseek"),
        default="deepseek",
        help="How to repair WARN-level heading-quality issues.",
    )
    parser.add_argument(
        "--review-output",
        help="DeepSeek review JSON path. Defaults to <output-dir>/heading_warn_deepseek_review.json.",
    )
    parser.add_argument(
        "--heading-review-overrides",
        help="Existing review JSON applied during the initial semantic rebuild.",
    )
    parser.add_argument("--review-context-radius", type=int, default=3)
    parser.add_argument("--review-limit", type=int, help="Maximum WARN issues to review.")
    parser.add_argument("--force-review", action="store_true", help="Ignore cached DeepSeek review responses.")
    parser.add_argument(
        "--section-reasoning",
        choices=("none", "collect", "review", "apply", "adopt"),
        default="none",
        help="Optional section-tree LLM reasoning stage.",
    )
    parser.add_argument("--skip-section-reasoning", action="store_true", help="Skip optional section reasoning.")
    parser.add_argument("--section-reasoning-limit", type=int, help="Maximum section-reasoning candidates to process.")
    parser.add_argument(
        "--section-reasoning-review-jobs",
        type=int,
        default=1,
        help="Parallel LLM workers for section reasoning review.",
    )
    parser.add_argument(
        "--section-reasoning-min-confidence",
        type=float,
        help="Minimum section-reasoning confidence for apply/adopt.",
    )
    parser.add_argument(
        "--section-reasoning-backup",
        action="store_true",
        help="Write .pre-adopt backups when section reasoning adopts main outputs.",
    )
    parser.add_argument(
        "--fail-on",
        choices=("none", "warn", "fail"),
        default="fail",
        help="Final heading-quality failure threshold.",
    )
    return parser.parse_args()


def cmd_text(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run_stage(name: str, cmd: Sequence[str]) -> None:
    print(f"\n==> {name}")
    print(f"$ {cmd_text(cmd)}")
    started = time.monotonic()
    result = subprocess.run(list(cmd), check=False)
    elapsed = time.monotonic() - started
    if result.returncode:
        print(f"<== {name} failed in {elapsed:.1f}s")
        raise PipelineError(name, result.returncode)
    print(f"<== {name} complete in {elapsed:.1f}s")


def module_cmd(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]


def document_name_from_args(args: argparse.Namespace) -> str | None:
    if args.document:
        return args.document
    if args.pdf:
        return output_document_dir(Path(args.output_dir), Path(args.pdf)).name
    return None


def parse_command(args: argparse.Namespace) -> list[str]:
    cmd = module_cmd("mineru_documents_markdown.mineru_batch_parse")
    if args.pdf:
        out_dir = output_document_dir(Path(args.output_dir), Path(args.pdf))
        cmd.extend(["--pdf", args.pdf, "--out", str(out_dir)])
    else:
        cmd.extend(["--docs-dir", args.docs_dir, "--out", args.output_dir])
    cmd.extend(["--chunk-size", str(args.chunk_size)])
    if args.token:
        cmd.extend(["--token", args.token])
    if args.max_upload_mb is not None:
        cmd.extend(["--max-upload-mb", str(args.max_upload_mb)])
    if args.api_base:
        cmd.extend(["--api-base", args.api_base])
    if args.model_version:
        cmd.extend(["--model-version", args.model_version])
    if args.language:
        cmd.extend(["--language", args.language])
    if args.resubmit_failed:
        cmd.append("--resubmit-failed")
    return cmd


def build_command(
    args: argparse.Namespace,
    *,
    document: str | None = None,
    review_output: Path | None = None,
) -> list[str]:
    cmd = module_cmd(
        "mineru_documents_markdown.build_structured_blocks",
        "--output-dir",
        args.output_dir,
        "--semantic-scope",
        args.semantic_scope,
        "--heading-strategy",
        args.heading_strategy,
        "--llm-confidence-threshold",
        str(args.llm_confidence_threshold),
        "--candidate-min-score",
        str(args.candidate_min_score),
        "--llm-batch-size",
        str(args.llm_batch_size),
    )
    if document:
        cmd.extend(["--document", document])
    if review_output:
        cmd.extend(["--heading-review-overrides", str(review_output)])
    return cmd


def heading_quality_command(args: argparse.Namespace, *, fail_on: str, document: str | None = None) -> list[str]:
    cmd = module_cmd(
        "mineru_documents_markdown.heading_quality",
        "--output-dir",
        args.output_dir,
        "--fail-on",
        fail_on,
    )
    if document:
        cmd.extend(["--document", document])
    return cmd


def review_command(args: argparse.Namespace, review_output: Path, document: str | None) -> list[str]:
    cmd = module_cmd(
        "mineru_documents_markdown.warn_review",
        "--output-dir",
        args.output_dir,
        "--review-output",
        str(review_output),
        "--context-radius",
        str(args.review_context_radius),
    )
    if document:
        cmd.extend(["--document", document])
    if args.review_limit is not None:
        cmd.extend(["--limit", str(args.review_limit)])
    if args.force_review:
        cmd.append("--force")
    return cmd


def section_reasoning_command(args: argparse.Namespace, mode: str, document: str | None) -> list[str]:
    cmd = module_cmd(
        "mineru_documents_markdown.section_reasoning",
        "--output-dir",
        args.output_dir,
        "--mode",
        mode,
    )
    if document:
        cmd.extend(["--document", document])
    if args.section_reasoning_limit is not None and mode in {"collect", "review"}:
        cmd.extend(["--limit", str(args.section_reasoning_limit)])
    if mode == "review" and args.section_reasoning_review_jobs:
        cmd.extend(["--review-jobs", str(args.section_reasoning_review_jobs)])
    if args.section_reasoning_min_confidence is not None and mode in {"summary", "apply", "adopt"}:
        cmd.extend(["--min-confidence", str(args.section_reasoning_min_confidence)])
    if mode == "adopt":
        cmd.extend(["--target", "main"])
        if args.section_reasoning_backup:
            cmd.append("--adoption-backup")
    return cmd


def run_section_reasoning(args: argparse.Namespace, document: str | None) -> bool:
    if args.skip_section_reasoning or args.section_reasoning == "none":
        print("==> Section reasoning skipped")
        return False

    run_stage("Section reasoning collect", section_reasoning_command(args, "collect", document))
    if args.section_reasoning == "collect":
        run_stage("Section reasoning report", section_reasoning_command(args, "report", document))
        run_stage("Section reasoning summary", section_reasoning_command(args, "summary", None))
        return False
    if args.section_reasoning == "review":
        run_stage("Section reasoning review", section_reasoning_command(args, "review", document))
        run_stage("Section reasoning report", section_reasoning_command(args, "report", document))
        run_stage("Section reasoning summary", section_reasoning_command(args, "summary", None))
        return False
    if args.section_reasoning == "apply":
        run_stage("Section reasoning apply sidecars", section_reasoning_command(args, "apply", document))
        run_stage("Section reasoning summary", section_reasoning_command(args, "summary", None))
        return False
    if args.section_reasoning == "adopt":
        run_stage("Section reasoning adopt main outputs", section_reasoning_command(args, "adopt", document))
        run_stage("Section reasoning summary", section_reasoning_command(args, "summary", None))
        return True
    return False


def reviewed_documents(review_output: Path) -> list[str]:
    if not review_output.exists():
        return []
    payload = json.loads(review_output.read_text(encoding="utf-8"))
    documents = {
        str(review.get("candidate", {}).get("document") or "")
        for review in payload.get("reviews", [])
        if isinstance(review, dict)
    }
    return sorted(document for document in documents if document)


def main() -> int:
    args = parse_args()
    document = document_name_from_args(args)
    review_output = Path(args.review_output) if args.review_output else Path(args.output_dir) / "heading_warn_deepseek_review.json"

    try:
        initial_review_overrides = Path(args.heading_review_overrides) if args.heading_review_overrides else None
        if args.skip_parse:
            print("==> Parse skipped")
        else:
            run_stage("MinerU parse", parse_command(args))

        run_stage("Validate MinerU outputs", module_cmd("mineru_documents_markdown.validate_outputs", "--output-dir", args.output_dir))
        run_stage("Profile documents", module_cmd("mineru_documents_markdown.profile_documents", "--output-dir", args.output_dir))
        run_stage("Build semantic Markdown", build_command(args, document=document, review_output=initial_review_overrides))
        run_stage("Initial heading quality", heading_quality_command(args, fail_on="none", document=document))

        if args.repair_warn_with == "deepseek" and not args.skip_review:
            run_stage("DeepSeek WARN review", review_command(args, review_output, document))
            documents = reviewed_documents(review_output)
            if documents:
                for name in documents:
                    run_stage(
                        f"Rebuild semantic Markdown with review overrides: {name}",
                        build_command(args, document=name, review_output=review_output),
                    )
                run_stage("Refresh document profiles", module_cmd("mineru_documents_markdown.profile_documents", "--output-dir", args.output_dir))
            else:
                print("==> No WARN reviews found; repair rebuild skipped.")
        else:
            print("==> DeepSeek WARN review skipped")

        run_stage("Final heading quality", heading_quality_command(args, fail_on=args.fail_on, document=document))
        section_reasoning_adopted = run_section_reasoning(args, document)
        if section_reasoning_adopted:
            run_stage(
                "Final heading quality after section reasoning adoption",
                heading_quality_command(args, fail_on=args.fail_on, document=document),
            )
        run_stage("Final output validation", module_cmd("mineru_documents_markdown.validate_outputs", "--output-dir", args.output_dir))
    except PipelineError as exc:
        print(f"Pipeline stopped at stage: {exc.stage}", file=sys.stderr)
        return exc.returncode

    print("\nPipeline complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
