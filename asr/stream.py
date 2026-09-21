#!/usr/bin/env python3
"""Mic → Deepgram streaming ASR → colorized terminal + raw jsonl.

Observation tool only. Does not import or call Phase 1 routing code.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import statistics
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from colorama import Fore, Style, init as colorama_init
from deepgram import DeepgramClient
from deepgram.core.events import EventType

# sounddevice is imported lazily so --help works without PortAudio.
# Windows wheels typically bundle PortAudio; Linux needs libportaudio.

# Audio matches Even Realities G2 capture: 16 kHz mono 16-bit PCM.
SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
SAMPLE_WIDTH_BYTES: int = 2
BLOCK_FRAMES: int = 800  # 50 ms
# Current general streaming model. Supports zh / zh-CN and ja.
# See asr/README.md for why this name (not Flux, not nova-2).
DEEPGRAM_MODEL: str = "nova-3"

# Latency (audio-in → FINAL return), measured as:
#   latency = t_final_recv - t_audio_end
# t_final_recv  = wall time the FINAL message arrived (perf_counter).
# t_audio_end   = session_start + Deepgram.start + Deepgram.duration
# Deepgram.start/duration are seconds on the audio clock beginning at the
# first byte we sent. Because we stream the mic in real time, that clock
# lines up with wall time from session_start. The difference is Deepgram's
# default endpointing wait + network + decode. We do not set endpointing.


@dataclass
class FinalStats:
    recv_rel_s: float
    start_s: float
    duration_s: float
    confidence: float
    speech_final: bool
    chars: int
    latency_s: float


@dataclass
class Session:
    started_perf: float
    jsonl: TextIO
    jsonl_path: Path
    finals: list[FinalStats] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def load_dotenv(path: Path | None = None) -> None:
    """Load `.env` without overwriting existing environ. No extra dependency."""
    env_path: Path = path if path is not None else Path(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line: str = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key: str
        value: str
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def fmt_session_ts(seconds: float) -> str:
    """Format seconds since session start as [mm:ss.ms] (e.g. [00:03.42])."""
    if seconds < 0:
        seconds = 0.0
    minutes: int = int(seconds // 60)
    rem: float = seconds - float(minutes) * 60.0
    return f"[{minutes:02d}:{rem:05.2f}]"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered: list[float] = sorted(float(v) for v in values)
    rank: float = (len(ordered) - 1) * (p / 100.0)
    lo: int = int(rank)
    hi: int = min(lo + 1, len(ordered) - 1)
    frac: float = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _sounddevice() -> Any:
    """Import sounddevice and fail with an install hint if PortAudio is missing."""
    try:
        import sounddevice as sd
    except OSError as exc:
        print(
            "asr/stream.py: PortAudio library not found (needed by sounddevice).\n"
            "  Windows: `pip install sounddevice` (wheel includes PortAudio).\n"
            "  Linux: install libportaudio2, then `pip install sounddevice`.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return sd


def list_input_devices() -> list[dict[str, Any]]:
    """Return PortAudio input devices (max_input_channels > 0)."""
    sd: Any = _sounddevice()
    raw: Any = sd.query_devices()
    devices: list[dict[str, Any]] = []
    if not isinstance(raw, (list, tuple)):
        raw = list(raw)
    for idx, dev in enumerate(raw):
        if not isinstance(dev, Mapping):
            continue
        max_in: int = int(dev.get("max_input_channels", 0) or 0)
        if max_in <= 0:
            continue
        devices.append(
            {
                "index": idx,
                "name": str(dev.get("name", "")),
                "max_input_channels": max_in,
                "default_samplerate": dev.get("default_samplerate"),
                "hostapi": dev.get("hostapi"),
            }
        )
    return devices


def default_input_index() -> int | None:
    try:
        sd: Any = _sounddevice()
        pair: Any = sd.default.device
        if isinstance(pair, (list, tuple)) and pair:
            idx = pair[0]
            return int(idx) if idx is not None and int(idx) >= 0 else None
    except Exception:
        return None
    return None


def print_devices(devices: list[dict[str, Any]], default_idx: int | None) -> None:
    print("Available input devices:")
    if not devices:
        print("  (none found)")
        return
    for dev in devices:
        mark: str = "  [default]" if default_idx is not None and dev["index"] == default_idx else ""
        rate: Any = dev.get("default_samplerate")
        rate_s: str = f"{rate:.0f} Hz" if isinstance(rate, (int, float)) else "?"
        print(
            f"  [{dev['index']}] {dev['name']}  "
            f"(in={dev['max_input_channels']}, default_sr={rate_s}){mark}"
        )


def resolve_device(spec: str | None, devices: list[dict[str, Any]]) -> int | None:
    """None → system default. Otherwise an index or a case-insensitive name substring."""
    if spec is None or spec.strip() == "":
        return None
    text: str = spec.strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    lowered: str = text.lower()
    for dev in devices:
        if lowered in str(dev["name"]).lower():
            return int(dev["index"])
    raise SystemExit(f"asr/stream.py: no input device matching {spec!r}")


def message_to_raw_json(message: object) -> str:
    """Serialize a Deepgram SDK message without field filtering."""
    if isinstance(message, (bytes, bytearray)):
        return json.dumps({"_raw_bytes_hex": bytes(message).hex()}, ensure_ascii=False)
    if hasattr(message, "model_dump_json"):
        return str(message.model_dump_json())
    if hasattr(message, "model_dump"):
        return json.dumps(message.model_dump(mode="json"), ensure_ascii=False)
    if isinstance(message, Mapping):
        return json.dumps(dict(message), ensure_ascii=False)
    return json.dumps({"_repr": repr(message)}, ensure_ascii=False)


def _alt0(message: object) -> tuple[str, float]:
    channel: Any = getattr(message, "channel", None)
    if channel is None and isinstance(message, Mapping):
        channel = message.get("channel")
    alts: Any = getattr(channel, "alternatives", None) if channel is not None else None
    if alts is None and isinstance(channel, Mapping):
        alts = channel.get("alternatives")
    if not alts:
        return "", 0.0
    first: Any = alts[0]
    transcript: str = str(getattr(first, "transcript", None) or (first.get("transcript") if isinstance(first, Mapping) else "") or "")
    conf_raw: Any = getattr(first, "confidence", None)
    if conf_raw is None and isinstance(first, Mapping):
        conf_raw = first.get("confidence")
    try:
        confidence: float = float(conf_raw) if conf_raw is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    return transcript, confidence


def handle_message(message: object, session: Session) -> None:
    raw: str = message_to_raw_json(message)
    with session.lock:
        session.jsonl.write(raw + "\n")
        session.jsonl.flush()

    msg_type: str = str(getattr(message, "type", "") or "")
    if msg_type and msg_type != "Results":
        return
    if not hasattr(message, "channel") and not (isinstance(message, Mapping) and "channel" in message):
        return

    transcript: str
    confidence: float
    transcript, confidence = _alt0(message)
    if not transcript.strip():
        return

    is_final_raw: Any = getattr(message, "is_final", None)
    if is_final_raw is None and isinstance(message, Mapping):
        is_final_raw = message.get("is_final")
    is_final: bool = bool(is_final_raw)

    speech_final_raw: Any = getattr(message, "speech_final", None)
    if speech_final_raw is None and isinstance(message, Mapping):
        speech_final_raw = message.get("speech_final")
    speech_final: bool = bool(speech_final_raw)

    try:
        duration: float = float(getattr(message, "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        start: float = float(getattr(message, "start", 0.0) or 0.0)
    except (TypeError, ValueError):
        start = 0.0

    now_rel: float = time.perf_counter() - session.started_perf
    stamp: str = fmt_session_ts(now_rel)
    if is_final:
        kind: str = f"{Fore.GREEN}FINAL{Style.RESET_ALL}  "
        print(f"{stamp} {kind}  {transcript}")
        meta: str = (
            f"{stamp}   ^ speech_final={str(speech_final).lower()}  "
            f"duration={duration:.2f}s  confidence={confidence:.2f}"
        )
        print(f"{Fore.CYAN}{meta}{Style.RESET_ALL}")
        # Audio-clock end of this utterance, mapped onto wall time.
        t_audio_end_rel: float = start + duration
        latency_s: float = max(0.0, now_rel - t_audio_end_rel)
        with session.lock:
            session.finals.append(
                FinalStats(
                    recv_rel_s=now_rel,
                    start_s=start,
                    duration_s=duration,
                    confidence=confidence,
                    speech_final=speech_final,
                    chars=len(transcript),
                    latency_s=latency_s,
                )
            )
    else:
        kind = f"{Fore.YELLOW}INTERIM{Style.RESET_ALL}"
        print(f"{stamp} {kind}  {transcript}")


def print_summary(session: Session) -> None:
    elapsed: float = time.perf_counter() - session.started_perf
    with session.lock:
        finals: list[FinalStats] = list(session.finals)
    n: int = len(finals)
    print()
    print(f"{Style.BRIGHT}--- session summary ---{Style.RESET_ALL}")
    print(f"total duration: {fmt_session_ts(elapsed).strip('[]')}")
    print(f"FINAL segment count: {n}")
    print(f"jsonl: {session.jsonl_path}")
    if n == 0:
        print("gaps / char counts / speech_final / latency: n/a (no FINAL)")
        return

    gaps: list[float] = []
    for i in range(1, n):
        gaps.append(finals[i].recv_rel_s - finals[i - 1].recv_rel_s)
    chars: list[float] = [float(f.chars) for f in finals]
    latencies: list[float] = [f.latency_s for f in finals]
    sf_n: int = sum(1 for f in finals if f.speech_final)

    if gaps:
        print(
            f"gaps between adjacent FINALs (recv clock): "
            f"p50={percentile(gaps, 50.0):.2f}s  p95={percentile(gaps, 95.0):.2f}s"
        )
    else:
        print("gaps between adjacent FINALs (recv clock): n/a (only one FINAL)")
    print(
        f"FINAL char counts: p50={percentile(chars, 50.0):.1f}  "
        f"p95={percentile(chars, 95.0):.1f}  max={max(chars):.0f}"
    )
    print(f"fraction speech_final=true: {sf_n}/{n} ({(sf_n / n) * 100.0:.1f}%)")
    print(
        f"latency audio-end → FINAL recv: "
        f"p50={percentile(latencies, 50.0) * 1000.0:.0f}ms  "
        f"p95={percentile(latencies, 95.0) * 1000.0:.0f}ms"
    )
    print(
        "  (latency = FINAL wall-recv − (session_start + Deepgram.start + duration); "
        "includes default endpointing, not tuned)"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream the system microphone to Deepgram (observation only)."
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Input device index or name substring (default: system default)",
    )
    parser.add_argument(
        "--lang",
        default="zh",
        help="Deepgram language code (default: zh; use ja later)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print input devices and exit (no API key required)",
    )
    return parser.parse_args(argv)


def require_api_key() -> str:
    load_dotenv()
    key: str = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if not key:
        print(
            "asr/stream.py: DEEPGRAM_API_KEY is not set. "
            "Export it or put it in .env.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return key


def run_stream(*, device: int | None, language: str) -> int:
    api_key: str = require_api_key()
    stamp: str = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path: Path = Path(f"transcript_{stamp}.jsonl")
    jsonl_fp: TextIO = jsonl_path.open("a", encoding="utf-8")

    audio_q: queue.Queue[bytes | None] = queue.Queue()
    stop: threading.Event = threading.Event()
    started_perf: float = time.perf_counter()
    session: Session = Session(
        started_perf=started_perf,
        jsonl=jsonl_fp,
        jsonl_path=jsonl_path,
    )

    client: DeepgramClient = DeepgramClient(api_key=api_key)
    print(
        f"Deepgram model={DEEPGRAM_MODEL}  language={language}  "
        f"audio={SAMPLE_RATE}Hz mono s16le  jsonl={jsonl_path}"
    )
    print("Ctrl-C to stop.\n")

    try:
        with client.listen.v1.connect(
            model=DEEPGRAM_MODEL,
            language=language,
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            interim_results=True,
            punctuate=True,
        ) as connection:
            connection.on(EventType.MESSAGE, lambda msg: handle_message(msg, session))
            connection.on(
                EventType.ERROR,
                lambda err: print(f"{Fore.RED}deepgram error: {err}{Style.RESET_ALL}", file=sys.stderr),
            )

            listen_thread: threading.Thread = threading.Thread(
                target=connection.start_listening,
                name="deepgram-listen",
                daemon=True,
            )
            listen_thread.start()

            def sender() -> None:
                while True:
                    chunk: bytes | None = audio_q.get()
                    if chunk is None:
                        break
                    if stop.is_set():
                        continue
                    try:
                        connection.send_media(chunk)
                    except Exception as exc:
                        print(f"{Fore.RED}send_media failed: {exc}{Style.RESET_ALL}", file=sys.stderr)
                        stop.set()
                        break

            send_thread: threading.Thread = threading.Thread(
                target=sender, name="deepgram-send", daemon=True
            )
            send_thread.start()

            session.started_perf = time.perf_counter()

            def on_audio(
                indata: Any,
                frames: int,
                time_info: Any,
                status: Any,
            ) -> None:
                if status:
                    print(f"{Fore.YELLOW}input status: {status}{Style.RESET_ALL}", file=sys.stderr)
                if stop.is_set():
                    return
                audio_q.put(bytes(indata))

            try:
                sd: Any = _sounddevice()
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=BLOCK_FRAMES,
                    device=device,
                    callback=on_audio,
                ):
                    while not stop.is_set():
                        time.sleep(0.2)
            except KeyboardInterrupt:
                print("\nStopping…")
            finally:
                stop.set()
                audio_q.put(None)
                try:
                    connection.send_finalize()
                except Exception:
                    pass
                send_thread.join(timeout=2.0)
    finally:
        print_summary(session)
        jsonl_fp.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    colorama_init()
    args: argparse.Namespace = parse_args(argv)
    try:
        devices: list[dict[str, Any]] = list_input_devices()
    except Exception as exc:
        print(f"asr/stream.py: could not query devices: {exc}", file=sys.stderr)
        devices = []
    default_idx: int | None = default_input_index()
    print_devices(devices, default_idx)
    if args.list_devices:
        return 0
    device: int | None = resolve_device(args.device, devices)
    if device is not None:
        print(f"Using input device index {device}")
    else:
        print("Using system default input device")
    return run_stream(device=device, language=str(args.lang))


if __name__ == "__main__":
    raise SystemExit(main())
