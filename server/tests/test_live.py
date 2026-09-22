"""live.py helpers: no API keys, no glasses hardware."""

from __future__ import annotations

import base64
import json
import unittest

from server.live import (
    display_speaker,
    locale_from_lang,
    map_speaker_role,
    parse_uplink_message,
)


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


if __name__ == "__main__":
    unittest.main()
