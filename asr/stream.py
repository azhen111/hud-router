#!/usr/bin/env python3
"""Mic → Deepgram streaming ASR → colorized terminal + raw jsonl.

Observation tool only. Does not import or call Phase 1 routing code.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping
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
# Deepgram listen handshake wait. Live path uses this; M1/M2 connect is the
# same SDK enter() and would hang forever if the WS never opens.
DEEPGRAM_HANDSHAKE_TIMEOUT_S: float = 60.0

# Latency (audio-in → FINAL return), measured as:
#   latency = t_final_recv - t_audio_end
# t_final_recv  = wall time the FINAL message arrived (perf_counter).
# t_audio_end   = session_start + Deepgram.start + Deepgram.duration
# Deepgram.start/duration are seconds on the audio clock beginning at the
# first byte we sent. Because we stream the mic in real time, that clock
# lines up with wall time from session_start. The difference is Deepgram's
# default endpointing wait + network + decode. We do not set endpointing.


@dataclass(frozen=True)
class ParsedAsrResult:
    """One Deepgram Results payload with a non-empty transcript."""

    transcript: str
    confidence: float
    is_final: bool
    speech_final: bool
    start_s: float
    duration_s: float


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


def parse_deepgram_result(message: object) -> ParsedAsrResult | None:
    """Parse a Results message. None if not a transcript-bearing Results event."""
    msg_type: str = str(getattr(message, "type", "") or "")
    if msg_type and msg_type != "Results":
        return None
    if not hasattr(message, "channel") and not (
        isinstance(message, Mapping) and "channel" in message
    ):
        return None
    transcript: str
    confidence: float
    transcript, confidence = _alt0(message)
    if not transcript.strip():
        return None
    is_final_raw: Any = getattr(message, "is_final", None)
    if is_final_raw is None and isinstance(message, Mapping):
        is_final_raw = message.get("is_final")
    speech_final_raw: Any = getattr(message, "speech_final", None)
    if speech_final_raw is None and isinstance(message, Mapping):
        speech_final_raw = message.get("speech_final")
    try:
        duration: float = float(getattr(message, "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if isinstance(message, Mapping) and duration == 0.0:
        try:
            duration = float(message.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
    try:
        start: float = float(getattr(message, "start", 0.0) or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    if isinstance(message, Mapping) and start == 0.0:
        try:
            start = float(message.get("start") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
    return ParsedAsrResult(
        transcript=transcript,
        confidence=confidence,
        is_final=bool(is_final_raw),
        speech_final=bool(speech_final_raw),
        start_s=start,
        duration_s=duration,
    )


def handle_message(message: object, session: Session) -> None:
    raw: str = message_to_raw_json(message)
    with session.lock:
        session.jsonl.write(raw + "\n")
        session.jsonl.flush()

    parsed: ParsedAsrResult | None = parse_deepgram_result(message)
    if parsed is None:
        return

    now_rel: float = time.perf_counter() - session.started_perf
    stamp: str = fmt_session_ts(now_rel)
    if parsed.is_final:
        kind: str = f"{Fore.GREEN}FINAL{Style.RESET_ALL}  "
        print(f"{stamp} {kind}  {parsed.transcript}")
        meta: str = (
            f"{stamp}   ^ speech_final={str(parsed.speech_final).lower()}  "
            f"duration={parsed.duration_s:.2f}s  confidence={parsed.confidence:.2f}"
        )
        print(f"{Fore.CYAN}{meta}{Style.RESET_ALL}")
        # Audio-clock end of this utterance, mapped onto wall time.
        t_audio_end_rel: float = parsed.start_s + parsed.duration_s
        latency_s: float = max(0.0, now_rel - t_audio_end_rel)
        with session.lock:
            session.finals.append(
                FinalStats(
                    recv_rel_s=now_rel,
                    start_s=parsed.start_s,
                    duration_s=parsed.duration_s,
                    confidence=parsed.confidence,
                    speech_final=parsed.speech_final,
                    chars=len(parsed.transcript),
                    latency_s=latency_s,
                )
            )
    else:
        kind = f"{Fore.YELLOW}INTERIM{Style.RESET_ALL}"
        print(f"{stamp} {kind}  {parsed.transcript}")


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


def load_keyterms(path: Path) -> list[str]:
    """Load plain keyterm strings. Nova-3 must not receive `keywords` or weights.

    https://developers.deepgram.com/docs/keyterm
    Keywords page: Nova-3 must use Keyterm Prompting (not `keywords`).
    """
    raw_obj: object = json.loads(path.read_text(encoding="utf-8"))
    items: list[object]
    if isinstance(raw_obj, list):
        items = raw_obj
    elif isinstance(raw_obj, dict):
        inner = raw_obj.get("keyterm") or raw_obj.get("keyterms") or raw_obj.get("terms")
        items = list(inner) if isinstance(inner, list) else []
    else:
        items = []
    terms: list[str] = []
    seen: set[str] = set()
    for item in items:
        pieces: list[str]
        if isinstance(item, dict):
            canon = str(item.get("term") or "").strip()
            variants = [
                str(v).strip()
                for v in (item.get("variants") or [])
                if str(v).strip()
            ]
            pieces = ([canon] if canon else []) + variants
        else:
            pieces = [str(item).strip()]
        for term in pieces:
            if not term or term.startswith("#"):
                continue
            # Drop legacy keywords intensifiers (term:1.5). keyterm is plain only.
            if ":" in term:
                maybe_w = term.rsplit(":", 1)[-1].lstrip("+-")
                if maybe_w.replace(".", "", 1).isdigit():
                    continue
            if term in seen:
                continue
            seen.add(term)
            terms.append(term)
    return terms


def listen_connect_kwargs(
    language: str,
    keyterms: list[str] | None = None,
) -> dict[str, Any]:
    """Shared nova-3 / 16 kHz mono s16le options for M1 and live PCM.

    Nova-3 does **not** support `keywords` (HTTP 400 / silent WS close).
    Pass repeated `keyterm` (plain terms, no intensifier). Never set `keywords`.
    https://developers.deepgram.com/docs/keyterm
    https://developers.deepgram.com/docs/keywords
    """
    kwargs: dict[str, Any] = {
        "model": DEEPGRAM_MODEL,
        "language": language,
        "encoding": "linear16",
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "interim_results": True,
        "punctuate": True,
    }
    if keyterms:
        kwargs["keyterm"] = list(keyterms)
    return kwargs


class DeepgramPcmSession:
    """Deepgram listen.v1 fed by PCM bytes (no microphone).

    Used by `server/live.py`. M1/M2 CLIs keep `run_mic_deepgram_session`.
    Handshake waits at most `DEEPGRAM_HANDSHAKE_TIMEOUT_S` (60s).
    """

    def __init__(
        self,
        *,
        api_key: str,
        language: str,
        on_message: Callable[[object], None],
        on_error: Callable[[object], None] | None = None,
        handshake_timeout_s: float = DEEPGRAM_HANDSHAKE_TIMEOUT_S,
        keyterms: list[str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._language = language
        self._on_message = on_message
        self._on_error = on_error
        self._handshake_timeout_s = handshake_timeout_s
        self._keyterms = list(keyterms) if keyterms else []
        self._halt = threading.Event()
        self._audio_q: queue.Queue[bytes | None] = queue.Queue()
        self._cm: Any = None
        self._connection: Any = None
        self._listen_thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._keep_thread: threading.Thread | None = None

    def start(self) -> None:
        client: DeepgramClient = DeepgramClient(api_key=self._api_key)
        self._cm = client.listen.v1.connect(
            **listen_connect_kwargs(self._language, self._keyterms or None)
        )
        box: list[tuple[str, Any]] = []

        def _enter() -> None:
            try:
                box.append(("ok", self._cm.__enter__()))
            except Exception as exc:
                box.append(("err", exc))

        waiter = threading.Thread(target=_enter, name="deepgram-handshake", daemon=True)
        waiter.start()
        waiter.join(self._handshake_timeout_s)
        if waiter.is_alive():
            raise TimeoutError(
                f"Deepgram handshake timed out after {self._handshake_timeout_s:.0f}s"
            )
        if not box:
            raise TimeoutError("Deepgram handshake returned no result")
        kind, val = box[0]
        if kind == "err":
            raise val
        self._connection = val

        def _on_error(err: object) -> None:
            if self._on_error is not None:
                self._on_error(err)
            else:
                print(f"{Fore.RED}deepgram error: {err}{Style.RESET_ALL}", file=sys.stderr)

        self._connection.on(EventType.MESSAGE, self._on_message)
        self._connection.on(EventType.ERROR, _on_error)
        self._listen_thread = threading.Thread(
            target=self._connection.start_listening,
            name="deepgram-listen",
            daemon=True,
        )
        self._listen_thread.start()

        def sender() -> None:
            while True:
                chunk: bytes | None = self._audio_q.get()
                if chunk is None:
                    break
                if self._halt.is_set():
                    continue
                try:
                    self._connection.send_media(chunk)
                except Exception as exc:
                    _on_error(exc)
                    self._halt.set()
                    break

        self._send_thread = threading.Thread(target=sender, name="deepgram-send", daemon=True)
        self._send_thread.start()

        def keeper() -> None:
            while not self._halt.wait(8.0):
                conn = self._connection
                if conn is None:
                    return
                try:
                    if hasattr(conn, "send_keep_alive"):
                        conn.send_keep_alive()
                except Exception:
                    return

        self._keep_thread = threading.Thread(target=keeper, name="deepgram-keepalive", daemon=True)
        self._keep_thread.start()

    def send_pcm(self, chunk: bytes) -> None:
        if self._halt.is_set() or not chunk:
            return
        self._audio_q.put(chunk)

    def close(self) -> None:
        self._halt.set()
        try:
            self._audio_q.put(None)
        except Exception:
            pass
        conn = self._connection
        if conn is not None:
            try:
                conn.send_finalize()
            except Exception:
                pass
        if self._send_thread is not None:
            self._send_thread.join(timeout=2.0)
        cm = self._cm
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        self._connection = None
        self._cm = None


def run_mic_deepgram_session(
    *,
    api_key: str,
    language: str,
    device: int | None,
    on_message: Callable[[object], None],
    stop: threading.Event | None = None,
    started_perf_out: list[float] | None = None,
    on_error: Callable[[object], None] | None = None,
) -> None:
    """Block: system mic → Deepgram listen.v1 until stop or Ctrl-C.

    Importable helper for M2 pipeline. M1 CLI uses this unchanged.
    Endpointing is not set (Deepgram defaults).
    """
    halt: threading.Event = stop if stop is not None else threading.Event()
    audio_q: queue.Queue[bytes | None] = queue.Queue()
    client: DeepgramClient = DeepgramClient(api_key=api_key)

    def _on_error(err: object) -> None:
        if on_error is not None:
            on_error(err)
        else:
            print(f"{Fore.RED}deepgram error: {err}{Style.RESET_ALL}", file=sys.stderr)

    with client.listen.v1.connect(**listen_connect_kwargs(language)) as connection:
        connection.on(EventType.MESSAGE, on_message)
        connection.on(EventType.ERROR, _on_error)

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
                if halt.is_set():
                    continue
                try:
                    connection.send_media(chunk)
                except Exception as exc:
                    print(f"{Fore.RED}send_media failed: {exc}{Style.RESET_ALL}", file=sys.stderr)
                    halt.set()
                    break

        send_thread: threading.Thread = threading.Thread(
            target=sender, name="deepgram-send", daemon=True
        )
        send_thread.start()

        t0: float = time.perf_counter()
        if started_perf_out is not None:
            if started_perf_out:
                started_perf_out[0] = t0
            else:
                started_perf_out.append(t0)

        def on_audio(
            indata: Any,
            frames: int,
            time_info: Any,
            status: Any,
        ) -> None:
            if status:
                print(f"{Fore.YELLOW}input status: {status}{Style.RESET_ALL}", file=sys.stderr)
            if halt.is_set():
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
                while not halt.is_set():
                    time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nStopping…")
        finally:
            halt.set()
            audio_q.put(None)
            try:
                connection.send_finalize()
            except Exception:
                pass
            send_thread.join(timeout=2.0)


def run_stream(*, device: int | None, language: str) -> int:
    api_key: str = require_api_key()
    stamp: str = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path: Path = Path(f"transcript_{stamp}.jsonl")
    jsonl_fp: TextIO = jsonl_path.open("a", encoding="utf-8")

    started_perf: float = time.perf_counter()
    session: Session = Session(
        started_perf=started_perf,
        jsonl=jsonl_fp,
        jsonl_path=jsonl_path,
    )
    start_box: list[float] = [started_perf]

    print(
        f"Deepgram model={DEEPGRAM_MODEL}  language={language}  "
        f"audio={SAMPLE_RATE}Hz mono s16le  jsonl={jsonl_path}"
    )
    print("Ctrl-C to stop.\n")

    def on_message(message: object) -> None:
        session.started_perf = start_box[0]
        handle_message(message, session)

    try:
        run_mic_deepgram_session(
            api_key=api_key,
            language=language,
            device=device,
            on_message=on_message,
            started_perf_out=start_box,
        )
    finally:
        session.started_perf = start_box[0]
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
