#!/usr/bin/env python3
"""Compatibility wrapper for semantic block rebuilding."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mineru_documents_markdown.build_structured_blocks import main


if __name__ == "__main__":
    raise SystemExit(main())
