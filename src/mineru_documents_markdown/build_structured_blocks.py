#!/usr/bin/env python3
"""Compatibility facade for semantic reconstruction."""

from .semantic_rebuild import build_for_output_dir, main, parse_args
from .semantic_render import render_semantic_markdown, semantic_heading_level
from .semantic_state import heading_stack_path, update_heading_stack

__all__ = [
    "build_for_output_dir",
    "heading_stack_path",
    "main",
    "parse_args",
    "render_semantic_markdown",
    "semantic_heading_level",
    "update_heading_stack",
]


if __name__ == "__main__":
    raise SystemExit(main())
