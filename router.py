"""HUD conversation router: one model call → structured display decision."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, TypedDict, cast

from openai import OpenAI

from prompts import ROUTER_SYSTEM_PROMPT, assemble_user_message, normalize_speaker

MAX_TURNS: Final[int] = 6
MAX_ANSWER_CHARS: Final[int] = 40  # full-width = 1 (Unicode code points)
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


@dataclass(frozen=True)
class RouterResult:
    """Strict output contract. `over_length` is internal (not serialized)."""

    should_respond: bool
    confidence: float
    kind: Kind
    reason: str
    answer: str
    needs_more_context: bool
    over_length: bool = False

    def to_dict(self) -> dict[str, bool | float | str]:
        """Public JSON fields only (fixed contract)."""
        return {
            "should_respond": self.should_respond,
            "confidence": self.confidence,
            "kind": self.kind,
            "reason": self.reason,
            "answer": self.answer,
            "needs_more_context": self.needs_more_context,
        }


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
    kwargs: dict[str, Any] = {"api_key": resolved_key, "timeout": 20.0}
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
    )


def normalize_result(raw: Mapping[str, Any]) -> RouterResult:
    """Coerce model JSON into the strict output contract. Never raises."""
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
    if over_length:
        # HUD safety: never emit more than 40 code points. evaluate.py
        # still counts this as a failure via RouterResult.over_length.
        answer = answer[:MAX_ANSWER_CHARS]
        if not reason:
            reason = "answer exceeded 40 characters; truncated"
    return RouterResult(
        should_respond=should,
        confidence=_as_confidence(raw.get("confidence")),
        kind=kind,
        reason=reason or "normalized",
        answer=answer,
        needs_more_context=needs_more,
        over_length=over_length,
    )


def parse_model_output(text: str | None) -> RouterResult:
    """Parse model text into RouterResult; illegal JSON degrades, never raises."""
    if text is None or not str(text).strip():
        return degrade("empty model output")
    obj: dict[str, Any] | None = extract_json_object(str(text))
    if obj is None:
        return degrade("unparseable model JSON")
    return normalize_result(obj)


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


def complete_chat(
    client: OpenAI,
    messages: list[dict[str, str]],
    model: str,
) -> str:
    """One chat completion. Prefer JSON mode; fall back if unsupported.

    Raises on transport/API failure so `route()` can degrade.
    """
    common: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 256,
    }
    try:
        response = client.chat.completions.create(
            **common,
            response_format={"type": "json_object"},
        )
    except TypeError:
        response = client.chat.completions.create(**common)
    except Exception as exc:
        if not _json_mode_unsupported(exc):
            raise
        response = client.chat.completions.create(**common)
    content: str | None = None
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        content = None
    return content or ""


def route(
    payload: Mapping[str, Any],
    *,
    client: OpenAI | None = None,
    completion_fn: Callable[[list[dict[str, str]]], str] | None = None,
) -> RouterResult:
    """Decide whether to show a short HUD answer for the last transcript turn.

    One model call returns the full decision JSON. Illegal output degrades to
    `should_respond=false` and never raises (except missing env config when a
    live client must be constructed).
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
    messages: list[dict[str, str]] = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    raw_text: str
    try:
        if completion_fn is not None:
            raw_text = completion_fn(messages)
        else:
            active: OpenAI = client if client is not None else build_client()
            _key, _base, model = get_settings()
            raw_text = complete_chat(active, messages, model)
    except RouterConfigError:
        raise
    except Exception as exc:
        return degrade(f"model call failed: {type(exc).__name__}")
    return parse_model_output(raw_text)


def result_json(result: RouterResult) -> str:
    """Serialize the public contract as compact JSON."""
    return json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ALLOWED_KINDS",
    "MAX_ANSWER_CHARS",
    "MAX_TURNS",
    "Kind",
    "RoutePayload",
    "RouterConfigError",
    "RouterResult",
    "Speaker",
    "Turn",
    "answer_char_len",
    "build_client",
    "complete_chat",
    "degrade",
    "extract_json_object",
    "get_settings",
    "load_dotenv",
    "normalize_result",
    "parse_model_output",
    "result_json",
    "route",
    "window_turns",
]
