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

DEFAULT_FIX_SIMILARITY = 0.85  # unused for matching; replacements are exact variants only
# Latin / dotted terms only. CJK and mixed (库布尔netes) are exact-needle
# replacements so "前端中FLCK" does not become one token.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9/_.+-]*")


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
    """Canon + variants for the fix layer / short-filter. Not for Deepgram."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for piece in entry.needles():
            if piece in seen:
                continue
            seen.add(piece)
            out.append(piece)
    return out


def _lookup_tables(
    entries: list[TermEntry],
) -> tuple[dict[str, TermEntry], dict[str, TermEntry]]:
    """casefold variant → entry; casefold canonical → entry."""
    variants: dict[str, TermEntry] = {}
    canons: dict[str, TermEntry] = {}
    for entry in entries:
        canons[entry.term.casefold()] = entry
        for raw in entry.variants:
            key = raw.casefold()
            if key and key not in canons:
                variants[key] = entry
    return variants, canons


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def _apply_cjk_variants(
    text: str,
    entries: list[TermEntry],
    hits: list[FixHit],
) -> str:
    """Exact replace of CJK / mixed variants. Longest needle first.

    Handles 线流→限流 and 库布尔netes→Kubernetes before Latin tokenization
    so a Chinese prefix cannot swallow FLCK / JVA / GraphQL.
    """
    needles: list[tuple[str, TermEntry]] = []
    for entry in entries:
        for needle in entry.needles():
            if needle == entry.term or not _has_cjk(needle):
                continue
            needles.append((needle, entry))
    needles.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    spans: list[tuple[int, int, TermEntry, str]] = []
    for needle, entry in needles:
        start = 0
        while True:
            idx = text.find(needle, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(needle), entry, needle))
            start = idx + len(needle)
    if not spans:
        return text
    spans.sort(key=lambda row: (row[0], -(row[1] - row[0])))
    used = [False] * len(text)
    kept: list[tuple[int, int, TermEntry, str]] = []
    for start, end, entry, needle in spans:
        if any(used[i] for i in range(start, end)):
            continue
        for i in range(start, end):
            used[i] = True
        kept.append((start, end, entry, needle))
    kept.sort(key=lambda row: row[0])
    out: list[str] = []
    cursor = 0
    for start, end, entry, needle in kept:
        out.append(text[cursor:start])
        out.append(entry.term)
        hits.append(FixHit(needle, entry.term, entry.term, 1.0))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def apply_fix(
    text: str,
    entries: list[TermEntry],
    *,
    threshold: float = DEFAULT_FIX_SIMILARITY,
) -> tuple[str, list[FixHit]]:
    """Replace tokens that match an explicit variant (or canonical casing).

    No edit-distance / fuzzy match. ``threshold`` is accepted for callers
    but is not used.
    """
    del threshold
    if not text or not entries:
        return text, []
    hits: list[FixHit] = []
    variant_map, canon_map = _lookup_tables(entries)

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        key = token.casefold()
        if key in variant_map:
            entry = variant_map[key]
            if token != entry.term:
                hits.append(FixHit(token, entry.term, entry.term, 1.0))
                return entry.term
            return token
        if key in canon_map and token != canon_map[key].term:
            entry = canon_map[key]
            hits.append(FixHit(token, entry.term, entry.term, 1.0))
            return entry.term
        return token

    fixed = _apply_cjk_variants(text, entries, hits)
    fixed = _TOKEN_RE.sub(repl, fixed)
    return fixed, hits
