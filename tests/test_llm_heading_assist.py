from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from unittest.mock import patch

from mineru_documents_markdown.llm_heading_assist import call_chat_completions, retry_after_seconds


class FakeResponse:
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        content = json.dumps({"ok": True})
        return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


class LLMHeadingAssistTests(unittest.TestCase):
    def test_retry_after_supports_seconds_and_http_date(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(3.5, retry_after_seconds("3.5", now=now))
        retry_at = now + timedelta(seconds=12)
        self.assertEqual(12.0, retry_after_seconds(retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT"), now=now))

    def test_retry_rebuilds_request_and_respects_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "4"
        error = urllib.error.HTTPError(
            "https://api.example/chat/completions",
            429,
            "rate limited",
            headers,
            io.BytesIO(b""),
        )
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: int) -> FakeResponse:
            requests.append(request)
            if len(requests) == 1:
                raise error
            return FakeResponse()

        with patch(
            "mineru_documents_markdown.llm_heading_assist.api_config",
            return_value=("secret", "https://api.example", "model"),
        ):
            with patch("mineru_documents_markdown.llm_heading_assist.urllib.request.urlopen", side_effect=fake_urlopen):
                with patch("mineru_documents_markdown.llm_heading_assist.time.sleep") as sleep:
                    result = call_chat_completions({"candidate": "x"}, retries=1)

        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, len(requests))
        self.assertIsNot(requests[0], requests[1])
        sleep.assert_called_once_with(4.0)


if __name__ == "__main__":
    unittest.main()
