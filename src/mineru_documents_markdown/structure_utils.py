#!/usr/bin/env python3
"""Shared helpers for profiling and rebuilding MinerU outputs."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .domain_profiles import DomainProfile

FRONT_MATTER_PATTERNS = (
    "目录",
    "前言",
    "编写说明",
    "出版者的话",
    "图书在版编目",
    "编委会",
    "主编",
    "版权",
)

BACK_MATTER_PATTERNS = ("参考文献", "附录", "索引", "后记")


def output_dirs(output_root: Path) -> list[Path]:
    return sorted(path.parent for path in output_root.glob("*/tasks.json"))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_tasks(out_dir: Path) -> list[dict[str, Any]]:
    return load_json(out_dir / "tasks.json")


def parse_page_range(page_range: str) -> tuple[int, int]:
    start, end = page_range.split("-", 1)
    return int(start), int(end)


def content_list_path(out_dir: Path, task: dict[str, Any]) -> Path | None:
    md_path = task.get("md_path")
    if md_path:
        extracted_dir = (out_dir / str(md_path)).parent
        candidates = sorted(path for path in extracted_dir.glob("*_content_list.json") if "_v2" not in path.name)
        if candidates:
            return candidates[0]
    part_dir = out_dir / "parts" / f"part_{int(task['index']):03d}_{task['page_range']}"
    candidates = sorted(path for path in part_dir.glob("extracted/*_content_list.json") if "_v2" not in path.name)
    return candidates[0] if candidates else None


def iter_content_items(out_dir: Path) -> Iterable[dict[str, Any]]:
    for task in load_tasks(out_dir):
        path = content_list_path(out_dir, task)
        if path is None:
            continue
        start, _ = parse_page_range(str(task["page_range"]))
        for item_index, item in enumerate(load_json(path)):
            page_idx = int(item.get("page_idx", 0))
            yield {
                "task": task,
                "task_index": int(task["index"]),
                "page_range": str(task["page_range"]),
                "source_path": str(path.relative_to(out_dir)),
                "item_index": item_index,
                "absolute_page": start + page_idx,
                "local_page_idx": page_idx,
                "item": item,
            }


def item_text(item: dict[str, Any]) -> str:
    if isinstance(item.get("text"), str):
        return item["text"]
    if isinstance(item.get("list_items"), list):
        return "\n".join(str(value) for value in item["list_items"])
    if isinstance(item.get("table_body"), str):
        return item["table_body"]
    if isinstance(item.get("content"), str):
        return item["content"]
    return ""


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def compact_heading_text(text: str) -> str:
    text = normalize_text(text)
    return re.sub(r"\s+", "", text)


def strip_toc_page_number(text: str, known_titles: Iterable[str] = ()) -> str:
    text = normalize_text(text)
    text = re.sub(r"[.．。·•…\s]+", " ", text).strip()
    text = re.sub(r"\s+\d{1,4}\s*$", "", text).strip()
    compact = re.sub(r"\s+", "", text)
    for title in sorted(known_titles, key=len, reverse=True):
        key = compact_heading_text(title)
        if compact.startswith(key) and compact[len(key) :].isdigit():
            return title
    match = re.match(r"^([（(][一二三四五六七八九十百\d]+[）)].*?)(\d{1,4})$", compact)
    if match:
        return match.group(1)
    return text


def heading_key(text: str) -> str:
    text = strip_toc_page_number(text)
    text = text.replace("(", "（").replace(")", "）")
    return compact_heading_text(text)


def markdown_file(out_dir: Path) -> Path | None:
    preferred = out_dir / f"{out_dir.name}.md"
    if preferred.exists():
        return preferred
    excluded_names = {
        "quality_report.md",
        "heading_quality.md",
        "section_reasoning_report.md",
        "section_reasoning_apply_report.md",
    }
    candidates = sorted(
        path
        for path in out_dir.glob("*.md")
        if not path.name.endswith((".semantic.md", ".reasoned.md"))
        and path.name not in excluded_names
        and not path.name.startswith("section_reasoning_")
    )
    return candidates[0] if candidates else None


def markdown_headings(out_dir: Path) -> list[tuple[int, str]]:
    path = markdown_file(out_dir)
    if path is None:
        return []
    headings: list[tuple[int, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((len(match.group(1)), match.group(2)))
    return headings


def looks_like_toc_text(text: str) -> bool:
    clean = normalize_text(text)
    if clean == "目录" or (clean.endswith("目录") and len(clean) <= 8):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        numbered = sum(1 for line in lines if re.search(r"\s\d{1,4}$", line))
        return numbered / len(lines) >= 0.45
    return bool(re.search(r".{2,30}\s+\d{1,4}$", clean))


def reference_caption_like(text: str) -> bool:
    clean = normalize_text(text)
    return bool(re.search(r"见[表图]\s*\d{1,4}(?:\s*[-－—]\s*\d{1,4})?[。.]?$", clean))


def figure_table_reference_tail(text: str) -> bool:
    clean = normalize_text(text)
    return bool(re.search(r"[（(]?[图表]\s*\d{1,4}(?:\s*[-－—]\s*\d{1,4})?[）)]?[：:]?$", clean))


def numeric_index_like(text: str) -> bool:
    clean = normalize_text(text)
    compact = re.sub(r"\s+", "", clean).replace("（", "(").replace("）", ")")
    return bool(re.fullmatch(r"(?:\(\d{1,4}\)\d{1,4}){2,}", compact))


def short_index_entry_like(text: str) -> bool:
    clean = normalize_text(text)
    if re.fullmatch(r"病案\s*\d{1,2}", clean):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z（）()·•]{1,16}\s+\d{1,4}", clean))


def cataloging_entry_like(text: str) -> bool:
    clean = normalize_text(text)
    roman_markers = len(re.findall(r"\b[IVX]{1,4}\.", clean))
    return roman_markers >= 2 and ("①" in clean or re.search(r"\bR\d", clean))


def digit_axis_like(text: str) -> bool:
    clean = normalize_text(text)
    return bool(re.search(r"(?:^|\s)0\s+1\s+2\s+3(?:\s+4)?", clean))


def reference_entry_like(text: str) -> bool:
    clean = normalize_text(text)
    if not re.match(r"^\d+[.．、]\s+", clean):
        return False
    return bool(
        re.search(r"\[[A-Z]\]", clean)
        or re.search(r"\b(?:19|20)\d{2}[，,]\s*\d", clean)
        or re.search(r"\b(?:19|20)\d{2}[，,]\s*\d+[(（]\d+[)）]", clean)
    )


def non_heading_line_like(text: str) -> bool:
    return (
        reference_caption_like(text)
        or numeric_index_like(text)
        or short_index_entry_like(text)
        or cataloging_entry_like(text)
        or digit_axis_like(text)
        or reference_entry_like(text)
    )


def looks_like_toc_entry(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or clean == "目录":
        return False
    if not re.search(r"[\u4e00-\u9fff]", clean):
        return False
    if re.fullmatch(r"病案\s*\d{1,2}", clean):
        return False
    if reference_caption_like(clean) or figure_table_reference_tail(clean):
        return False
    if cataloging_entry_like(clean) or digit_axis_like(clean) or reference_entry_like(clean):
        return False
    if looks_like_toc_text(clean):
        return True
    return bool(re.search(r".{1,60}(?:…+|\.{2,}|[.．。·•])\s*\d{1,4}$", clean))


def item_region_key(wrapped: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(wrapped["task_index"]),
        int(wrapped["absolute_page"]),
        int(wrapped["item_index"]),
    )


def is_margin_item(item: dict[str, Any]) -> bool:
    return str(item.get("type") or "") in {"header", "footer", "page_number"}


def is_body_paragraph_text(text: str) -> bool:
    clean = normalize_text(text)
    return len(clean) >= 35 and bool(re.search(r"[，,。；;]", clean))


def has_body_text_after(wrapped_items: list[dict[str, Any]], index: int, window: int = 12) -> bool:
    inspected = 0
    for wrapped in wrapped_items[index + 1 :]:
        if inspected >= window:
            break
        item = wrapped["item"]
        if is_margin_item(item):
            continue
        text = normalize_text(item_text(item))
        if not text:
            continue
        inspected += 1
        if text == "目录" or looks_like_toc_entry(text):
            return False
        if is_body_paragraph_text(text):
            return True
        if str(item.get("type") or "") in {"table", "image"}:
            return True
    return False


def is_body_start_heading(text: str, domain_profile: DomainProfile | None = None) -> bool:
    clean = normalize_text(text)
    if not clean or clean == "目录" or looks_like_toc_entry(clean):
        return False
    if clean in {"绪论", "导论", "概论", "总论"}:
        return True
    if re.match(r"^第[一二三四五六七八九十百千万\d]+[章节篇编部卷]", clean):
        return True
    if re.match(r"^[上中下][篇编部卷]\s*\S{0,30}$", clean):
        return True
    if is_probable_major_heading(clean, domain_profile):
        return True
    return False


def classify_item_regions(
    wrapped_items: list[dict[str, Any]],
    domain_profile: DomainProfile | None = None,
) -> dict[tuple[int, int, int], str]:
    pages: dict[int, list[str]] = {}
    for wrapped in wrapped_items:
        text = normalize_text(item_text(wrapped["item"]))
        if text:
            pages.setdefault(int(wrapped["absolute_page"]), []).append(text)
    max_page = max(pages) if pages else 0

    toc_start: int | None = None
    for index, wrapped in enumerate(wrapped_items):
        page = int(wrapped["absolute_page"])
        if page > 50:
            break
        item = wrapped["item"]
        if is_margin_item(item):
            continue
        text = normalize_text(item_text(item))
        if text == "目录":
            toc_start = index
            break
    if toc_start is None:
        for index, wrapped in enumerate(wrapped_items):
            page = int(wrapped["absolute_page"])
            if page > 50:
                break
            item = wrapped["item"]
            if is_margin_item(item):
                continue
            text = normalize_text(item_text(item))
            if looks_like_toc_entry(text):
                toc_start = index
                break

    body_start: int | None = None
    if toc_start is not None:
        for index in range(toc_start + 1, len(wrapped_items)):
            wrapped = wrapped_items[index]
            page = int(wrapped["absolute_page"])
            if page > 80:
                break
            item = wrapped["item"]
            if is_margin_item(item):
                continue
            text = normalize_text(item_text(item))
            if is_body_start_heading(text, domain_profile) and has_body_text_after(wrapped_items, index):
                body_start = index
                break

    regions: dict[tuple[int, int, int], str] = {}
    for index, wrapped in enumerate(wrapped_items):
        page = int(wrapped["absolute_page"])
        key = item_region_key(wrapped)
        joined = " ".join(pages.get(page, [])[:40])
        if toc_start is not None and index >= toc_start and (body_start is None or index < body_start):
            regions[key] = "toc"
        elif body_start is not None and index >= body_start:
            if page >= int(max_page * 0.75) and any(pattern in joined for pattern in BACK_MATTER_PATTERNS):
                regions[key] = "back_matter"
            else:
                regions[key] = "body"
        elif page <= 15 and any(pattern in joined for pattern in FRONT_MATTER_PATTERNS):
            regions[key] = "front_matter"
        elif page >= int(max_page * 0.75) and any(pattern in joined for pattern in BACK_MATTER_PATTERNS):
            regions[key] = "back_matter"
        else:
            regions[key] = "body"
    return regions


def page_texts(out_dir: Path) -> dict[int, list[str]]:
    pages: dict[int, list[str]] = {}
    for wrapped in iter_content_items(out_dir):
        text = normalize_text(item_text(wrapped["item"]))
        if text:
            pages.setdefault(int(wrapped["absolute_page"]), []).append(text)
    return pages


def classify_page_regions(out_dir: Path) -> dict[int, str]:
    pages = page_texts(out_dir)
    max_page = max(pages) if pages else 0
    toc_pages = {
        page
        for page, texts in pages.items()
        if page <= 50
        for text in texts
        if looks_like_toc_text(text) or text == "目录"
    }
    toc_end = max(toc_pages) if toc_pages else 0
    regions: dict[int, str] = {}
    for page, texts in pages.items():
        joined = " ".join(texts[:40])
        if page <= toc_end or (page <= 15 and any(pattern in joined for pattern in FRONT_MATTER_PATTERNS)):
            regions[page] = "front_matter"
        elif page >= int(max_page * 0.75) and any(pattern in joined for pattern in BACK_MATTER_PATTERNS):
            regions[page] = "back_matter"
        else:
            regions[page] = "body"
    return regions


def repeated_margin_texts(out_dir: Path, min_repeats: int = 3) -> set[str]:
    counts: Counter[str] = Counter()
    for wrapped in iter_content_items(out_dir):
        item = wrapped["item"]
        if item.get("type") not in {"header", "footer", "page_number", "aside_text"}:
            continue
        text = normalize_text(item_text(item))
        if text:
            counts[text] += 1
    return {text for text, count in counts.items() if count >= min_repeats}


def table_caption(item: dict[str, Any]) -> str:
    captions = item.get("table_caption")
    if isinstance(captions, list):
        return normalize_text(" ".join(str(value) for value in captions))
    return normalize_text(str(captions or ""))


def image_caption(item: dict[str, Any]) -> str:
    captions = item.get("image_caption")
    if isinstance(captions, list):
        return normalize_text(" ".join(str(value) for value in captions))
    return normalize_text(str(captions or ""))


def is_probable_body_section(text: str, domain_profile: DomainProfile | None = None) -> bool:
    if domain_profile is None:
        return False
    clean = heading_key(text)
    return clean in domain_profile.body_section_keys


def is_probable_numbered_subsection(text: str) -> bool:
    raw = normalize_text(text)
    if re.search(r"[，,。；;！？!?：:]", raw):
        return False
    clean = heading_key(text)
    if not clean or len(clean) > 28:
        return False
    return bool(
        re.match(r"^[（(][一二三四五六七八九十百\d]+[）)]", clean)
        or re.match(r"^[一二三四五六七八九十百\d]+[）)]", clean)
    )


def is_numbered_prose_fragment(text: str) -> bool:
    raw = normalize_text(text)
    if not re.match(r"^(?:\d+[.．、]|[（(][一二三四五六七八九十百\d]+[）)])\s*\S+", raw):
        return False
    if len(raw) < 32:
        return False
    has_punctuation = bool(re.search(r"[，,。；;：:]", raw))
    if len(raw) > 70 and has_punctuation:
        return True
    prose_cues = (
        "根据",
        "认为",
        "患者",
        "符合",
        "可将",
        "除了",
        "同时",
        "下述",
        "以下",
        "上表",
        "诊断",
        "分级",
        "临床上",
        "治疗本病",
        "本病的",
    )
    if has_punctuation and any(cue in raw for cue in prose_cues):
        return True
    return bool(has_punctuation and re.search(r"将.{0,16}分为", raw))


def heading_level_from_text(text: str, toc_levels: dict[str, int] | None = None) -> int | None:
    clean = normalize_text(text)
    key = heading_key(clean)
    if toc_levels and key in toc_levels:
        return toc_levels[key]
    if clean in {"绪论", "导论", "概论", "总论"}:
        return 1
    if re.match(r"^第[一二三四五六七八九十百千万\d]+[章篇编部卷]", clean):
        return 1
    if re.match(r"^[上中下][篇编部卷]\s*\S{0,30}$", clean):
        return 1
    if re.match(r"^第[一二三四五六七八九十百千万\d]+节", clean):
        return 2
    if re.match(r"^[一二三四五六七八九十百]+[、.．]\s*\S+", clean):
        return 2
    if re.match(r"^[（(][一二三四五六七八九十百\d]+[）)]\s*\S+", clean):
        return 3
    match = re.match(r"^(\d+(?:\.\d+)*)(?:[.．、]\s*)\S+", clean)
    if match:
        return min(4 + match.group(1).count("."), 6)
    return None


def toc_heading_levels(
    out_dir: Path,
    domain_profile: DomainProfile | None = None,
) -> dict[str, int]:
    regions = classify_page_regions(out_dir)
    levels: dict[str, int] = {}
    for wrapped in iter_content_items(out_dir):
        page = int(wrapped["absolute_page"])
        if page > 50 or regions.get(page) != "front_matter":
            continue
        text = item_text(wrapped["item"])
        for line in text.splitlines():
            title = strip_toc_page_number(
                line,
                domain_profile.body_section_titles if domain_profile else (),
            )
            key = heading_key(title)
            if not key or key in {"目录"}:
                continue
            if is_probable_body_section(title, domain_profile):
                levels.setdefault(key, 2)
            elif is_probable_numbered_subsection(title):
                levels.setdefault(key, 3)
            elif looks_like_toc_text(line) and is_probable_major_heading(title, domain_profile):
                levels.setdefault(key, 1)
    return levels


def is_probable_major_heading(text: str, domain_profile: DomainProfile | None = None) -> bool:
    raw = normalize_text(text)
    if re.search(r"[，,。；;：:]", raw):
        return False
    clean = heading_key(text)
    if not clean or len(clean) > 26:
        return False
    if is_probable_body_section(text, domain_profile):
        return False
    if is_probable_numbered_subsection(text):
        return False
    if looks_like_toc_text(text):
        return False
    if clean in {"目录", "前言", "编写说明"}:
        return False
    if clean in {"绪论", "导论", "概论", "总论"}:
        return True
    if re.match(r"^第[一二三四五六七八九十百千万\d]+[章篇编部卷]", raw):
        return True
    keywords = domain_profile.major_heading_keywords if domain_profile else ()
    return any(keyword in clean for keyword in keywords)
