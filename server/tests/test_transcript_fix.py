"""Deterministic transcript_fix. No LLM, no glasses."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from server.live import DEFAULT_TERMS_PATH, build_settings, parse_args
from server.transcript_fix import (
    DEFAULT_FIX_SIMILARITY,
    apply_fix,
    flatten_keyterms,
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
    ("WE3", "Vue3"),
    ("Spingboot", "Spring Boot"),
]

SESSION_SAMPLES_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "asr_mistranscribe_samples.json"
)


class LoadEntries(unittest.TestCase):
    def test_mixed_schema(self) -> None:
        entries = load_term_entries(DEFAULT_TERMS_PATH)
        terms = {e.term for e in entries}
        self.assertIn("JWT", terms)
        self.assertIn("Flex", terms)
        jwt = next(e for e in entries if e.term == "JWT")
        self.assertIn("GWT", jwt.variants)
        self.assertIn("JWA", jwt.variants)
        self.assertIn("Jva", jwt.variants)
        terms_needed = {"Vue", "Vue2", "Vue3", "React", "Angular", "Java", "Spring", "Spring Boot"}
        self.assertTrue(terms_needed <= terms)
        vue3 = next(e for e in entries if e.term == "Vue3")
        self.assertIn("WE3", vue3.variants)
        java = next(e for e in entries if e.term == "Java")
        self.assertIn("扎瓦", java.variants)
        boot = next(e for e in entries if e.term == "Spring Boot")
        self.assertIn("Spingboot", boot.variants)
        self.assertIn("SRINBU", boot.variants)
        grpc = next(e for e in entries if e.term == "gRPC")
        self.assertIn("GRPC", grpc.variants)
        self.assertGreaterEqual(len(entries), 40)
        flat = flatten_keyterms(entries)
        self.assertIn("GWT", flat)
        self.assertIn("JWT", flat)


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

    def test_we3_and_spingboot(self) -> None:
        vue, hits = apply_fix("WE3的实现原理是什么", self.entries)
        self.assertEqual(vue, "Vue3的实现原理是什么")
        self.assertEqual([h.term for h in hits], ["Vue3"])
        boot, hits2 = apply_fix("Spingboot框架你使用过吗", self.entries)
        self.assertEqual(boot, "Spring Boot框架你使用过吗")
        self.assertEqual([h.term for h in hits2], ["Spring Boot"])
        phrase, hits3 = apply_fix("Sping Boot 和 React", self.entries)
        self.assertEqual(phrase, "Spring Boot 和 React")
        self.assertIn("Spring Boot", {h.term for h in hits3})

    def test_jwa_with_spring_not_forced_to_jwt(self) -> None:
        keep, hits = apply_fix("JWA中的SpringBoot框架", self.entries)
        self.assertEqual(keep, "JWA中的Spring Boot框架")
        self.assertNotIn("JWT", {h.term for h in hits})
        self.assertNotIn("JWT", keep)
        srin, hits2 = apply_fix("JWA中的SRINBU的框架", self.entries)
        self.assertEqual(srin, "JWA中的Spring Boot的框架")
        self.assertNotIn("JWT", srin)
        java, hits3 = apply_fix("扎瓦中的Spring框架", self.entries)
        self.assertEqual(java, "Java中的Spring框架")
        self.assertEqual([h.term for h in hits3], ["Java"])
        # Isolated JWA is still JWT (no Java/Spring stack nearby).
        lone, hits4 = apply_fix("JWA有哪些特性", self.entries)
        self.assertEqual(lone, "JWT有哪些特性")
        self.assertEqual([h.term for h in hits4], ["JWT"])

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

    def test_cjk_particle_not_swallowed(self) -> None:
        fixed, hits = apply_fix("和GrafficQL怎么选", self.entries)
        self.assertEqual(fixed, "和GraphQL怎么选")
        self.assertEqual([h.term for h in hits], ["GraphQL"])

    def test_cjk_variant_inside_sentence(self) -> None:
        fixed, hits = apply_fix("熔断和线流有什么区别", self.entries)
        self.assertEqual(fixed, "熔断和限流有什么区别")
        self.assertEqual([h.term for h in hits], ["限流"])
        rolled, hits2 = apply_fix("灰度发布怎么回拱", self.entries)
        self.assertEqual(rolled, "灰度发布怎么回滚")
        self.assertEqual([h.term for h in hits2], ["回滚"])

    def test_grpc_casing_and_htl(self) -> None:
        fixed, hits = apply_fix("GRPC和HTL", self.entries)
        self.assertEqual(fixed, "gRPC和HTTP")
        self.assertEqual({h.term for h in hits}, {"gRPC", "HTTP"})

    def test_session_samples_file(self) -> None:
        rows = json.loads(SESSION_SAMPLES_PATH.read_text(encoding="utf-8"))
        missed: list[str] = []
        garbage_ok = 0
        for row in rows:
            raw = row["text"]
            expect = row.get("expect")
            fixed, _hits = apply_fix(raw, self.entries, threshold=DEFAULT_FIX_SIMILARITY)
            if expect is None:
                if fixed != raw:
                    missed.append(f"garbage {raw!r} changed to {fixed!r}")
                else:
                    garbage_ok += 1
            elif fixed != expect:
                missed.append(f"{raw!r} → {fixed!r} want {expect!r}")
        self.assertEqual(missed, [])
        self.assertEqual(garbage_ok, 2)


class NoFixFlag(unittest.TestCase):
    def test_no_fix_settings(self) -> None:
        settings = build_settings(parse_args(["--no-fix", "--log", "/tmp/live_nofix.jsonl"]))
        self.assertFalse(settings.fix_enabled)
        on = build_settings(parse_args(["--log", "/tmp/live_fixon.jsonl"]))
        self.assertTrue(on.fix_enabled)
        self.assertAlmostEqual(on.fix_similarity, 0.85)


class SimilarityHelper(unittest.TestCase):
    def test_bocker_docker(self) -> None:
        self.assertGreaterEqual(similarity("Bocker", "Docker"), 0.6)

    def test_pdf_does_not_become_pod(self) -> None:
        entries = load_term_entries(DEFAULT_TERMS_PATH)
        fixed, hits = apply_fix("PDF 里的 Pod 怎么配", entries)
        self.assertEqual(fixed, "PDF 里的 Pod 怎么配")
        self.assertEqual(hits, [])
        near, hits2 = apply_fix("Pud 启动失败", entries)
        self.assertEqual(near, "Pod 启动失败")
        self.assertEqual([h.term for h in hits2], ["Pod"])

    def test_no_fuzzy_unlisted_token(self) -> None:
        entries = load_term_entries(DEFAULT_TERMS_PATH)
        fixed, hits = apply_fix("Bockerx 不是词表", entries)
        self.assertEqual(fixed, "Bockerx 不是词表")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
