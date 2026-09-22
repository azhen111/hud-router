"""Router unit/smoke: original_answer on truncate; no API retry; prompt append."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prompts import ROUTER_SYSTEM_PROMPT
from router import (
    MAX_ANSWER_CHARS,
    build_client,
    complete_chat,
    normalize_result,
    route,
)


class TestOriginalAnswer(unittest.TestCase):
    def test_truncated_keeps_pre_clear_text(self) -> None:
        long_answer = "X" * (MAX_ANSWER_CHARS + 5)
        result = normalize_result(
            {
                "should_respond": True,
                "confidence": 0.9,
                "kind": "answer",
                "reason": "fact",
                "answer": long_answer,
                "needs_more_context": False,
            }
        )
        self.assertTrue(result.truncated_to_none)
        self.assertTrue(result.over_length)
        self.assertFalse(result.should_respond)
        self.assertEqual(result.kind, "none")
        self.assertEqual(result.answer, "")
        self.assertEqual(result.original_answer, long_answer)
        public = result.to_public_dict()
        self.assertEqual(public["original_answer"], long_answer)
        self.assertEqual(public["truncated_to_none"], True)

    def test_original_answer_empty_when_not_truncated(self) -> None:
        result = normalize_result(
            {
                "should_respond": True,
                "confidence": 0.8,
                "kind": "term",
                "reason": "gloss",
                "answer": "short",
                "needs_more_context": False,
            }
        )
        self.assertFalse(result.truncated_to_none)
        self.assertEqual(result.original_answer, "")
        self.assertEqual(result.to_public_dict()["original_answer"], "")

    def test_non_trigger_does_not_populate_original_answer(self) -> None:
        result = normalize_result(
            {
                "should_respond": False,
                "confidence": 0.4,
                "kind": "none",
                "reason": "backchannel",
                "answer": "Y" * 80,
                "needs_more_context": False,
            }
        )
        self.assertFalse(result.truncated_to_none)
        self.assertEqual(result.answer, "")
        self.assertEqual(result.original_answer, "")

    def test_route_completion_fn_smoke(self) -> None:
        long_answer = "这是一段超过二十八个字的模型回答会被清空所以必须保留原文。"

        def fake_complete(_messages: list[dict[str, str]]) -> str:
            return json.dumps(
                {
                    "should_respond": True,
                    "confidence": 0.91,
                    "kind": "answer",
                    "reason": "definition",
                    "answer": long_answer,
                    "needs_more_context": False,
                },
                ensure_ascii=False,
            )

        result = route(
            {
                "recent_turns": [
                    {"speaker": "OTHER", "text": "那是什么意思", "ts": 1}
                ],
                "locale": "zh",
            },
            completion_fn=fake_complete,
        )
        self.assertTrue(result.truncated_to_none)
        self.assertEqual(result.original_answer, long_answer)
        self.assertEqual(result.answer, "")
        self.assertIn(long_answer, json.dumps(result.to_public_dict(), ensure_ascii=False))


class _CountingFailCompletions:
    def __init__(self) -> None:
        self.n = 0

    def create(self, **_kwargs: object) -> object:
        self.n += 1
        raise TimeoutError("simulated timeout")


class _FakeFailClient:
    def __init__(self) -> None:
        self.completions = _CountingFailCompletions()
        self.chat = SimpleNamespace(completions=self.completions)
        self.seen_options: dict[str, object] = {}

    def with_options(self, **kwargs: object) -> _FakeFailClient:
        self.seen_options = dict(kwargs)
        return self


class TestNoRetry(unittest.TestCase):
    def test_build_client_disables_sdk_retries(self) -> None:
        captured: dict[str, object] = {}

        def fake_openai(**kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(max_retries=kwargs.get("max_retries"))

        with patch("router.OpenAI", side_effect=fake_openai):
            build_client(api_key="sk-test", base_url="http://127.0.0.1:9/v1")
        self.assertEqual(captured.get("max_retries"), 0)

    def test_complete_chat_does_not_retry_timeout(self) -> None:
        client = _FakeFailClient()
        with self.assertRaises(TimeoutError):
            complete_chat(
                client,  # type: ignore[arg-type]
                [{"role": "user", "content": "hi"}],
                "dummy-model",
            )
        self.assertEqual(client.completions.n, 1)
        self.assertEqual(client.seen_options.get("max_retries"), 0)

    def test_route_degrades_on_first_timeout(self) -> None:
        client = _FakeFailClient()
        with patch("router.get_settings", return_value=("k", None, "dummy-model")):
            result = route(
                {
                    "recent_turns": [
                        {"speaker": "OTHER", "text": "what is CAGR", "ts": 1}
                    ],
                    "locale": "en",
                },
                client=client,  # type: ignore[arg-type]
            )
        self.assertEqual(client.completions.n, 1)
        self.assertFalse(result.should_respond)
        self.assertIn("model call failed", result.reason)
        self.assertEqual(result.original_answer, "")


class TestPromptAppend(unittest.TestCase):
    def test_never_answer_with_a_question_at_end_of_answer_section(self) -> None:
        start = ROUTER_SYSTEM_PROMPT.index("## The answer")
        end = ROUTER_SYSTEM_PROMPT.index("## kind")
        section = ROUTER_SYSTEM_PROMPT[start:end]
        bullet = (
            "- Never answer with a question. If you would have to ask the speaker\n"
            "  what they meant, the referent was not resolvable and you should have\n"
            "  set should_respond to false. An answer that asks something back cannot\n"
            "  be acted on: the wearer is in a conversation and cannot reply to the\n"
            "  display."
        )
        self.assertIn(bullet, section)
        self.assertTrue(section.rstrip().endswith(bullet))
        self.assertIn("28 characters maximum", section)


if __name__ == "__main__":
    unittest.main()
