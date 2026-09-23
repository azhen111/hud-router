"""Router 决策 → 镜片推送之间的显示策略。

不接 ASR / LLM。阈值可通过 CLI / 环境变量 / JSON 配置覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

Action = Literal["push", "hint", "preempt", "drop"]


DEFAULT_CONF_FULL = 0.85
DEFAULT_CONF_HINT = 0.70
DEFAULT_BUDGET_WINDOW_MS = 60_000
DEFAULT_BUDGET_MAX = 1
DEFAULT_PREEMPT_MARGIN = 0.1
DEFAULT_DEDUP_WINDOW = 10
DEFAULT_DEDUP_THRESHOLD = 0.85
DEFAULT_TTL_MS = 25_000
DEFAULT_MAX_CHARS = 240  # drop only when len(text) > this (one G2 screen)
DEFAULT_ONE_LINE_CHARS = 28  # wrap hint; algorithm unchanged
DEFAULT_HINT_TEXT = "・?"
DEFAULT_CLEAR_PLACEHOLDER = "・"


def _as_float(value: object, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pick(
    cli: object,
    env_name: str,
    file_map: dict[str, Any],
    default: object,
) -> object:
    if cli is not None:
        return cli
    env_val = os.environ.get(env_name)
    if env_val is not None and env_val != "":
        return env_val
    if env_name in file_map:
        return file_map[env_name]
    short = env_name.removeprefix("POLICY_")
    if short.lower() in file_map:
        return file_map[short.lower()]
    if env_name.lower() in file_map:
        return file_map[env_name.lower()]
    return default


@dataclass(frozen=True)
class PolicySettings:
    conf_full: float = DEFAULT_CONF_FULL
    conf_hint: float = DEFAULT_CONF_HINT
    budget_window_ms: int = DEFAULT_BUDGET_WINDOW_MS
    budget_max: int = DEFAULT_BUDGET_MAX
    preempt_margin: float = DEFAULT_PREEMPT_MARGIN
    dedup_window: int = DEFAULT_DEDUP_WINDOW
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD
    ttl_ms: int = DEFAULT_TTL_MS
    max_chars: int = DEFAULT_MAX_CHARS
    one_line_chars: int = DEFAULT_ONE_LINE_CHARS
    hint_text: str = DEFAULT_HINT_TEXT
    clear_placeholder: str = DEFAULT_CLEAR_PLACEHOLDER


def load_policy_settings(
    *,
    cli: argparse.Namespace | None = None,
    config_path: Path | None = None,
) -> PolicySettings:
    file_map: dict[str, Any] = {}
    if config_path is not None:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SystemExit(f"config must be a JSON object: {config_path}")
        file_map = raw

    def g(env: str, attr: str, default: object, caster: Callable[[object, Any], Any]) -> Any:
        cli_val = getattr(cli, attr, None) if cli is not None else None
        return caster(_pick(cli_val, env, file_map, default), default)

    return PolicySettings(
        conf_full=g("POLICY_CONF_FULL", "conf_full", DEFAULT_CONF_FULL, _as_float),
        conf_hint=g("POLICY_CONF_HINT", "conf_hint", DEFAULT_CONF_HINT, _as_float),
        budget_window_ms=g(
            "POLICY_BUDGET_WINDOW_MS", "budget_window_ms", DEFAULT_BUDGET_WINDOW_MS, _as_int
        ),
        budget_max=g("POLICY_BUDGET_MAX", "budget_max", DEFAULT_BUDGET_MAX, _as_int),
        preempt_margin=g(
            "POLICY_PREEMPT_MARGIN", "preempt_margin", DEFAULT_PREEMPT_MARGIN, _as_float
        ),
        dedup_window=g("POLICY_DEDUP_WINDOW", "dedup_window", DEFAULT_DEDUP_WINDOW, _as_int),
        dedup_threshold=g(
            "POLICY_DEDUP_THRESHOLD", "dedup_threshold", DEFAULT_DEDUP_THRESHOLD, _as_float
        ),
        ttl_ms=g("POLICY_TTL_MS", "ttl_ms", DEFAULT_TTL_MS, _as_int),
        max_chars=g("POLICY_MAX_CHARS", "max_chars", DEFAULT_MAX_CHARS, _as_int),
        one_line_chars=g(
            "POLICY_ONE_LINE_CHARS", "one_line_chars", DEFAULT_ONE_LINE_CHARS, _as_int
        ),
        hint_text=str(
            _pick(
                getattr(cli, "hint_text", None) if cli is not None else None,
                "POLICY_HINT_TEXT",
                file_map,
                DEFAULT_HINT_TEXT,
            )
        ),
        clear_placeholder=str(
            _pick(
                getattr(cli, "clear_placeholder", None) if cli is not None else None,
                "POLICY_CLEAR_PLACEHOLDER",
                file_map,
                DEFAULT_CLEAR_PLACEHOLDER,
            )
        ),
    )


def add_policy_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--conf-full", dest="conf_full", type=float, default=None)
    p.add_argument("--conf-hint", dest="conf_hint", type=float, default=None)
    p.add_argument("--budget-window-ms", dest="budget_window_ms", type=int, default=None)
    p.add_argument("--budget-max", dest="budget_max", type=int, default=None)
    p.add_argument("--preempt-margin", dest="preempt_margin", type=float, default=None)
    p.add_argument("--dedup-window", dest="dedup_window", type=int, default=None)
    p.add_argument("--dedup-threshold", dest="dedup_threshold", type=float, default=None)
    p.add_argument("--ttl-ms", dest="ttl_ms", type=int, default=None)
    p.add_argument("--max-chars", dest="max_chars", type=int, default=None)
    p.add_argument("--one-line-chars", dest="one_line_chars", type=int, default=None)
    p.add_argument("--hint-text", dest="hint_text", default=None)
    p.add_argument("--clear-placeholder", dest="clear_placeholder", default=None)
    p.add_argument("--policy-config", dest="policy_config", default=None)


@dataclass
class Candidate:
    text: str
    confidence: float
    kind: str = "answer"
    ts_ms: int | None = None


@dataclass
class PolicyDecision:
    action: Action
    reason: str
    display_text: str
    consume_budget: bool
    tier: str = "none"


def normalize_text(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text)
    return "".join(folded.split()).lower()


def char_similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def format_length(
    text: str,
    max_chars: int,
    one_line_chars: int | None = None,
) -> tuple[str | None, str]:
    """Keep text up to max_chars; drop only when longer.

    `one_line_chars` is a wrap hint (default 28). 29–56 stay two lines and
    are not dropped when max_chars is 56.
    """
    n = len(text)
    if n > max_chars:
        return None, "drop_length"
    wrap_at = DEFAULT_ONE_LINE_CHARS if one_line_chars is None else one_line_chars
    if wrap_at > 0 and wrap_at < max_chars and n > wrap_at:
        return text[:wrap_at] + "\n" + text[wrap_at:], "two_line"
    return text, "one_line"


@dataclass
class DisplayPolicy:
    settings: PolicySettings
    now_ms: Callable[[], int]
    set_timer: Callable[[int, Callable[[], None]], None]
    clear_timer: Callable[[], None]
    on_expire: Callable[[], None]
    stats: dict[str, int] = field(default_factory=dict)
    recent: list[str] = field(default_factory=list)
    window_start_ms: int | None = None
    used: int = 0
    preempts: int = 0
    current_score: float = 0.0
    current_text: str = ""
    showing: bool = False

    def _stat(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def _expire_window_if_needed(self, now: int) -> None:
        if self.window_start_ms is None:
            return
        if now - self.window_start_ms >= self.settings.budget_window_ms:
            self.window_start_ms = None
            self.used = 0
            self.preempts = 0

    def _is_dup(self, display_text: str) -> bool:
        norm = normalize_text(display_text)
        window = self.recent[-self.settings.dedup_window :]
        for prev in window:
            prev_n = normalize_text(prev)
            if prev_n == norm:
                return True
            if char_similarity(prev_n, norm) >= self.settings.dedup_threshold:
                return True
        return False

    def _arm_ttl(self) -> None:
        self.clear_timer()
        self.set_timer(self.settings.ttl_ms, self._on_ttl)

    def _on_ttl(self) -> None:
        if not self.showing:
            return
        self.showing = False
        self._stat("ttl_clear")
        self.on_expire()

    def consider(self, cand: Candidate) -> PolicyDecision:
        now = cand.ts_ms if cand.ts_ms is not None else self.now_ms()
        self._expire_window_if_needed(now)

        formatted, len_tag = format_length(
            cand.text, self.settings.max_chars, self.settings.one_line_chars
        )
        if formatted is None:
            self._stat("drop_length")
            return PolicyDecision("drop", "over_max_two_lines", "", False)

        if cand.confidence < self.settings.conf_hint:
            self._stat("drop_conf")
            return PolicyDecision("drop", "below_hint", "", False)

        if cand.confidence < self.settings.conf_full:
            tier = "hint"
            display = self.settings.hint_text
            action_if_ok: Action = "hint"
        else:
            tier = "full"
            display = formatted
            action_if_ok = "push"

        if self._is_dup(display):
            self._stat("drop_dedup")
            return PolicyDecision("drop", "dedup", display, False, tier)

        if self.window_start_ms is None:
            accept = True
            action: Action = action_if_ok
            consume = True
        elif self.used < self.settings.budget_max:
            accept = True
            action = action_if_ok
            consume = True
        elif (
            self.preempts < 1
            and cand.confidence >= self.current_score + self.settings.preempt_margin
        ):
            accept = True
            action = "preempt"
            consume = False
        else:
            self._stat("drop_budget")
            return PolicyDecision("drop", "budget", display, False, tier)

        if not accept:
            self._stat("drop_budget")
            return PolicyDecision("drop", "budget", display, False, tier)

        if self.window_start_ms is None:
            self.window_start_ms = now
        if consume:
            self.used += 1
        if action == "preempt":
            self.preempts += 1
            self._stat("preempt")
        elif action == "hint":
            self._stat("hint")
        else:
            self._stat("push")

        self.current_score = cand.confidence
        self.current_text = display
        self.showing = True
        self.recent.append(display)
        if len(self.recent) > self.settings.dedup_window:
            self.recent = self.recent[-self.settings.dedup_window :]
        self._arm_ttl()
        return PolicyDecision(action, len_tag, display, consume, tier)

    def fire_due_timers_for_tests(self) -> None:
        """No-op hook; FakeClock advances timers itself."""
        return
