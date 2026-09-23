"""live.py helpers: no API keys, no glasses hardware."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import unittest
from contextlib import redirect_stdout

from server.display_policy import Candidate, DisplayPolicy, PolicySettings
from server.live import (
    DEFAULT_MIN_ROUTE_CHARS,
    DEFAULT_ROUTER_TIMEOUT_MS,
    DEFAULT_TERMS_PATH,
    apply_trigger_policy,
    build_settings,
    pick_display_answer,
    display_speaker,
    format_router_timeout_skip,
    hits_keyterm,
    locale_from_lang,
    log_startup,
    map_speaker_role,
    parse_args,
    parse_uplink_message,
    too_short_for_router,
)
from settings import DEFAULT_AGG_SILENCE_MS
from stream import (
    DEEPGRAM_KEYTERM_MAX,
    listen_connect_kwargs,
    load_keyterms,
    load_keyterms_for_deepgram,
    sanitize_deepgram_keyterms,
)
from server.transcript_fix import load_term_entries
from server.tests.test_display_policy import FakeClock, make_policy


class SpeakerMap(unittest.TestCase):
    def test_self_other_unknown(self) -> None:
        self.assertEqual(map_speaker_role("self"), "SELF")
        self.assertEqual(map_speaker_role("Self"), "SELF")
        self.assertEqual(map_speaker_role("other"), "OTHER")
        self.assertEqual(map_speaker_role("Other"), "OTHER")
        self.assertEqual(map_speaker_role("unknown"), "UNKNOWN")
        self.assertEqual(map_speaker_role(""), "UNKNOWN")
        self.assertEqual(map_speaker_role(None), "UNKNOWN")

    def test_display_labels(self) -> None:
        self.assertEqual(display_speaker("other"), "Other")
        self.assertEqual(display_speaker("self"), "Self")
        self.assertEqual(display_speaker("unknown"), "Unknown")


class Locale(unittest.TestCase):
    def test_from_lang(self) -> None:
        self.assertEqual(locale_from_lang("zh"), "zh")
        self.assertEqual(locale_from_lang("zh-CN"), "zh")
        self.assertEqual(locale_from_lang("ja"), "ja")
        self.assertEqual(locale_from_lang("en"), "en")
        self.assertEqual(locale_from_lang("multi"), "zh")


class UplinkParse(unittest.TestCase):
    def test_json_pcm_with_meta(self) -> None:
        pcm = b"\x01\x02\x03\x04"
        raw = json.dumps(
            {
                "type": "pcm",
                "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                "speakerRole": "other",
                "direction": 12,
            }
        )
        parsed = parse_uplink_message(raw)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        got_pcm, role, direction = parsed
        self.assertEqual(got_pcm, pcm)
        self.assertEqual(role, "other")
        self.assertEqual(direction, 12)

    def test_direction_null(self) -> None:
        pcm = b"\x00\x00"
        raw = json.dumps(
            {
                "type": "pcm",
                "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                "speakerRole": "unknown",
                "direction": None,
            }
        )
        parsed = parse_uplink_message(raw)
        assert parsed is not None
        self.assertEqual(parsed[2], None)

    def test_raw_bytes_fallback(self) -> None:
        parsed = parse_uplink_message(b"\x11\x22")
        assert parsed is not None
        self.assertEqual(parsed[0], b"\x11\x22")
        self.assertEqual(parsed[1], "unknown")

    def test_ignores_non_pcm(self) -> None:
        self.assertIsNone(parse_uplink_message('{"type":"hello"}'))
        self.assertIsNone(parse_uplink_message("not-json"))


class MinRouteChars(unittest.TestCase):
    def test_default_is_three(self) -> None:
        self.assertEqual(DEFAULT_MIN_ROUTE_CHARS, 3)
        self.assertTrue(too_short_for_router("嗯", 3))
        self.assertTrue(too_short_for_router("  你好  ", 3))
        self.assertFalse(too_short_for_router("幂等吗", 3))
        self.assertFalse(too_short_for_router("这个接口保证幂等吗", 3))

    def test_keyterm_hit_exempts_short_filter(self) -> None:
        terms = ["RAG", "REST", "Pod", "限流", "JWT"]
        self.assertFalse(too_short_for_router("RAG", 3, terms))
        self.assertFalse(too_short_for_router("rest", 3, terms))
        self.assertFalse(too_short_for_router("Pod", 6, terms))
        self.assertFalse(too_short_for_router("线流", 3, ["线流", "限流"]))
        self.assertTrue(too_short_for_router("嗯", 3, terms))
        self.assertTrue(too_short_for_router("ab", 3, terms))
        self.assertTrue(hits_keyterm("什么是 jwt", ["JWT"]))
        self.assertFalse(hits_keyterm("嗯嗯", ["JWT"]))


class KeytermLoad(unittest.TestCase):
    def test_deepgram_gets_canons_only(self) -> None:
        terms = load_keyterms_for_deepgram(DEFAULT_TERMS_PATH)
        self.assertEqual(terms, load_keyterms(DEFAULT_TERMS_PATH))
        self.assertGreaterEqual(len(terms), 45)
        self.assertLessEqual(len(terms), DEEPGRAM_KEYTERM_MAX)
        self.assertLessEqual(len(terms), 80)
        required = {
            "Kubernetes",
            "Kafka",
            "JWT",
            "Pod",
            "GraphQL",
            "Redis",
            "gRPC",
            "限流",
            "回滚",
            "熔断",
            "灰度",
            "幂等",
            "分片",
            "扩容",
            "Flex",
            "Docker",
            "HTTP",
            "PostgreSQL",
            "MySQL",
            "CICD",
        }
        missing = required - set(terms)
        self.assertEqual(missing, set())
        variants = {
            "库布尔netes",
            "GrafficQL",
            "CFCAR",
            "GWT",
            "pud",
            "线流",
            "回拱",
            "FLCK",
            "Bocker",
            "HTL",
            "postgress",
            "ICD",
            "RST",
            "MyCircle",
            "hostgreatcircle",
            "GRPC",
            "Jva",
            "CI/CD",
        }
        leaked = variants & set(terms)
        self.assertEqual(leaked, set())
        self.assertEqual(len(terms), len(set(terms)))
        for term in terms:
            self.assertIsInstance(term, str)
            self.assertNotIn(",", term)
            self.assertNotIn("/", term)
            self.assertFalse(term.endswith(":1"))
            self.assertFalse(term.endswith(":5"))
            self.assertNotIn(":", term)

    def test_object_variants_stay_on_fix_layer(self) -> None:
        dg = set(load_keyterms_for_deepgram(DEFAULT_TERMS_PATH))
        entries = load_term_entries(DEFAULT_TERMS_PATH)
        jwt = next(e for e in entries if e.term == "JWT")
        self.assertIn("GWT", jwt.variants)
        self.assertIn("JWA", jwt.variants)
        self.assertNotIn("GWT", dg)
        self.assertIn("JWT", dg)
        cicd = next(e for e in entries if e.term == "CI/CD")
        self.assertIn("ICD", cicd.variants)
        self.assertNotIn("CI/CD", dg)

    def test_sanitize_strips_slash_and_caps(self) -> None:
        self.assertEqual(sanitize_deepgram_keyterms(["CI/CD", "JWT"]), ["CICD", "JWT"])
        flooded = [f"T{i}" for i in range(97)]
        capped = sanitize_deepgram_keyterms(flooded)
        self.assertEqual(len(capped), DEEPGRAM_KEYTERM_MAX)
        kwargs = listen_connect_kwargs("zh", flooded)
        self.assertEqual(len(kwargs["keyterm"]), DEEPGRAM_KEYTERM_MAX)
        self.assertNotIn("keywords", kwargs)

    def test_connect_kwargs_uses_keyterm_not_keywords(self) -> None:
        terms = ["REST", "Kafka", "幂等"]
        kwargs = listen_connect_kwargs("zh", terms)
        self.assertEqual(kwargs["keyterm"], terms)
        self.assertNotIn("keywords", kwargs)
        self.assertEqual(kwargs["model"], "nova-3")
        bare = listen_connect_kwargs("multi")
        self.assertNotIn("keywords", bare)
        self.assertNotIn("keyterm", bare)
        self.assertEqual(bare["language"], "multi")

    def test_build_settings_loads_keyterms_once(self) -> None:
        settings = build_settings(parse_args(["--lang", "multi", "--log", "/tmp/live_test.jsonl"]))
        self.assertEqual(settings.lang, "multi")
        self.assertGreaterEqual(len(settings.keyterms), 45)
        self.assertEqual(settings.min_route_chars, 3)
        self.assertEqual(settings.silence_ms, 1200)
        self.assertEqual(settings.router_timeout_ms, 3000)
        self.assertEqual(DEFAULT_AGG_SILENCE_MS, 1200)
        self.assertEqual(DEFAULT_ROUTER_TIMEOUT_MS, 3000)
        probe = listen_connect_kwargs(settings.lang, settings.keyterms)
        self.assertNotIn("keywords", probe)
        self.assertEqual(len(probe["keyterm"]), len(settings.keyterms))
        self.assertLessEqual(len(probe["keyterm"]), DEEPGRAM_KEYTERM_MAX)
        off = build_settings(
            parse_args(["--no-keyterms", "--min-route-chars", "6", "--silence-ms", "800"])
        )
        self.assertEqual(off.keyterms, [])
        self.assertEqual(off.min_route_chars, 6)
        self.assertEqual(off.silence_ms, 800)


class MultiLang(unittest.TestCase):
    def test_multi_keeps_keyterm_and_zh_locale(self) -> None:
        settings = build_settings(parse_args(["--lang", "multi", "--log", "/tmp/live_multi.jsonl"]))
        self.assertEqual(settings.lang, "multi")
        self.assertEqual(settings.locale, "zh")
        kwargs = listen_connect_kwargs(settings.lang, settings.keyterms)
        self.assertEqual(kwargs["language"], "multi")
        self.assertEqual(kwargs["model"], "nova-3")
        self.assertIn("keyterm", kwargs)
        self.assertGreaterEqual(len(kwargs["keyterm"]), 45)
        self.assertNotIn("keywords", kwargs)


class RouterTimeoutTruth(unittest.TestCase):
    def test_default_is_3000_and_logged(self) -> None:
        self.assertEqual(DEFAULT_ROUTER_TIMEOUT_MS, 3000)
        settings = build_settings(parse_args(["--log", "/tmp/live_to.jsonl"]))
        self.assertEqual(settings.router_timeout_ms, 3000)
        self.assertIn("3000", settings.router_timeout_source)
        line = format_router_timeout_skip(
            settings.router_timeout_ms, settings.router_timeout_source
        )
        self.assertIn("waited=3000ms", line)
        self.assertIn("DEFAULT_ROUTER_TIMEOUT_MS=3000", line)
        buf = io.StringIO()
        with redirect_stdout(buf):
            log_startup(settings)
        out = buf.getvalue()
        self.assertIn("router_timeout_ms=3000", out)
        self.assertIn("DEFAULT_ROUTER_TIMEOUT_MS=3000", out)

    def test_cli_override_source(self) -> None:
        settings = build_settings(
            parse_args(["--router-timeout-ms", "1800", "--log", "/tmp/live_to2.jsonl"])
        )
        self.assertEqual(settings.router_timeout_ms, 1800)
        self.assertIn("cli", settings.router_timeout_source)
        self.assertIn("1800", settings.router_timeout_source)


class PolicyWiring(unittest.TestCase):
    def test_allow_push(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        allowed, text, audit = apply_trigger_policy(p, "幂等：多次执行结果相同", 0.90)
        self.assertTrue(allowed)
        self.assertEqual(text, "幂等：多次执行结果相同")
        self.assertTrue(audit["enabled"])
        self.assertTrue(audit["allowed"])
        self.assertEqual(audit["action"], "push")
        self.assertEqual(audit["mode"], "full")
        self.assertEqual(audit["budget_used"], 1)
        self.assertEqual(audit["budget_max"], 1)
        self.assertEqual(audit["ttl_ms"], 25_000)

    def test_deny_below_hint(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        allowed, text, audit = apply_trigger_policy(p, "幂等：多次执行结果相同", 0.69)
        self.assertFalse(allowed)
        self.assertEqual(text, "")
        self.assertEqual(audit["reason"], "below_hint")
        self.assertFalse(audit["allowed"])

    def test_deny_budget(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        apply_trigger_policy(p, "第一条答案够一眼", 0.90, ts_ms=0)
        allowed, _, audit = apply_trigger_policy(p, "第二条完全不同的话", 0.91, ts_ms=1000)
        self.assertFalse(allowed)
        self.assertEqual(audit["reason"], "budget")
        self.assertEqual(audit["budget_used"], 1)

    def test_deny_dedup(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        apply_trigger_policy(p, "同一句话不要再推", 0.90, ts_ms=0)
        clock.t = 60_000
        allowed, _, audit = apply_trigger_policy(p, "同一句话不要再推", 0.95, ts_ms=60_000)
        self.assertFalse(allowed)
        self.assertEqual(audit["reason"], "dedup")

    def test_deny_over_length(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock, max_chars=56)
        allowed, _, audit = apply_trigger_policy(p, "测" * 57, 0.90)
        self.assertFalse(allowed)
        self.assertEqual(audit["reason"], "over_max_two_lines")

    def test_no_policy_bypasses(self) -> None:
        settings = build_settings(
            parse_args(["--no-policy", "--log", "/tmp/live_nopol.jsonl"])
        )
        self.assertFalse(settings.policy_enabled)
        allowed, text, audit = apply_trigger_policy(
            None, "幂等：多次执行结果相同", 0.50
        )
        self.assertTrue(allowed)
        self.assertEqual(text, "幂等：多次执行结果相同")
        self.assertFalse(audit["enabled"])
        self.assertEqual(audit["reason"], "bypassed")
        buf = io.StringIO()
        with redirect_stdout(buf):
            log_startup(settings)
        self.assertIn("policy=off", buf.getvalue())

    def test_default_policy_off(self) -> None:
        settings = build_settings(parse_args(["--log", "/tmp/live_poloff.jsonl"]))
        self.assertFalse(settings.policy_enabled)
        buf = io.StringIO()
        with redirect_stdout(buf):
            log_startup(settings)
        self.assertIn("policy=off", buf.getvalue())

    def test_startup_prints_policy_on(self) -> None:
        settings = build_settings(parse_args(["--policy", "--log", "/tmp/live_pol.jsonl"]))
        self.assertTrue(settings.policy_enabled)
        self.assertEqual(settings.policy.conf_full, 0.85)
        self.assertEqual(settings.policy.conf_hint, 0.70)
        self.assertEqual(settings.policy.budget_window_ms, 60_000)
        self.assertEqual(settings.policy.budget_max, 1)
        self.assertEqual(settings.policy.ttl_ms, 25_000)
        self.assertEqual(settings.policy.dedup_window, 10)
        self.assertEqual(settings.policy.max_chars, 240)
        buf = io.StringIO()
        with redirect_stdout(buf):
            log_startup(settings)
        out = buf.getvalue()
        self.assertIn("policy=on", out)
        self.assertIn("full=0.85", out)
        self.assertIn("ttl=25000ms", out)
        self.assertIn("max_chars=240", out)


class TwoTierWiring(unittest.TestCase):
    def test_defaults_two_tier_and_fix_on(self) -> None:
        settings = build_settings(parse_args(["--log", "/tmp/live_tier.jsonl"]))
        self.assertTrue(settings.two_tier)
        self.assertTrue(settings.fix_enabled)
        self.assertEqual(settings.answer_timeout_ms, 4000)
        buf = io.StringIO()
        with redirect_stdout(buf):
            log_startup(settings)
        out = buf.getvalue()
        self.assertIn("two_tier=on", out)
        self.assertIn("answer_timeout_ms=4000", out)
        self.assertIn("judge.answer ignored", out)
        self.assertIn("fix=on", out)
        self.assertIn("policy=off", out)
        self.assertFalse(settings.policy_enabled)
        self.assertFalse(settings.permissive)
        self.assertIn("keyterms_dg=", out)
        self.assertIn("fix_entries=", out)
        self.assertIn("canon only", out)

    def test_single_shot_uses_judge_answer(self) -> None:
        settings = build_settings(
            parse_args(["--single-shot", "--log", "/tmp/live_ss.jsonl"])
        )
        self.assertFalse(settings.two_tier)
        alias = build_settings(
            parse_args(["--no-answer-tier", "--log", "/tmp/live_nat.jsonl"])
        )
        self.assertFalse(alias.two_tier)
        text, skip = pick_display_answer(
            two_tier=False,
            judge_answer="JWT：JSON Web Token",
            answer_text="should not be used",
            answer_timed_out=False,
        )
        self.assertEqual(text, "JWT：JSON Web Token")
        self.assertIsNone(skip)

    def test_two_tier_ignores_judge_answer(self) -> None:
        text, skip = pick_display_answer(
            two_tier=True,
            judge_answer="JUDGE SHOULD BE IGNORED",
            answer_text="JWT：JSON Web Token",
            answer_timed_out=False,
        )
        self.assertEqual(text, "JWT：JSON Web Token")
        self.assertIsNone(skip)

    def test_permissive_flag(self) -> None:
        settings = build_settings(
            parse_args(["--permissive", "--log", "/tmp/live_perm.jsonl"])
        )
        self.assertTrue(settings.permissive)
        buf = io.StringIO()
        with redirect_stdout(buf):
            log_startup(settings)
        self.assertIn("permissive=on", buf.getvalue())

    def test_two_tier_skip_and_timeout(self) -> None:
        text, skip = pick_display_answer(
            two_tier=True,
            judge_answer="x",
            answer_text="SKIP",
            answer_timed_out=False,
        )
        self.assertIsNone(text)
        self.assertEqual(skip, "answer_skip")
        text2, skip2 = pick_display_answer(
            two_tier=True,
            judge_answer="x",
            answer_text="anything",
            answer_timed_out=True,
        )
        self.assertIsNone(text2)
        self.assertEqual(skip2, "answer_timeout")


class PolicyTtlTimer(unittest.IsolatedAsyncioTestCase):
    async def test_ttl_clear_fires_without_new_candidate(self) -> None:
        expired: list[str] = []
        loop = asyncio.get_running_loop()
        handles: list[asyncio.TimerHandle] = []

        def set_timer(delay_ms: int, cb) -> None:
            handles.append(loop.call_later(delay_ms / 1000.0, cb))

        def clear_timer() -> None:
            for h in handles:
                h.cancel()
            handles.clear()

        policy = DisplayPolicy(
            settings=PolicySettings(ttl_ms=40),
            now_ms=lambda: int(loop.time() * 1000),
            set_timer=set_timer,
            clear_timer=clear_timer,
            on_expire=lambda: expired.append("clear"),
        )
        d = policy.consider(Candidate("幂等：多次执行结果相同", 0.90))
        self.assertEqual(d.action, "push")
        self.assertEqual(expired, [])
        await asyncio.sleep(0.08)
        self.assertEqual(expired, ["clear"])
        self.assertFalse(policy.showing)


if __name__ == "__main__":
    unittest.main()
