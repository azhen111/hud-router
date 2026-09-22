#!/usr/bin/env python3
"""Phase 3 / M1 live path: G2 PCM → Deepgram → router → lens.

No display_policy, no aggregator, no RAG. Each speech_final segment is one
router turn. Mis-triggers are expected this milestone.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
ASR_DIR = ROOT / "asr"
for _p in (str(ROOT), str(ASR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from colorama import Fore, Style, init as colorama_init

from router import RouterConfigError, RouterResult, route
from stream import (
    DEEPGRAM_HANDSHAKE_TIMEOUT_S,
    DEEPGRAM_MODEL,
    SAMPLE_RATE,
    DeepgramPcmSession,
    fmt_session_ts,
    load_dotenv,
    parse_deepgram_result,
    require_api_key,
    ParsedAsrResult,
)

try:
    from websockets.asyncio.server import serve
except ImportError:  # websockets < 13
    from websockets.server import serve  # type: ignore[no-redef]


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8766
DEFAULT_LANG = "zh"
DEFAULT_WEARER_NOTE = "佩戴者是软件工程师，当前对话为 IT 技术讨论"
DEFAULT_ROUTER_TIMEOUT_MS = 1800


def locale_from_lang(lang: str) -> str:
    raw = (lang or "").strip().lower()
    if raw.startswith("zh"):
        return "zh"
    if raw.startswith("ja"):
        return "ja"
    if raw.startswith("en"):
        return "en"
    return raw or "zh"


def map_speaker_role(raw: object) -> str:
    """Self→SELF, Other→OTHER, else UNKNOWN. Logs keep the uplink string."""
    text = str(raw or "").strip().lower()
    if text == "self":
        return "SELF"
    if text == "other":
        return "OTHER"
    return "UNKNOWN"


def display_speaker(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return "Unknown"
    lowered = text.lower()
    if lowered == "self":
        return "Self"
    if lowered == "other":
        return "Other"
    if lowered == "unknown":
        return "Unknown"
    return text


def parse_uplink_message(raw: object) -> tuple[bytes, str, int | None] | None:
    """Parse one glasses uplink frame. Returns (pcm, speakerRole, direction)."""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw), "unknown", None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("type") or "pcm").strip().lower()
    if kind not in {"pcm", "audio"}:
        return None
    pcm_b64 = obj.get("pcm_b64") or obj.get("pcm") or ""
    if not isinstance(pcm_b64, str) or not pcm_b64.strip():
        return None
    try:
        pcm = base64.b64decode(pcm_b64, validate=False)
    except Exception:
        return None
    if not pcm:
        return None
    role = str(obj.get("speakerRole") or obj.get("speaker_role") or "unknown")
    direction: int | None
    dir_raw = obj.get("direction")
    if dir_raw is None or dir_raw == "":
        direction = None
    else:
        try:
            direction = int(dir_raw)
        except (TypeError, ValueError):
            direction = None
    return pcm, role, direction


def route_with_timeout(
    payload: dict[str, object],
    timeout_s: float,
) -> tuple[RouterResult | None, bool]:
    """Call route() once. On timeout return (None, True). No retry."""
    box: list[RouterResult | BaseException] = []

    def work() -> None:
        try:
            box.append(route(payload))
        except RouterConfigError as exc:
            box.append(exc)
        except Exception as exc:
            box.append(exc)

    worker = threading.Thread(target=work, name="live-route", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, True
    if not box:
        return None, True
    item = box[0]
    if isinstance(item, RouterConfigError):
        raise item
    if isinstance(item, RouterResult):
        return item, False
    return None, False


def client_addr(ws: Any) -> str:
    try:
        remote = ws.remote_address
        if remote is None:
            return "?"
        if isinstance(remote, tuple) and len(remote) >= 2:
            return f"{remote[0]}:{remote[1]}"
        return str(remote)
    except Exception:
        return "?"


class LiveSettings:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        lang: str,
        wearer_note: str,
        router_timeout_ms: int,
        handshake_timeout_s: float,
        log_path: Path,
    ) -> None:
        self.host = host
        self.port = port
        self.lang = lang
        self.locale = locale_from_lang(lang)
        self.wearer_note = wearer_note
        self.router_timeout_ms = router_timeout_ms
        self.handshake_timeout_s = handshake_timeout_s
        self.log_path = log_path


class LiveSession:
    def __init__(
        self,
        ws: Any,
        settings: LiveSettings,
        jsonl: TextIO,
        jsonl_lock: threading.Lock,
        started_perf: float,
        dg_key: str,
    ) -> None:
        self.ws = ws
        self.settings = settings
        self.jsonl = jsonl
        self.jsonl_lock = jsonl_lock
        self.started_perf = started_perf
        self.dg_key = dg_key
        self.loop = asyncio.get_running_loop()
        self.dg: DeepgramPcmSession | None = None
        self.last_role = "unknown"
        self.last_direction: int | None = None
        self.route_lock = threading.Lock()
        self.closed = False

    def now_rel(self) -> float:
        return time.perf_counter() - self.started_perf

    def start_deepgram(self) -> None:
        session = DeepgramPcmSession(
            api_key=self.dg_key,
            language=self.settings.lang,
            on_message=self._on_dg_message,
            on_error=self._on_dg_error,
            handshake_timeout_s=self.settings.handshake_timeout_s,
        )
        session.start()
        self.dg = session

    def _on_dg_error(self, err: object) -> None:
        print(f"{Fore.RED}deepgram error: {err}{Style.RESET_ALL}", file=sys.stderr, flush=True)
        asyncio.run_coroutine_threadsafe(
            self._send_json({"error": f"deepgram: {err}"}),
            self.loop,
        )

    def _on_dg_message(self, message: object) -> None:
        parsed = parse_deepgram_result(message)
        if parsed is None:
            return
        if not parsed.speech_final:
            return
        asyncio.run_coroutine_threadsafe(self._handle_speech_final(parsed), self.loop)

    async def feed_uplink(self, raw: object) -> None:
        parsed = parse_uplink_message(raw)
        if parsed is None:
            return
        pcm, role, direction = parsed
        self.last_role = role
        self.last_direction = direction
        if self.dg is not None:
            self.dg.send_pcm(pcm)

    async def _handle_speech_final(self, parsed: ParsedAsrResult) -> None:
        t0 = time.perf_counter()
        rel = self.now_rel()
        role = self.last_role
        direction = self.last_direction
        speaker = map_speaker_role(role)
        stamp = fmt_session_ts(rel)
        print(
            f"{stamp} {display_speaker(role)}  {parsed.transcript}",
            flush=True,
        )
        payload: dict[str, object] = {
            "recent_turns": [
                {
                    "speaker": speaker,
                    "text": parsed.transcript,
                    "ts": time.time(),
                }
            ],
            "locale": self.settings.locale,
            "wearer_note": self.settings.wearer_note,
        }
        timeout_s = self.settings.router_timeout_ms / 1000.0
        t_route = time.perf_counter()
        try:
            result, timed_out = await self.loop.run_in_executor(
                None,
                lambda: self._route_once(payload, timeout_s),
            )
        except RouterConfigError as exc:
            print(f"{stamp}   → skip  router config: {exc}", flush=True)
            self._write_jsonl(
                parsed,
                role,
                direction,
                speaker,
                None,
                False,
                False,
                t0,
                t_route,
                None,
                error=str(exc),
            )
            await self._send_json({"error": str(exc)})
            return
        router_ms = (time.perf_counter() - t_route) * 1000.0
        pushed = False
        t_down: float | None = None
        if timed_out:
            print(
                f"{fmt_session_ts(self.now_rel())}   → skip  reason: router timeout "
                f"({self.settings.router_timeout_ms}ms)",
                flush=True,
            )
        elif result is None:
            print(
                f"{fmt_session_ts(self.now_rel())}   → skip  reason: router error",
                flush=True,
            )
        elif result.should_respond and result.answer:
            print(
                f"{fmt_session_ts(self.now_rel())}   → {Fore.GREEN}TRIGGER{Style.RESET_ALL}  "
                f"conf={result.confidence:.2f}  lat={router_ms:.0f}ms",
                flush=True,
            )
            print(
                f"{fmt_session_ts(self.now_rel())}     {result.answer}",
                flush=True,
            )
            t_down = time.perf_counter()
            await self._send_json({"text": result.answer})
            t_down = (time.perf_counter() - t_down) * 1000.0
            pushed = True
        else:
            reason = result.reason if result is not None else "none"
            conf = result.confidence if result is not None else 0.0
            print(
                f"{fmt_session_ts(self.now_rel())}   → skip  "
                f"conf={conf:.2f}  reason: {reason}",
                flush=True,
            )
        self._write_jsonl(
            parsed,
            role,
            direction,
            speaker,
            result,
            timed_out,
            pushed,
            t0,
            t_route,
            t_down,
        )

    def _route_once(
        self, payload: dict[str, object], timeout_s: float
    ) -> tuple[RouterResult | None, bool]:
        with self.route_lock:
            return route_with_timeout(payload, timeout_s)

    def _write_jsonl(
        self,
        parsed: ParsedAsrResult,
        role: str,
        direction: int | None,
        speaker: str,
        result: RouterResult | None,
        timed_out: bool,
        pushed: bool,
        t0: float,
        t_route: float,
        downlink_ms: float | None,
        error: str | None = None,
    ) -> None:
        record: dict[str, object] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "session_rel_s": round(self.now_rel(), 3),
            "transcript": parsed.transcript,
            "speech_final": parsed.speech_final,
            "is_final": parsed.is_final,
            "asr_confidence": parsed.confidence,
            "speakerRole": role,
            "direction": direction,
            "speaker": speaker,
            "router": result.to_dict() if result is not None else None,
            "router_timeout": timed_out,
            "pushed": pushed,
            "timings": {
                "router_ms": round((time.perf_counter() - t_route) * 1000.0, 1),
                "downlink_ms": None if downlink_ms is None else round(downlink_ms, 1),
                "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            },
        }
        if error:
            record["error"] = error
        with self.jsonl_lock:
            self.jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.jsonl.flush()

    async def _send_json(self, obj: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception as exc:
            print(f"[live] send failed: {exc}", file=sys.stderr, flush=True)

    def close(self) -> None:
        self.closed = True
        if self.dg is not None:
            try:
                self.dg.close()
            except Exception:
                pass
            self.dg = None


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=None, help=f"bind host (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=None, help=f"WS port (default {DEFAULT_PORT})")
    p.add_argument("--lang", default=None, help=f"Deepgram language (default {DEFAULT_LANG})")
    p.add_argument(
        "--wearer-note",
        default=None,
        help="router wearer_note (default: IT-engineer Chinese note)",
    )
    p.add_argument(
        "--router-timeout-ms",
        type=int,
        default=None,
        help=f"abandon route() after this many ms (default {DEFAULT_ROUTER_TIMEOUT_MS})",
    )
    p.add_argument(
        "--handshake-timeout-s",
        type=float,
        default=None,
        help=f"Deepgram handshake timeout (default {DEEPGRAM_HANDSHAKE_TIMEOUT_S:.0f}s)",
    )
    p.add_argument("--log", default=None, help="jsonl path (default live_YYYYmmdd_HHMMSS.jsonl)")
    return p.parse_args(argv)


def build_settings(args: argparse.Namespace) -> LiveSettings:
    load_dotenv()
    host = args.host if args.host is not None else _env("LIVE_HOST", DEFAULT_HOST)
    port = args.port if args.port is not None else int(_env("LIVE_PORT", str(DEFAULT_PORT)))
    lang = args.lang if args.lang is not None else _env("LIVE_LANG", DEFAULT_LANG)
    note = (
        args.wearer_note
        if args.wearer_note is not None
        else _env("LIVE_WEARER_NOTE", DEFAULT_WEARER_NOTE)
    )
    timeout_ms = (
        args.router_timeout_ms
        if args.router_timeout_ms is not None
        else int(_env("LIVE_ROUTER_TIMEOUT_MS", str(DEFAULT_ROUTER_TIMEOUT_MS)))
    )
    handshake = (
        args.handshake_timeout_s
        if args.handshake_timeout_s is not None
        else float(_env("DEEPGRAM_HANDSHAKE_TIMEOUT_S", str(DEEPGRAM_HANDSHAKE_TIMEOUT_S)))
    )
    if args.log:
        log_path = Path(args.log)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = Path(f"live_{stamp}.jsonl")
    return LiveSettings(
        host=host,
        port=port,
        lang=lang,
        wearer_note=note,
        router_timeout_ms=timeout_ms,
        handshake_timeout_s=handshake,
        log_path=log_path,
    )


async def run_server(settings: LiveSettings, dg_key: str) -> None:
    jsonl = settings.log_path.open("a", encoding="utf-8")
    jsonl_lock = threading.Lock()
    started = time.perf_counter()

    async def handler(websocket: Any, path: Any = None) -> None:
        addr = client_addr(websocket)
        print(f"[conn] + {addr}", flush=True)
        session = LiveSession(websocket, settings, jsonl, jsonl_lock, started, dg_key)
        try:
            try:
                await asyncio.to_thread(session.start_deepgram)
            except Exception as exc:
                print(f"{Fore.RED}Deepgram handshake failed: {exc}{Style.RESET_ALL}", flush=True)
                await websocket.send(
                    json.dumps({"error": f"Deepgram handshake failed: {exc}"}, ensure_ascii=False)
                )
                return
            await websocket.send(json.dumps({"status": "deepgram_ready"}, ensure_ascii=False))
            print(f"[live] Deepgram ready for {addr}", flush=True)
            async for message in websocket:
                await session.feed_uplink(message)
        finally:
            session.close()
            print(f"[conn] - {addr}", flush=True)

    print(
        f"live  ws://{settings.host}:{settings.port}  "
        f"dg={DEEPGRAM_MODEL} lang={settings.lang} locale={settings.locale}  "
        f"audio={SAMPLE_RATE}Hz mono s16le  "
        f"router_timeout={settings.router_timeout_ms}ms  "
        f"dg_handshake={settings.handshake_timeout_s:.0f}s",
        flush=True,
    )
    print(f"wearer_note={settings.wearer_note!r}", flush=True)
    print(f"jsonl={settings.log_path}", flush=True)
    print("Connect glasses/app, then speak. Ctrl-C to stop.\n", flush=True)
    try:
        async with serve(handler, settings.host, settings.port):
            await asyncio.Future()
    finally:
        jsonl.close()


def main(argv: list[str] | None = None) -> int:
    colorama_init()
    args = parse_args(argv)
    settings = build_settings(args)
    try:
        dg_key = require_api_key()
    except SystemExit:
        print(
            "server/live.py: DEEPGRAM_API_KEY is required (export or .env).",
            file=sys.stderr,
        )
        return 2
    try:
        from router import get_settings as router_get_settings

        router_get_settings()
    except RouterConfigError as exc:
        print(f"server/live.py: {exc}", file=sys.stderr)
        print(
            "Need OPENAI_API_KEY, ROUTER_MODEL, optional OPENAI_BASE_URL.",
            file=sys.stderr,
        )
        return 2
    try:
        asyncio.run(run_server(settings, dg_key))
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
