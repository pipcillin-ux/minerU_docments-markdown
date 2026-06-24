"""Extract generic heading candidates from MinerU content blocks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .defaults import HEADING_CANDIDATE_MIN_SCORE
from .structure_utils import heading_key, item_text, normalize_text


SENTENCE_PUNCTUATION = "，,。；;：:！？!?"


@dataclass(frozen=True)
class HeadingCandidate:
    candidate_id: str
    block_id: str
    text: str
    page: int
    task_index: int
    item_index: int
    mineru_type: str
    bbox: list[Any] | None
    candidate_score: float
    signals: dict[str, bool]
    context_before: list[str]
    context_after: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "block_id": self.block_id,
            "text": self.text,
            "normalized_key": heading_key(self.text),
            "page": self.page,
            "task_index": self.task_index,
            "item_index": self.item_index,
            "mineru_type": self.mineru_type,
            "bbox": self.bbox,
            "candidate_score": round(self.candidate_score, 4),
            "signals": self.signals,
            "context_before": self.context_before,
            "context_after": self.context_after,
        }


def block_id(document: str, task_index: int, page: int, item_index: int) -> str:
    return f"{document}:{task_index}:{page}:{item_index}"


def candidate_id(task_index: int, page: int, item_index: int) -> str:
    return f"t{task_index}_p{page}_i{item_index}"


def is_numbered_heading_like(text: str) -> bool:
    clean = normalize_text(text)
    patterns = (
        r"^第[一二三四五六七八九十百千万\d]+[章节篇部分编卷]",
        r"^\d+(?:\.\d+){0,5}[、.．]\s*\S+",
        r"^[一二三四五六七八九十百]+[、.．]\s*\S+",
        r"^[（(][一二三四五六七八九十百\d]+[）)]\s*\S+",
        r"^[A-Z][、.．]\s*\S+",
    )
    return any(re.match(pattern, clean) for pattern in patterns)


def sentence_like(text: str) -> bool:
    clean = normalize_text(text)
    if len(clean) > 80:
        return True
    return any(char in clean for char in SENTENCE_PUNCTUATION)


def score_candidate(item: dict[str, Any], text: str) -> tuple[float, dict[str, bool]]:
    clean = normalize_text(text)
    mineru_type = str(item.get("type") or "unknown")
    bbox = item.get("bbox")
    bbox_height = 0.0
    if isinstance(bbox, list) and len(bbox) >= 4:
        try:
            bbox_height = float(bbox[3]) - float(bbox[1])
        except (TypeError, ValueError):
            bbox_height = 0.0

    signals = {
        "short_text": 0 < len(clean) <= 48,
        "very_short_text": 0 < len(clean) <= 20,
        "numbered": is_numbered_heading_like(clean),
        "sentence_like": sentence_like(clean),
        "mineru_heading_hint": bool(item.get("text_level")) or mineru_type in {"title", "heading", "aside_text"},
        "image_short_text": mineru_type == "image" and 0 < len(clean) <= 30,
        "large_bbox_height": bbox_height >= 28,
        "line_breaks": "\n" in text.strip(),
    }
    score = 0.0
    if signals["short_text"]:
        score += 0.22
    if signals["very_short_text"]:
        score += 0.1
    if signals["numbered"]:
        score += 0.24
    if signals["mineru_heading_hint"]:
        score += 0.22
    if signals["image_short_text"]:
        score += 0.16
    if signals["large_bbox_height"]:
        score += 0.1
    if signals["sentence_like"]:
        score -= 0.28
    if signals["line_breaks"]:
        score -= 0.08
    if not clean:
        score = 0.0
    return max(0.0, min(score, 1.0)), signals


def text_window(wrapped_items: list[dict[str, Any]], index: int, radius: int = 2) -> tuple[list[str], list[str]]:
    before: list[str] = []
    after: list[str] = []
    for wrapped in wrapped_items[max(0, index - radius) : index]:
        text = normalize_text(item_text(wrapped["item"]))
        if text:
            before.append(text[:160])
    for wrapped in wrapped_items[index + 1 : index + 1 + radius]:
        text = normalize_text(item_text(wrapped["item"]))
        if text:
            after.append(text[:160])
    return before, after


def extract_heading_candidates(
    document: str,
    wrapped_items: list[dict[str, Any]],
    min_score: float = HEADING_CANDIDATE_MIN_SCORE,
) -> list[HeadingCandidate]:
    candidates: list[HeadingCandidate] = []
    for index, wrapped in enumerate(wrapped_items):
        item = wrapped["item"]
        if str(item.get("type") or "unknown") in {"header", "footer", "page_number"}:
            continue
        text = normalize_text(item_text(item))
        if not text:
            continue
        score, signals = score_candidate(item, text)
        if score < min_score:
            continue
        page = int(wrapped["absolute_page"])
        task_index = int(wrapped["task_index"])
        item_index = int(wrapped["item_index"])
        before, after = text_window(wrapped_items, index)
        candidates.append(
            HeadingCandidate(
                candidate_id=candidate_id(task_index, page, item_index),
                block_id=block_id(document, task_index, page, item_index),
                text=text,
                page=page,
                task_index=task_index,
                item_index=item_index,
                mineru_type=str(item.get("type") or "unknown"),
                bbox=item.get("bbox") if isinstance(item.get("bbox"), list) else None,
                candidate_score=score,
                signals=signals,
                context_before=before,
                context_after=after,
            )
        )
    return candidates


def write_heading_candidates(path: Path, candidates: list[HeadingCandidate]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for candidate in candidates:
            fh.write(json.dumps(candidate.to_dict(), ensure_ascii=False) + "\n")
