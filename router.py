"""HUD conversation router: one model call → structured display decision."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, TypedDict, cast

from openai import OpenAI

from prompts import (
    ANSWER_SYSTEM_PROMPT,
    PERMISSIVE_JUDGE_APPEND,
    ROUTER_SYSTEM_PROMPT,
    assemble_answer_user_message,
    assemble_user_message,
    normalize_speaker,
)

MAX_TURNS: Final[int] = 6
MAX_ANSWER_CHARS: Final[int] = 240  # one-screen HUD; full-width = 1
HUD_LINE_CHARS: Final[int] = 28
HUD_MAX_LINES: Final[int] = 10
# Non-blocking quality warn: many ultra-short lines waste the 10×28 screen.
HUD_SHORT_LINE_AVG: Final[float] = 16.0
HUD_SHORT_LINE_MIN_LINES: Final[int] = 4
ALLOWED_KINDS: Final[frozenset[str]] = frozenset(
    {"answer", "term", "number", "translation", "none"}
)
Kind = Literal["answer", "term", "number", "translation", "none"]
Speaker = Literal["OTHER", "SELF", "UNKNOWN"]


class RouterConfigError(RuntimeError):
    """Missing or invalid environment configuration."""


class Turn(TypedDict, total=False):
    speaker: str
    text: str
    ts: int | float


class RoutePayload(TypedDict, total=False):
    recent_turns: list[Turn]
    locale: str
    wearer_note: str


class CallTiming(TypedDict, total=False):
    """Per-call breakdown for one chat completion (milliseconds)."""

    request_start_ms: float
    ttfb_ms: float | None
    total_ms: float


@dataclass(frozen=True)
class RouterResult:
    """Decision JSON plus logging fields (`over_length`, `truncated_to_none`)."""

    should_respond: bool
    confidence: float
    kind: Kind
    reason: str
    answer: str
    needs_more_context: bool
    over_length: bool = False
    truncated_to_none: bool = False
    # Only set when truncated_to_none; otherwise empty.
    original_answer: str = ""
    call_timing: CallTiming | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Public dump: contract fields, truncation flags, and original_answer.

        `original_answer` is the pre-clear model text only when
        `truncated_to_none` is true; otherwise it is the empty string.
        """
        payload: dict[str, Any] = {
            "should_respond": self.should_respond,
            "confidence": self.confidence,
            "kind": self.kind,
            "reason": self.reason,
            "answer": self.answer,
            "original_answer": (
                self.original_answer if self.truncated_to_none else ""
            ),
            "needs_more_context": self.needs_more_context,
            "truncated_to_none": self.truncated_to_none,
        }
        if self.call_timing is not None:
            payload["call_timing"] = dict(self.call_timing)
        return payload

    def to_dict(self) -> dict[str, Any]:
        """Alias of `to_public_dict` for CLI / asr session logs / evaluate."""
        return self.to_public_dict()


def load_dotenv(path: Path | None = None) -> None:
    """Load `.env` key=value pairs without overwriting existing environ.

    No python-dotenv dependency: the only external package is the OpenAI client.
    """
    env_path: Path = path if path is not None else Path(".env")
    if not env_path.is_file():
        return
    raw_text: str = env_path.read_text(encoding="utf-8")
    for raw_line in raw_text.splitlines():
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


def get_settings() -> tuple[str, str | None, str]:
    """Return `(api_key, base_url_or_none, model)` from the environment."""
    load_dotenv()
    api_key: str = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url: str = os.environ.get("OPENAI_BASE_URL", "").strip()
    model: str = os.environ.get("ROUTER_MODEL", "").strip()
    if not api_key:
        raise RouterConfigError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env or export it."
        )
    if not model:
        raise RouterConfigError(
            "ROUTER_MODEL is not set. Copy .env.example to .env or export it."
        )
    return api_key, (base_url or None), model


def build_client(api_key: str | None = None, base_url: str | None = None) -> OpenAI:
    """Construct an OpenAI-compatible chat-completions client from env/args."""
    resolved_key: str
    resolved_base: str | None
    if api_key is None or base_url is None:
        env_key: str
        env_base: str | None
        env_key, env_base, _model = get_settings()
        resolved_key = api_key if api_key is not None else env_key
        resolved_base = base_url if base_url is not None else env_base
    else:
        resolved_key = api_key
        resolved_base = base_url
    # max_retries=0: OpenAI Python SDK defaults to max_retries=2 with
    # exponential backoff (INITIAL_RETRY_DELAY=0.5s, MAX_RETRY_DELAY=8s) on
    # timeout / 408 / 429 / 5xx. That second attempt is the likely cause of
    # ~12.2s evaluate spikes (id=8, id=25). On timeout/error, fail immediately
    # so route() can degrade — no SDK retry, no manual loop.
    kwargs: dict[str, Any] = {
        "api_key": resolved_key,
        "timeout": 20.0,
        "max_retries": 0,
    }
    if resolved_base:
        kwargs["base_url"] = resolved_base
    return OpenAI(**kwargs)


def _as_float_ts(value: object, fallback: float) -> float:
    try:
        return float(cast(int | float | str, value))
    except (TypeError, ValueError):
        return fallback


def window_turns(payload: Mapping[str, Any]) -> list[Turn]:
    """Sort turns by time (stable), keep at most the last 6, speakers normalized."""
    raw: object = payload.get("recent_turns", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    indexed: list[tuple[int, Turn]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        turn: Turn = {
            "speaker": normalize_speaker(item.get("speaker")),
            "text": str(item.get("text", "") or ""),
            "ts": _as_float_ts(item.get("ts"), float(i)),
        }
        indexed.append((i, turn))
    indexed.sort(key=lambda pair: (_as_float_ts(pair[1].get("ts"), float(pair[0])), pair[0]))
    kept: list[Turn] = [turn for _i, turn in indexed[-MAX_TURNS:]]
    return kept


def answer_char_len(text: str) -> int:
    """Count answer length: each Unicode code point is 1, including full-width."""
    return len(text)


def _strip_markdown_fence(text: str) -> str:
    stripped: str = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines: list[str] = stripped.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Tolerant JSON object extraction; never raises."""
    candidate: str = _strip_markdown_fence(text)
    try:
        parsed: object = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start: int = candidate.find("{")
    end: int = candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered: str = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0", ""}:
            return False
    return default


def _as_confidence(value: object) -> float:
    try:
        number: float = float(cast(int | float | str, value))
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _as_kind(value: object) -> Kind:
    raw: str = str(value).strip().lower() if value is not None else "none"
    if raw in ALLOWED_KINDS:
        return cast(Kind, raw)
    return "none"


def degrade(reason: str) -> RouterResult:
    """Safe default when the model output cannot be trusted."""
    return RouterResult(
        should_respond=False,
        confidence=0.0,
        kind="none",
        reason=reason,
        answer="",
        needs_more_context=False,
        over_length=False,
        truncated_to_none=False,
        original_answer="",
    )


def normalize_result(
    raw: Mapping[str, Any],
    *,
    ignore_answer: bool = False,
) -> RouterResult:
    """Coerce model JSON into the strict output contract. Never raises.

    ``ignore_answer`` is for the live two-tier judge: keep
    should_respond / confidence / kind / reason / needs_more_context, but
    do not let the unused ``answer`` field flip a trigger (over-length).
    The raw answer is still stored for jsonl comparison.
    """
    needs_more: bool = _as_bool(raw.get("needs_more_context"), False)
    should: bool = _as_bool(raw.get("should_respond"), False)
    if needs_more:
        should = False
    kind: Kind = _as_kind(raw.get("kind"))
    reason: str = str(raw.get("reason", "") or "").strip()
    answer: str = str(raw.get("answer", "") or "")
    if not should:
        answer = ""
        kind = "none"
    elif kind == "none":
        kind = "answer"
    over_length: bool = answer_char_len(answer) > MAX_ANSWER_CHARS
    truncated_to_none: bool = False
    original_answer: str = ""
    if ignore_answer:
        # Judge answer is unused for display; length must not kill the trigger.
        over_length = False
        truncated_to_none = False
        original_answer = ""
    elif over_length:
        # Do not truncate-and-keep: a >240 answer is a failed trigger.
        # Keep the pre-clear text so evaluate/reports can show what the
        # model actually wrote (id=9 / id=29 were blank without this).
        original_answer = answer
        should = False
        kind = "none"
        answer = ""
        truncated_to_none = True
        reason = (
            f"{reason}; over-length answer converted to none"
            if reason
            else "over-length answer converted to none"
        )
    return RouterResult(
        should_respond=should,
        confidence=_as_confidence(raw.get("confidence")),
        kind=kind,
        reason=reason or "normalized",
        answer=answer,
        needs_more_context=needs_more,
        over_length=over_length,
        truncated_to_none=truncated_to_none,
        original_answer=original_answer if truncated_to_none else "",
    )


def parse_model_output(
    text: str | None,
    *,
    ignore_answer: bool = False,
) -> RouterResult:
    """Parse model text into RouterResult; illegal JSON degrades, never raises."""
    if text is None or not str(text).strip():
        return degrade("empty model output")
    obj: dict[str, Any] | None = extract_json_object(str(text))
    if obj is None:
        return degrade("unparseable model JSON")
    return normalize_result(obj, ignore_answer=ignore_answer)


def _json_mode_unsupported(exc: BaseException) -> bool:
    message: str = str(exc).lower()
    needles: tuple[str, ...] = (
        "response_format",
        "json_object",
        "json schema",
        "structured output",
        "not supported",
        "unknown parameter",
        "unexpected keyword",
    )
    return any(n in message for n in needles)


# Last complete_chat timing (including failed calls) for route()/evaluate.
_LAST_CALL_TIMING: CallTiming | None = None


def _no_retry_client(client: OpenAI) -> OpenAI:
    """Return a client that will not retry timeout/transport errors."""
    with_opts = getattr(client, "with_options", None)
    if callable(with_opts):
        return with_opts(max_retries=0)
    return client


def _ttfb_seconds(raw: Any) -> float | None:
    """Headers / first-byte elapsed from a with_raw_response object, if any."""
    elapsed = getattr(raw, "elapsed", None)
    if elapsed is None:
        http_resp = getattr(raw, "http_response", None)
        elapsed = getattr(http_resp, "elapsed", None) if http_resp is not None else None
    if elapsed is None:
        return None
    total_seconds = getattr(elapsed, "total_seconds", None)
    if callable(total_seconds):
        try:
            return float(total_seconds())
        except (TypeError, ValueError):
            return None
    try:
        return float(elapsed)
    except (TypeError, ValueError):
        return None


def _log_call_timing(timing: CallTiming, model: str) -> None:
    """Print connect/request-start, TTFB, and total. stderr so CLI JSON stays clean."""
    ttfb = timing.get("ttfb_ms")
    ttfb_s = f"{ttfb:.1f}" if isinstance(ttfb, (int, float)) else "n/a"
    total = timing.get("total_ms")
    total_s = f"{total:.1f}" if isinstance(total, (int, float)) else "n/a"
    start = timing.get("request_start_ms", 0.0)
    print(
        f"[router] model call timing: request_start={start:.1f}ms "
        f"ttfb={ttfb_s}ms total={total_s}ms model={model!r}",
        file=sys.stderr,
        flush=True,
    )


def _one_completion(client: OpenAI, kwargs: dict[str, Any]) -> tuple[Any, float | None]:
    """Single create(); no retry. Prefer with_raw_response so TTFB is available."""
    api: OpenAI = _no_retry_client(client)
    completions = api.chat.completions
    raw_wrapper = getattr(completions, "with_raw_response", None)
    if raw_wrapper is not None:
        raw = raw_wrapper.create(**kwargs)
        ttfb = _ttfb_seconds(raw)
        parsed = raw.parse() if hasattr(raw, "parse") else raw
        return parsed, ttfb
    return completions.create(**kwargs), None


def get_answer_model() -> str:
    """ANSWER_MODEL if set, otherwise the same id as ROUTER_MODEL."""
    load_dotenv()
    override: str = os.environ.get("ANSWER_MODEL", "").strip()
    if override:
        return override
    _key, _base, model = get_settings()
    return model


def complete_chat(
    client: OpenAI,
    messages: list[dict[str, str]],
    model: str,
    *,
    json_mode: bool = True,
    max_tokens: int = 256,
) -> tuple[str, CallTiming]:
    """One chat completion. Prefer JSON mode; fall back if unsupported.

    Raises on transport/API failure so `route()` can degrade. Timeout and
    HTTP errors are not retried (SDK max_retries forced to 0). The extra
    JSON-mode fallback is only when the endpoint rejects response_format,
    not on timeout.
    """
    common: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,  # deterministic; keep at 0.0
        "max_tokens": max_tokens,
    }
    global _LAST_CALL_TIMING
    t0: float = time.perf_counter()
    ttfb_s: float | None = None
    try:
        try:
            if json_mode:
                response, ttfb_s = _one_completion(
                    client, {**common, "response_format": {"type": "json_object"}}
                )
            else:
                response, ttfb_s = _one_completion(client, common)
        except TypeError:
            response, ttfb_s = _one_completion(client, common)
        except Exception as exc:
            if json_mode and _json_mode_unsupported(exc):
                response, ttfb_s = _one_completion(client, common)
            else:
                raise
        content: str | None = None
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            content = None
        timing: CallTiming = {
            "request_start_ms": 0.0,
            "ttfb_ms": (ttfb_s * 1000.0) if ttfb_s is not None else None,
            "total_ms": (time.perf_counter() - t0) * 1000.0,
        }
        _LAST_CALL_TIMING = timing
        _log_call_timing(timing, model)
        return content or "", timing
    except Exception:
        timing = {
            "request_start_ms": 0.0,
            "ttfb_ms": (ttfb_s * 1000.0) if ttfb_s is not None else None,
            "total_ms": (time.perf_counter() - t0) * 1000.0,
        }
        _LAST_CALL_TIMING = timing
        _log_call_timing(timing, model)
        raise


def route(
    payload: Mapping[str, Any],
    *,
    client: OpenAI | None = None,
    completion_fn: Callable[[list[dict[str, str]]], str] | None = None,
    ignore_answer: bool = False,
    extra_system: str | None = None,
) -> RouterResult:
    """Decide whether to show a short HUD answer for the last transcript turn.

    One model call returns the full decision JSON. Illegal output degrades to
    `should_respond=false` and never raises (except missing env config when a
    live client must be constructed).

    Speaker labels (SELF / OTHER / UNKNOWN) are context for the model and
    for logs only. A last-speaker SELF turn is sent to the model the same
    way as OTHER — there is no pre-model hard exclude.

    ``ignore_answer`` (live two-tier judge): do not convert an over-length
    unused ``answer`` into ``should_respond=false``. Display uses ``answer()``.
    """
    turns: list[Turn] = window_turns(payload)
    locale: str = str(payload.get("locale") or "ja")
    wearer_note_raw: object = payload.get("wearer_note")
    wearer_note: str | None
    if wearer_note_raw is None or str(wearer_note_raw).strip() == "":
        wearer_note = None
    else:
        wearer_note = str(wearer_note_raw)
    user_message: str = assemble_user_message(turns, locale, wearer_note)
    system: str = ROUTER_SYSTEM_PROMPT
    extra: str = (extra_system or "").strip()
    if extra:
        system = f"{ROUTER_SYSTEM_PROMPT.rstrip()}\n\n{extra}"
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]
    raw_text: str
    timing: CallTiming | None = None
    try:
        if completion_fn is not None:
            raw_text = completion_fn(messages)
        else:
            active: OpenAI = client if client is not None else build_client()
            _key, _base, model = get_settings()
            raw_text, timing = complete_chat(active, messages, model)
    except RouterConfigError:
        raise
    except Exception as exc:
        failed = degrade(f"model call failed: {type(exc).__name__}")
        attach: CallTiming | None = timing if timing is not None else _LAST_CALL_TIMING
        return replace(failed, call_timing=attach) if attach is not None else failed
    parsed: RouterResult = parse_model_output(raw_text, ignore_answer=ignore_answer)
    if timing is not None:
        return replace(parsed, call_timing=timing)
    return parsed


def is_skip_answer(text: str) -> bool:
    """Answer-tier exact SKIP (after strip) means no trigger."""
    return text.strip() == "SKIP"


@dataclass(frozen=True)
class AnswerPayload:
    """Answer-tier JSON after hud post-process."""

    hud: str
    detail: str
    raw: str = ""
    parse_ok: bool = True
    hud_wrapped: bool = False
    hud_lines_truncated: int = 0
    hud_discarded: bool = False
    skipped: bool = False
    hud_quality: dict[str, Any] = field(default_factory=dict)

    def glasses_text(self) -> str:
        return self.hud.strip()

    def has_detail(self) -> bool:
        return bool(self.detail.strip())

    def no_push(self) -> bool:
        return not self.glasses_text() and not self.has_detail()

    def to_dict(self) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "hud": self.hud,
            "detail": self.detail,
            "parse_ok": self.parse_ok,
            "hud_wrapped": self.hud_wrapped,
            "hud_lines_truncated": self.hud_lines_truncated,
            "hud_discarded": self.hud_discarded,
            "skipped": self.skipped,
        }
        if self.hud_quality:
            rec["hud_quality"] = dict(self.hud_quality)
        return rec


def wrap_hud_line(line: str, width: int = HUD_LINE_CHARS) -> list[str]:
    if not line:
        return [""]
    out: list[str] = []
    rest = line
    while rest:
        out.append(rest[:width])
        rest = rest[width:]
    return out


def postprocess_hud(raw: str) -> tuple[str, dict[str, Any]]:
    """Wrap at 28, cap 10 lines, discard if content chars > 240."""
    stats: dict[str, Any] = {
        "wrapped": False,
        "lines_truncated": 0,
        "discarded": False,
        "n_lines": 0,
        "avg_line_len": 0.0,
    }
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if is_skip_answer(text) or not text.strip():
        return "", stats
    content_len = answer_char_len(text.replace("\n", ""))
    if content_len > MAX_ANSWER_CHARS:
        stats["discarded"] = True
        return "", stats
    lines: list[str] = []
    for part in text.split("\n"):
        pieces = wrap_hud_line(part)
        if len(pieces) > 1:
            stats["wrapped"] = True
        lines.extend(pieces)
    if len(lines) > HUD_MAX_LINES:
        stats["lines_truncated"] = len(lines) - HUD_MAX_LINES
        lines = lines[:HUD_MAX_LINES]
    nonempty = [ln for ln in lines if ln.strip()]
    stats["n_lines"] = len(nonempty)
    if nonempty:
        stats["avg_line_len"] = round(
            sum(answer_char_len(ln) for ln in nonempty) / len(nonempty), 1
        )
    result = "\n".join(lines)
    if answer_char_len(result.replace("\n", "")) > MAX_ANSWER_CHARS:
        stats["discarded"] = True
        return "", stats
    return result, stats


# Longer Chinese markers first so 怎么样 wins over 怎么 / 怎样.
_ZH_INTERROG: Final[tuple[str, ...]] = (
    "怎么样",
    "怎样",
    "如何",
    "为什么",
    "为何",
    "怎么",
    "什么",
    "多少",
    "哪些",
    "哪个",
    "哪儿",
    "哪里",
)
_EN_INTERROG: Final[tuple[str, ...]] = ("how", "what", "why", "which")
_EN_STOP: Final[frozenset[str]] = frozenset(
    {
        "the",
        "and",
        "for",
        "you",
        "do",
        "does",
        "did",
        "is",
        "are",
        "was",
        "to",
        "of",
        "a",
        "an",
        "it",
        "in",
        "on",
        "with",
    }
)
# If the omitted clause is about evaluation, accept any of these in hud.
_EVAL_SYNONYMS: Final[frozenset[str]] = frozenset(
    {
        "评估",
        "评测",
        "测评",
        "衡量",
        "抽检",
        "标注集",
        "hit@k",
        "recall@k",
        "evaluate",
        "evaluation",
        "metric",
        "metrics",
    }
)


def _find_interrogative_spans(question: str) -> list[tuple[int, str]]:
    """Left-to-right (index, marker) hits; Chinese longest-first, English words."""
    found: list[tuple[int, str]] = []
    i = 0
    n = len(question)
    lower = question.casefold()
    while i < n:
        hit: str | None = None
        for marker in _ZH_INTERROG:
            if question.startswith(marker, i):
                hit = marker
                break
        if hit is None:
            for marker in _EN_INTERROG:
                end = i + len(marker)
                if lower.startswith(marker, i):
                    left_ok = i == 0 or not lower[i - 1].isalpha()
                    right_ok = end >= n or not lower[end].isalpha()
                    if left_ok and right_ok:
                        hit = marker
                        break
        if hit is not None:
            found.append((i, hit))
            i += len(hit)
        else:
            i += 1
    return found


def interrogative_clauses(question: str) -> list[tuple[str, str]]:
    """Split a question into (marker, clause) pairs in order."""
    text = " ".join(str(question or "").split())
    spans = _find_interrogative_spans(text)
    if not spans:
        return []
    out: list[tuple[str, str]] = []
    for idx, (start, marker) in enumerate(spans):
        end = spans[idx + 1][0] if idx + 1 < len(spans) else len(text)
        out.append((marker, text[start:end].strip()))
    return out


def _clause_needles(marker: str, clause: str) -> list[str]:
    """Content words after the interrogative itself (评估 from 如何评估)."""
    rest = clause
    if rest.casefold().startswith(marker.casefold()):
        rest = rest[len(marker) :]
    rest = rest.strip(" ，。？?！!、；;：:and")
    needles: list[str] = []
    cjk = "".join(ch for ch in rest if "\u4e00" <= ch <= "\u9fff")
    if len(cjk) >= 2:
        needles.append(cjk)
        if len(cjk) > 2:
            needles.append(cjk[:2])
    for token in rest.replace("/", " ").replace("@", " ").split():
        folded = token.casefold().strip(".,;:!?()[]")
        if len(folded) >= 4 and folded not in _EN_STOP and folded not in {
            m.casefold() for m in _EN_INTERROG
        }:
            needles.append(folded)
    # unique, first occurrence wins
    seen: set[str] = set()
    uniq: list[str] = []
    for item in needles:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            uniq.append(item)
    return uniq


def _needle_covered(needle: str, hud_fold: str) -> bool:
    folded = needle.casefold()
    if folded in hud_fold:
        return True
    if folded in {s.casefold() for s in _EVAL_SYNONYMS}:
        return any(syn.casefold() in hud_fold for syn in _EVAL_SYNONYMS)
    return False


def omitted_interrogative_needles(question: str, hud: str) -> list[str]:
    """Needles from the 2nd+ interrogative clause that hud does not cover."""
    clauses = interrogative_clauses(question)
    if len(clauses) < 2 or not hud.strip():
        return []
    hud_fold = hud.casefold()
    missing: list[str] = []
    for marker, clause in clauses[1:]:
        needles = _clause_needles(marker, clause)
        if not needles:
            continue
        if not any(_needle_covered(n, hud_fold) for n in needles):
            missing.append(needles[0])
    return missing


def assess_hud_quality(hud: str, question: str = "") -> dict[str, Any]:
    """Non-blocking quality flags: short lines / omitted 2nd interrogative.

    Never discards hud. Used only for layer-stats warnings.
    """
    lines = [
        ln
        for ln in str(hud or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if ln.strip()
    ]
    n_lines = len(lines)
    avg = (
        sum(answer_char_len(ln) for ln in lines) / n_lines if n_lines else 0.0
    )
    short_lines = n_lines >= HUD_SHORT_LINE_MIN_LINES and avg < HUD_SHORT_LINE_AVG
    omitted = omitted_interrogative_needles(question, hud) if hud.strip() else []
    warnings: list[str] = []
    if short_lines:
        warnings.append("short_lines")
    if omitted:
        warnings.append("omitted_clause")
    return {
        "n_lines": n_lines,
        "avg_line_len": round(avg, 1),
        "short_lines": short_lines,
        "omitted": omitted,
        "warnings": warnings,
    }


def parse_answer_output(
    text: str | None, *, question: str = ""
) -> AnswerPayload:
    """Parse answer-tier JSON. Empty hud + empty detail = no trigger."""
    raw = "" if text is None else str(text)
    if is_skip_answer(raw):
        return AnswerPayload(hud="", detail="", raw=raw, skipped=True)
    obj = extract_json_object(raw)
    if obj is None:
        return AnswerPayload(
            hud="", detail="", raw=raw, parse_ok=False, skipped=True
        )
    hud_raw = str(obj.get("hud") or "")
    detail = str(obj.get("detail") or "").strip()
    if is_skip_answer(hud_raw):
        hud_raw = ""
    hud, stats = postprocess_hud(hud_raw)
    skipped = not hud.strip() and not detail
    quality = assess_hud_quality(hud, question) if hud.strip() else {}
    return AnswerPayload(
        hud=hud,
        detail=detail,
        raw=raw,
        parse_ok=True,
        hud_wrapped=bool(stats["wrapped"]),
        hud_lines_truncated=int(stats["lines_truncated"]),
        hud_discarded=bool(stats["discarded"]),
        skipped=skipped,
        hud_quality=quality,
    )


def answer(
    payload: Mapping[str, Any],
    *,
    kind: str = "answer",
    client: OpenAI | None = None,
    completion_fn: Callable[[list[dict[str, str]]], str] | None = None,
) -> tuple[AnswerPayload, CallTiming | None]:
    """Answer-tier: JSON ``hud`` + ``detail``. Judge ``answer`` is not used."""
    turns: list[Turn] = window_turns(payload)
    locale: str = str(payload.get("locale") or "ja")
    wearer_note_raw: object = payload.get("wearer_note")
    wearer_note: str | None
    if wearer_note_raw is None or str(wearer_note_raw).strip() == "":
        wearer_note = None
    else:
        wearer_note = str(wearer_note_raw)
    user_message: str = assemble_answer_user_message(
        turns, locale, kind, wearer_note
    )
    last_text: str = str(turns[-1].get("text") or "") if turns else ""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    if completion_fn is not None:
        return (
            parse_answer_output(
                str(completion_fn(messages) or ""), question=last_text
            ),
            None,
        )
    active: OpenAI = client if client is not None else build_client()
    model: str = get_answer_model()
    raw_text, timing = complete_chat(
        active, messages, model, json_mode=True, max_tokens=1024
    )
    return parse_answer_output(raw_text, question=last_text), timing


def result_json(result: RouterResult) -> str:
    """Serialize the public contract as compact JSON."""
    return json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ALLOWED_KINDS",
    "HUD_LINE_CHARS",
    "HUD_MAX_LINES",
    "HUD_SHORT_LINE_AVG",
    "HUD_SHORT_LINE_MIN_LINES",
    "MAX_ANSWER_CHARS",
    "MAX_TURNS",
    "PERMISSIVE_JUDGE_APPEND",
    "Kind",
    "RoutePayload",
    "RouterConfigError",
    "AnswerPayload",
    "CallTiming",
    "RouterResult",
    "Speaker",
    "Turn",
    "answer_char_len",
    "answer",
    "assess_hud_quality",
    "build_client",
    "complete_chat",
    "degrade",
    "extract_json_object",
    "get_answer_model",
    "get_settings",
    "interrogative_clauses",
    "is_skip_answer",
    "load_dotenv",
    "normalize_result",
    "omitted_interrogative_needles",
    "parse_answer_output",
    "parse_model_output",
    "postprocess_hud",
    "result_json",
    "route",
    "window_turns",
]
