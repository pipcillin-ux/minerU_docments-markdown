#!/usr/bin/env python3
"""Review heading-quality WARN issues with an OpenAI-compatible LLM."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .io_utils import load_dotenv, load_json, load_jsonl
from .llm_heading_assist import api_config, cache_key, call_chat_completions
from .structure_utils import normalize_text, output_dirs

ALLOWED_ACTIONS = {"keep_heading", "promote_to_heading", "demote_to_paragraph", "split_heading"}
REVIEW_STATUSES = {"needs_fix", "benign_warn", "uncertain"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review heading-quality WARN issues with DeepSeek.")
    parser.add_argument("--output-dir", default="output", help="Root output directory.")
    parser.add_argument("--document", action="append", help="Only review this output directory name.")
    parser.add_argument(
        "--review-output",
        help="Review JSON path. Defaults to <output-dir>/heading_warn_deepseek_review.json.",
    )
    parser.add_argument(
        "--markdown-output",
        help="Markdown report path. Defaults to the review JSON path with .md suffix.",
    )
    parser.add_argument(
        "--cache-dir",
        help="LLM response cache directory. Defaults to <output-dir>/heading_warn_review_cache.",
    )
    parser.add_argument("--context-radius", type=int, default=3, help="Neighboring blocks before/after each issue.")
    parser.add_argument("--limit", type=int, help="Maximum number of WARN issues to review.")
    parser.add_argument("--force", action="store_true", help="Ignore cached LLM responses.")
    return parser.parse_args()


def short_text(value: Any, limit: int = 260) -> str:
    text = normalize_text(str(value or ""))
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def block_summary(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "block_type": block.get("block_type"),
        "heading_level": block.get("heading_level"),
        "text": short_text(block.get("text")),
        "section_path": block.get("section_path") or [],
    }


def issue_candidate(
    document: str,
    issue: dict[str, Any],
    block: dict[str, Any],
    previous_blocks: list[dict[str, Any]],
    next_blocks: list[dict[str, Any]],
    rule_decision: dict[str, Any],
) -> dict[str, Any]:
    block_id = str(issue.get("block_id") or block.get("block_id") or "")
    text = short_text(issue.get("text") or block.get("text"), 500)
    return {
        "candidate_id": f"{document}:{block_id}",
        "document": document,
        "issue_code": str(issue.get("code") or ""),
        "page": issue.get("page") or block.get("page"),
        "block_id": block_id,
        "text": text,
        "current_block_type": block.get("block_type"),
        "current_heading_level": block.get("heading_level"),
        "current_section_path": block.get("section_path") or [],
        "rule_decision": rule_decision,
        "previous_context": [block_summary(item) for item in previous_blocks],
        "next_context": [block_summary(item) for item in next_blocks],
    }


def collect_warn_candidates(
    output_root: Path,
    document_names: set[str] | None,
    context_radius: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    if limit is not None and limit <= 0:
        return []
    candidates: list[dict[str, Any]] = []
    for out_dir in output_dirs(output_root):
        if document_names and out_dir.name not in document_names:
            continue
        quality = load_json(out_dir / "heading_quality.json", {})
        issues = [
            issue for issue in quality.get("issues", []) if isinstance(issue, dict) and issue.get("severity") == "WARN"
        ]
        if not issues:
            continue
        blocks = load_jsonl(out_dir / "structured_blocks.jsonl")
        decisions = load_jsonl(out_dir / "heading_decisions.jsonl")
        blocks_by_id = {str(block.get("block_id") or ""): block for block in blocks}
        block_index = {str(block.get("block_id") or ""): index for index, block in enumerate(blocks)}
        decisions_by_block = {str(decision.get("block_id") or ""): decision for decision in decisions}
        for issue in issues:
            block_id = str(issue.get("block_id") or "")
            block = blocks_by_id.get(block_id, {})
            index = block_index.get(block_id)
            if index is None:
                previous_blocks: list[dict[str, Any]] = []
                next_blocks: list[dict[str, Any]] = []
            else:
                previous_blocks = blocks[max(0, index - context_radius) : index]
                next_blocks = blocks[index + 1 : index + 1 + context_radius]
            candidates.append(
                issue_candidate(
                    out_dir.name,
                    issue,
                    block,
                    previous_blocks,
                    next_blocks,
                    decisions_by_block.get(block_id, {}),
                )
            )
            if limit is not None and len(candidates) >= limit:
                return candidates
    return candidates


def review_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "Review one heading-quality WARN issue from a PDF-to-Markdown pipeline.",
        "candidate": candidate,
        "instructions": [
            "Decide whether the current block is a real heading or prose.",
            "Use demote_to_paragraph when a heading is actually normal prose.",
            "Use split_heading when a heading prefix is glued to prose.",
            "Use keep_heading when the WARN is benign.",
            "Use promote_to_heading only if the current block was not a heading but should be one.",
            "Do not rewrite content except splitting an obvious heading prefix from prose.",
        ],
        "schema": {
            "action": "keep_heading | promote_to_heading | demote_to_paragraph | split_heading",
            "is_heading": "boolean",
            "heading_text": "string",
            "remaining_text": "string",
            "level": "integer 1-6 or null",
            "confidence": "number 0-1",
            "reason": "short Chinese or English explanation",
            "review_status": "needs_fix | benign_warn | uncertain",
        },
    }


def unwrap_decision(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("deepseek"), dict):
        return data["deepseek"]
    if isinstance(data.get("decision"), dict):
        return data["decision"]
    decisions = data.get("decisions")
    if isinstance(decisions, list) and decisions and isinstance(decisions[0], dict):
        return decisions[0]
    return data


def clamp_confidence(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def normalize_decision(data: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    raw = unwrap_decision(data)
    candidate_text = normalize_text(str(candidate.get("text") or ""))
    current_level = candidate.get("current_heading_level")
    action = str(raw.get("action") or "keep_heading")
    if action not in ALLOWED_ACTIONS:
        action = "keep_heading"
    is_heading = bool(raw.get("is_heading", action != "demote_to_paragraph"))
    level = raw.get("level", current_level if is_heading else None)
    if level is not None:
        try:
            level = max(1, min(int(level), 6))
        except (TypeError, ValueError):
            level = current_level if isinstance(current_level, int) else None
    heading_text = normalize_text(str(raw.get("heading_text") or ""))
    remaining_text = normalize_text(str(raw.get("remaining_text") or ""))

    if action == "demote_to_paragraph":
        is_heading = False
        level = None
        heading_text = ""
        remaining_text = remaining_text or candidate_text
    elif action in {"keep_heading", "promote_to_heading"}:
        is_heading = True
        heading_text = heading_text or candidate_text
        remaining_text = ""
    elif action == "split_heading":
        is_heading = True
        heading_text = heading_text or candidate_text

    review_status = str(raw.get("review_status") or "")
    if review_status not in REVIEW_STATUSES:
        level_changed = isinstance(current_level, int) and isinstance(level, int) and level != current_level
        needs_fix = action in {"promote_to_heading", "demote_to_paragraph", "split_heading"} or level_changed
        review_status = "needs_fix" if needs_fix else "benign_warn"

    return {
        "candidate_id": str(raw.get("candidate_id") or candidate.get("candidate_id") or ""),
        "action": action,
        "is_heading": is_heading,
        "heading_text": heading_text,
        "remaining_text": remaining_text,
        "level": level,
        "confidence": clamp_confidence(raw.get("confidence")),
        "reason": str(raw.get("reason") or "Reviewed by DeepSeek heading WARN review."),
        "review_status": review_status,
    }


def review_with_cache(candidate: dict[str, Any], cache_dir: Path, force: bool) -> dict[str, Any]:
    payload = review_payload(candidate)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{cache_key(payload)}.json"
    if path.exists() and not force:
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = call_chat_completions(payload)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalize_decision(data, candidate)


def write_markdown_report(path: Path, payload: dict[str, Any]) -> None:
    counts = Counter(
        str(review.get("deepseek", {}).get("review_status") or "unknown") for review in payload.get("reviews", [])
    )
    lines = [
        "# DeepSeek Heading WARN Review",
        "",
        f"- Reviewed issues: {payload.get('count', 0)}",
        f"- Needs fix: {counts.get('needs_fix', 0)}",
        f"- Benign WARN: {counts.get('benign_warn', 0)}",
        f"- Uncertain: {counts.get('uncertain', 0)}",
        "",
        "## Reviews",
        "",
    ]
    reviews = payload.get("reviews", [])
    if not reviews:
        lines.append("- No WARN issues were found.")
    for review in reviews:
        candidate = review.get("candidate", {})
        decision = review.get("deepseek", {})
        text = str(candidate.get("text") or "").replace("\n", " ")
        reason = str(decision.get("reason") or "").replace("\n", " ")
        lines.append(
            "- "
            f"`{candidate.get('document')}` "
            f"p.{candidate.get('page')} "
            f"`{candidate.get('issue_code')}` "
            f"`{decision.get('review_status')}` "
            f"`{decision.get('action')}`: {text}"
        )
        if reason:
            lines.append(f"  - Reason: {reason}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    load_dotenv()
    output_root = Path(args.output_dir)
    review_output = (
        Path(args.review_output) if args.review_output else output_root / "heading_warn_deepseek_review.json"
    )
    markdown_output = Path(args.markdown_output) if args.markdown_output else review_output.with_suffix(".md")
    cache_dir = Path(args.cache_dir) if args.cache_dir else output_root / "heading_warn_review_cache"
    document_names = set(args.document) if args.document else None

    candidates = collect_warn_candidates(
        output_root,
        document_names,
        max(0, args.context_radius),
        args.limit,
    )
    api_key, base_url, model = api_config()
    if candidates and not api_key:
        print("DEEPSEEK_API_KEY or OPENAI_API_KEY is required to review WARN issues.", file=sys.stderr)
        return 2

    reviews: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        print(
            f"[{index}/{len(candidates)}] Reviewing {candidate['document']} "
            f"p.{candidate.get('page')} {candidate.get('issue_code')}: {candidate.get('text')}"
        )
        decision = review_with_cache(candidate, cache_dir, args.force)
        reviews.append({"candidate": candidate, "deepseek": decision})

    payload = {
        "count": len(reviews),
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "base_url": base_url,
        "source": {
            "output_dir": str(output_root),
            "documents": sorted(document_names) if document_names else "all",
            "severity": "WARN",
        },
        "reviews": reviews,
    }
    review_output.parent.mkdir(parents=True, exist_ok=True)
    review_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown_report(markdown_output, payload)
    print(f"Heading WARN review complete: {len(reviews)} issue(s).")
    print(f"Wrote {review_output}")
    print(f"Wrote {markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
