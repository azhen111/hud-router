#!/usr/bin/env python3
"""Run the HUD router against testcases.jsonl and print a scored report."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from router import (
    MAX_ANSWER_CHARS,
    RouterConfigError,
    RouterResult,
    answer_char_len,
    route,
)

RED: str = "\033[31m"
YELLOW: str = "\033[33m"
GREEN: str = "\033[32m"
BOLD: str = "\033[1m"
RESET: str = "\033[0m"

DEFAULT_CASES: Path = Path(__file__).resolve().parent / "testcases.jsonl"
BASE_TS: int = 1_700_000_000


@dataclass(frozen=True)
class TestCase:
    id: int
    locale: str
    turns: list[tuple[str, str]]
    expect: bool
    note: str
    needs_more_context: bool | None
    kind: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class CaseResult:
    case: TestCase
    result: RouterResult
    latency_ms: float
    correct: bool


def _color_enabled(stream: TextIO) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{code}{text}{RESET}"


def load_cases(path: Path) -> list[TestCase]:
    cases: list[TestCase] = []
    text: str = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        obj: Any = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{line_no}: expected object")
        raw_turns: object = obj.get("turns")
        if not isinstance(raw_turns, list):
            raise ValueError(f"{path}:{line_no}: turns must be a list")
        turns: list[tuple[str, str]] = []
        for item in raw_turns:
            if not isinstance(item, list) or len(item) < 2:
                raise ValueError(f"{path}:{line_no}: turn must be [speaker, text]")
            turns.append((str(item[0]), str(item[1])))
        nmc_raw: object = obj.get("needs_more_context", None)
        nmc: bool | None
        if nmc_raw is None:
            nmc = None
        else:
            nmc = bool(nmc_raw)
        kind_raw: object = obj.get("kind")
        cases.append(
            TestCase(
                id=int(obj["id"]),
                locale=str(obj.get("locale") or "ja"),
                turns=turns,
                expect=bool(obj["expect"]),
                note=str(obj.get("note") or ""),
                needs_more_context=nmc,
                kind=str(kind_raw) if kind_raw is not None else None,
                raw=obj,
            )
        )
    return cases


def case_to_payload(case: TestCase) -> dict[str, Any]:
    """Convert a jsonl case into the route() input contract.

    `ts` values are synthetic and strictly ascending.
    """
    recent_turns: list[dict[str, Any]] = []
    for i, (speaker, text) in enumerate(case.turns):
        recent_turns.append(
            {
                "speaker": speaker,
                "text": text,
                "ts": BASE_TS + i,
            }
        )
    return {
        "recent_turns": recent_turns,
        "locale": case.locale,
    }


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered: list[float] = sorted(float(v) for v in values)
    rank: float = (len(ordered) - 1) * (p / 100.0)
    lo: int = int(rank)
    hi: int = min(lo + 1, len(ordered) - 1)
    frac: float = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _last_text(case: TestCase) -> str:
    if not case.turns:
        return ""
    return case.turns[-1][1]


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _suppressed_over_length(result: RouterResult) -> bool:
    """True when an over-length answer was converted to none (observational)."""
    return result.truncated_to_none or result.over_length


def run_cases(cases: Sequence[TestCase]) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        payload: dict[str, Any] = case_to_payload(case)
        t0: float = time.perf_counter()
        result: RouterResult = route(payload)
        latency_ms: float = (time.perf_counter() - t0) * 1000.0
        correct: bool = result.should_respond is case.expect
        results.append(
            CaseResult(
                case=case,
                result=result,
                latency_ms=latency_ms,
                correct=correct,
            )
        )
    return results


def render_report(rows: Sequence[CaseResult], *, color: bool) -> str:
    n: int = len(rows)
    tp: int = sum(1 for r in rows if r.case.expect and r.result.should_respond)
    tn: int = sum(1 for r in rows if (not r.case.expect) and (not r.result.should_respond))
    fp: int = sum(1 for r in rows if (not r.case.expect) and r.result.should_respond)
    fn: int = sum(1 for r in rows if r.case.expect and (not r.result.should_respond))
    n_pos: int = tp + fn
    n_neg: int = tn + fp
    accuracy: float = (tp + tn) / n if n else 0.0
    fpr: float = fp / n_neg if n_neg else 0.0
    fnr: float = fn / n_pos if n_pos else 0.0
    suppressed_over_length_n: int = sum(
        1 for r in rows if _suppressed_over_length(r.result)
    )
    latencies: list[float] = [r.latency_ms for r in rows]
    p50: float = percentile(latencies, 50.0)
    p95: float = percentile(latencies, 95.0)
    mean_ms: float = statistics.fmean(latencies) if latencies else 0.0

    def ok(flag: bool) -> str:
        return paint("PASS", GREEN + BOLD, color) if flag else paint("FAIL", RED + BOLD, color)

    acc_ok: bool = accuracy > 0.80
    fp_ok: bool = fpr < 0.15
    lat_ok: bool = p95 < 2000.0

    fp_rows: list[CaseResult] = [
        r for r in rows if (not r.case.expect) and r.result.should_respond
    ]
    by_reason: dict[str, list[CaseResult]] = defaultdict(list)
    for r in fp_rows:
        by_reason[r.result.reason or "(empty reason)"].append(r)

    out: list[str] = []
    out.append(paint("HUD router evaluation", BOLD, color))
    out.append(f"cases: {n}   positives(expect=true): {n_pos}   negatives(expect=false): {n_neg}")
    out.append("")
    out.append(paint("Acceptance (PASS/FAIL gates only)", BOLD, color))
    out.append(
        f"  should_respond accuracy > 80% : {accuracy * 100:5.1f}%   {ok(acc_ok)}"
        f"   ({tp + tn}/{n})"
    )
    out.append(
        f"  false positive rate    < 15% : {fpr * 100:5.1f}%   {ok(fp_ok)}"
        f"   ({fp}/{n_neg})"
    )
    out.append(
        f"  latency p50 / p95  p95<2000 : {p50:6.1f} / {p95:6.1f} ms   {ok(lat_ok)}"
        f"   (mean {mean_ms:.1f} ms)"
    )
    out.append("")
    out.append(paint("Informational", BOLD, color))
    out.append(
        f"  false negative rate          : {fnr * 100:5.1f}%"
        f"   ({fn}/{n_pos})   (not a gate)"
    )
    out.append("")
    out.append(paint("False positives (MOST IMPORTANT)", BOLD, color))
    out.append(
        "  expect=false, got should_respond=true — a false HUD flash during conversation."
    )
    if not fp_rows:
        out.append("  none")
    else:
        out.append(f"  count: {fp}   grouped by model reason:")
        for reason, group in sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            out.append(paint(f"  [{len(group)}] {reason}", RED if color else "", color))
            for r in group:
                out.append(
                    f"       id={r.case.id}  locale={r.case.locale}  "
                    f"note={r.case.note!r}  last={_last_text(r.case)!r}"
                )
    out.append("")
    out.append(paint("False negatives", BOLD, color))
    fn_rows: list[CaseResult] = [
        r for r in rows if r.case.expect and (not r.result.should_respond)
    ]
    if not fn_rows:
        out.append("  none")
    else:
        for r in fn_rows:
            out.append(
                f"  id={r.case.id}  reason={r.result.reason!r}  "
                f"note={r.case.note!r}  last={_last_text(r.case)!r}"
            )
    out.append("")
    out.append(
        paint(
            "Observational: suppressed_over_length (not a gate)",
            YELLOW + BOLD,
            color,
        )
    )
    out.append(
        f"  count: {suppressed_over_length_n}   "
        "(over-length model answer converted to should_respond=false / none)"
    )
    if not suppressed_over_length_n:
        out.append("  none")
    else:
        for r in rows:
            if _suppressed_over_length(r.result):
                out.append(
                    f"  id={r.case.id}  kind={r.result.kind}  "
                    f"truncated_to_none={r.result.truncated_to_none}  "
                    f"answer={r.result.answer!r}  (limit {MAX_ANSWER_CHARS})"
                )
    out.append("")

    out.append(paint("Per-case comparison", BOLD, color))
    header: str = (
        f"{'id':>4}  {'exp':<5} {'got':<5} {'ok':<3} "
        f"{'conf':>4} {'kind':<12} {'nmc':<5} {'sol':<3} "
        f"{'ms':>7}  {'note':<22}  reason"
    )
    out.append(header)
    out.append("-" * 108)
    for r in rows:
        exp_s: str = "true" if r.case.expect else "false"
        got_s: str = "true" if r.result.should_respond else "false"
        mark: str = " " if r.correct else "X"
        line: str = (
            f"{r.case.id:4d}  {exp_s:<5} {got_s:<5} {mark:<3} "
            f"{r.result.confidence:4.2f} {r.result.kind:<12} "
            f"{str(r.result.needs_more_context):<5} "
            f"{'Y' if _suppressed_over_length(r.result) else '':<3} "
            f"{r.latency_ms:7.1f}  {_clip(r.case.note, 22):<22}  "
            f"{r.result.reason}"
        )
        if not r.correct:
            line = paint(line, RED, color)
        elif _suppressed_over_length(r.result):
            line = paint(line, YELLOW, color)
        out.append(line)
        extra: list[str] = []
        if r.result.answer:
            extra.append(f"answer={r.result.answer!r} ({answer_char_len(r.result.answer)}ch)")
        if r.case.kind is not None and r.result.kind != r.case.kind and r.result.should_respond:
            extra.append(f"kind_expect={r.case.kind}")
        if (
            r.case.needs_more_context is not None
            and r.result.needs_more_context != r.case.needs_more_context
        ):
            extra.append(f"nmc_expect={r.case.needs_more_context}")
        if extra:
            pad: str = " " * 4
            detail: str = pad + "  ".join(extra)
            if not r.correct:
                detail = paint(detail, RED, color)
            out.append(detail)
    out.append("")
    out.append(
        "Notes: expected `should_respond` values are not modified. "
        "`kind` / `needs_more_context` on a case are informational. "
        "`sol` = suppressed_over_length (observational, not an acceptance gate)."
    )
    return "\n".join(out) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES,
        help="Path to testcases.jsonl (default: ./testcases.jsonl)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI highlighting of wrong rows",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = parse_args(argv)
    color: bool = (not args.no_color) and _color_enabled(sys.stdout)
    cases_path: Path = args.cases
    if not cases_path.is_file():
        print(f"evaluate.py: cases file not found: {cases_path}", file=sys.stderr)
        return 2
    cases: list[TestCase] = load_cases(cases_path)
    try:
        rows: list[CaseResult] = run_cases(cases)
    except RouterConfigError as exc:
        print(f"evaluate.py: {exc}", file=sys.stderr)
        print(
            "Live eval needs OPENAI_API_KEY, ROUTER_MODEL, and optionally "
            "OPENAI_BASE_URL (see .env.example).",
            file=sys.stderr,
        )
        return 2
    sys.stdout.write(render_report(rows, color=color))
    # Non-zero only when the three acceptance gates fail.
    n: int = len(rows)
    tp_tn: int = sum(1 for r in rows if r.correct)
    accuracy: float = tp_tn / n if n else 0.0
    n_neg: int = sum(1 for r in rows if not r.case.expect)
    fp: int = sum(1 for r in rows if (not r.case.expect) and r.result.should_respond)
    fpr: float = fp / n_neg if n_neg else 0.0
    p95: float = percentile([r.latency_ms for r in rows], 95.0)
    failed: bool = (accuracy <= 0.80) or (fpr >= 0.15) or (p95 >= 2000.0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
