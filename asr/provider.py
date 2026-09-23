"""Streaming ASR factory: one event shape, vendor adapters.

Upstream (``server/live.py`` aggregator) only sees ``AsrEvent``:

- ``text`` — transcript fragment
- ``is_final`` — sentence-complete (aggregator only ingests these today)
- ``speech_final`` — vendor analogue of Deepgram speech_final; live still
  closes turns on silence (``use_speech_final=False``)
- ``confidence`` — 0..1 when the vendor provides it
- ``ts`` — local unix seconds when the event was built
- ``start_s`` / ``duration_s`` — audio-clock fields ``FinalSegment`` already uses
- ``provider`` — ``deepgram`` | ``aliyun``

Deepgram is the default. Add a third vendor: one adapter class + one
``create_asr_session`` branch. Do not send Deepgram ``keyterm`` to Aliyun.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from stream import (
    DeepgramPcmSession,
    ParsedAsrResult,
    parse_deepgram_result,
    require_api_key,
)

ASR_DEEPGRAM: str = "deepgram"
ASR_ALIYUN: str = "aliyun"
ASR_PROVIDERS: frozenset[str] = frozenset({ASR_DEEPGRAM, ASR_ALIYUN})


class AsrConfigError(RuntimeError):
    """Missing credentials, unknown provider, or handshake configuration."""


@dataclass(frozen=True)
class AsrEvent:
    """Unified streaming transcript event (Deepgram + Aliyun + future)."""

    text: str
    is_final: bool
    speech_final: bool
    confidence: float
    ts: float
    start_s: float = 0.0
    duration_s: float = 0.0
    provider: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "is_final": self.is_final,
            "speech_final": self.speech_final,
            "confidence": self.confidence,
            "ts": self.ts,
            "start_s": self.start_s,
            "duration_s": self.duration_s,
            "provider": self.provider,
        }


@runtime_checkable
class AsrSession(Protocol):
    """PCM-in session. ``start`` blocks until the vendor handshake succeeds."""

    provider: str

    def start(self) -> None: ...

    def send_pcm(self, chunk: bytes) -> None: ...

    def close(self) -> None: ...


def resolve_asr_provider(raw: str | None = None) -> str:
    """CLI ``--asr`` > ``LIVE_ASR`` > deepgram."""
    text = (raw if raw is not None else os.environ.get("LIVE_ASR", "") or "").strip()
    if not text:
        return ASR_DEEPGRAM
    key = text.lower()
    if key in {"deepgram", "dg", "nova", "nova-3"}:
        return ASR_DEEPGRAM
    if key in {"aliyun", "ali", "nls", "alibaba", "aliyun-nls"}:
        return ASR_ALIYUN
    raise AsrConfigError(
        f"unknown ASR provider {text!r}; use deepgram or aliyun"
    )


def asr_event_from_deepgram(
    parsed: ParsedAsrResult, *, ts: float | None = None
) -> AsrEvent:
    return AsrEvent(
        text=parsed.transcript,
        is_final=parsed.is_final,
        speech_final=parsed.speech_final,
        confidence=parsed.confidence,
        ts=time.time() if ts is None else ts,
        start_s=parsed.start_s,
        duration_s=parsed.duration_s,
        provider=ASR_DEEPGRAM,
    )


class DeepgramAsrSession:
    """Adapter: ``DeepgramPcmSession`` → ``AsrEvent`` callbacks."""

    provider: str = ASR_DEEPGRAM

    def __init__(
        self,
        *,
        api_key: str,
        language: str,
        on_event: Callable[[AsrEvent], None],
        on_error: Callable[[object], None] | None = None,
        handshake_timeout_s: float = 60.0,
        keyterms: list[str] | None = None,
    ) -> None:
        self._on_event = on_event
        self._inner = DeepgramPcmSession(
            api_key=api_key,
            language=language,
            on_message=self._on_message,
            on_error=on_error,
            handshake_timeout_s=handshake_timeout_s,
            keyterms=keyterms,
        )

    def _on_message(self, message: object) -> None:
        parsed = parse_deepgram_result(message)
        if parsed is None:
            return
        self._on_event(asr_event_from_deepgram(parsed))

    def start(self) -> None:
        self._inner.start()

    def send_pcm(self, chunk: bytes) -> None:
        self._inner.send_pcm(chunk)

    def close(self) -> None:
        self._inner.close()


def create_asr_session(
    *,
    provider: str,
    language: str,
    on_event: Callable[[AsrEvent], None],
    on_error: Callable[[object], None] | None = None,
    handshake_timeout_s: float = 60.0,
    keyterms: list[str] | None = None,
    api_key: str | None = None,
) -> AsrSession:
    """Build a started-later PCM session. Deepgram gets keyterms; Aliyun does not."""
    name = resolve_asr_provider(provider)
    if name == ASR_DEEPGRAM:
        key = (api_key or "").strip()
        if not key:
            key = require_api_key()
        return DeepgramAsrSession(
            api_key=key,
            language=language,
            on_event=on_event,
            on_error=on_error,
            handshake_timeout_s=handshake_timeout_s,
            keyterms=keyterms,
        )
    if name == ASR_ALIYUN:
        from aliyun_nls import AliyunNlsSession

        return AliyunNlsSession(
            on_event=on_event,
            on_error=on_error,
            handshake_timeout_s=handshake_timeout_s,
        )
    raise AsrConfigError(f"unknown ASR provider {provider!r}")
