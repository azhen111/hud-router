"""live.py helpers: no API keys, no glasses hardware."""

from __future__ import annotations

import base64
import json
import unittest

from server.live import (
    DEFAULT_MIN_ROUTE_CHARS,
    DEFAULT_ROUTER_TIMEOUT_MS,
    DEFAULT_TERMS_PATH,
    build_settings,
    display_speaker,
    hits_keyterm,
    locale_from_lang,
    map_speaker_role,
    parse_args,
    parse_uplink_message,
    too_short_for_router,
)
from settings import DEFAULT_AGG_SILENCE_MS
from stream import listen_connect_kwargs, load_keyterms


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
    def test_terms_file_has_about_fifty_plain_terms(self) -> None:
        terms = load_keyterms(DEFAULT_TERMS_PATH)
        self.assertGreaterEqual(len(terms), 45)
        self.assertLessEqual(len(terms), 90)
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
            "库布尔netes",
            "GrafficQL",
            "CFCAR",
            "GWT",
            "pud",
            "线流",
            "回拱",
        }
        missing = required - set(terms)
        self.assertEqual(missing, set())
        self.assertEqual(len(terms), len(set(terms)))
        for term in terms:
            self.assertIsInstance(term, str)
            self.assertNotIn(",", term)
            self.assertFalse(term.endswith(":1"))
            self.assertFalse(term.endswith(":5"))
            self.assertNotIn(":", term)

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
        off = build_settings(
            parse_args(["--no-keyterms", "--min-route-chars", "6", "--silence-ms", "800"])
        )
        self.assertEqual(off.keyterms, [])
        self.assertEqual(off.min_route_chars, 6)
        self.assertEqual(off.silence_ms, 800)


if __name__ == "__main__":
    unittest.main()
