"""Render rebuilt semantic blocks as Markdown."""

from __future__ import annotations

import re
from typing import Any

from .domain_profiles import DomainProfile
from .structure_utils import heading_key, normalize_text
from .toc_parser import split_stuck_toc_line


def normalize_heading_text(text: str) -> str:
    text = normalize_text(text)
    return re.sub(r"\s+", "", text) if len(text) <= 20 else text


def normalized_lines(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [line for value in values if (line := normalize_text(str(value)))]


def toc_plain_lines(
    content: str,
    domain_profile: DomainProfile | None = None,
) -> list[str]:
    rendered: list[str] = []
    for raw_line in content.splitlines():
        for line in split_stuck_toc_line(raw_line, domain_profile):
            line = normalize_text(line)
            if not line:
                continue
            if line == "目录":
                rendered.extend(["", "**目录**", ""])
            else:
                rendered.append(f"- {line}")
    if rendered and rendered[-1] != "":
        rendered.append("")
    return rendered


def toc_semantic_lines(
    content: str,
    toc_levels: dict[str, int],
    domain_profile: DomainProfile | None = None,
) -> list[str] | None:
    rendered: list[str] = []
    matched = 0
    total = 0
    for raw_line in content.splitlines():
        for line in split_stuck_toc_line(raw_line, domain_profile):
            line = normalize_text(line)
            if not line:
                continue
            total += 1
            level = toc_levels.get(heading_key(line))
            if level:
                rendered.extend(["", f"{'#' * level} {normalize_heading_text(line)}", ""])
                matched += 1
            else:
                rendered.append(line)
    if total and matched / total >= 0.5:
        return toc_plain_lines(content, domain_profile)
    return None


def front_matter_heading_splits(
    content: str,
    heading_level: int,
    toc_levels: dict[str, int],
    domain_profile: DomainProfile | None = None,
) -> list[tuple[str, int]]:
    parts = [
        normalize_text(part)
        for part in split_stuck_toc_line(content, domain_profile)
    ]
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        return []
    resolved: list[tuple[str, int]] = []
    matched = 0
    for part in parts:
        level = toc_levels.get(heading_key(part))
        if level:
            matched += 1
        resolved.append((part, level or heading_level))
    if matched / len(parts) < 0.5:
        return []
    return resolved


def semantic_heading_level(block: dict[str, Any]) -> int | None:
    try:
        raw_level = int(block.get("heading_level") or 0)
    except (TypeError, ValueError):
        raw_level = 0
    local_level = raw_level if 1 <= raw_level <= 6 else None

    if block.get("region") == "body":
        try:
            tree_level = int(block.get("tree_heading_level") or 0)
        except (TypeError, ValueError):
            tree_level = 0
        if 1 <= tree_level <= 6:
            path = list(block.get("tree_section_path") or [])
            text_key = heading_key(str(block.get("text") or ""))
            section_key = heading_key(str(path[-1])) if path else ""
            if text_key and text_key == section_key:
                return tree_level
            if local_level is not None:
                return min(6, max(tree_level + 1, local_level))
            return tree_level
    return local_level


def render_semantic_markdown(
    blocks: list[dict[str, Any]],
    toc_levels: dict[str, int],
    domain_profile: DomainProfile | None = None,
) -> tuple[str, int]:
    lines: list[str] = []
    semantic_count = 0
    for block in blocks:
        if not block.get("include_in_semantic"):
            continue
        block_type = str(block.get("block_type") or "")
        region = str(block.get("region") or "")
        content = str(block.get("text") or "")
        content_clean = normalize_text(content)

        if block_type == "toc":
            lines.extend(toc_plain_lines(content, domain_profile))
        elif block_type == "heading":
            toc_lines = (
                toc_semantic_lines(content, toc_levels, domain_profile)
                if region == "front_matter"
                else None
            )
            if toc_lines is not None:
                lines.extend(toc_lines)
            else:
                level = semantic_heading_level(block)
                if level:
                    lines.extend(["", f"{'#' * level} {normalize_heading_text(content_clean)}", ""])
                elif content_clean:
                    lines.extend([content_clean, ""])
            remaining_text = normalize_text(str(block.get("remaining_text") or ""))
            if remaining_text:
                lines.extend([remaining_text.strip(), ""])
        elif block_type == "table":
            caption = block.get("table_caption")
            if caption:
                lines.extend(["", f"**表：{caption}**", ""])
            lines.extend(["", content.strip(), ""])
            for footnote in normalized_lines(block.get("table_footnote")):
                lines.append(footnote)
            if block.get("table_footnote"):
                lines.append("")
        elif block_type == "image":
            image_path = block.get("image_path")
            caption = block.get("image_caption")
            if caption:
                lines.extend(["", f"**图：{caption}**"])
            if image_path:
                lines.append(f"![]({image_path})")
            if content_clean:
                lines.append(content_clean)
            for footnote in normalized_lines(block.get("image_footnote")):
                lines.append(footnote)
            lines.append("")
        elif block_type == "list":
            toc_lines = (
                toc_semantic_lines(content, toc_levels, domain_profile)
                if region == "front_matter"
                else None
            )
            if toc_lines is not None:
                lines.extend(toc_lines)
            else:
                lines.extend(f"- {line.strip()}" for line in content.splitlines() if line.strip())
                lines.append("")
        else:
            toc_lines = (
                toc_semantic_lines(content, toc_levels, domain_profile)
                if region == "front_matter"
                else None
            )
            if toc_lines is not None:
                lines.extend(toc_lines)
            elif content.strip():
                lines.extend([content.strip(), ""])
        semantic_count += 1

    semantic_text = "\n".join(lines)
    semantic_text = re.sub(r"\n{3,}", "\n\n", semantic_text).strip() + "\n"
    return semantic_text, semantic_count

