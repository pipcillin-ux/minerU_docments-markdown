"""Manage an isolated pipeline workspace and rollback-safe publication."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable


class WorkspaceError(RuntimeError):
    """Raised when a pipeline workspace cannot be prepared or published."""


def resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def default_work_dir(output_dir: Path) -> Path:
    output = resolved(output_dir)
    return output.with_name(f".{output.name}.pipeline-work")


def validate_workspace_paths(output_dir: Path, work_dir: Path) -> tuple[Path, Path]:
    output = resolved(output_dir)
    work = resolved(work_dir)
    if output == work:
        raise WorkspaceError("The pipeline work directory must differ from the final output directory.")
    if output in work.parents or work in output.parents:
        raise WorkspaceError("The pipeline work directory and final output directory cannot contain each other.")
    return output, work


def clone_tree(source: Path, destination: Path) -> str:
    source = resolved(source)
    destination = resolved(destination)
    if not source.is_dir():
        raise WorkspaceError(f"Cannot initialize pipeline workspace; output directory is missing: {source}")
    if destination.exists():
        raise WorkspaceError(f"Pipeline workspace already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Darwin":
        result = subprocess.run(
            ["cp", "-cR", str(source), str(destination)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "clonefile"
        if destination.exists():
            shutil.rmtree(destination)
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        print(f"APFS clone unavailable ({detail}); falling back to a full copy.", file=sys.stderr)

    shutil.copytree(source, destination, copy_function=shutil.copy2)
    return "copy"


def prepare_workspace(
    output_dir: Path,
    work_dir: Path,
    *,
    skip_parse: bool,
    fresh_work: bool,
) -> str:
    output, work = validate_workspace_paths(output_dir, work_dir)
    if fresh_work and work.exists():
        shutil.rmtree(work)
    if work.exists():
        if not work.is_dir():
            raise WorkspaceError(f"Pipeline workspace is not a directory: {work}")
        return "resumed"
    if skip_parse:
        method = clone_tree(output, work)
        return f"seeded:{method}"
    work.mkdir(parents=True, exist_ok=False)
    return "created"


def same_filesystem(left: Path, right: Path) -> bool:
    return left.stat().st_dev == right.stat().st_dev


def backup_path_for(output_dir: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return output_dir.with_name(f".{output_dir.name}.publish-backup-{timestamp}-{os.getpid()}")


def normalize_root_report_paths(output_dir: Path, published_output_dir: Path) -> int:
    output = resolved(output_dir)
    published = resolved(published_output_dir)
    source_text = str(output)
    replacement = str(published)
    changed = 0
    for path in output.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".csv", ".json", ".jsonl", ".md"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if source_text not in text:
            continue
        path.write_text(text.replace(source_text, replacement), encoding="utf-8")
        changed += 1
    return changed


def publish_workspace(
    output_dir: Path,
    work_dir: Path,
    *,
    replace: Callable[[Path, Path], None] = os.replace,
) -> Path | None:
    output, work = validate_workspace_paths(output_dir, work_dir)
    if not work.is_dir():
        raise WorkspaceError(f"Pipeline workspace is missing: {work}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not same_filesystem(work, output.parent):
        raise WorkspaceError("Pipeline workspace and final output must be on the same filesystem for publication.")

    backup = backup_path_for(output)
    moved_old_output = False
    try:
        if output.exists():
            replace(output, backup)
            moved_old_output = True
        replace(work, output)
    except BaseException as exc:
        try:
            if output.exists() and not work.exists():
                replace(output, work)
            if moved_old_output and backup.exists() and not output.exists():
                replace(backup, output)
        except BaseException as rollback_exc:
            raise WorkspaceError(
                f"Publication failed ({exc}) and rollback also failed ({rollback_exc}). "
                f"Recover the previous output from {backup}."
            ) from rollback_exc
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise WorkspaceError(f"Publication failed; previous output was preserved: {exc}") from exc

    if moved_old_output and backup.exists():
        try:
            shutil.rmtree(backup)
        except (OSError, KeyboardInterrupt) as exc:
            print(f"Published successfully, but could not remove backup {backup}: {exc}", file=sys.stderr)
            return backup
    return None
