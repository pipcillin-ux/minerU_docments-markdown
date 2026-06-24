"""Diagnostics derived from rebuilt semantic blocks."""

from __future__ import annotations

import re
from typing import Any

from .domain_profiles import DomainProfile, strict_subsection_like
from .heading_decisions import HeadingDecision


def heading_diagnostics(
    blocks: list[dict[str, Any]],
    decisions: list[HeadingDecision],
    domain_profile: DomainProfile | None = None,
) -> dict[str, Any]:
    heading_levels: dict[str, int] = {}
    long_heading_samples: list[dict[str, Any]] = []
    split_count = 0
    demote_count = 0
    promote_count = 0
    low_confidence = 0
    h1_subsection_samples: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("block_type") != "heading":
            continue
        level = block.get("heading_level")
        heading_levels[str(level)] = heading_levels.get(str(level), 0) + 1
        text = str(block.get("text") or "")
        if len(text) > 80 or re.search(r"[，,。；;]", text):
            long_heading_samples.append({"page": block.get("page"), "text": text[:160]})
        if level == 1 and domain_profile and strict_subsection_like(text, domain_profile):
            h1_subsection_samples.append({"page": block.get("page"), "text": text[:160]})
    for decision in decisions:
        split_count += decision.action == "split_heading"
        demote_count += decision.action == "demote_to_paragraph"
        promote_count += decision.action == "promote_to_heading"
        low_confidence += decision.confidence < 0.6
    return {
        "heading_levels": heading_levels,
        "decision_count": len(decisions),
        "split_heading_count": split_count,
        "demote_to_paragraph_count": demote_count,
        "promote_to_heading_count": promote_count,
        "low_confidence_decision_count": low_confidence,
        "long_heading_samples": long_heading_samples[:30],
        "h1_subsection_samples": h1_subsection_samples[:30],
    }
