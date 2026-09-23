"""CLI / env / JSON config for the M2 pipeline. All thresholds overridable."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_AGG_SILENCE_MS: int = 1200
DEFAULT_AGG_MAX_TURN_MS: int = 15_000
DEFAULT_AGG_MIN_CHARS: int = 4
DEFAULT_AGG_USE_SPEECH_FINAL: bool = True
DEFAULT_WINDOW_TURNS: int = 6
DEFAULT_ROUTER_TIMEOUT_MS: int = 1_800
DEFAULT_LANG: str = "zh"
DEFAULT_LOCALE: str = "zh"


@dataclass(frozen=True)
class PipelineSettings:
    silence_ms: int = DEFAULT_AGG_SILENCE_MS
    max_turn_ms: int = DEFAULT_AGG_MAX_TURN_MS
    min_chars: int = DEFAULT_AGG_MIN_CHARS
    use_speech_final: bool = DEFAULT_AGG_USE_SPEECH_FINAL
    window_turns: int = DEFAULT_WINDOW_TURNS
    router_timeout_ms: int = DEFAULT_ROUTER_TIMEOUT_MS
    lang: str = DEFAULT_LANG
    locale: str = DEFAULT_LOCALE
    device: str | None = None


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text: str = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _file_values(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"config file not found: {path}")
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"config file must be a JSON object: {path}")
    return raw


def _pick(
    cli: object,
    env_name: str,
    file_map: dict[str, Any],
    file_keys: tuple[str, ...],
    default: object,
) -> object:
    if cli is not None:
        return cli
    env_val: str | None = os.environ.get(env_name)
    if env_val is not None and env_val != "":
        return env_val
    for key in file_keys:
        if key in file_map:
            return file_map[key]
    return default


def load_settings(args: argparse.Namespace) -> PipelineSettings:
    """Resolve CLI > env > --config JSON > defaults."""
    file_map: dict[str, Any] = {}
    config_path: str | None = getattr(args, "config", None)
    if config_path:
        file_map = _file_values(Path(config_path))

    silence: int = _as_int(
        _pick(getattr(args, "silence_ms", None), "AGG_SILENCE_MS", file_map, ("AGG_SILENCE_MS", "silence_ms"), None),
        DEFAULT_AGG_SILENCE_MS,
    )
    max_turn: int = _as_int(
        _pick(getattr(args, "max_turn_ms", None), "AGG_MAX_TURN_MS", file_map, ("AGG_MAX_TURN_MS", "max_turn_ms"), None),
        DEFAULT_AGG_MAX_TURN_MS,
    )
    min_chars: int = _as_int(
        _pick(getattr(args, "min_chars", None), "AGG_MIN_CHARS", file_map, ("AGG_MIN_CHARS", "min_chars"), None),
        DEFAULT_AGG_MIN_CHARS,
    )
    use_sf_cli: object = getattr(args, "use_speech_final", None)
    use_sf: bool = _as_bool(
        _pick(use_sf_cli, "AGG_USE_SPEECH_FINAL", file_map, ("AGG_USE_SPEECH_FINAL", "use_speech_final"), None),
        DEFAULT_AGG_USE_SPEECH_FINAL,
    )
    window_turns: int = _as_int(
        _pick(getattr(args, "window_turns", None), "WINDOW_TURNS", file_map, ("WINDOW_TURNS", "window_turns"), None),
        DEFAULT_WINDOW_TURNS,
    )
    timeout_ms: int = _as_int(
        _pick(
            getattr(args, "router_timeout_ms", None),
            "ROUTER_TIMEOUT_MS",
            file_map,
            ("ROUTER_TIMEOUT_MS", "router_timeout_ms"),
            None,
        ),
        DEFAULT_ROUTER_TIMEOUT_MS,
    )
    lang: str = str(
        _pick(getattr(args, "lang", None), "ASR_LANG", file_map, ("lang", "ASR_LANG"), DEFAULT_LANG)
        or DEFAULT_LANG
    )
    locale_raw: object = _pick(
        getattr(args, "locale", None), "ASR_LOCALE", file_map, ("locale", "ASR_LOCALE"), None
    )
    locale: str = str(locale_raw) if locale_raw else (lang.split("-")[0] or DEFAULT_LOCALE)
    device_raw: object = _pick(
        getattr(args, "device", None), "ASR_DEVICE", file_map, ("device", "ASR_DEVICE"), None
    )
    device: str | None = str(device_raw) if device_raw is not None and str(device_raw) != "" else None
    return PipelineSettings(
        silence_ms=silence,
        max_turn_ms=max_turn,
        min_chars=min_chars,
        use_speech_final=use_sf,
        window_turns=max(1, window_turns),
        router_timeout_ms=timeout_ms,
        lang=lang,
        locale=locale,
        device=device,
    )


def add_pipeline_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="JSON config file (CLI and env win over it)")
    parser.add_argument("--device", default=None, help="Input device index or name substring")
    parser.add_argument("--lang", default=None, help=f"Deepgram language (default {DEFAULT_LANG})")
    parser.add_argument("--locale", default=None, help="Router locale (default: derived from --lang)")
    parser.add_argument("--silence-ms", dest="silence_ms", type=int, default=None)
    parser.add_argument("--max-turn-ms", dest="max_turn_ms", type=int, default=None)
    parser.add_argument("--min-chars", dest="min_chars", type=int, default=None)
    parser.add_argument(
        "--use-speech-final",
        dest="use_speech_final",
        action="store_true",
        default=None,
        help="End a turn on speech_final=true (default on)",
    )
    parser.add_argument(
        "--no-speech-final",
        dest="use_speech_final",
        action="store_false",
        help="Ignore speech_final; only silence / max-turn close a turn",
    )
    parser.add_argument("--window-turns", dest="window_turns", type=int, default=None)
    parser.add_argument("--router-timeout-ms", dest="router_timeout_ms", type=int, default=None)
    parser.add_argument("--list-devices", action="store_true")
