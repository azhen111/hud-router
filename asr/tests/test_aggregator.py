"""Unit tests for TurnAggregator. No microphone, constructed FINAL sequences."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ASR: Path = Path(__file__).resolve().parents[1]
if str(_ASR) not in sys.path:
    sys.path.insert(0, str(_ASR))

from aggregator import AggregatorConfig, FinalSegment, TurnAggregator, join_segment_texts


def _seg(
    text: str,
    *,
    start: float,
    dur: float,
    recv: float | None = None,
    speech_final: bool = False,
) -> FinalSegment:
    end: float = start + dur
    return FinalSegment(
        text=text,
        start_s=start,
        duration_s=dur,
        speech_final=speech_final,
        recv_s=end if recv is None else recv,
    )


class JoinTextsTests(unittest.TestCase):
    def test_cjk_concat_without_space(self) -> None:
        self.assertEqual(
            join_segment_texts(["这个接口", "保证幂等吗"]),
            "这个接口保证幂等吗",
        )

    def test_ascii_words_get_space(self) -> None:
        self.assertEqual(join_segment_texts(["hello", "world"]), "hello world")


class SilenceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agg = TurnAggregator(
            AggregatorConfig(silence_ms=800, min_chars=1, use_speech_final=False)
        )

    def test_silence_timeout_ends_turn(self) -> None:
        self.assertEqual(self.agg.push(_seg("这个接口保证", start=0.0, dur=0.4)), [])
        self.assertEqual(self.agg.tick(1.19), [])
        out = self.agg.tick(1.201)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].discarded)
        self.assertEqual(out[0].text, "这个接口保证")
        self.assertEqual(out[0].segments, 1)
        self.assertEqual(out[0].speaker, "UNKNOWN")

    def test_close_finals_merge_before_silence(self) -> None:
        self.assertEqual(self.agg.push(_seg("这个接口", start=0.0, dur=0.4)), [])
        self.assertEqual(self.agg.push(_seg("保证幂等吗", start=0.45, dur=0.4)), [])
        out = self.agg.tick(1.70)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].segments, 2)
        self.assertEqual(out[0].text, "这个接口保证幂等吗")

    def test_gap_on_next_final_closes_previous(self) -> None:
        self.assertEqual(self.agg.push(_seg("第一句", start=0.0, dur=0.3)), [])
        out = self.agg.push(_seg("第二句", start=1.5, dur=0.3, recv=1.8))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "第一句")
        later = self.agg.tick(2.7)
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0].text, "第二句")


class MaxTurnTests(unittest.TestCase):
    def test_max_turn_force_end(self) -> None:
        agg = TurnAggregator(
            AggregatorConfig(
                silence_ms=60_000, max_turn_ms=1000, min_chars=1, use_speech_final=False
            )
        )
        self.assertEqual(agg.push(_seg("开始", start=0.0, dur=0.2)), [])
        self.assertEqual(agg.tick(0.9), [])
        out = agg.tick(1.05)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "开始")
        self.assertGreaterEqual(out[0].duration_ms, 1000.0)

    def test_long_span_on_push_force_end(self) -> None:
        agg = TurnAggregator(
            AggregatorConfig(
                silence_ms=60_000, max_turn_ms=15_000, min_chars=1, use_speech_final=False
            )
        )
        self.assertEqual(agg.push(_seg("aaa", start=0.0, dur=0.2)), [])
        out = agg.push(_seg("bbb", start=14.8, dur=0.5, recv=15.3))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].segments, 2)


class MinCharsTests(unittest.TestCase):
    def test_short_turn_discarded(self) -> None:
        agg = TurnAggregator(AggregatorConfig(min_chars=4, use_speech_final=True))
        out = agg.push(_seg("嗯嗯", start=0.0, dur=0.2, speech_final=True))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].discarded)
        self.assertEqual(agg.discarded_min_chars, 1)

    def test_four_chars_kept(self) -> None:
        agg = TurnAggregator(AggregatorConfig(min_chars=4, use_speech_final=True))
        out = agg.push(_seg("保证幂等", start=0.0, dur=0.5, speech_final=True))
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].discarded)
        self.assertEqual(out[0].text, "保证幂等")


class SpeechFinalTests(unittest.TestCase):
    def test_speech_final_ends_turn(self) -> None:
        agg = TurnAggregator(AggregatorConfig(silence_ms=800, min_chars=1, use_speech_final=True))
        self.assertEqual(agg.push(_seg("那个网关", start=0.0, dur=0.4, speech_final=False)), [])
        out = agg.push(_seg("限流吗", start=0.45, dur=0.3, speech_final=True))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].segments, 2)
        self.assertEqual(out[0].text, "那个网关限流吗")

    def test_speech_final_ignored_when_disabled(self) -> None:
        agg = TurnAggregator(AggregatorConfig(silence_ms=800, min_chars=1, use_speech_final=False))
        self.assertEqual(agg.push(_seg("还没结束", start=0.0, dur=0.4, speech_final=True)), [])
        self.assertEqual(agg.tick(0.9), [])
        out = agg.tick(1.21)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "还没结束")


class FlushTests(unittest.TestCase):
    def test_flush_emits_open_turn(self) -> None:
        agg = TurnAggregator(AggregatorConfig(min_chars=1, use_speech_final=False))
        agg.push(_seg("未完成", start=0.0, dur=0.3))
        out = agg.flush(0.4)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "未完成")


if __name__ == "__main__":
    unittest.main()
