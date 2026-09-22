"""Aggregate Deepgram FINAL segments into complete turns.

Speaker is always UNKNOWN (no diarization). Thresholds come from
AggregatorConfig and are never baked in without an override path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from settings import (
    DEFAULT_AGG_MAX_TURN_MS,
    DEFAULT_AGG_MIN_CHARS,
    DEFAULT_AGG_SILENCE_MS,
    DEFAULT_AGG_USE_SPEECH_FINAL,
)


@dataclass(frozen=True)
class AggregatorConfig:
    silence_ms: int = DEFAULT_AGG_SILENCE_MS
    max_turn_ms: int = DEFAULT_AGG_MAX_TURN_MS
    min_chars: int = DEFAULT_AGG_MIN_CHARS
    use_speech_final: bool = DEFAULT_AGG_USE_SPEECH_FINAL


@dataclass(frozen=True)
class FinalSegment:
    """One ASR FINAL. Clocks are seconds on a shared session timeline."""

    text: str
    start_s: float
    duration_s: float
    speech_final: bool
    recv_s: float


@dataclass(frozen=True)
class AggregatedTurn:
    speaker: str
    text: str
    ts: float
    segments: int
    duration_ms: float
    discarded: bool

    def to_emit_dict(self) -> dict[str, object]:
        return {
            "speaker": self.speaker,
            "text": self.text,
            "ts": self.ts,
            "segments": self.segments,
            "duration_ms": self.duration_ms,
        }


def _needs_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    a: str = left[-1]
    b: str = right[0]
    if a.isspace() or b.isspace():
        return False
    ascii_word: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return a in ascii_word and b in ascii_word


def join_segment_texts(texts: list[str]) -> str:
    """Join FINAL pieces: space only between ASCII words, none between CJK."""
    out: str = ""
    for raw in texts:
        piece: str = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        out = f"{out} {piece}" if _needs_space(out, piece) else f"{out}{piece}"
    return out


@dataclass
class _OpenTurn:
    segments: list[FinalSegment] = field(default_factory=list)

    @property
    def first(self) -> FinalSegment:
        return self.segments[0]

    @property
    def last(self) -> FinalSegment:
        return self.segments[-1]

    def last_end_s(self) -> float:
        """Silence is measured from when the last FINAL ended (recv or audio end)."""
        audio_end: float = self.last.start_s + self.last.duration_s
        return max(self.last.recv_s, audio_end)

    def start_s(self) -> float:
        return self.first.start_s

    def duration_ms_at(self, now_s: float) -> float:
        end: float = max(now_s, self.last_end_s())
        return max(0.0, (end - self.start_s()) * 1000.0)


class TurnAggregator:
    """Push FINAL segments; tick(now) to apply silence / max-turn.

    Silence close is independent-timer-driven: after the last FINAL,
    the host must call ``tick(now)`` on its own clock. Waiting
    ``silence_ms`` with no further ASR events is enough to close the
    turn. ``push()`` also closes a prior turn when a new FINAL arrives
    after a silence gap, but that is not required for close.
    """

    def __init__(self, config: AggregatorConfig | None = None) -> None:
        self.config: AggregatorConfig = config if config is not None else AggregatorConfig()
        self._open: _OpenTurn | None = None
        self.discarded_min_chars: int = 0

    def push(self, segment: FinalSegment) -> list[AggregatedTurn]:
        text: str = segment.text.strip()
        if not text:
            return []
        emitted: list[AggregatedTurn] = []
        if self._open is not None:
            gap_ms: float = (segment.recv_s - self._open.last_end_s()) * 1000.0
            if gap_ms + 1e-6 >= float(self.config.silence_ms):
                emitted.extend(self._close(self._open.last_end_s()))
        if self._open is None:
            self._open = _OpenTurn(segments=[segment])
        else:
            self._open.segments.append(segment)
        if self._open.duration_ms_at(self._open.last_end_s()) + 1e-6 >= float(self.config.max_turn_ms):
            emitted.extend(self._close(self._open.last_end_s()))
        elif self.config.use_speech_final and segment.speech_final:
            emitted.extend(self._close(self._open.last_end_s()))
        return emitted

    def tick(self, now_s: float) -> list[AggregatedTurn]:
        if self._open is None:
            return []
        if self._open.duration_ms_at(now_s) + 1e-6 >= float(self.config.max_turn_ms):
            return self._close(now_s)
        silent_ms: float = (now_s - self._open.last_end_s()) * 1000.0
        if silent_ms + 1e-6 >= float(self.config.silence_ms):
            return self._close(now_s)
        return []

    def flush(self, now_s: float) -> list[AggregatedTurn]:
        if self._open is None:
            return []
        return self._close(now_s)

    def _close(self, now_s: float) -> list[AggregatedTurn]:
        open_turn: _OpenTurn | None = self._open
        self._open = None
        if open_turn is None or not open_turn.segments:
            return []
        text: str = join_segment_texts([s.text for s in open_turn.segments])
        duration_ms: float = open_turn.duration_ms_at(now_s)
        discarded: bool = len(text) < self.config.min_chars
        if discarded:
            self.discarded_min_chars += 1
        turn: AggregatedTurn = AggregatedTurn(
            speaker="UNKNOWN",
            text=text,
            ts=open_turn.start_s(),
            segments=len(open_turn.segments),
            duration_ms=duration_ms,
            discarded=discarded,
        )
        return [turn]
