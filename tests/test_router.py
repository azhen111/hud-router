"""Router unit/smoke: original_answer on truncate; no API retry; prompt append."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from evaluate import CaseResult, TestCase, render_report
from prompts import ANSWER_SYSTEM_PROMPT, PERMISSIVE_JUDGE_APPEND, ROUTER_SYSTEM_PROMPT
from router import (
    HUD_LINE_CHARS,
    HUD_MAX_LINES,
    MAX_ANSWER_CHARS,
    answer,
    build_client,
    complete_chat,
    get_answer_model,
    is_skip_answer,
    normalize_result,
    parse_answer_output,
    postprocess_hud,
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

    def test_exactly_max_chars_not_truncated(self) -> None:
        text = "Y" * MAX_ANSWER_CHARS
        result = normalize_result(
            {
                "should_respond": True,
                "confidence": 0.9,
                "kind": "answer",
                "reason": "fact",
                "answer": text,
                "needs_more_context": False,
            }
        )
        self.assertFalse(result.truncated_to_none)
        self.assertTrue(result.should_respond)
        self.assertEqual(result.answer, text)
        self.assertEqual(result.original_answer, "")

    def test_evaluate_report_shows_original_answer(self) -> None:
        long_answer = "Z" * (MAX_ANSWER_CHARS + 3)
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
        case = TestCase(
            id=9,
            locale="ja",
            turns=[("OTHER", "what")],
            expect=True,
            note="over",
            needs_more_context=None,
            kind=None,
            raw={},
        )
        report = render_report(
            [CaseResult(case=case, result=result, latency_ms=1.0, correct=False)],
            color=False,
        )
        self.assertIn(f"original_answer={long_answer!r}", report)
        model_line = next(
            line[len("model=") :]
            for line in report.splitlines()
            if line.startswith("model={")
        )
        dumped = json.loads(model_line)
        self.assertEqual(dumped["original_answer"], long_answer)
        self.assertTrue(dumped["truncated_to_none"])
        self.assertIn("Legend: `original_answer`", report)

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
        long_answer = "超限原文" + ("字" * (MAX_ANSWER_CHARS - 2))

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
        self.assertIn("56 characters maximum", section)
        self.assertIn("This is at most two lines on the display", section)

    def test_self_not_hard_excluded_in_prompt(self) -> None:
        self.assertNotIn("does NOT trigger", ROUTER_SYSTEM_PROMPT)
        self.assertIn("Judge SELF the same", ROUTER_SYSTEM_PROMPT)
        self.assertIn("Do not suppress a trigger just because the last", ROUTER_SYSTEM_PROMPT)

    def test_answer_prompt_verbatim_and_router_untouched(self) -> None:
        expected = (
            "You write one-screen answers for a heads-up display worn during a\n"
            "live conversation. The wearer is a software engineer; the conversation\n"
            "is a technical discussion. Resolve technical acronyms and terms in\n"
            "that context — RAG means retrieval-augmented generation, not a\n"
            "red/amber/green status; SLA, JWT, Pod and similar terms take their\n"
            "software-engineering sense."
        )
        self.assertTrue(ANSWER_SYSTEM_PROMPT.startswith(expected))
        self.assertIn("240 characters maximum", ANSWER_SYSTEM_PROMPT)
        self.assertIn('"hud": "..."', ANSWER_SYSTEM_PROMPT)
        self.assertIn('"detail":', ANSWER_SYSTEM_PROMPT)
        self.assertNotIn("When in doubt, prefer to respond.", ROUTER_SYSTEM_PROMPT)
        self.assertEqual(PERMISSIVE_JUDGE_APPEND, "When in doubt, prefer to respond.")
        self.assertIn("Never answer with a question. If you would have to ask the speaker", ROUTER_SYSTEM_PROMPT)


class TestSelfCallsRoute(unittest.TestCase):
    def test_self_last_speaker_invokes_model(self) -> None:
        seen: list[list[dict[str, str]]] = []

        def fake_complete(messages: list[dict[str, str]]) -> str:
            seen.append(messages)
            return json.dumps(
                {
                    "should_respond": True,
                    "confidence": 0.88,
                    "kind": "term",
                    "reason": "wearer asked for a gloss",
                    "answer": "JWT：JSON Web Token",
                    "needs_more_context": False,
                },
                ensure_ascii=False,
            )

        result = route(
            {
                "recent_turns": [
                    {"speaker": "SELF", "text": "JWT 是什么", "ts": 1}
                ],
                "locale": "zh",
            },
            completion_fn=fake_complete,
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("SELF: JWT 是什么", seen[0][1]["content"])
        self.assertTrue(result.should_respond)
        self.assertEqual(result.kind, "term")
        self.assertNotIn("last speaker is SELF", result.reason)


class TestAnswerTier(unittest.TestCase):
    def test_judge_answer_not_used_by_answer_helper(self) -> None:
        def fake_complete(messages: list[dict[str, str]]) -> str:
            self.assertEqual(messages[0]["content"], ANSWER_SYSTEM_PROMPT)
            self.assertIn("<judge_kind>term</judge_kind>", messages[1]["content"])
            self.assertIn("JWT 是什么", messages[1]["content"])
            return json.dumps(
                {"hud": "JWT：JSON Web Token", "detail": "JSON Web Token 用于鉴权。"},
                ensure_ascii=False,
            )

        payload, timing = answer(
            {
                "recent_turns": [
                    {"speaker": "OTHER", "text": "JWT 是什么", "ts": 1}
                ],
                "locale": "zh",
            },
            kind="term",
            completion_fn=fake_complete,
        )
        self.assertEqual(payload.hud, "JWT：JSON Web Token")
        self.assertIn("鉴权", payload.detail)
        self.assertIsNone(timing)
        self.assertFalse(payload.skipped)

    def test_skip_is_exact_trim(self) -> None:
        self.assertTrue(is_skip_answer("SKIP"))
        self.assertTrue(is_skip_answer("  SKIP  "))
        self.assertFalse(is_skip_answer("skip"))
        self.assertFalse(is_skip_answer("SKIP now"))

        def fake_complete(_messages: list[dict[str, str]]) -> str:
            return "SKIP"

        payload, _ = answer(
            {"recent_turns": [{"speaker": "OTHER", "text": "那是啥", "ts": 1}], "locale": "zh"},
            kind="answer",
            completion_fn=fake_complete,
        )
        self.assertTrue(payload.skipped)
        self.assertTrue(payload.no_push())

    def test_hud_postprocess_and_phone_only(self) -> None:
        self.assertEqual(MAX_ANSWER_CHARS, 240)
        self.assertEqual(HUD_LINE_CHARS, 28)
        self.assertEqual(HUD_MAX_LINES, 10)
        wrapped, stats = postprocess_hud("A" * 40)
        self.assertTrue(stats["wrapped"])
        self.assertEqual(wrapped.split("\n")[0], "A" * 28)
        discarded, stats2 = postprocess_hud("B" * 241)
        self.assertEqual(discarded, "")
        self.assertTrue(stats2["discarded"])
        lines = "\n".join(f"L{i}" for i in range(12))
        cut, stats3 = postprocess_hud(lines)
        self.assertEqual(len(cut.split("\n")), 10)
        self.assertEqual(stats3["lines_truncated"], 2)
        parsed = parse_answer_output(
            json.dumps({"hud": "SKIP", "detail": "只给手机的解释" * 4}, ensure_ascii=False)
        )
        self.assertEqual(parsed.hud, "")
        self.assertTrue(parsed.has_detail())
        self.assertFalse(parsed.no_push())

    def test_permissive_appends_without_editing_prompt(self) -> None:
        seen: list[list[dict[str, str]]] = []

        def fake_complete(messages: list[dict[str, str]]) -> str:
            seen.append(messages)
            return json.dumps(
                {
                    "should_respond": False,
                    "confidence": 0.2,
                    "kind": "none",
                    "reason": "unsure",
                    "answer": "",
                    "needs_more_context": False,
                }
            )

        route(
            {"recent_turns": [{"speaker": "OTHER", "text": "嗯", "ts": 1}], "locale": "zh"},
            completion_fn=fake_complete,
            extra_system=PERMISSIVE_JUDGE_APPEND,
        )
        self.assertIn(PERMISSIVE_JUDGE_APPEND, seen[0][0]["content"])
        self.assertTrue(seen[0][0]["content"].startswith(ROUTER_SYSTEM_PROMPT.rstrip()))
        self.assertNotIn(PERMISSIVE_JUDGE_APPEND, ROUTER_SYSTEM_PROMPT)

    def test_ignore_answer_keeps_overlength_trigger(self) -> None:
        long_answer = "X" * (MAX_ANSWER_CHARS + 8)
        payload = {
            "should_respond": True,
            "confidence": 0.91,
            "kind": "term",
            "reason": "gloss",
            "answer": long_answer,
            "needs_more_context": False,
        }
        killed = normalize_result(payload)
        self.assertFalse(killed.should_respond)
        self.assertTrue(killed.truncated_to_none)
        kept = normalize_result(payload, ignore_answer=True)
        self.assertTrue(kept.should_respond)
        self.assertEqual(kept.kind, "term")
        self.assertFalse(kept.truncated_to_none)
        self.assertEqual(kept.answer, long_answer)

        def fake_complete(_messages: list[dict[str, str]]) -> str:
            return json.dumps(payload, ensure_ascii=False)

        routed = route(
            {
                "recent_turns": [
                    {"speaker": "OTHER", "text": "JWT 是什么", "ts": 1}
                ],
                "locale": "zh",
            },
            completion_fn=fake_complete,
            ignore_answer=True,
        )
        self.assertTrue(routed.should_respond)
        self.assertEqual(routed.kind, "term")
        self.assertFalse(routed.truncated_to_none)

    def test_unset_answer_model_equals_router(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": "k", "ROUTER_MODEL": "gpt-4o-mini"}, clear=False):
            os_env_pop = "ANSWER_MODEL"
            old = __import__("os").environ.pop(os_env_pop, None)
            try:
                self.assertEqual(get_answer_model(), "gpt-4o-mini")
            finally:
                if old is not None:
                    __import__("os").environ[os_env_pop] = old

    def test_complete_chat_plain_text_skips_json_mode(self) -> None:
        text, _timing = complete_chat(
            _FakePlainClient(),
            [{"role": "user", "content": "hi"}],
            "dummy-model",
            json_mode=False,
        )
        self.assertEqual(text, "plain")


class _FakePlainClient:
    def __init__(self) -> None:
        self.n = 0

    def with_options(self, **_kwargs: object) -> "_FakePlainClient":
        return self

    @property
    def chat(self) -> SimpleNamespace:
        outer = self

        class _C:
            def create(self, **kwargs: object) -> SimpleNamespace:
                outer.n += 1
                if "response_format" in kwargs:
                    raise AssertionError("answer tier must not request json_object")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="plain"))]
                )

        return SimpleNamespace(completions=_C())


if __name__ == "__main__":
    unittest.main()
