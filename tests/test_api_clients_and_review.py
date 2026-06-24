from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mineru_documents_markdown.heading_candidates import HeadingCandidate
from mineru_documents_markdown.heading_decisions import HeadingDecision
from mineru_documents_markdown.llm_heading_assist import (
    assist_decision,
    assist_decision_batch,
    batch_decisions_from_llm,
    batch_payload,
    cache_key,
    candidate_payload,
    decision_from_llm,
    maybe_assist_decisions,
)
from mineru_documents_markdown.mineru_batch_parse import (
    BatchTask,
    MinerUError,
    ensure_url_submitted,
    load_tasks,
    load_token_from_dotenv,
    make_data_id,
    make_ranges,
    parse_page_range,
    poll_batch,
    poll_task,
    request_json,
    request_upload_batch,
    save_tasks,
    submit_task,
    validate_args,
)
from mineru_documents_markdown.warn_review import (
    clamp_confidence,
    collect_warn_candidates,
    normalize_decision,
    review_payload,
    review_with_cache,
    short_text,
    unwrap_decision,
    write_markdown_report,
)


class StubLimiter:
    def __init__(self) -> None:
        self.units: list[int] = []

    def consume(self, units: int = 1) -> None:
        self.units.append(units)


class JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> JsonResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def candidate() -> HeadingCandidate:
    return HeadingCandidate(
        candidate_id="c1",
        block_id="b1",
        text="第一章 总论",
        page=2,
        task_index=1,
        item_index=3,
        mineru_type="text",
        bbox=[0, 0, 100, 30],
        candidate_score=0.7,
        signals={"short_text": True},
        context_before=["目录"],
        context_after=["正文"],
    )


def fallback() -> HeadingDecision:
    return HeadingDecision(
        candidate_id="c1",
        block_id="b1",
        action="keep_heading",
        is_heading=True,
        heading_text="第一章 总论",
        remaining_text="",
        level=1,
        parent_path=[],
        confidence=0.6,
        decision_source="rule",
        reason="rule",
    )


class MinerUApiClientTests(unittest.TestCase):
    def test_ranges_tasks_token_and_validation_helpers(self) -> None:
        self.assertEqual(["1-2", "3-4", "5-5"], make_ranges(5, 2))
        self.assertEqual((3, 9), parse_page_range("3-9"))
        for invalid in ("3", "0-2", "4-2"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                parse_page_range(invalid)
        with self.assertRaises(ValueError):
            make_ranges(0, 2)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / ".env"
            env_path.write_text("# comment\nOTHER=x\nmineru_api_token='secret'\n", encoding="utf-8")
            self.assertEqual("secret", load_token_from_dotenv(env_path))
            self.assertIsNone(load_token_from_dotenv(root / "missing"))

            tasks_path = root / "tasks.json"
            tasks = load_tasks(tasks_path, ["1-2", "3-4"])
            tasks[0].status = "done"
            save_tasks(tasks_path, tasks)
            restored = load_tasks(tasks_path, ["1-2", "3-4", "5-5"])
            self.assertEqual("done", restored[0].status)
            self.assertEqual(3, len(restored))
            self.assertIn("part_001", make_data_id(Path("中文 教材.pdf"), restored[0]))

        valid = argparse.Namespace(
            max_retries=1,
            submit_files_per_minute=1,
            result_requests_per_minute=1,
            daily_upload_file_limit=1,
            max_upload_mb=1,
        )
        self.assertEqual(0, validate_args(valid))
        for field in vars(valid):
            broken = argparse.Namespace(**vars(valid))
            setattr(broken, field, 0)
            self.assertEqual(2, validate_args(broken))

    def test_request_submission_and_polling(self) -> None:
        limiter = StubLimiter()
        args = argparse.Namespace(
            submit_rate_limiter=limiter,
            result_rate_limiter=limiter,
            url="https://example.test/book.pdf",
            model_version="vlm",
            language="ch",
            ocr=True,
            table=True,
            formula=True,
            no_cache=True,
            api_base="https://api.example/",
            token="token",
            max_retries=2,
            resubmit_failed=False,
        )
        task = BatchTask(index=1, page_range="1-2")
        with patch(
            "mineru_documents_markdown.mineru_batch_parse.request_json",
            return_value={"code": 0, "data": {"task_id": "t1", "state": "pending"}},
        ):
            submit_task(args, task)
        self.assertEqual("t1", task.task_id)
        self.assertEqual("pending", task.status)

        with patch(
            "mineru_documents_markdown.mineru_batch_parse.request_json",
            return_value={
                "code": 0,
                "data": {"state": "done", "full_zip_url": "https://example.test/result.zip"},
            },
        ):
            poll_task(args, task)
        self.assertEqual("done", task.status)
        self.assertTrue(task.full_zip_url)

        batch_task = BatchTask(
            index=2,
            page_range="3-4",
            data_id="d2",
            file_name="part.pdf",
            status="pending",
        )
        with patch(
            "mineru_documents_markdown.mineru_batch_parse.request_json",
            return_value={
                "code": 0,
                "data": {
                    "extract_result": [
                        {
                            "data_id": "d2",
                            "state": "failed",
                            "err_msg": "bad pdf",
                        }
                    ]
                },
            },
        ):
            poll_batch(args, "batch", [batch_task])
        self.assertEqual("bad pdf", batch_task.error)

        task.status = "done"
        task.full_zip_url = "ready"
        with patch("mineru_documents_markdown.mineru_batch_parse.submit_task") as mocked:
            ensure_url_submitted(args, task)
            mocked.assert_not_called()

    def test_upload_batch_and_request_json(self) -> None:
        limiter = StubLimiter()
        args = argparse.Namespace(
            submit_rate_limiter=limiter,
            model_version="vlm",
            language="ch",
            ocr=True,
            table=True,
            formula=False,
            api_base="https://api.example",
            token="token",
            max_retries=1,
        )
        tasks = [
            BatchTask(index=1, page_range="1-2", chunk_path="chunks/a.pdf"),
            BatchTask(index=2, page_range="3-4", chunk_path="chunks/b.pdf"),
        ]
        with patch(
            "mineru_documents_markdown.mineru_batch_parse.request_json",
            return_value={
                "code": 0,
                "data": {
                    "batch_id": "batch",
                    "file_urls": ["https://upload/a", "https://upload/b"],
                },
            },
        ):
            request_upload_batch(args, Path("book.pdf"), tasks)
        self.assertEqual([2], limiter.units)
        self.assertTrue(all(task.batch_id == "batch" for task in tasks))

        with patch(
            "mineru_documents_markdown.mineru_batch_parse.urllib.request.urlopen",
            return_value=JsonResponse({"code": 0, "data": {"ok": True}}),
        ):
            payload = request_json("GET", "https://api.example/test", "token")
        self.assertTrue(payload["data"]["ok"])

        with patch(
            "mineru_documents_markdown.mineru_batch_parse.urllib.request.urlopen",
            return_value=JsonResponse({"code": 7, "msg": "denied"}),
        ):
            with self.assertRaises(MinerUError):
                request_json("GET", "https://api.example/test", "token")


class LlmAndWarnReviewTests(unittest.TestCase):
    def test_llm_payload_normalization_cache_and_fallback(self) -> None:
        item = candidate()
        rule = fallback()
        self.assertEqual("c1", candidate_payload("doc", item, rule, [])["candidate"]["candidate_id"])
        self.assertEqual(1, len(batch_payload("doc", [item], [rule], [])["candidates"]))
        self.assertEqual(cache_key({"a": 1}), cache_key({"a": 1}))

        decision = decision_from_llm(
            {
                "action": "split_heading",
                "heading_text": "第一章",
                "remaining_text": "正文",
                "level": 9,
                "confidence": 2,
            },
            rule,
        )
        self.assertEqual(6, decision.level)
        self.assertEqual(1.0, decision.confidence)
        self.assertEqual("llm", decision.decision_source)
        self.assertEqual(
            [rule],
            batch_decisions_from_llm({"decisions": []}, [rule]),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            with patch(
                "mineru_documents_markdown.llm_heading_assist.call_chat_completions",
                return_value={"action": "keep_heading", "confidence": 0.9},
            ) as mocked:
                reviewed = assist_decision("doc", item, rule, [], cache_dir)
                cached = assist_decision("doc", item, rule, [], cache_dir)
            self.assertEqual(0.9, reviewed.confidence)
            self.assertEqual(0.9, cached.confidence)
            mocked.assert_called_once()

            with patch(
                "mineru_documents_markdown.llm_heading_assist.call_chat_completions",
                return_value={"decisions": [{"candidate_id": "c1", "confidence": 0.8}]},
            ):
                batch = assist_decision_batch("other", [item], [rule], [], cache_dir)
            self.assertEqual(0.8, batch[0].confidence)

            with patch(
                "mineru_documents_markdown.llm_heading_assist.assist_decision_batch",
                side_effect=RuntimeError("offline"),
            ):
                assisted = maybe_assist_decisions(
                    "doc",
                    [item],
                    [rule],
                    {},
                    {},
                    [],
                    "hybrid",
                    cache_dir,
                    threshold=0.7,
                )
            self.assertEqual(1, len(assisted))

    def test_warn_review_collection_normalization_and_cache(self) -> None:
        self.assertTrue(short_text("x" * 300).endswith("…"))
        self.assertEqual(0.5, clamp_confidence("bad"))
        self.assertEqual(1.0, clamp_confidence(2))
        self.assertEqual({"a": 1}, unwrap_decision({"deepseek": {"a": 1}}))
        candidate_data = {
            "candidate_id": "doc:b1",
            "text": "标题正文",
            "current_heading_level": 2,
        }
        demoted = normalize_decision(
            {
                "decision": {
                    "action": "demote_to_paragraph",
                    "confidence": 0.9,
                }
            },
            candidate_data,
        )
        self.assertFalse(demoted["is_heading"])
        self.assertEqual("标题正文", demoted["remaining_text"])
        self.assertIn("schema", review_payload(candidate_data))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            out_dir = root / "doc"
            out_dir.mkdir()
            out_dir.joinpath("tasks.json").write_text("[]", encoding="utf-8")
            out_dir.joinpath("heading_quality.json").write_text(
                json.dumps(
                    {
                        "issues": [
                            {
                                "severity": "WARN",
                                "code": "sentence_like_heading",
                                "block_id": "b1",
                                "text": "标题正文",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            out_dir.joinpath("structured_blocks.jsonl").write_text(
                json.dumps(
                    {
                        "block_id": "b1",
                        "block_type": "heading",
                        "heading_level": 2,
                        "text": "标题正文",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            out_dir.joinpath("heading_decisions.jsonl").write_text(
                json.dumps({"block_id": "b1", "action": "keep_heading"}) + "\n",
                encoding="utf-8",
            )
            candidates = collect_warn_candidates(root, None, 2, 1)
            self.assertEqual(1, len(candidates))

            cache_dir = root / "cache"
            with patch(
                "mineru_documents_markdown.warn_review.call_chat_completions",
                return_value={"action": "keep_heading", "confidence": 0.8},
            ) as mocked:
                first = review_with_cache(candidates[0], cache_dir, False)
                second = review_with_cache(candidates[0], cache_dir, False)
            self.assertEqual(first, second)
            mocked.assert_called_once()

            report = root / "review.md"
            write_markdown_report(
                report,
                {
                    "count": 1,
                    "reviews": [
                        {
                            "candidate": candidates[0],
                            "deepseek": {**first, "review_status": "benign_warn"},
                        }
                    ],
                },
            )
            self.assertIn("Benign WARN", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
