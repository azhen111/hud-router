"""Deterministic transcript_fix. No LLM, no glasses."""

from __future__ import annotations

import unittest
from pathlib import Path

from server.live import DEFAULT_TERMS_PATH, build_settings, parse_args
from server.transcript_fix import (
    DEFAULT_FIX_SIMILARITY,
    apply_fix,
    load_term_entries,
    similarity,
)

SAMPLES = [
    ("FLCK", "Flex"),
    ("JWA", "JWT"),
    ("CFCAR", "Kafka"),
    ("库布尔netes", "Kubernetes"),
    ("GrafficQL", "GraphQL"),
    ("线流", "限流"),
    ("回拱", "回滚"),
    ("Bocker", "Docker"),
    ("pud", "Pod"),
    ("postgress", "PostgreSQL"),
]


class LoadEntries(unittest.TestCase):
    def test_mixed_schema(self) -> None:
        entries = load_term_entries(DEFAULT_TERMS_PATH)
        terms = {e.term for e in entries}
        self.assertIn("JWT", terms)
        self.assertIn("Flex", terms)
        jwt = next(e for e in entries if e.term == "JWT")
        self.assertIn("GWT", jwt.variants)
        self.assertIn("JWA", jwt.variants)
        self.assertGreaterEqual(len(entries), 40)


class ExactAndFuzzy(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = load_term_entries(DEFAULT_TERMS_PATH)

    def test_exact_variant_hit(self) -> None:
        fixed, hits = apply_fix("这个用 GWT 鉴权吗", self.entries)
        self.assertEqual(fixed, "这个用 JWT 鉴权吗")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].term, "JWT")
        self.assertEqual(hits[0].similarity, 1.0)

    def test_similarity_threshold_blocks_weak(self) -> None:
        fixed, hits = apply_fix("hello", self.entries, threshold=0.99)
        self.assertEqual(fixed, "hello")
        self.assertEqual(hits, [])

    def test_canonical_left_alone(self) -> None:
        fixed, hits = apply_fix("JWT 是什么", self.entries)
        self.assertEqual(fixed, "JWT 是什么")
        self.assertEqual(hits, [])

    def test_ten_known_mistranscribes(self) -> None:
        missed: list[str] = []
        for raw, want in SAMPLES:
            fixed, hits = apply_fix(raw, self.entries, threshold=DEFAULT_FIX_SIMILARITY)
            if fixed != want:
                missed.append(f"{raw!r} → {fixed!r} want {want!r} hits={hits}")
        self.assertEqual(missed, [], "fix layer should correct the documented sample set")

    def test_in_sentence(self) -> None:
        fixed, hits = apply_fix("Bocker 和 pud 怎么配", self.entries)
        self.assertEqual(fixed, "Docker 和 Pod 怎么配")
        self.assertEqual({h.term for h in hits}, {"Docker", "Pod"})


class NoFixFlag(unittest.TestCase):
    def test_no_fix_settings(self) -> None:
        settings = build_settings(parse_args(["--no-fix", "--log", "/tmp/live_nofix.jsonl"]))
        self.assertFalse(settings.fix_enabled)
        on = build_settings(parse_args(["--log", "/tmp/live_fixon.jsonl"]))
        self.assertTrue(on.fix_enabled)
        self.assertAlmostEqual(on.fix_similarity, 0.6)


class SimilarityHelper(unittest.TestCase):
    def test_bocker_docker(self) -> None:
        self.assertGreaterEqual(similarity("Bocker", "Docker"), 0.6)


if __name__ == "__main__":
    unittest.main()
