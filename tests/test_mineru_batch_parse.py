from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mineru_documents_markdown.mineru_batch_parse import (
    UPLOAD_BUFFER_SIZE,
    BatchTask,
    MinerUError,
    RateLimiter,
    clean_document_stem,
    merge_markdown,
    safe_extract_zip,
    upload_pdf_to_signed_url,
    validate_output_name_collisions,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeResponse:
    def __init__(self, status: int = 200, retry_after: str | None = None) -> None:
        self.status = status
        self.retry_after = retry_after

    def read(self) -> bytes:
        return b""

    def getheader(self, name: str) -> str | None:
        return self.retry_after if name.lower() == "retry-after" else None


class FakeConnection:
    instances: list[FakeConnection] = []
    fail_first_send = False

    def __init__(self, host: str, timeout: int) -> None:
        self.host = host
        self.timeout = timeout
        self.headers: dict[str, str] = {}
        self.sent_sizes: list[int] = []
        self.send_calls = 0
        self.__class__.instances.append(self)

    def putrequest(self, method: str, path: str) -> None:
        self.method = method
        self.path = path

    def putheader(self, name: str, value: str) -> None:
        self.headers[name] = value

    def endheaders(self) -> None:
        return None

    def send(self, data: bytes) -> None:
        self.send_calls += 1
        if self.__class__.fail_first_send and len(self.__class__.instances) == 1 and self.send_calls == 1:
            raise OSError("simulated connection reset")
        self.sent_sizes.append(len(data))

    def getresponse(self) -> FakeResponse:
        return FakeResponse()

    def close(self) -> None:
        return None


class MinerUBatchParseTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeConnection.instances = []
        FakeConnection.fail_first_send = False

    def test_safe_extract_zip_accepts_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "result.zip"
            destination = root / "extracted"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("nested/result.md", "# Result")

            safe_extract_zip(archive_path, destination)

            self.assertEqual("# Result", destination.joinpath("nested/result.md").read_text(encoding="utf-8"))

    def test_safe_extract_zip_rejects_unsafe_paths_and_symlinks(self) -> None:
        unsafe_members = ("../escape.txt", "/absolute.txt", "C:/drive.txt", "..\\escape.txt")
        for member_name in unsafe_members:
            with self.subTest(member=member_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    archive_path = root / "result.zip"
                    destination = root / "extracted"
                    with zipfile.ZipFile(archive_path, "w") as archive:
                        archive.writestr(member_name, "bad")

                    with self.assertRaises(MinerUError):
                        safe_extract_zip(archive_path, destination)

                    self.assertFalse(destination.exists())
                    self.assertEqual([], list(root.glob(".extracted.tmp-*")))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "result.zip"
            destination = root / "extracted"
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(link, "../../outside")

            with self.assertRaises(MinerUError):
                safe_extract_zip(archive_path, destination)

            self.assertFalse(destination.exists())

    def test_upload_streams_bounded_chunks_and_reopens_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "chunk.pdf"
            payload = b"x" * (UPLOAD_BUFFER_SIZE * 2 + 123)
            pdf_path.write_bytes(payload)
            FakeConnection.fail_first_send = True

            with patch("mineru_documents_markdown.mineru_batch_parse.http.client.HTTPConnection", FakeConnection):
                with patch("mineru_documents_markdown.mineru_batch_parse.time.sleep"):
                    upload_pdf_to_signed_url(pdf_path, "http://example.test/upload", max_retries=2)

            self.assertEqual(2, len(FakeConnection.instances))
            successful = FakeConnection.instances[1]
            self.assertEqual(str(len(payload)), successful.headers["Content-Length"])
            self.assertEqual(len(payload), sum(successful.sent_sizes))
            self.assertLessEqual(max(successful.sent_sizes), UPLOAD_BUFFER_SIZE)

    def test_rate_limiter_prevents_fixed_window_boundary_burst(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(2, 60, "test", clock=clock.now, sleeper=clock.sleep)

        limiter.consume(2)
        clock.value = 59.0
        limiter.consume(1)

        self.assertEqual([1.0], clock.sleeps)
        self.assertEqual(60.0, clock.value)
        self.assertEqual(1, len(limiter.timestamps))

    def test_document_stem_is_preserved_and_collisions_fail(self) -> None:
        self.assertEqual("1_2_3", clean_document_stem(Path("1_2_3.pdf")))
        with self.assertRaises(MinerUError):
            validate_output_name_collisions([Path("Book.pdf"), Path("book.PDF")])

    def test_failed_tasks_preserve_error_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            task = BatchTask(index=1, page_range="1-10", status="failed", error="upload rejected")

            merge_markdown([task], out_dir, "book.md")

            failed = json.loads(out_dir.joinpath("failed_tasks.json").read_text(encoding="utf-8"))
            self.assertEqual("upload rejected", failed[0]["error"])

    def test_cli_help_does_not_expose_token_argument(self) -> None:
        for module in (
            "mineru_documents_markdown.mineru_batch_parse",
            "mineru_documents_markdown.run_pipeline",
        ):
            result = subprocess.run(
                [sys.executable, "-m", module, "--help"],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
            )
            self.assertNotIn("--token", result.stdout)


if __name__ == "__main__":
    unittest.main()
