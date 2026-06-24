#!/usr/bin/env python3
"""Batch parse long PDFs with MinerU and merge the Markdown outputs.

The script submits the same PDF URL multiple times with different
``page_ranges`` values, so a document that is longer than MinerU's per-task
page limit can be processed safely and resumed after interruption.
"""

from __future__ import annotations

import argparse
import collections
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

API_BASE = "https://mineru.net/api/v4"
DEFAULT_CHUNK_SIZE = 200
DEFAULT_MODEL_VERSION = "vlm"
DEFAULT_MAX_UPLOAD_MB = 200
DEFAULT_SUBMIT_FILES_PER_MINUTE = 50
DEFAULT_RESULT_REQUESTS_PER_MINUTE = 1000
DEFAULT_DAILY_UPLOAD_FILE_LIMIT = 5000
UPLOAD_BUFFER_SIZE = 1024 * 1024
DONE_STATES = {"done"}
RUNNING_STATES = {"pending", "running", "converting", "uploading", "waiting-file"}
FAILED_STATES = {"failed"}


class MinerUError(RuntimeError):
    """Raised when MinerU returns an unsuccessful response."""


class RateLimiter:
    """Sliding-window limiter for MinerU API quotas."""

    def __init__(
        self,
        max_units: int,
        window_seconds: float,
        label: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_units = max_units
        self.window_seconds = window_seconds
        self.label = label
        self.clock = clock
        self.sleeper = sleeper
        self.timestamps: collections.deque[float] = collections.deque()

    def consume(self, units: int = 1) -> None:
        if units <= 0:
            return
        if units > self.max_units:
            raise MinerUError(
                f"{self.label} quota is {self.max_units} units per "
                f"{self.window_seconds:.0f}s, but one operation needs {units}."
            )

        while True:
            now = self.clock()
            cutoff = now - self.window_seconds
            while self.timestamps and self.timestamps[0] <= cutoff:
                self.timestamps.popleft()

            if len(self.timestamps) + units <= self.max_units:
                self.timestamps.extend([now] * units)
                return

            sleep_seconds = max(0.001, self.timestamps[0] + self.window_seconds - now)
            print(
                f"MinerU {self.label} rate limit reached; sleeping {sleep_seconds:.1f}s...",
                flush=True,
            )
            self.sleeper(sleep_seconds)


@dataclass
class BatchTask:
    index: int
    page_range: str
    status: str = "new"
    task_id: str | None = None
    batch_id: str | None = None
    data_id: str | None = None
    file_name: str | None = None
    chunk_path: str | None = None
    upload_url: str | None = None
    uploaded: bool = False
    full_zip_url: str | None = None
    zip_path: str | None = None
    md_path: str | None = None
    error: str | None = None
    attempts: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BatchTask:
        return cls(
            index=int(raw["index"]),
            page_range=str(raw["page_range"]),
            status=str(raw.get("status", "new")),
            task_id=raw.get("task_id"),
            batch_id=raw.get("batch_id"),
            data_id=raw.get("data_id"),
            file_name=raw.get("file_name"),
            chunk_path=raw.get("chunk_path"),
            upload_url=raw.get("upload_url"),
            uploaded=bool(raw.get("uploaded", False)),
            full_zip_url=raw.get("full_zip_url"),
            zip_path=raw.get("zip_path"),
            md_path=raw.get("md_path"),
            error=raw.get("error"),
            attempts=int(raw.get("attempts", 0)),
            data=dict(raw.get("data", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "page_range": self.page_range,
            "status": self.status,
            "task_id": self.task_id,
            "batch_id": self.batch_id,
            "data_id": self.data_id,
            "file_name": self.file_name,
            "chunk_path": self.chunk_path,
            "upload_url": self.upload_url,
            "uploaded": self.uploaded,
            "full_zip_url": self.full_zip_url,
            "zip_path": self.zip_path,
            "md_path": self.md_path,
            "error": self.error,
            "attempts": self.attempts,
            "data": self.data,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse a long PDF through MinerU in page-range batches.",
    )
    parser.add_argument("--pdf", help="Local PDF path. If omitted, all PDFs in --docs-dir are processed.")
    parser.add_argument("--docs-dir", default="docs", help="PDF directory used when --pdf is omitted.")
    parser.add_argument(
        "--url",
        help="Optional public PDF URL. If omitted, the local PDF is uploaded through MinerU signed URLs.",
    )
    parser.add_argument("--out", default="output", help="Output base directory.")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--api-base", default=API_BASE)
    parser.add_argument("--model-version", default=DEFAULT_MODEL_VERSION)
    parser.add_argument("--language", default="ch")
    parser.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--table", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--formula", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=60 * 60)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--submit-files-per-minute",
        type=int,
        default=DEFAULT_SUBMIT_FILES_PER_MINUTE,
        help="MinerU submit quota shared by parse creation and batch upload APIs.",
    )
    parser.add_argument(
        "--result-requests-per-minute",
        type=int,
        default=DEFAULT_RESULT_REQUESTS_PER_MINUTE,
        help="MinerU result-query quota shared by single and batch result APIs.",
    )
    parser.add_argument(
        "--daily-upload-file-limit",
        type=int,
        default=DEFAULT_DAILY_UPLOAD_FILE_LIMIT,
        help="Stop before uploading more files than MinerU's daily user limit.",
    )
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=DEFAULT_MAX_UPLOAD_MB,
        help="Maximum size MinerU accepts for each uploaded PDF chunk.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count pages and print page ranges; do not call MinerU.",
    )
    parser.add_argument(
        "--resubmit-failed",
        action="store_true",
        help="Resubmit tasks recorded as failed in tasks.json.",
    )
    parser.add_argument(
        "--submit-only",
        action="store_true",
        help="Submit missing tasks and exit without waiting for completion.",
    )
    return parser.parse_args()


def get_pdf_page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                "Missing PDF reader dependency. Install the project with: python -m pip install -e ."
            ) from exc

    with pdf_path.open("rb") as fh:
        return len(PdfReader(fh).pages)


def make_ranges(total_pages: int, chunk_size: int) -> list[str]:
    if total_pages <= 0:
        raise ValueError("PDF has no pages.")
    if chunk_size <= 0:
        raise ValueError("--chunk-size must be positive.")
    return [f"{start}-{min(start + chunk_size - 1, total_pages)}" for start in range(1, total_pages + 1, chunk_size)]


def parse_page_range(page_range: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)-(\d+)", page_range)
    if not match:
        raise ValueError(f"Unsupported page range format: {page_range}")
    start, end = int(match.group(1)), int(match.group(2))
    if start <= 0 or end < start:
        raise ValueError(f"Invalid page range: {page_range}")
    return start, end


def load_token_from_dotenv(env_path: Path) -> str | None:
    if not env_path.exists():
        return None
    wanted_keys = {"MINERU_TOKEN", "mineru_api_token", "MINERU_API_TOKEN"}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted_keys:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            return value
    return None


def load_tasks(path: Path, ranges: list[str]) -> list[BatchTask]:
    if path.exists():
        raw_tasks = json.loads(path.read_text(encoding="utf-8"))
        existing = {str(item["page_range"]): BatchTask.from_dict(item) for item in raw_tasks}
    else:
        existing = {}

    tasks: list[BatchTask] = []
    for index, page_range in enumerate(ranges, start=1):
        task = existing.get(page_range)
        if task is None:
            task = BatchTask(index=index, page_range=page_range)
        task.index = index
        tasks.append(task)
    return tasks


def make_data_id(pdf_path: Path, task: BatchTask) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", pdf_path.stem).strip("._-") or "document"
    page_range = re.sub(r"[^A-Za-z0-9_.-]+", "_", task.page_range)
    data_id = f"{stem}_part_{task.index:03d}_{page_range}"
    return data_id[:128]


def clean_document_stem(pdf_path: Path) -> str:
    return pdf_path.stem


def output_markdown_name(pdf_path: Path) -> str:
    return f"{clean_document_stem(pdf_path)}.md"


def output_document_dir(base_out_dir: Path, pdf_path: Path) -> Path:
    return base_out_dir / clean_document_stem(pdf_path)


def find_input_pdfs(args: argparse.Namespace) -> list[Path]:
    if args.pdf:
        return [Path(args.pdf)]

    docs_dir = Path(args.docs_dir)
    if not docs_dir.exists():
        raise SystemExit(f"Docs directory not found: {docs_dir}")
    pdfs = sorted(path for path in docs_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")
    if not pdfs:
        raise SystemExit(f"No PDF files found in: {docs_dir}")
    return pdfs


def validate_output_name_collisions(pdfs: list[Path]) -> None:
    by_name: dict[str, list[Path]] = {}
    for pdf_path in pdfs:
        normalized = unicodedata.normalize("NFC", clean_document_stem(pdf_path)).casefold()
        by_name.setdefault(normalized, []).append(pdf_path)
    collisions = [paths for paths in by_name.values() if len(paths) > 1]
    if not collisions:
        return
    details = "; ".join(", ".join(str(path) for path in paths) for paths in collisions)
    raise MinerUError(f"PDF output-name collision detected before parsing: {details}")


def chunk_pdf_path(out_dir: Path, task: BatchTask) -> Path:
    safe_range = task.page_range.replace(",", "_").replace("-", "-")
    return out_dir / "chunks" / f"part_{task.index:03d}_{safe_range}.pdf"


def ensure_pdf_chunk(pdf_path: Path, out_dir: Path, task: BatchTask) -> Path:
    chunk_path = chunk_pdf_path(out_dir, task)
    task.chunk_path = str(chunk_path.relative_to(out_dir))
    if chunk_path.exists() and chunk_path.stat().st_size > 0:
        return chunk_path

    start, end = parse_page_range(task.page_range)
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    from pypdf import PdfReader, PdfWriter  # type: ignore

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for page_index in range(start - 1, end):
        writer.add_page(reader.pages[page_index])
    with chunk_path.open("wb") as fh:
        writer.write(fh)
    return chunk_path


def save_tasks(path: Path, tasks: list[BatchTask]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([task.to_dict() for task in tasks], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 60,
    max_retries: int = 3,
) -> dict[str, Any]:
    body = None
    headers = {
        "Accept": "*/*",
        "Authorization": f"Bearer {token}",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    raw = ""
    for attempt in range(1, max_retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if 400 <= exc.code < 500 and exc.code != 429:
                raise MinerUError(f"HTTP {exc.code} from {url}: {detail}") from exc
            if attempt >= max_retries:
                raise MinerUError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt >= max_retries:
                raise MinerUError(f"Request failed for {url}: {exc}") from exc
        time.sleep(min(2**attempt, 30))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MinerUError(f"Invalid JSON response from {url}: {raw[:500]}") from exc

    if data.get("code") != 0:
        raise MinerUError(f"MinerU error from {url}: {data.get('msg') or data}")
    return data


def submit_task(args: argparse.Namespace, task: BatchTask) -> None:
    args.submit_rate_limiter.consume(1)
    payload: dict[str, Any] = {
        "url": args.url,
        "model_version": args.model_version,
        "page_ranges": task.page_range,
        "language": args.language,
        "is_ocr": args.ocr,
        "enable_table": args.table,
        "enable_formula": args.formula,
    }
    if args.no_cache:
        payload["no_cache"] = True

    response = request_json(
        "POST",
        urllib.parse.urljoin(args.api_base.rstrip("/") + "/", "extract/task"),
        args.token,
        payload,
        max_retries=args.max_retries,
    )
    data = response.get("data") or {}
    task.task_id = data.get("task_id")
    if not task.task_id:
        raise MinerUError(f"Task submission returned no task_id: {response}")
    task.status = data.get("state") or "submitted"
    task.error = None
    task.attempts += 1
    task.data = data


def request_upload_batch(args: argparse.Namespace, pdf_path: Path, tasks: list[BatchTask]) -> None:
    files: list[dict[str, Any]] = []
    for task in tasks:
        task.data_id = task.data_id or make_data_id(pdf_path, task)
        if not task.chunk_path:
            raise MinerUError(f"Task {task.page_range} has no prepared PDF chunk.")
        task.file_name = task.file_name or Path(task.chunk_path).name
        files.append(
            {
                "name": task.file_name,
                "is_ocr": args.ocr,
                "data_id": task.data_id,
            }
        )

    args.submit_rate_limiter.consume(len(files))
    payload: dict[str, Any] = {
        "files": files,
        "model_version": args.model_version,
        "language": args.language,
        "enable_table": args.table,
        "enable_formula": args.formula,
    }
    response = request_json(
        "POST",
        urllib.parse.urljoin(args.api_base.rstrip("/") + "/", "file-urls/batch"),
        args.token,
        payload,
        max_retries=args.max_retries,
    )
    data = response.get("data") or {}
    batch_id = data.get("batch_id")
    upload_urls = data.get("file_urls") or data.get("files") or []
    if not batch_id:
        raise MinerUError(f"Upload URL request returned no batch_id: {response}")
    if len(upload_urls) != len(tasks):
        raise MinerUError(f"Upload URL count mismatch: expected {len(tasks)}, got {len(upload_urls)}")

    for task, upload_url in zip(tasks, upload_urls, strict=True):
        task.batch_id = batch_id
        task.upload_url = upload_url
        task.status = "waiting-file"
        task.uploaded = False
        task.error = None
        task.attempts += 1
        task.data = {"batch_id": batch_id}


def upload_pdf_to_signed_url(pdf_path: Path, upload_url: str, max_retries: int) -> None:
    parsed = urllib.parse.urlparse(upload_url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    content_length = pdf_path.stat().st_size

    for attempt in range(1, max_retries + 1):
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(parsed.netloc, timeout=300)
        try:
            connection.putrequest("PUT", path)
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            with pdf_path.open("rb") as stream:
                while chunk := stream.read(UPLOAD_BUFFER_SIZE):
                    connection.send(chunk)
            response = connection.getresponse()
            detail = response.read().decode("utf-8", errors="replace")
            if 200 <= response.status < 300:
                return
            if (response.status == 429 or response.status >= 500) and attempt < max_retries:
                retry_after = response.getheader("Retry-After")
                try:
                    delay = max(0.0, float(retry_after)) if retry_after else min(2**attempt, 30)
                except ValueError:
                    delay = min(2**attempt, 30)
                time.sleep(delay)
                continue
            raise MinerUError(f"Upload failed with HTTP {response.status}: {detail}")
        except (OSError, http.client.HTTPException) as exc:
            if attempt >= max_retries:
                raise MinerUError(f"Upload failed for signed URL: {exc}") from exc
            time.sleep(min(2**attempt, 30))
        finally:
            connection.close()


def poll_task(args: argparse.Namespace, task: BatchTask) -> None:
    if not task.task_id:
        raise MinerUError(f"Task {task.page_range} has no task_id.")

    args.result_rate_limiter.consume(1)
    response = request_json(
        "GET",
        urllib.parse.urljoin(args.api_base.rstrip("/") + "/", f"extract/task/{task.task_id}"),
        args.token,
        max_retries=args.max_retries,
    )
    data = response.get("data") or {}
    state = data.get("state") or task.status
    task.status = state
    task.data = data
    task.full_zip_url = data.get("full_zip_url") or task.full_zip_url
    if state in FAILED_STATES:
        task.error = data.get("err_msg") or response.get("msg") or "MinerU task failed."
    else:
        task.error = None


def poll_batch(args: argparse.Namespace, batch_id: str, tasks: list[BatchTask]) -> None:
    args.result_rate_limiter.consume(1)
    response = request_json(
        "GET",
        urllib.parse.urljoin(args.api_base.rstrip("/") + "/", f"extract-results/batch/{batch_id}"),
        args.token,
        max_retries=args.max_retries,
    )
    data = response.get("data") or {}
    results = data.get("extract_result") or []
    by_data_id = {item.get("data_id"): item for item in results if item.get("data_id")}
    by_file_name = {item.get("file_name"): item for item in results if item.get("file_name")}

    for task in tasks:
        item = None
        if task.data_id:
            item = by_data_id.get(task.data_id)
        if item is None and task.file_name:
            item = by_file_name.get(task.file_name)
        if item is None:
            continue

        state = item.get("state") or task.status
        task.status = state
        task.data = item
        task.full_zip_url = item.get("full_zip_url") or task.full_zip_url
        if state in FAILED_STATES:
            task.error = item.get("err_msg") or "MinerU task failed."
        else:
            task.error = None


def ensure_url_submitted(args: argparse.Namespace, task: BatchTask) -> None:
    if task.status in DONE_STATES and task.full_zip_url:
        return
    if task.status in FAILED_STATES and not args.resubmit_failed:
        return
    if task.task_id and task.status in RUNNING_STATES | {"submitted"}:
        return
    submit_task(args, task)


def ensure_local_upload_submitted(
    args: argparse.Namespace,
    tasks: list[BatchTask],
    pdf_path: Path,
    tasks_path: Path,
) -> None:
    for task in tasks:
        if task.status in FAILED_STATES and args.resubmit_failed:
            task.status = "new"
            task.batch_id = None
            task.upload_url = None
            task.uploaded = False
            task.full_zip_url = None
            task.zip_path = None
            task.md_path = None
            task.file_name = None
            task.error = None

    max_upload_bytes = args.max_upload_mb * 1024 * 1024
    for task in tasks:
        if task.status in DONE_STATES or task.status in FAILED_STATES:
            continue
        chunk_path = ensure_pdf_chunk(pdf_path, tasks_path.parent, task)
        if chunk_path.stat().st_size > max_upload_bytes:
            task.status = "failed"
            task.error = (
                f"PDF chunk {chunk_path} is {chunk_path.stat().st_size} bytes, "
                f"larger than --max-upload-mb={args.max_upload_mb}. "
                "Rerun with a smaller --chunk-size."
            )
            save_tasks(tasks_path, tasks)

    needs_upload_url = [
        task
        for task in tasks
        if task.status not in DONE_STATES
        and task.status not in FAILED_STATES
        and not task.batch_id
        and not task.upload_url
    ]
    scheduled_uploads = getattr(args, "upload_files_scheduled", 0)
    if scheduled_uploads + len(needs_upload_url) > args.daily_upload_file_limit:
        raise MinerUError(
            f"Need to upload {scheduled_uploads + len(needs_upload_url)} files today, which exceeds "
            f"--daily-upload-file-limit={args.daily_upload_file_limit}."
        )
    args.upload_files_scheduled = scheduled_uploads + len(needs_upload_url)

    upload_batch_size = args.submit_files_per_minute
    for start in range(0, len(needs_upload_url), upload_batch_size):
        chunk = needs_upload_url[start : start + upload_batch_size]
        request_upload_batch(args, pdf_path, chunk)
        save_tasks(tasks_path, tasks)

    for task in tasks:
        if task.status in DONE_STATES or task.status in FAILED_STATES or task.uploaded:
            continue
        if not task.upload_url:
            continue
        try:
            upload_path = tasks_path.parent / task.chunk_path if task.chunk_path else pdf_path
            upload_pdf_to_signed_url(upload_path, task.upload_url, args.max_retries)
            task.uploaded = True
            task.status = "pending"
            task.error = None
            print(f"[{task.index}/{len(tasks)}] {task.page_range}: uploaded {task.batch_id or ''}")
        except Exception as exc:  # noqa: BLE001 - keep other batches resumable.
            task.status = "failed"
            task.error = str(exc)
            print(f"[{task.index}/{len(tasks)}] {task.page_range}: upload failed: {exc}", file=sys.stderr)
        finally:
            save_tasks(tasks_path, tasks)


def wait_for_completion(args: argparse.Namespace, tasks: list[BatchTask], tasks_path: Path) -> None:
    deadline = time.monotonic() + args.timeout
    while True:
        pending: list[BatchTask] = []
        polled_batches: set[str] = set()
        for task in tasks:
            if task.status in DONE_STATES or task.status in FAILED_STATES:
                continue
            if task.batch_id:
                if task.batch_id not in polled_batches:
                    batch_tasks = [item for item in tasks if item.batch_id == task.batch_id]
                    poll_batch(args, task.batch_id, batch_tasks)
                    polled_batches.add(task.batch_id)
                if task.status not in DONE_STATES and task.status not in FAILED_STATES:
                    pending.append(task)
                continue
            if not task.task_id:
                continue
            poll_task(args, task)
            if task.status not in DONE_STATES and task.status not in FAILED_STATES:
                pending.append(task)
        save_tasks(tasks_path, tasks)

        if not pending:
            return
        if time.monotonic() >= deadline:
            waiting = ", ".join(task.page_range for task in pending)
            raise TimeoutError(f"Timed out waiting for page ranges: {waiting}")
        time.sleep(args.poll_interval)


def download_file(url: str, dest: Path, token: str | None = None, max_retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "*/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(1, max_retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with dest.open("wb") as fh:
                    shutil.copyfileobj(response, fh)
            return
        except urllib.error.URLError:
            if attempt >= max_retries:
                raise
            time.sleep(min(2**attempt, 30))


def part_dir(base: Path, task: BatchTask) -> Path:
    safe_range = task.page_range.replace(",", "_").replace("-", "-")
    return base / "parts" / f"part_{task.index:03d}_{safe_range}"


def locate_markdown(root: Path) -> Path:
    candidates = sorted(root.rglob("*.md"), key=lambda path: (len(path.parts), str(path)))
    if not candidates:
        raise FileNotFoundError(f"No Markdown file found under {root}")
    preferred = [path for path in candidates if path.name.lower() in {"result.md", "full.md"}]
    return preferred[0] if preferred else candidates[0]


def copy_assets_and_rewrite_md(md_path: Path, part_root: Path, assets_root: Path, part_name: str) -> str:
    part_assets = assets_root / part_name
    part_assets.mkdir(parents=True, exist_ok=True)

    for child in part_root.iterdir():
        if child == md_path or child.name == "__MACOSX":
            continue
        target = part_assets / child.name
        if child.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(child, target)
        elif child.is_file() and child.suffix.lower() != ".zip":
            shutil.copy2(child, target)

    text = md_path.read_text(encoding="utf-8", errors="replace")
    return rewrite_markdown_links(text, md_path.parent, Path("assets") / part_name)


def rewrite_markdown_links(text: str, source_dir: Path, new_prefix: Path) -> str:
    image_pattern = re.compile(r"(!\[[^\]]*]\()([^)#?]+)((?:[?#][^)]*)?\))")

    def replace(match: re.Match[str]) -> str:
        before, link, after = match.groups()
        link = link.strip()
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme or link.startswith("/") or link.startswith("data:"):
            return match.group(0)
        if not (source_dir / urllib.parse.unquote(link)).exists():
            return match.group(0)
        rewritten = (new_prefix / link).as_posix()
        return f"{before}{rewritten}{after}"

    return image_pattern.sub(replace, text)


def download_and_extract(args: argparse.Namespace, tasks: list[BatchTask], out_dir: Path, tasks_path: Path) -> None:
    for task in tasks:
        if task.status not in DONE_STATES:
            continue
        if not task.full_zip_url:
            if task.batch_id:
                poll_batch(args, task.batch_id, [task])
            else:
                poll_task(args, task)
            if not task.full_zip_url:
                task.status = "failed"
                task.error = "Task completed but no full_zip_url was returned."
                continue

        current_part_dir = part_dir(out_dir, task)
        zip_path = current_part_dir / "result.zip"
        extracted_dir = current_part_dir / "extracted"
        if not zip_path.exists():
            download_file(task.full_zip_url, zip_path, max_retries=args.max_retries)
        if not extracted_dir.exists():
            safe_extract_zip(zip_path, extracted_dir)
        md_path = locate_markdown(extracted_dir)
        task.zip_path = str(zip_path.relative_to(out_dir))
        task.md_path = str(md_path.relative_to(out_dir))
        save_tasks(tasks_path, tasks)


def merge_markdown(tasks: list[BatchTask], out_dir: Path, output_name: str) -> None:
    assets_root = out_dir / "assets"
    merged_parts: list[str] = []
    failed: list[dict[str, Any]] = []

    for task in tasks:
        if task.status != "done" or not task.md_path:
            failed.append(task.to_dict())
            continue
        md_path = out_dir / task.md_path
        part_name = f"part_{task.index:03d}"
        rewritten = copy_assets_and_rewrite_md(md_path, md_path.parent, assets_root, part_name)
        merged_parts.append(f"<!-- MinerU batch: pages {task.page_range} -->\n\n{rewritten.strip()}\n")

    (out_dir / output_name).write_text("\n\n".join(merged_parts), encoding="utf-8")
    failed_path = out_dir / "failed_tasks.json"
    if failed:
        failed_path.write_text(json.dumps(failed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif failed_path.exists():
        failed_path.unlink()


def safe_extract_zip(zip_path: Path, extracted_dir: Path) -> None:
    extracted_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{extracted_dir.name}.tmp-", dir=extracted_dir.parent))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            destinations: list[tuple[zipfile.ZipInfo, Path]] = []
            for member in members:
                name = member.filename
                if "\x00" in name or "\\" in name:
                    raise MinerUError(f"Unsafe ZIP member path: {name!r}")
                pure_path = PurePosixPath(name)
                if not name or pure_path == PurePosixPath("."):
                    raise MinerUError(f"Unsafe ZIP member path: {name!r}")
                if pure_path.is_absolute() or ".." in pure_path.parts:
                    raise MinerUError(f"Unsafe ZIP member path: {name!r}")
                if re.match(r"^[A-Za-z]:", name):
                    raise MinerUError(f"Unsafe ZIP member path: {name!r}")
                mode = member.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise MinerUError(f"ZIP symbolic links are not allowed: {name!r}")
                destination = (temporary_dir / Path(*pure_path.parts)).resolve()
                if not destination.is_relative_to(temporary_dir.resolve()):
                    raise MinerUError(f"Unsafe ZIP member path: {name!r}")
                destinations.append((member, destination))

            for member, destination in destinations:
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
        os.replace(temporary_dir, extracted_dir)
    except BaseException:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def validate_args(args: argparse.Namespace) -> int:
    if args.max_retries <= 0:
        print("--max-retries must be positive.", file=sys.stderr)
        return 2
    if args.submit_files_per_minute <= 0:
        print("--submit-files-per-minute must be positive.", file=sys.stderr)
        return 2
    if args.result_requests_per_minute <= 0:
        print("--result-requests-per-minute must be positive.", file=sys.stderr)
        return 2
    if args.daily_upload_file_limit <= 0:
        print("--daily-upload-file-limit must be positive.", file=sys.stderr)
        return 2
    if args.max_upload_mb <= 0:
        print("--max-upload-mb must be positive.", file=sys.stderr)
        return 2
    return 0


def process_pdf(args: argparse.Namespace, pdf_path: Path, out_dir: Path) -> int:
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2
    total_pages = get_pdf_page_count(pdf_path)
    ranges = make_ranges(total_pages, args.chunk_size)
    if args.dry_run:
        print(f"{pdf_path.name}: {total_pages} pages; {len(ranges)} batch(es)")
        for index, page_range in enumerate(ranges, start=1):
            print(f"[{index}/{len(ranges)}] {page_range}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    tasks_path = out_dir / "tasks.json"

    tasks = load_tasks(tasks_path, ranges)
    save_tasks(tasks_path, tasks)

    print(f"\n== {pdf_path.name} ==")
    print(f"PDF pages: {total_pages}; batches: {len(tasks)}")
    if args.url:
        for task in tasks:
            try:
                ensure_url_submitted(args, task)
                print(f"[{task.index}/{len(tasks)}] {task.page_range}: {task.status} {task.task_id or ''}")
            except Exception as exc:  # noqa: BLE001 - keep batch processing resumable.
                task.status = "failed"
                task.error = str(exc)
                print(f"[{task.index}/{len(tasks)}] {task.page_range}: failed: {exc}", file=sys.stderr)
            finally:
                save_tasks(tasks_path, tasks)
    else:
        ensure_local_upload_submitted(args, tasks, pdf_path, tasks_path)

    if args.submit_only:
        return 0

    try:
        wait_for_completion(args, tasks, tasks_path)
        download_and_extract(args, tasks, out_dir, tasks_path)
        merge_markdown(tasks, out_dir, output_markdown_name(pdf_path))
    finally:
        save_tasks(tasks_path, tasks)

    failed_count = sum(1 for task in tasks if task.status != "done")
    if failed_count:
        print(f"Finished with {failed_count} failed batch(es). See {out_dir / 'failed_tasks.json'}")
        return 1
    print(f"Done: {out_dir / output_markdown_name(pdf_path)}")
    return 0


def main() -> int:
    args = parse_args()
    validation_status = validate_args(args)
    if validation_status:
        return validation_status

    pdfs = find_input_pdfs(args)
    try:
        validate_output_name_collisions(pdfs)
    except MinerUError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.url and len(pdfs) != 1:
        print("--url can only be used with a single --pdf input.", file=sys.stderr)
        return 2

    if args.dry_run:
        for pdf_path in pdfs:
            process_pdf(args, pdf_path, output_document_dir(Path(args.out), pdf_path))
        return 0

    args.token = os.environ.get("MINERU_TOKEN") or load_token_from_dotenv(Path(".env"))
    if not args.token:
        print("MINERU_TOKEN is required in the environment or .env.", file=sys.stderr)
        return 2

    args.submit_rate_limiter = RateLimiter(
        args.submit_files_per_minute,
        60,
        "submit-files",
    )
    args.result_rate_limiter = RateLimiter(
        args.result_requests_per_minute,
        60,
        "result-requests",
    )
    args.upload_files_scheduled = 0

    failures = 0
    base_out_dir = Path(args.out)
    for pdf_path in pdfs:
        out_dir = base_out_dir if args.pdf else output_document_dir(base_out_dir, pdf_path)
        failures += 1 if process_pdf(args, pdf_path, out_dir) else 0
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
