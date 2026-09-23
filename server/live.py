#!/usr/bin/env python3
"""Phase 3 live path: G2 PCM → Deepgram → aggregator → fix → judge → answer → policy → lens.

Silence-closed turns go to the judge (independent ticker; speech_final does
not close). Default path is two-tier: judge (ROUTER_SYSTEM_PROMPT) then
answer (ANSWER_SYSTEM_PROMPT) only on should_respond. Judge ``answer`` is
ignored for display. Deterministic transcript_fix runs before the judge.
Nova-3 uses `keyterm` (not `keywords`). No RAG.
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
from collections.abc import Sequence
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
ASR_DIR = ROOT / "asr"
SERVER_DIR = Path(__file__).resolve().parent
for _p in (str(ROOT), str(ASR_DIR), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from colorama import Fore, Style, init as colorama_init

from aggregator import AggregatedTurn, AggregatorConfig, FinalSegment, TurnAggregator
from display_policy import (
    DEFAULT_CLEAR_PLACEHOLDER,
    Candidate,
    DisplayPolicy,
    PolicySettings,
    add_policy_args,
    load_policy_settings,
)
from router import (
    RouterConfigError,
    RouterResult,
    answer as answer_turn,
    get_answer_model,
    is_skip_answer,
    route,
)
from transcript_fix import (
    DEFAULT_FIX_SIMILARITY,
    TermEntry,
    apply_fix,
    load_term_entries,
)
from settings import DEFAULT_AGG_MAX_TURN_MS, DEFAULT_AGG_SILENCE_MS
from stream import (
    DEEPGRAM_HANDSHAKE_TIMEOUT_S,
    DEEPGRAM_MODEL,
    SAMPLE_RATE,
    DeepgramPcmSession,
    fmt_session_ts,
    listen_connect_kwargs,
    load_dotenv,
    load_keyterms,
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
DEFAULT_ROUTER_TIMEOUT_MS = 3000
DEFAULT_MIN_ROUTE_CHARS = 3
DEFAULT_TERMS_PATH = Path(__file__).resolve().parent / "terms_zh.json"
DEFAULT_ANSWER_TIMEOUT_MS = 4000
POLICY_ACCEPT_ACTIONS = frozenset({"push", "hint", "preempt"})


def locale_from_lang(lang: str) -> str:
    raw = (lang or "").strip().lower()
    if raw in {"multi", "multilingual"}:
        # Deepgram language=multi; router locale stays zh for this IT-meeting path.
        return "zh"
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


def hits_keyterm(text: str, keyterms: Sequence[str]) -> bool:
    """Case-insensitive substring match against the loaded keyterm list."""
    hay = text.strip().casefold()
    if not hay:
        return False
    for term in keyterms:
        needle = str(term).strip()
        if needle and needle.casefold() in hay:
            return True
    return False


def too_short_for_router(
    text: str,
    min_chars: int,
    keyterms: Sequence[str] | None = None,
) -> bool:
    """CJK and ASCII both count as len(stripped). Do not call route() if True.

    A keyterm / terms hit exempts the short filter even when len < min_chars
    (e.g. ``RAG``, ``REST``, ``Pod``, ``限流``).
    """
    stripped = text.strip()
    if keyterms and hits_keyterm(stripped, keyterms):
        return False
    return len(stripped) < min_chars


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


def resolve_router_timeout_ms(args: argparse.Namespace) -> tuple[int, str]:
    """Return (ms, source). CLI > env LIVE_ROUTER_TIMEOUT_MS > DEFAULT 3000."""
    if getattr(args, "router_timeout_ms", None) is not None:
        ms = int(args.router_timeout_ms)
        return ms, f"cli --router-timeout-ms={ms}"
    env_raw = os.environ.get("LIVE_ROUTER_TIMEOUT_MS")
    if env_raw is not None and str(env_raw).strip() != "":
        ms = int(env_raw)
        return ms, f"env LIVE_ROUTER_TIMEOUT_MS={ms}"
    return (
        DEFAULT_ROUTER_TIMEOUT_MS,
        f"default DEFAULT_ROUTER_TIMEOUT_MS={DEFAULT_ROUTER_TIMEOUT_MS}",
    )


def format_router_timeout_skip(timeout_ms: int, source: str) -> str:
    """Unambiguous timeout skip line (must include the waited-ms number)."""
    return (
        f"router_timeout  waited={timeout_ms}ms  source={source}  "
        f"(code DEFAULT_ROUTER_TIMEOUT_MS={DEFAULT_ROUTER_TIMEOUT_MS})"
    )


def policy_enabled_from_args(args: argparse.Namespace) -> bool:
    if getattr(args, "no_policy", False):
        return False
    raw = _env("LIVE_NO_POLICY", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return False
    return True


def pick_display_answer(
    *,
    two_tier: bool,
    judge_answer: str,
    answer_text: str | None,
    answer_timed_out: bool,
) -> tuple[str | None, str | None]:
    """Choose lens text. Two-tier ignores judge.answer. SKIP / timeout drop."""
    if two_tier:
        if answer_timed_out:
            return None, "answer_timeout"
        if answer_text is None:
            return None, "answer_error"
        if is_skip_answer(answer_text):
            return None, "answer_skip"
        return answer_text.strip(), None
    text = (judge_answer or "").strip()
    if not text:
        return None, "none"
    return text, None


def _fix_record(
    original: str,
    fixed: str,
    hits: Sequence[Any],
    enabled: bool,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "original": original,
        "fixed": fixed,
        "corrections": [h.to_dict() if hasattr(h, "to_dict") else h for h in hits],
    }


def apply_trigger_policy(
    policy: DisplayPolicy | None,
    answer: str,
    confidence: float,
    kind: str = "answer",
    ts_ms: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Run display_policy on a router trigger. policy=None means --no-policy.

    Returns (allowed, display_text, audit).
    """
    if policy is None:
        audit = policy_audit(
            enabled=False,
            allowed=True,
            reason="bypassed",
            action="push",
            mode="full",
            confidence=confidence,
            display_text=answer,
            budget_used=None,
            budget_max=None,
            ttl_ms=None,
            ttl_deadline_ms=None,
            consume_budget=None,
        )
        return True, answer, audit
    cand = Candidate(text=answer, confidence=confidence, kind=kind, ts_ms=ts_ms)
    decision = policy.consider(cand)
    allowed = decision.action in POLICY_ACCEPT_ACTIONS
    now = cand.ts_ms if cand.ts_ms is not None else policy.now_ms()
    ttl_deadline = (now + policy.settings.ttl_ms) if allowed else None
    audit = policy_audit(
        enabled=True,
        allowed=allowed,
        reason=decision.reason,
        action=decision.action,
        mode=decision.tier,
        confidence=confidence,
        display_text=decision.display_text,
        budget_used=policy.used,
        budget_max=policy.settings.budget_max,
        ttl_ms=policy.settings.ttl_ms,
        ttl_deadline_ms=ttl_deadline,
        consume_budget=decision.consume_budget,
    )
    return allowed, (decision.display_text if allowed else ""), audit


def policy_audit(
    *,
    enabled: bool,
    allowed: bool,
    reason: str,
    action: str | None,
    mode: str,
    confidence: float | None,
    display_text: str,
    budget_used: int | None,
    budget_max: int | None,
    ttl_ms: int | None,
    ttl_deadline_ms: int | None,
    consume_budget: bool | None,
) -> dict[str, Any]:
    """Stable jsonl `policy` object for offline false-trigger analysis."""
    return {
        "enabled": enabled,
        "allowed": allowed,
        "reason": reason,
        "action": action,
        "mode": mode,
        "confidence": confidence,
        "display_text": display_text,
        "budget_used": budget_used,
        "budget_max": budget_max,
        "ttl_ms": ttl_ms,
        "ttl_deadline_ms": ttl_deadline_ms,
        "consume_budget": consume_budget,
    }


def call_with_timeout(fn: Any, timeout_s: float, name: str) -> tuple[Any, bool]:
    """Run fn() in a daemon thread. On timeout return (None, True). No retry."""
    box: list[Any] = []

    def work() -> None:
        try:
            box.append(fn())
        except BaseException as exc:
            box.append(exc)

    worker = threading.Thread(target=work, name=name, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, True
    if not box:
        return None, True
    item = box[0]
    if isinstance(item, RouterConfigError):
        raise item
    if isinstance(item, BaseException):
        return None, False
    return item, False


def route_with_timeout(
    payload: dict[str, object],
    timeout_s: float,
    *,
    ignore_answer: bool = False,
) -> tuple[RouterResult | None, bool]:
    """Call route() once. On timeout return (None, True). No retry."""
    item, timed_out = call_with_timeout(
        lambda: route(payload, ignore_answer=ignore_answer),
        timeout_s,
        "live-judge",
    )
    if timed_out:
        return None, True
    if isinstance(item, RouterResult):
        return item, False
    return None, False


def answer_with_timeout(
    payload: dict[str, object],
    kind: str,
    timeout_s: float,
) -> tuple[str | None, bool]:
    """Answer tier. On timeout return (None, True). No retry."""
    item, timed_out = call_with_timeout(
        lambda: answer_turn(payload, kind=kind)[0],
        timeout_s,
        "live-answer",
    )
    if timed_out:
        return None, True
    if isinstance(item, str):
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
        router_timeout_source: str,
        handshake_timeout_s: float,
        log_path: Path,
        silence_ms: int,
        min_route_chars: int,
        keyterms: list[str],
        terms_path: Path | None,
        policy_enabled: bool,
        policy: PolicySettings,
        two_tier: bool,
        answer_timeout_ms: int,
        answer_model: str,
        fix_enabled: bool,
        fix_similarity: float,
        fix_entries: list[TermEntry],
    ) -> None:
        self.host = host
        self.port = port
        self.lang = lang
        self.locale = locale_from_lang(lang)
        self.wearer_note = wearer_note
        self.router_timeout_ms = router_timeout_ms
        self.router_timeout_source = router_timeout_source
        self.handshake_timeout_s = handshake_timeout_s
        self.log_path = log_path
        self.silence_ms = silence_ms
        self.min_route_chars = min_route_chars
        self.keyterms = keyterms
        self.terms_path = terms_path
        self.policy_enabled = policy_enabled
        self.policy = policy
        self.two_tier = two_tier
        self.answer_timeout_ms = answer_timeout_ms
        self.answer_model = answer_model
        self.fix_enabled = fix_enabled
        self.fix_similarity = fix_similarity
        self.fix_entries = fix_entries


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
        # speech_final off: Deepgram often marks each short FINAL speech_final,
        # which closed 1-seg turns before AGG_SILENCE_MS could merge split
        # questions. Close is driven by _agg_ticker → tick() alone.
        self.agg = TurnAggregator(
            AggregatorConfig(
                silence_ms=settings.silence_ms,
                max_turn_ms=DEFAULT_AGG_MAX_TURN_MS,
                min_chars=1,
                use_speech_final=False,
            )
        )
        self.agg_lock = threading.Lock()
        self._recent_finals: list[dict[str, Any]] = []
        self._tick_task: asyncio.Task[None] | None = None
        self.policy_engine: DisplayPolicy | None = None
        self._ttl_handle: asyncio.TimerHandle | None = None
        self._ttl_deadline_ms: int | None = None
        if settings.policy_enabled:
            self.policy_engine = DisplayPolicy(
                settings=settings.policy,
                now_ms=lambda: int(time.time() * 1000),
                set_timer=self._policy_set_timer,
                clear_timer=self._policy_clear_timer,
                on_expire=self._policy_on_expire,
            )

    def now_rel(self) -> float:
        return time.perf_counter() - self.started_perf

    def _policy_clear_timer(self) -> None:
        if self._ttl_handle is not None:
            self._ttl_handle.cancel()
            self._ttl_handle = None
        self._ttl_deadline_ms = None

    def _policy_set_timer(self, delay_ms: int, cb: Any) -> None:
        self._policy_clear_timer()
        self._ttl_deadline_ms = int(time.time() * 1000) + int(delay_ms)
        self._ttl_handle = self.loop.call_later(max(0.0, delay_ms / 1000.0), cb)

    def _policy_on_expire(self) -> None:
        if self.closed:
            return
        self.loop.create_task(self._policy_clear_downlink())

    async def _policy_clear_downlink(self) -> None:
        placeholder = self.settings.policy.clear_placeholder or DEFAULT_CLEAR_PLACEHOLDER
        stamp = fmt_session_ts(self.now_rel())
        print(f"{stamp}   → policy_clear  text={placeholder!r}", flush=True)
        await self._send_json({"text": placeholder})
        self._write_record(
            {
                "kind": "policy_clear",
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "session_rel_s": round(self.now_rel(), 3),
                "text": placeholder,
                "ttl_ms": self.settings.policy.ttl_ms,
            }
        )

    def start_deepgram(self) -> None:
        session = DeepgramPcmSession(
            api_key=self.dg_key,
            language=self.settings.lang,
            on_message=self._on_dg_message,
            on_error=self._on_dg_error,
            handshake_timeout_s=self.settings.handshake_timeout_s,
            keyterms=self.settings.keyterms or None,
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
        if parsed is None or not parsed.is_final:
            return
        asyncio.run_coroutine_threadsafe(self._ingest_final(parsed), self.loop)

    async def feed_uplink(self, raw: object) -> None:
        parsed = parse_uplink_message(raw)
        if parsed is None:
            return
        pcm, role, direction = parsed
        self.last_role = role
        self.last_direction = direction
        if self.dg is not None:
            self.dg.send_pcm(pcm)

    async def _ingest_final(self, parsed: ParsedAsrResult) -> None:
        recv_s = self.now_rel()
        rec: dict[str, Any] = {
            "transcript": parsed.transcript,
            "speech_final": parsed.speech_final,
            "is_final": parsed.is_final,
            "asr_confidence": parsed.confidence,
            "start_s": parsed.start_s,
            "duration_s": parsed.duration_s,
            "recv_s": recv_s,
            "speakerRole": self.last_role,
            "direction": self.last_direction,
        }
        self._write_record(
            {
                "kind": "final",
                **rec,
                "session_rel_s": round(recv_s, 3),
                "ts": datetime.now().isoformat(timespec="milliseconds"),
            }
        )
        print(
            f"{fmt_session_ts(recv_s)} FINAL  {display_speaker(self.last_role)}  "
            f"speech_final={str(parsed.speech_final).lower()}  {parsed.transcript}",
            flush=True,
        )
        seg = FinalSegment(
            text=parsed.transcript,
            start_s=parsed.start_s,
            duration_s=parsed.duration_s,
            speech_final=parsed.speech_final,
            recv_s=recv_s,
        )
        self._recent_finals.append(rec)
        with self.agg_lock:
            emitted = self.agg.push(seg)
        await self._handle_turns(emitted)

    async def _agg_ticker(self) -> None:
        try:
            while not self.closed:
                await asyncio.sleep(0.05)
                with self.agg_lock:
                    emitted = self.agg.tick(self.now_rel())
                if emitted:
                    await self._handle_turns(emitted)
        except asyncio.CancelledError:
            return

    async def _handle_turns(self, turns: list[AggregatedTurn]) -> None:
        pending = list(self._recent_finals)
        leftover: list[dict[str, Any]] = pending
        for turn in turns:
            n = max(0, int(turn.segments))
            raws = leftover[:n]
            leftover = leftover[n:]
            await self._handle_one_turn(turn, raws)
        self._recent_finals = leftover

    async def _handle_one_turn(
        self, turn: AggregatedTurn, raws: list[dict[str, Any]]
    ) -> None:
        t0 = time.perf_counter()
        rel = self.now_rel()
        role = self.last_role
        direction = self.last_direction
        speaker = map_speaker_role(role)
        raw_text = turn.text
        fix_hits: list[Any] = []
        fix_ms = 0.0
        text = raw_text
        if self.settings.fix_enabled and self.settings.fix_entries:
            t_fix = time.perf_counter()
            text, fix_hits = apply_fix(
                raw_text,
                self.settings.fix_entries,
                threshold=self.settings.fix_similarity,
            )
            fix_ms = (time.perf_counter() - t_fix) * 1000.0
            for hit in fix_hits:
                self._write_record(
                    {
                        "kind": "fix",
                        "ts": datetime.now().isoformat(timespec="milliseconds"),
                        "session_rel_s": round(self.now_rel(), 3),
                        **hit.to_dict(),
                    }
                )
                print(
                    f"{fmt_session_ts(self.now_rel())}   → fix  "
                    f"{hit.original!r} → {hit.fixed!r}  "
                    f"term={hit.term}  sim={hit.similarity:.2f}",
                    flush=True,
                )
        stamp = fmt_session_ts(rel)
        print(
            f"{stamp} {display_speaker(role)}  ({turn.segments} segs)  {text}",
            flush=True,
        )
        if too_short_for_router(
            text, self.settings.min_route_chars, self.settings.keyterms
        ):
            n = len(text.strip())
            reason = (
                f"too_short ({n} < {self.settings.min_route_chars}; no keyterm hit)"
            )
            print(f"{stamp}   → skip  reason: {reason}", flush=True)
            self._write_turn_jsonl(
                turn,
                raws,
                role,
                direction,
                speaker,
                None,
                False,
                False,
                t0,
                t0,
                None,
                skip_reason="too_short",
                error=reason,
                fix=_fix_record(raw_text, text, fix_hits, self.settings.fix_enabled),
                extra_timings={"fix_ms": round(fix_ms, 1), "judge_ms": 0.0, "answer_ms": None},
            )
            return

        payload: dict[str, object] = {
            "recent_turns": [
                {
                    "speaker": speaker,
                    "text": text,
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
            self._write_turn_jsonl(
                turn,
                raws,
                role,
                direction,
                speaker,
                None,
                False,
                False,
                t0,
                t_route,
                None,
                skip_reason="router_config",
                error=str(exc),
                fix=_fix_record(raw_text, text, fix_hits, self.settings.fix_enabled),
                extra_timings={"fix_ms": round(fix_ms, 1), "judge_ms": 0.0, "answer_ms": None},
            )
            await self._send_json({"error": str(exc)})
            return
        judge_ms = (time.perf_counter() - t_route) * 1000.0
        pushed = False
        t_down: float | None = None
        skip_reason: str | None = None
        policy_rec: dict[str, Any] | None = None
        answer_rec: dict[str, Any] | None = None
        answer_ms: float | None = None
        display_src = ""
        if timed_out:
            skip_reason = "router_timeout"
            print(
                f"{fmt_session_ts(self.now_rel())}   → skip  reason: "
                f"{format_router_timeout_skip(self.settings.router_timeout_ms, self.settings.router_timeout_source)}",
                flush=True,
            )
        elif result is None:
            skip_reason = "router_error"
            print(
                f"{fmt_session_ts(self.now_rel())}   → skip  reason: router error",
                flush=True,
            )
        elif result.should_respond:
            print(
                f"{fmt_session_ts(self.now_rel())}   → {Fore.GREEN}TRIGGER{Style.RESET_ALL}  "
                f"conf={result.confidence:.2f}  judge_ms={judge_ms:.0f}",
                flush=True,
            )
            if self.settings.two_tier:
                print(
                    f"{fmt_session_ts(self.now_rel())}     judge.answer ignored  "
                    f"kind={result.kind}",
                    flush=True,
                )
                t_ans = time.perf_counter()
                try:
                    ans_text, ans_to = await self.loop.run_in_executor(
                        None,
                        lambda: self._answer_once(
                            payload,
                            result.kind,
                            self.settings.answer_timeout_ms / 1000.0,
                        ),
                    )
                except RouterConfigError as exc:
                    skip_reason = "router_config"
                    print(f"{stamp}   → skip  answer config: {exc}", flush=True)
                    ans_text, ans_to = None, False
                answer_ms = (time.perf_counter() - t_ans) * 1000.0
                if ans_to:
                    skip_reason = "answer_timeout"
                    print(
                        f"{fmt_session_ts(self.now_rel())}   → skip  reason: "
                        f"answer_timeout  waited={self.settings.answer_timeout_ms}ms  "
                        f"model={self.settings.answer_model}",
                        flush=True,
                    )
                    answer_rec = {
                        "text": None,
                        "skip": False,
                        "timeout": True,
                        "model": self.settings.answer_model,
                    }
                elif ans_text is None:
                    skip_reason = skip_reason or "answer_error"
                    print(
                        f"{fmt_session_ts(self.now_rel())}   → skip  reason: answer error",
                        flush=True,
                    )
                    answer_rec = {
                        "text": None,
                        "skip": False,
                        "timeout": False,
                        "model": self.settings.answer_model,
                    }
                elif is_skip_answer(ans_text):
                    skip_reason = "answer_skip"
                    print(
                        f"{fmt_session_ts(self.now_rel())}   → skip  reason: answer SKIP  "
                        f"answer_ms={answer_ms:.0f}",
                        flush=True,
                    )
                    answer_rec = {
                        "text": "SKIP",
                        "skip": True,
                        "timeout": False,
                        "model": self.settings.answer_model,
                    }
                else:
                    display_src = ans_text.strip()
                    answer_rec = {
                        "text": display_src,
                        "skip": False,
                        "timeout": False,
                        "model": self.settings.answer_model,
                    }
            else:
                display_src = (result.answer or "").strip()
                if not display_src:
                    skip_reason = "none"
                    print(
                        f"{fmt_session_ts(self.now_rel())}   → skip  "
                        f"single-shot empty judge answer",
                        flush=True,
                    )
            if display_src:
                print(
                    f"{fmt_session_ts(self.now_rel())}     {display_src}",
                    flush=True,
                )
                allowed, display, policy_rec = apply_trigger_policy(
                    self.policy_engine,
                    display_src,
                    result.confidence,
                    result.kind,
                )
                if self._ttl_deadline_ms is not None and policy_rec is not None:
                    policy_rec["ttl_deadline_ms"] = self._ttl_deadline_ms
                if not allowed:
                    skip_reason = f"policy_{policy_rec.get('reason', 'drop')}"
                    print(
                        f"{fmt_session_ts(self.now_rel())}   → skip  policy  "
                        f"reason={policy_rec.get('reason')}  "
                        f"budget_used={policy_rec.get('budget_used')}/{policy_rec.get('budget_max')}",
                        flush=True,
                    )
                else:
                    print(
                        f"{fmt_session_ts(self.now_rel())}   → policy  "
                        f"action={policy_rec.get('action')}  mode={policy_rec.get('mode')}  "
                        f"ttl={self.settings.policy.ttl_ms}ms",
                        flush=True,
                    )
                    t_down = time.perf_counter()
                    await self._send_json({"text": display})
                    t_down = (time.perf_counter() - t_down) * 1000.0
                    pushed = True
        else:
            skip_reason = "none"
            reason = result.reason if result is not None else "none"
            conf = result.confidence if result is not None else 0.0
            print(
                f"{fmt_session_ts(self.now_rel())}   → skip  "
                f"conf={conf:.2f}  reason: {reason}",
                flush=True,
            )
        self._write_turn_jsonl(
            turn,
            raws,
            role,
            direction,
            speaker,
            result,
            timed_out,
            pushed,
            t0,
            t_route,
            t_down,
            skip_reason=skip_reason,
            policy=policy_rec,
            fix=_fix_record(raw_text, text, fix_hits, self.settings.fix_enabled),
            answer=answer_rec,
            extra_timings={
                "fix_ms": round(fix_ms, 1),
                "judge_ms": round(judge_ms, 1),
                "answer_ms": None if answer_ms is None else round(answer_ms, 1),
                "router_ms": round(judge_ms, 1),
            },
        )

    def _route_once(
        self, payload: dict[str, object], timeout_s: float
    ) -> tuple[RouterResult | None, bool]:
        with self.route_lock:
            return route_with_timeout(
                payload, timeout_s, ignore_answer=self.settings.two_tier
            )

    def _answer_once(
        self, payload: dict[str, object], kind: str, timeout_s: float
    ) -> tuple[str | None, bool]:
        with self.route_lock:
            return answer_with_timeout(payload, kind, timeout_s)

    def _write_record(self, record: dict[str, Any]) -> None:
        with self.jsonl_lock:
            self.jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.jsonl.flush()

    def _write_turn_jsonl(
        self,
        turn: AggregatedTurn,
        raws: list[dict[str, Any]],
        role: str,
        direction: int | None,
        speaker: str,
        result: RouterResult | None,
        timed_out: bool,
        pushed: bool,
        t0: float,
        t_route: float,
        downlink_ms: float | None,
        skip_reason: str | None = None,
        error: str | None = None,
        policy: dict[str, Any] | None = None,
        fix: dict[str, Any] | None = None,
        answer: dict[str, Any] | None = None,
        extra_timings: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "kind": "turn",
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "session_rel_s": round(self.now_rel(), 3),
            "transcript": (fix or {}).get("fixed", turn.text) if fix else turn.text,
            "aggregated_text": turn.text,
            "raw_finals": [r.get("transcript") for r in raws],
            "raw_final_records": raws,
            "segments": turn.segments,
            "duration_ms": turn.duration_ms,
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
            "two_tier": self.settings.two_tier,
            "judge_answer_ignored": bool(self.settings.two_tier),
        }
        if extra_timings:
            record["timings"].update(extra_timings)
        if skip_reason:
            record["skip_reason"] = skip_reason
        if error:
            record["error"] = error
        if policy is not None:
            record["policy"] = policy
        if fix is not None:
            record["fix"] = fix
        if answer is not None:
            record["answer"] = answer
        self._write_record(record)

    async def _send_json(self, obj: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception as exc:
            print(f"[live] send failed: {exc}", file=sys.stderr, flush=True)

    async def aclose(self) -> None:
        self.closed = True
        self._policy_clear_timer()
        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None
        leftover: list[AggregatedTurn]
        with self.agg_lock:
            leftover = self.agg.flush(self.now_rel())
        if leftover:
            await self._handle_turns(leftover)
        if self.dg is not None:
            try:
                self.dg.close()
            except Exception:
                pass
            self.dg = None

    def close(self) -> None:
        """Sync fallback when not on the session loop."""
        self.closed = True
        self._policy_clear_timer()
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
    p.add_argument(
        "--lang",
        default=None,
        help=f"Deepgram language: zh (default) or multi",
    )
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
    p.add_argument(
        "--silence-ms",
        type=int,
        default=None,
        help=f"aggregator silence (default {DEFAULT_AGG_SILENCE_MS}; env AGG_SILENCE_MS)",
    )
    p.add_argument(
        "--min-route-chars",
        type=int,
        default=None,
        help=f"skip route() if aggregated text shorter (default {DEFAULT_MIN_ROUTE_CHARS})",
    )
    p.add_argument(
        "--terms",
        default=None,
        help=f"keyterm JSON path (default {DEFAULT_TERMS_PATH})",
    )
    p.add_argument(
        "--no-keyterms",
        action="store_true",
        help="do not send keyterm (A/B baseline)",
    )
    p.add_argument(
        "--no-policy",
        action="store_true",
        help="bypass display_policy; router trigger pushes as before (A/B)",
    )
    p.add_argument(
        "--no-fix",
        action="store_true",
        help="bypass deterministic transcript_fix (A/B)",
    )
    p.add_argument(
        "--fix-similarity",
        type=float,
        default=None,
        help=f"fuzzy match threshold (default {DEFAULT_FIX_SIMILARITY}; env FIX_SIMILARITY)",
    )
    p.add_argument(
        "--single-shot",
        "--no-answer-tier",
        action="store_true",
        help="A/B: one judge call only; use judge answer for display (LIVE_TWO_TIER=0)",
    )
    p.add_argument(
        "--answer-timeout-ms",
        type=int,
        default=None,
        help=f"abandon answer() after this many ms (default {DEFAULT_ANSWER_TIMEOUT_MS})",
    )
    add_policy_args(p)
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
    timeout_ms, timeout_source = resolve_router_timeout_ms(args)
    handshake = (
        args.handshake_timeout_s
        if args.handshake_timeout_s is not None
        else float(_env("DEEPGRAM_HANDSHAKE_TIMEOUT_S", str(DEEPGRAM_HANDSHAKE_TIMEOUT_S)))
    )
    silence_ms = (
        args.silence_ms
        if args.silence_ms is not None
        else int(_env("AGG_SILENCE_MS", str(DEFAULT_AGG_SILENCE_MS)))
    )
    min_route = (
        args.min_route_chars
        if args.min_route_chars is not None
        else int(_env("MIN_ROUTE_CHARS", str(DEFAULT_MIN_ROUTE_CHARS)))
    )
    if args.log:
        log_path = Path(args.log)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = Path(f"live_{stamp}.jsonl")
    terms_path: Path | None
    keyterms: list[str]
    if getattr(args, "no_keyterms", False):
        terms_path = None
        keyterms = []
    else:
        raw_terms = args.terms if args.terms is not None else _env("LIVE_TERMS_PATH", "")
        terms_path = Path(raw_terms) if raw_terms else DEFAULT_TERMS_PATH
        keyterms = load_keyterms(terms_path) if terms_path.is_file() else []
    policy_on = policy_enabled_from_args(args)
    cfg = getattr(args, "policy_config", None)
    policy = load_policy_settings(
        cli=args,
        config_path=Path(cfg) if cfg else None,
    )
    two_tier = True
    if getattr(args, "single_shot", False):
        two_tier = False
    else:
        raw_tier = _env("LIVE_TWO_TIER", "1").strip().lower()
        if raw_tier in {"0", "false", "no", "off"}:
            two_tier = False
    answer_timeout = (
        args.answer_timeout_ms
        if getattr(args, "answer_timeout_ms", None) is not None
        else int(_env("ANSWER_TIMEOUT_MS", str(DEFAULT_ANSWER_TIMEOUT_MS)))
    )
    try:
        answer_model = get_answer_model()
    except RouterConfigError:
        answer_model = _env("ANSWER_MODEL", "") or _env("ROUTER_MODEL", "")
    fix_on = not getattr(args, "no_fix", False)
    if fix_on:
        raw_fix = _env("LIVE_NO_FIX", "").strip().lower()
        if raw_fix in {"1", "true", "yes", "on"}:
            fix_on = False
    fix_sim = (
        args.fix_similarity
        if getattr(args, "fix_similarity", None) is not None
        else float(_env("FIX_SIMILARITY", str(DEFAULT_FIX_SIMILARITY)))
    )
    fix_entries: list[TermEntry] = []
    if terms_path is not None and terms_path.is_file():
        fix_entries = load_term_entries(terms_path)
    elif DEFAULT_TERMS_PATH.is_file():
        fix_entries = load_term_entries(DEFAULT_TERMS_PATH)
    return LiveSettings(
        host=host,
        port=port,
        lang=lang,
        wearer_note=note,
        router_timeout_ms=timeout_ms,
        router_timeout_source=timeout_source,
        handshake_timeout_s=handshake,
        log_path=log_path,
        silence_ms=silence_ms,
        min_route_chars=min_route,
        keyterms=keyterms,
        terms_path=terms_path,
        policy_enabled=policy_on,
        policy=policy,
        two_tier=two_tier,
        answer_timeout_ms=answer_timeout,
        answer_model=answer_model,
        fix_enabled=fix_on,
        fix_similarity=fix_sim,
        fix_entries=fix_entries,
    )


def log_startup(settings: LiveSettings) -> None:
    n = len(settings.keyterms)
    src = str(settings.terms_path) if settings.terms_path else "(disabled)"
    print(
        f"live  ws://{settings.host}:{settings.port}  "
        f"dg={DEEPGRAM_MODEL} lang={settings.lang} locale={settings.locale}  "
        f"audio={SAMPLE_RATE}Hz mono s16le  "
        f"router_timeout_ms={settings.router_timeout_ms}  "
        f"router_timeout_source={settings.router_timeout_source}  "
        f"(DEFAULT_ROUTER_TIMEOUT_MS={DEFAULT_ROUTER_TIMEOUT_MS})  "
        f"dg_handshake={settings.handshake_timeout_s:.0f}s  "
        f"agg_silence={settings.silence_ms}ms  "
        f"min_route_chars={settings.min_route_chars}",
        flush=True,
    )
    print(
        f"keyterms={n} from {src}  "
        "(nova-3 uses keyterm, not keywords; see https://developers.deepgram.com/docs/keyterm)",
        flush=True,
    )
    pol = settings.policy
    if settings.policy_enabled:
        print(
            f"policy=on  full={pol.conf_full} hint={pol.conf_hint}  "
            f"budget={pol.budget_max}/{pol.budget_window_ms}ms  "
            f"ttl={pol.ttl_ms}ms  max_chars={pol.max_chars}  "
            f"dedup={pol.dedup_window}  clear={pol.clear_placeholder!r}",
            flush=True,
        )
    else:
        print("policy=off  (--no-policy / LIVE_NO_POLICY)", flush=True)
    if settings.two_tier:
        print(
            f"two_tier=on  judge={_env('ROUTER_MODEL', '(ROUTER_MODEL)')}  "
            f"answer={settings.answer_model or '(same as ROUTER_MODEL)'}  "
            f"answer_timeout_ms={settings.answer_timeout_ms}  "
            f"(unset ANSWER_MODEL → same id as ROUTER_MODEL; judge.answer ignored)",
            flush=True,
        )
    else:
        print("two_tier=off  (--single-shot / LIVE_TWO_TIER=0); judge answer used", flush=True)
    if settings.fix_enabled:
        print(
            f"fix=on  similarity={settings.fix_similarity}  "
            f"entries={len(settings.fix_entries)}",
            flush=True,
        )
    else:
        print("fix=off  (--no-fix / LIVE_NO_FIX)", flush=True)
    print(f"wearer_note={settings.wearer_note!r}", flush=True)
    print(f"jsonl={settings.log_path}", flush=True)
    print("Connect glasses/app, then speak. Ctrl-C to stop.\n", flush=True)


async def run_server(settings: LiveSettings, dg_key: str) -> None:
    # Fail-fast: live connect kwargs must never include keywords.
    probe = listen_connect_kwargs(settings.lang, settings.keyterms or None)
    if "keywords" in probe:
        raise RuntimeError("nova-3 connect must not set keywords")
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
            session._tick_task = asyncio.create_task(session._agg_ticker())
            async for message in websocket:
                await session.feed_uplink(message)
        finally:
            await session.aclose()
            print(f"[conn] - {addr}", flush=True)

    log_startup(settings)
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
