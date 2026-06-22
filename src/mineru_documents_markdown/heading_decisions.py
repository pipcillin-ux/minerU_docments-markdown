"""Heading decision schema and rule-based fallback engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .heading_candidates import SENTENCE_PUNCTUATION, HeadingCandidate
from .structure_utils import (
    heading_level_from_text,
    heading_key,
    is_probable_body_section,
    is_probable_major_heading,
    is_probable_numbered_subsection,
    normalize_text,
    non_heading_line_like,
    reference_caption_like,
)


DecisionAction = str


@dataclass
class HeadingDecision:
    candidate_id: str
    block_id: str
    action: DecisionAction
    is_heading: bool
    heading_text: str
    remaining_text: str
    level: int | None
    parent_path: list[str]
    confidence: float
    decision_source: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "block_id": self.block_id,
            "action": self.action,
            "is_heading": self.is_heading,
            "heading_text": self.heading_text,
            "remaining_text": self.remaining_text,
            "level": self.level,
            "parent_path": self.parent_path,
            "confidence": round(self.confidence, 4),
            "decision_source": self.decision_source,
            "reason": self.reason,
        }


def split_heading_text(text: str) -> tuple[str, str] | None:
    clean = normalize_text(text)
    if len(clean) < 24:
        return None
    if reference_caption_like(clean):
        return None
    patterns = (
        r"^([（(][一二三四五六七八九十百\d]+[）)]\s*[^:：]{2,44})[:：]\s*(.{8,})$",
        r"^(\d+[.．、]\s*【[^】]{2,40}】[^:：]{0,40})[:：]\s*(.{8,})$",
        r"^([（(][一二三四五六七八九十百\d]+[）)]\s*[^:：]{2,36}证)[:：]\s*(.{8,})$",
        r"^(第[一二三四五六七八九十百千万\d]+节\s*[^，,。；;：:]{2,16}?)((?:拇指|食指|中指|随音乐|用|以|患者|医师).{8,})$",
        r"^([（(]\d+[）)]\s*[^，,。；;：:]{2,44})\s+((?:阻断|抑制|促进|使|可|能|具有|通过|降低|增加|发自|起自|位于|沿|向|分布|每次|以|用|取|将|适用|适用于|同).{8,})$",
        r"^([（(][一二三四五六七八九十百\d]+[）)]\s*[^，,。；;：:—-]{1,24})[—-](.+)$",
        r"^(\d+[.．、]\s*[^，,。；;：:—-]{1,24})[—-](.+)$",
        r"^(第[一二三四五六七八九十百千万\d]+[章节篇部分编卷]\s*[^，,。；;：:—-]{0,30})[—-](.+)$",
        r"^(\d+[.．、]\s*[\u4e00-\u9fffA-Za-z（）()]{2,18})\s+(.{12,})$",
        r"^([（(][一二三四五六七八九十百\d]+[）)]\s*[\u4e00-\u9fffA-Za-z（）()]{2,18})\s+(.{12,})$",
    )
    for pattern in patterns:
        match = re.match(pattern, clean)
        if not match:
            continue
        heading = normalize_text(match.group(1))
        remaining = normalize_text(match.group(2))
        if re.search(r"\d$", heading) and re.match(r"\d", remaining):
            continue
        if re.search(r"[（(]?[图表]\s*\d{1,4}$", heading):
            continue
        if pattern.endswith(r"(.{12,})$") and not paragraph_like(remaining):
            continue
        if heading and remaining:
            return heading, remaining
    return None


def numbered_depth(text: str) -> int | None:
    clean = normalize_text(text)
    if re.match(r"^第[一二三四五六七八九十百千万\d]+[章节篇部分编卷]", clean):
        return 1
    match = re.match(r"^(\d+(?:\.\d+)*)(?:[.．、]\s*)\S+", clean)
    if match:
        return min(match.group(1).count(".") + 1, 6)
    if re.match(r"^[一二三四五六七八九十百]+[、.．]\s*\S+", clean):
        return 2
    if re.match(r"^[（(][一二三四五六七八九十百\d]+[）)]\s*\S+", clean):
        return 3
    return None


def paragraph_like(text: str) -> bool:
    clean = normalize_text(text)
    if len(clean) > 90:
        return True
    return any(char in clean for char in SENTENCE_PUNCTUATION)


def strong_paragraph_like(text: str) -> bool:
    clean = normalize_text(text)
    return len(clean) > 80 or bool(re.search(r"[。；;！？!?]", clean))


def fragment_like(text: str) -> bool:
    clean = normalize_text(text)
    compact = re.sub(r"\s+", "", clean)
    if re.fullmatch(r"[\d.．、,，;；:：-]+", compact):
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]", compact):
        return True
    return bool(len(compact) <= 6 and re.fullmatch(r"(?:[\u4e00-\u9fff]\s+){1,5}[\u4e00-\u9fff]", clean))


def rule_decision_for_candidate(
    candidate: HeadingCandidate,
    toc_levels: dict[str, int],
    toc_paths: dict[str, list[str]],
) -> HeadingDecision:
    text = normalize_text(candidate.text)
    key = heading_key(text)
    if non_heading_line_like(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="demote_to_paragraph",
            is_heading=False,
            heading_text="",
            remaining_text=text,
            level=None,
            parent_path=[],
            confidence=0.78,
            decision_source="rule",
            reason="The candidate is an index, cataloging, reference, or table/figure line, not a heading.",
        )

    split = split_heading_text(text)
    if split:
        heading, remaining = split
        split_key = heading_key(heading)
        level = toc_levels.get(split_key) or numbered_depth(heading) or 3
        path = toc_paths.get(split_key, [])
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="split_heading",
            is_heading=True,
            heading_text=heading,
            remaining_text=remaining,
            level=level,
            parent_path=path[:-1],
            confidence=0.78,
            decision_source="rule",
            reason="The block starts with a heading-like prefix and continues with sentence-like prose.",
        )

    if key in toc_levels:
        path = toc_paths.get(key, [])
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="keep_heading",
            is_heading=True,
            heading_text=text,
            remaining_text="",
            level=toc_levels[key],
            parent_path=path[:-1],
            confidence=0.92,
            decision_source="rule",
            reason="The candidate matches a TOC node.",
        )

    if is_probable_body_section(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="keep_heading",
            is_heading=True,
            heading_text=text,
            remaining_text="",
            level=2,
            parent_path=[],
            confidence=0.82,
            decision_source="rule",
            reason="The candidate matches a known section heading pattern.",
        )

    if fragment_like(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="demote_to_paragraph",
            is_heading=False,
            heading_text="",
            remaining_text=text,
            level=None,
            parent_path=[],
            confidence=0.72,
            decision_source="rule",
            reason="The candidate looks like a short OCR/layout heading fragment.",
        )

    pattern_level = heading_level_from_text(text, toc_levels)
    if pattern_level is not None and not paragraph_like(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="keep_heading",
            is_heading=True,
            heading_text=text,
            remaining_text="",
            level=pattern_level,
            parent_path=[],
            confidence=0.78,
            decision_source="rule",
            reason="The candidate matches a stable heading numbering pattern.",
        )

    if strong_paragraph_like(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="demote_to_paragraph",
            is_heading=False,
            heading_text="",
            remaining_text=text,
            level=None,
            parent_path=[],
            confidence=0.78,
            decision_source="rule",
            reason="The candidate is a numbered prose sentence, not a stable heading.",
        )

    if is_probable_numbered_subsection(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="keep_heading",
            is_heading=True,
            heading_text=text,
            remaining_text="",
            level=3,
            parent_path=[],
            confidence=0.76,
            decision_source="rule",
            reason="The candidate uses a parenthesized subsection numbering pattern.",
        )

    depth = numbered_depth(text)
    if depth is not None and not paragraph_like(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="keep_heading",
            is_heading=True,
            heading_text=text,
            remaining_text="",
            level=max(1, min(depth + 1, 6)) if depth > 1 else 4,
            parent_path=[],
            confidence=0.64,
            decision_source="rule",
            reason="The candidate is a short numbered heading-like block.",
        )

    if is_probable_major_heading(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="keep_heading",
            is_heading=True,
            heading_text=text,
            remaining_text="",
            level=1,
            parent_path=[],
            confidence=0.68,
            decision_source="rule",
            reason="The candidate looks like a major heading.",
        )

    if paragraph_like(text):
        return HeadingDecision(
            candidate_id=candidate.candidate_id,
            block_id=candidate.block_id,
            action="demote_to_paragraph",
            is_heading=False,
            heading_text="",
            remaining_text=text,
            level=None,
            parent_path=[],
            confidence=0.7,
            decision_source="rule",
            reason="The candidate contains sentence punctuation or is too long for a reliable heading.",
        )

    action = "promote_to_heading" if candidate.candidate_score >= 0.42 else "demote_to_paragraph"
    is_heading = action == "promote_to_heading"
    return HeadingDecision(
        candidate_id=candidate.candidate_id,
        block_id=candidate.block_id,
        action=action,
        is_heading=is_heading,
        heading_text=text if is_heading else "",
        remaining_text="" if is_heading else text,
        level=4 if is_heading else None,
        parent_path=[],
        confidence=candidate.candidate_score,
        decision_source="rule",
        reason="Fallback rule based on local candidate score.",
    )


def build_rule_decisions(
    candidates: list[HeadingCandidate],
    toc_levels: dict[str, int],
    toc_paths: dict[str, list[str]],
) -> list[HeadingDecision]:
    return [rule_decision_for_candidate(candidate, toc_levels, toc_paths) for candidate in candidates]


def load_decisions(path: Path) -> dict[str, HeadingDecision]:
    decisions: dict[str, HeadingDecision] = {}
    if not path.exists():
        return decisions
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        decision = HeadingDecision(
            candidate_id=str(data.get("candidate_id") or ""),
            block_id=str(data.get("block_id") or ""),
            action=str(data.get("action") or "demote_to_paragraph"),
            is_heading=bool(data.get("is_heading")),
            heading_text=str(data.get("heading_text") or ""),
            remaining_text=str(data.get("remaining_text") or ""),
            level=data.get("level"),
            parent_path=list(data.get("parent_path") or []),
            confidence=float(data.get("confidence") or 0),
            decision_source=str(data.get("decision_source") or "unknown"),
            reason=str(data.get("reason") or ""),
        )
        decisions[decision.block_id] = decision
    return decisions


def write_heading_decisions(path: Path, decisions: list[HeadingDecision]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for decision in decisions:
            fh.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")
