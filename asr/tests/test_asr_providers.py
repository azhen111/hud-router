"""Unified ASR events + Aliyun NLS mapping. No network, no secrets."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ASR: Path = Path(__file__).resolve().parents[1]
if str(_ASR) not in sys.path:
    sys.path.insert(0, str(_ASR))

from aliyun_nls import (
    build_create_token_params,
    canonical_query,
    parse_aliyun_nls_event,
    percent_encode,
    require_aliyun_nls_config,
    sign_pop_rpc,
    start_transcription_message,
)
from provider import (
    ASR_ALIYUN,
    ASR_DEEPGRAM,
    AsrConfigError,
    AsrEvent,
    asr_event_from_deepgram,
    create_asr_session,
    resolve_asr_provider,
)
from stream import ParsedAsrResult, parse_deepgram_result


class ResolveProvider(unittest.TestCase):
    def test_default_deepgram(self) -> None:
        with patch.dict("os.environ", {"LIVE_ASR": ""}, clear=False):
            os_environ_pop = "LIVE_ASR"
            old = __import__("os").environ.pop(os_environ_pop, None)
            try:
                self.assertEqual(resolve_asr_provider(None), ASR_DEEPGRAM)
            finally:
                if old is not None:
                    __import__("os").environ[os_environ_pop] = old

    def test_cli_and_aliases(self) -> None:
        self.assertEqual(resolve_asr_provider("deepgram"), ASR_DEEPGRAM)
        self.assertEqual(resolve_asr_provider("aliyun"), ASR_ALIYUN)
        self.assertEqual(resolve_asr_provider("nls"), ASR_ALIYUN)
        with self.assertRaises(AsrConfigError):
            resolve_asr_provider("unknown-vendor")


class DeepgramParseStillWorks(unittest.TestCase):
    def test_results_to_event(self) -> None:
        parsed = parse_deepgram_result(
            {
                "type": "Results",
                "is_final": True,
                "speech_final": False,
                "start": 1.25,
                "duration": 0.8,
                "channel": {
                    "alternatives": [{"transcript": "JWT 是什么", "confidence": 0.91}]
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        event = asr_event_from_deepgram(parsed, ts=1700000000.0)
        self.assertEqual(event.text, "JWT 是什么")
        self.assertTrue(event.is_final)
        self.assertFalse(event.speech_final)
        self.assertAlmostEqual(event.confidence, 0.91)
        self.assertEqual(event.provider, ASR_DEEPGRAM)
        self.assertEqual(event.ts, 1700000000.0)

    def test_interim_dropped_shape(self) -> None:
        parsed = ParsedAsrResult(
            transcript="今",
            confidence=0.2,
            is_final=False,
            speech_final=False,
            start_s=0.0,
            duration_s=0.2,
        )
        event = asr_event_from_deepgram(parsed, ts=1.0)
        self.assertFalse(event.is_final)
        self.assertEqual(event.text, "今")


class AliyunEventMap(unittest.TestCase):
    def test_sentence_end_is_final(self) -> None:
        event = parse_aliyun_nls_event(
            {
                "header": {"name": "SentenceEnd", "status": 20000000},
                "payload": {
                    "index": 1,
                    "time": 1500,
                    "begin_time": 200,
                    "result": "Vue3的原理是什么",
                    "confidence": 0.88,
                },
            },
            ts=99.0,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event.is_final)
        self.assertTrue(event.speech_final)
        self.assertEqual(event.text, "Vue3的原理是什么")
        self.assertEqual(event.provider, ASR_ALIYUN)
        self.assertAlmostEqual(event.start_s, 0.2)
        self.assertAlmostEqual(event.duration_s, 1.3)
        self.assertAlmostEqual(event.confidence, 0.88)
        self.assertEqual(event.ts, 99.0)

    def test_result_changed_is_interim(self) -> None:
        event = parse_aliyun_nls_event(
            {
                "header": {"name": "TranscriptionResultChanged", "status": 20000000},
                "payload": {"index": 1, "time": 800, "result": "Vue3的"},
            },
            ts=1.0,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertFalse(event.is_final)
        self.assertFalse(event.speech_final)
        self.assertEqual(event.text, "Vue3的")

    def test_started_and_empty_skipped(self) -> None:
        self.assertIsNone(
            parse_aliyun_nls_event(
                {"header": {"name": "TranscriptionStarted", "status": 20000000}}
            )
        )
        self.assertIsNone(
            parse_aliyun_nls_event(
                {
                    "header": {"name": "SentenceEnd", "status": 20000000},
                    "payload": {"result": "  "},
                }
            )
        )


class AliyunTokenSign(unittest.TestCase):
    def test_percent_and_canonical_stable(self) -> None:
        self.assertEqual(percent_encode("a b"), "a%20b")
        params = build_create_token_params(
            "testid", nonce="abc", timestamp="2020-01-01T00:00:00Z"
        )
        self.assertEqual(params["Action"], "CreateToken")
        self.assertEqual(params["Version"], "2019-02-28")
        canon = canonical_query(params)
        self.assertIn("Action=CreateToken", canon)
        sig = sign_pop_rpc("POST", params, "testsecret")
        # Golden HMAC-SHA1/base64 for this exact param set + secret.
        self.assertEqual(sig, "tF5XoCF6X85Vf2xkjoXrYny4SW0=")

    def test_missing_creds_fail_loud(self) -> None:
        env = {
            "ALIYUN_NLS_APPKEY": "",
            "ALIYUN_NLS_TOKEN": "",
            "ALIYUN_ACCESS_KEY_ID": "",
            "ALIYUN_ACCESS_KEY_SECRET": "",
        }
        with patch.dict("os.environ", env, clear=False):
            with self.assertRaises(AsrConfigError) as ctx:
                require_aliyun_nls_config()
            self.assertIn("ALIYUN_NLS_APPKEY", str(ctx.exception))
            self.assertIn("local .env", str(ctx.exception))

    def test_start_message_has_hex_ids(self) -> None:
        msg = start_transcription_message("appkey-placeholder", "a" * 32)
        self.assertEqual(msg["header"]["namespace"], "SpeechTranscriber")
        self.assertEqual(msg["header"]["name"], "StartTranscription")
        self.assertEqual(len(msg["header"]["message_id"]), 32)
        self.assertEqual(msg["payload"]["format"], "pcm")
        self.assertEqual(msg["payload"]["sample_rate"], 16000)
        self.assertTrue(msg["payload"]["enable_intermediate_result"])


class Factory(unittest.TestCase):
    def test_aliyun_session_without_connect(self) -> None:
        seen: list[AsrEvent] = []
        sess = create_asr_session(
            provider="aliyun",
            language="zh",
            on_event=seen.append,
            handshake_timeout_s=1.0,
        )
        self.assertEqual(sess.provider, ASR_ALIYUN)
        self.assertTrue(hasattr(sess, "send_pcm"))
        self.assertTrue(hasattr(sess, "close"))

    def test_deepgram_adapter_constructed(self) -> None:
        sess = create_asr_session(
            provider="deepgram",
            language="zh",
            on_event=lambda _e: None,
            api_key="not-a-real-key",
            keyterms=["JWT"],
        )
        self.assertEqual(sess.provider, ASR_DEEPGRAM)


if __name__ == "__main__":
    unittest.main()
