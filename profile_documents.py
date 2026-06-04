#!/usr/bin/env python3
"""Compatibility wrapper for document profiling."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mineru_documents_markdown.profile_documents import main


if __name__ == "__main__":
    raise SystemExit(main())
