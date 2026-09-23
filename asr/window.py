"""Rolling last-N turns → Phase 1 route() payload. Speaker stays UNKNOWN."""

from __future__ import annotations

from settings import DEFAULT_LOCALE, DEFAULT_WINDOW_TURNS

from aggregator import AggregatedTurn


class TurnWindow:
    """Keep at most `max_turns` completed (non-discarded) turns, oldest first."""

    def __init__(self, max_turns: int = DEFAULT_WINDOW_TURNS, locale: str = DEFAULT_LOCALE) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self.max_turns: int = max_turns
        self.locale: str = locale
        self._turns: list[dict[str, object]] = []

    def add(self, turn: AggregatedTurn) -> dict[str, object]:
        """Append a kept turn and return the current router payload."""
        item: dict[str, object] = {
            "speaker": "UNKNOWN",
            "text": turn.text,
            "ts": turn.ts,
        }
        self._turns.append(item)
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns :]
        return self.payload()

    def payload(self) -> dict[str, object]:
        """Phase 1 input contract: recent_turns / locale / wearer_note."""
        return {
            "recent_turns": [dict(t) for t in self._turns],
            "locale": self.locale,
            "wearer_note": None,
        }
