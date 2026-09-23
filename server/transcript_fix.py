"""Deterministic ASR term repair. No LLM.

Runs after the aggregator and before the judge. Looks up tokens against
``server/terms_zh.json`` (plain strings or ``{term, variants}`` objects).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from difflib import SequenceMatcher

DEFAULT_FIX_SIMILARITY = 0.6
_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9/_.+-]*"  # REST, CI/CD, GraphQL
    r"|[\u4e00-\u9fff]+[A-Za-z0-9]*"  # 库布尔netes, 限流
    r"|[0-9]+[A-Za-z]+"  # 5xx-ish leftovers; usually unused
)


@dataclass(frozen=True)
class TermEntry:
    term: str
    variants: tuple[str, ...]

    def needles(self) -> tuple[str, ...]:
        seen: list[str] = []
        for raw in (self.term, *self.variants):
            t = raw.strip()
            if t and t not in seen:
                seen.append(t)
        return tuple(seen)


@dataclass(frozen=True)
class FixHit:
    original: str
    fixed: str
    term: str
    similarity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "fixed": self.fixed,
            "term": self.term,
            "similarity": round(self.similarity, 4),
        }


def _skeleton(text: str) -> str:
    """Lower alnum; drop vowels on Latin so Bocker≈Docker."""
    folded = "".join(c.casefold() for c in text if c.isalnum())
    if not folded or any("\u4e00" <= c <= "\u9fff" for c in folded):
        return folded
    vowels = set("aeiou")
    head = folded[0]
    tail = "".join(c for c in folded[1:] if c not in vowels)
    return head + tail


def similarity(a: str, b: str) -> float:
    """Best of casefold ratio and Latin consonant-skeleton ratio."""
    if not a or not b:
        return 0.0
    fa, fb = a.casefold(), b.casefold()
    if fa == fb:
        return 1.0
    ratios = [SequenceMatcher(None, fa, fb).ratio()]
    sa, sb = _skeleton(a), _skeleton(b)
    if sa and sb and (sa != fa or sb != fb):
        ratios.append(SequenceMatcher(None, sa, sb).ratio())
    return max(ratios)


def load_term_entries(path: Path) -> list[TermEntry]:
    raw_obj: object = json.loads(path.read_text(encoding="utf-8"))
    items: list[object]
    if isinstance(raw_obj, list):
        items = raw_obj
    elif isinstance(raw_obj, dict):
        inner = raw_obj.get("keyterm") or raw_obj.get("keyterms") or raw_obj.get("terms")
        items = list(inner) if isinstance(inner, list) else []
    else:
        items = []
    out: list[TermEntry] = []
    seen_term: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            term = str(item.get("term") or "").strip()
            variants = tuple(
                str(v).strip()
                for v in (item.get("variants") or [])
                if str(v).strip()
            )
        else:
            term = str(item).strip()
            variants = ()
        if not term or term.startswith("#") or term in seen_term:
            continue
        seen_term.add(term)
        out.append(TermEntry(term=term, variants=variants))
    return out


def flatten_keyterms(entries: list[TermEntry]) -> list[str]:
    """Plain strings for Deepgram ``keyterm`` (canonical + variants)."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for piece in entry.needles():
            if piece in seen:
                continue
            seen.add(piece)
            out.append(piece)
    return out


def _min_fuzzy_len(token: str) -> int:
    if any("\u4e00" <= c <= "\u9fff" for c in token):
        return 2
    return 3


def _best_match(
    token: str,
    entries: list[TermEntry],
    threshold: float,
) -> tuple[TermEntry, float] | None:
    exact_fold = token.casefold()
    canonical = {e.term.casefold() for e in entries}
    if exact_fold in canonical:
        return None
    # "鉴权吗" must not collapse to "鉴权"
    for canon in canonical:
        if exact_fold.startswith(canon) and len(exact_fold) > len(canon):
            return None
    best: tuple[TermEntry, float] | None = None
    tied = False
    for entry in entries:
        score = 0.0
        for needle in entry.needles():
            if token.casefold() == needle.casefold():
                return entry, 1.0
            score = max(score, similarity(token, needle))
        if score + 1e-9 < threshold:
            continue
        if best is None or score > best[1] + 1e-9:
            best = (entry, score)
            tied = False
        elif abs(score - best[1]) <= 1e-9 and entry.term != best[0].term:
            tied = True
    if tied:
        return None
    return best


def apply_fix(
    text: str,
    entries: list[TermEntry],
    *,
    threshold: float = DEFAULT_FIX_SIMILARITY,
) -> tuple[str, list[FixHit]]:
    """Replace ASR tokens that match a term or variant. Identity if no hits."""
    if not text or not entries:
        return text, []
    hits: list[FixHit] = []

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(token) < _min_fuzzy_len(token):
            # still allow exact variant hits (e.g. "线流")
            for entry in entries:
                for needle in entry.needles():
                    if token.casefold() == needle.casefold() and token != entry.term:
                        hits.append(FixHit(token, entry.term, entry.term, 1.0))
                        return entry.term
            return token
        found = _best_match(token, entries, threshold)
        if found is None:
            return token
        entry, score = found
        if token == entry.term:
            return token
        hits.append(FixHit(token, entry.term, entry.term, score))
        return entry.term

    fixed = _TOKEN_RE.sub(repl, text)
    return fixed, hits
