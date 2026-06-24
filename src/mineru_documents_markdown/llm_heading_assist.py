"""Optional OpenAI-compatible LLM heading decision assist."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .defaults import LLM_REVIEW_CONFIDENCE_THRESHOLD
from .domain_profiles import DomainProfile
from .heading_candidates import HeadingCandidate
from .heading_decisions import HeadingDecision, rule_decision_for_candidate
from .http_utils import retry_after_seconds as retry_after_seconds
from .http_utils import retry_delay

SYSTEM_PROMPT = """You repair PDF heading structure. Return strict JSON only.
For each candidate decide one action:
keep_heading, promote_to_heading, demote_to_paragraph, split_heading.
Use heading levels 1-6. If split_heading, put title in heading_text and prose in remaining_text.
Never rewrite content except splitting an obvious heading prefix from prose."""


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or os.environ.get(name.lower()) or default).strip()


def api_config() -> tuple[str, str, str]:
    api_key = env("DEEPSEEK_API_KEY") or env("OPENAI_API_KEY")
    base_url = env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = env("DEEPSEEK_MODEL", "deepseek-chat")
    return api_key, base_url.rstrip("/"), model


def cache_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def call_chat_completions(
    payload: dict[str, Any],
    timeout: int = 90,
    retries: int = 2,
    system_prompt: str = SYSTEM_PROMPT,
) -> dict[str, Any]:
    api_key, base_url, model = api_config()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY or OPENAI_API_KEY is not set.")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            content = response_data["choices"][0]["message"]["content"]
            return json.loads(content)
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code == 503 or 500 <= exc.code < 600
            if not retryable or attempt >= retries:
                raise RuntimeError(f"LLM heading assist failed: HTTP {exc.code}") from exc
            time.sleep(retry_delay(attempt, exc.headers.get("Retry-After"), cap=8))
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"LLM heading assist failed: {exc}") from exc
            time.sleep(min(2**attempt, 8))
    raise RuntimeError("LLM heading assist failed.")


def candidate_payload(
    document: str,
    candidate: HeadingCandidate,
    rule_decision: HeadingDecision,
    toc_context: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document": document,
        "candidate": candidate.to_dict(),
        "rule_decision": rule_decision.to_dict(),
        "toc_context": toc_context[:40],
        "schema": {
            "candidate_id": "string",
            "action": "keep_heading | promote_to_heading | demote_to_paragraph | split_heading",
            "is_heading": "boolean",
            "heading_text": "string",
            "remaining_text": "string",
            "level": "integer 1-6 or null",
            "parent_path": "array of strings",
            "confidence": "number 0-1",
            "reason": "short string",
        },
    }


def batch_payload(
    document: str,
    candidates: list[HeadingCandidate],
    fallbacks: list[HeadingDecision],
    toc_context: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document": document,
        "candidates": [
            {
                "candidate": candidate.to_dict(),
                "rule_decision": fallback.to_dict(),
            }
            for candidate, fallback in zip(candidates, fallbacks, strict=True)
        ],
        "toc_context": toc_context[:60],
        "instructions": [
            "Return one decision for every candidate_id.",
            "Do not omit candidates.",
            "Use split_heading when a heading prefix is glued to prose.",
            "Use demote_to_paragraph when a candidate is actually normal prose.",
            "Use promote_to_heading when MinerU treated a real heading as prose.",
        ],
        "schema": {
            "decisions": [
                {
                    "candidate_id": "string",
                    "action": "keep_heading | promote_to_heading | demote_to_paragraph | split_heading",
                    "is_heading": "boolean",
                    "heading_text": "string",
                    "remaining_text": "string",
                    "level": "integer 1-6 or null",
                    "parent_path": "array of strings",
                    "confidence": "number 0-1",
                    "reason": "short string",
                }
            ]
        },
    }


def decision_from_llm(data: dict[str, Any], fallback: HeadingDecision) -> HeadingDecision:
    action = str(data.get("action") or fallback.action)
    is_heading = bool(data.get("is_heading", fallback.is_heading))
    level = data.get("level", fallback.level)
    if level is not None:
        try:
            level = max(1, min(int(level), 6))
        except (TypeError, ValueError):
            level = fallback.level
    confidence = data.get("confidence", fallback.confidence)
    try:
        confidence = max(0.0, min(float(confidence), 1.0))
    except (TypeError, ValueError):
        confidence = fallback.confidence
    return HeadingDecision(
        candidate_id=fallback.candidate_id,
        block_id=fallback.block_id,
        action=action,
        is_heading=is_heading,
        heading_text=str(data.get("heading_text") or fallback.heading_text),
        remaining_text=str(data.get("remaining_text") or fallback.remaining_text),
        level=level,
        parent_path=list(data.get("parent_path") or fallback.parent_path),
        confidence=confidence,
        decision_source="llm",
        reason=str(data.get("reason") or "LLM heading assist decision."),
    )


def batch_decisions_from_llm(data: dict[str, Any], fallbacks: list[HeadingDecision]) -> list[HeadingDecision]:
    by_id = {str(item.get("candidate_id") or ""): item for item in data.get("decisions", []) if isinstance(item, dict)}
    decisions: list[HeadingDecision] = []
    for fallback in fallbacks:
        item = by_id.get(fallback.candidate_id)
        decisions.append(decision_from_llm(item, fallback) if item else fallback)
    return decisions


def assist_decision(
    document: str,
    candidate: HeadingCandidate,
    fallback: HeadingDecision,
    toc_context: list[dict[str, Any]],
    cache_dir: Path,
) -> HeadingDecision:
    payload = candidate_payload(document, candidate, fallback, toc_context)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{cache_key(payload)}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = call_chat_completions(payload)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return decision_from_llm(data, fallback)


def assist_decision_batch(
    document: str,
    candidates: list[HeadingCandidate],
    fallbacks: list[HeadingDecision],
    toc_context: list[dict[str, Any]],
    cache_dir: Path,
) -> list[HeadingDecision]:
    payload = batch_payload(document, candidates, fallbacks, toc_context)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{cache_key(payload)}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = call_chat_completions(payload)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return batch_decisions_from_llm(data, fallbacks)


def maybe_assist_decisions(
    document: str,
    candidates: list[HeadingCandidate],
    rule_decisions: list[HeadingDecision],
    toc_levels: dict[str, int],
    toc_paths: dict[str, list[str]],
    toc_context: list[dict[str, Any]],
    strategy: str,
    cache_dir: Path,
    threshold: float = LLM_REVIEW_CONFIDENCE_THRESHOLD,
    batch_size: int = 20,
    domain_profile: DomainProfile | None = None,
) -> list[HeadingDecision]:
    if strategy == "rule":
        return rule_decisions
    assisted: list[HeadingDecision] = []
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    pending_candidates: list[HeadingCandidate] = []
    pending_fallbacks: list[HeadingDecision] = []

    def flush_pending() -> None:
        if not pending_candidates:
            return
        try:
            assisted.extend(
                assist_decision_batch(
                    document,
                    list(pending_candidates),
                    list(pending_fallbacks),
                    toc_context,
                    cache_dir,
                )
            )
        except RuntimeError:
            assisted.extend(
                rule_decision_for_candidate(candidate, toc_levels, toc_paths, domain_profile)
                for candidate in pending_candidates
            )
        pending_candidates.clear()
        pending_fallbacks.clear()

    for fallback in rule_decisions:
        candidate = candidate_by_id.get(fallback.candidate_id)
        if candidate is None:
            assisted.append(fallback)
            continue
        should_call = strategy == "llm" or fallback.confidence < threshold
        if not should_call:
            flush_pending()
            assisted.append(fallback)
            continue
        pending_candidates.append(candidate)
        pending_fallbacks.append(fallback)
        if len(pending_candidates) >= max(1, batch_size):
            flush_pending()
    flush_pending()
    return assisted
