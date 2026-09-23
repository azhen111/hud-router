#!/usr/bin/env python3
"""Mic → Deepgram → turn aggregator → window → Phase 1 route() → terminal.

Does not modify Phase 1. Calls only router.route / public result fields.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from colorama import Fore, Style, init as colorama_init

_ASR_DIR: Path = Path(__file__).resolve().parent
_ROOT: Path = _ASR_DIR.parent
for _p in (str(_ROOT), str(_ASR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aggregator import AggregatedTurn, AggregatorConfig, FinalSegment, TurnAggregator
from settings import PipelineSettings, add_pipeline_args, load_settings
from stream import (
    default_input_index,
    fmt_session_ts,
    list_input_devices,
    load_dotenv,
    parse_deepgram_result,
    percentile,
    print_devices,
    require_api_key,
    resolve_device,
    run_mic_deepgram_session,
    ParsedAsrResult,
)
from window import TurnWindow

from router import RouterConfigError, RouterResult, route


def route_with_timeout(
    payload: dict[str, object],
    timeout_s: float,
) -> tuple[RouterResult | None, bool]:
    """Call route() on a worker thread. On timeout return (None, True). Never raises timeout."""
    box: list[RouterResult | BaseException] = []

    def work() -> None:
        try:
            box.append(route(payload))
        except RouterConfigError as exc:
            box.append(exc)
        except Exception as exc:
            box.append(exc)

    worker: threading.Thread = threading.Thread(target=work, name="route", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, True
    if not box:
        return None, True
    item: RouterResult | BaseException = box[0]
    if isinstance(item, RouterConfigError):
        raise item
    if isinstance(item, RouterResult):
        return item, False
    return None, False


class PipelineStats:
    def __init__(self) -> None:
        self.turns: int = 0
        self.triggers: int = 0
        self.timeouts: int = 0
        self.discarded_min_chars: int = 0
        self.segments: list[float] = []
        self.chars: list[float] = []
        self.e2e_ms: list[float] = []


def print_turn_line(rel_s: float, turn: AggregatedTurn) -> None:
    stamp: str = fmt_session_ts(rel_s)
    segs: int = turn.segments
    dur_s: float = turn.duration_ms / 1000.0
    print(
        f"{stamp} {Style.BRIGHT}TURN{Style.RESET_ALL} "
        f"({segs} segs, {dur_s:.1f}s)  {turn.text}"
    )


def print_router_line(
    rel_s: float,
    result: RouterResult | None,
    latency_ms: float,
    timed_out: bool,
) -> None:
    stamp: str = fmt_session_ts(rel_s)
    if timed_out:
        print(f"{stamp}   → {Fore.YELLOW}skip{Style.RESET_ALL}  reason: router timeout")
        return
    if result is None:
        print(f"{stamp}   → {Fore.YELLOW}skip{Style.RESET_ALL}  reason: router error")
        return
    if result.should_respond:
        print(
            f"{stamp}   → {Fore.GREEN}TRIGGER{Style.RESET_ALL}  "
            f"conf={result.confidence:.2f}  kind={result.kind}  lat={latency_ms:.0f}ms"
        )
        if result.answer:
            print(f"{stamp}     {result.answer}")
        print(f"{stamp}     reason: {result.reason}")
    else:
        print(
            f"{stamp}   → {Fore.YELLOW}skip{Style.RESET_ALL}  "
            f"conf={result.confidence:.2f}  reason: {result.reason}"
        )


def append_session_line(
    fp: TextIO,
    turn: AggregatedTurn,
    result: RouterResult | None,
    latency_ms: float,
    timed_out: bool,
) -> None:
    record: dict[str, object] = {
        "turn": turn.to_emit_dict(),
        "discarded": turn.discarded,
        "timeout": timed_out,
        "latency_ms": latency_ms,
        "router": result.to_dict() if result is not None else None,
    }
    fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    fp.flush()


def print_pipeline_summary(stats: PipelineStats, jsonl_path: Path, elapsed_s: float) -> None:
    print()
    print(f"{Style.BRIGHT}--- pipeline summary ---{Style.RESET_ALL}")
    print(f"total duration: {fmt_session_ts(elapsed_s).strip('[]')}")
    print(f"total turns: {stats.turns}")
    print(f"trigger count: {stats.triggers}")
    rate: float = (stats.triggers / stats.turns * 100.0) if stats.turns else 0.0
    print(f"trigger rate: {rate:.1f}%")
    print(f"router timeouts: {stats.timeouts}")
    print(f"discarded by AGG_MIN_CHARS: {stats.discarded_min_chars}")
    print(f"jsonl: {jsonl_path}")
    if stats.segments:
        print(
            f"segments per turn: p50={percentile(stats.segments, 50.0):.1f}  "
            f"p95={percentile(stats.segments, 95.0):.1f}  "
            f"max={max(stats.segments):.0f}"
        )
    else:
        print("segments per turn: n/a")
    if stats.chars:
        print(
            f"turn char counts: p50={percentile(stats.chars, 50.0):.1f}  "
            f"p95={percentile(stats.chars, 95.0):.1f}"
        )
    else:
        print("turn char counts: n/a")
    if stats.e2e_ms:
        print(
            f"e2e latency (turn end → router return): "
            f"p50={percentile(stats.e2e_ms, 50.0):.0f}ms  "
            f"p95={percentile(stats.e2e_ms, 95.0):.0f}ms"
        )
    else:
        print("e2e latency (turn end → router return): n/a")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mic → Deepgram → aggregator → window → Phase 1 router."
    )
    add_pipeline_args(parser)
    return parser.parse_args(argv)


def run_pipeline(settings: PipelineSettings, device: int | None) -> int:
    load_dotenv()
    dg_key: str = require_api_key()
    # Fail fast on missing router env (route() would raise on first turn otherwise).
    try:
        from router import get_settings as router_get_settings

        router_get_settings()
    except RouterConfigError as exc:
        print(f"asr/pipeline.py: {exc}", file=sys.stderr)
        return 2

    stamp: str = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path: Path = Path(f"session_{stamp}.jsonl")
    jsonl_fp: TextIO = jsonl_path.open("a", encoding="utf-8")

    agg: TurnAggregator = TurnAggregator(
        AggregatorConfig(
            silence_ms=settings.silence_ms,
            max_turn_ms=settings.max_turn_ms,
            min_chars=settings.min_chars,
            use_speech_final=settings.use_speech_final,
        )
    )
    window: TurnWindow = TurnWindow(max_turns=settings.window_turns, locale=settings.locale)
    stats: PipelineStats = PipelineStats()
    start_box: list[float] = [time.perf_counter()]
    halt: threading.Event = threading.Event()
    lock: threading.Lock = threading.Lock()
    emit_lock: threading.Lock = threading.Lock()
    timeout_s: float = settings.router_timeout_ms / 1000.0

    print(
        f"pipeline  lang={settings.lang} locale={settings.locale}  "
        f"silence={settings.silence_ms}ms max_turn={settings.max_turn_ms}ms "
        f"min_chars={settings.min_chars} speech_final={settings.use_speech_final}  "
        f"window={settings.window_turns} router_timeout={settings.router_timeout_ms}ms"
    )
    print(f"session jsonl={jsonl_path}")
    print("Ctrl-C to stop.\n")

    def now_rel() -> float:
        return time.perf_counter() - start_box[0]

    def handle_emitted(turns: list[AggregatedTurn]) -> None:
        with emit_lock:
            _handle_emitted_locked(turns)

    def _handle_emitted_locked(turns: list[AggregatedTurn]) -> None:
        for turn in turns:
            turn_end_rel: float = now_rel()
            print_turn_line(turn_end_rel, turn)
            if turn.discarded:
                print(
                    f"{fmt_session_ts(turn_end_rel)}   → {Fore.YELLOW}skip{Style.RESET_ALL}  "
                    "reason: AGG_MIN_CHARS"
                )
                append_session_line(jsonl_fp, turn, None, 0.0, False)
                continue
            stats.turns += 1
            stats.segments.append(float(turn.segments))
            stats.chars.append(float(len(turn.text)))
            payload: dict[str, object] = window.add(turn)
            t0: float = time.perf_counter()
            result: RouterResult | None
            timed_out: bool
            try:
                result, timed_out = route_with_timeout(payload, timeout_s)
            except RouterConfigError as exc:
                print(f"asr/pipeline.py: {exc}", file=sys.stderr)
                halt.set()
                return
            latency_ms: float = (time.perf_counter() - t0) * 1000.0
            if timed_out:
                stats.timeouts += 1
            if result is not None and result.should_respond:
                stats.triggers += 1
            if not timed_out:
                stats.e2e_ms.append(latency_ms)
            print_router_line(now_rel(), result, latency_ms, timed_out)
            append_session_line(jsonl_fp, turn, result, latency_ms, timed_out)

    def on_message(message: object) -> None:
        parsed: ParsedAsrResult | None = parse_deepgram_result(message)
        if parsed is None or not parsed.is_final:
            return
        recv_s: float = now_rel()
        seg: FinalSegment = FinalSegment(
            text=parsed.transcript,
            start_s=parsed.start_s,
            duration_s=parsed.duration_s,
            speech_final=parsed.speech_final,
            recv_s=recv_s,
        )
        with lock:
            emitted: list[AggregatedTurn] = agg.push(seg)
        handle_emitted(emitted)

    def ticker() -> None:
        while not halt.is_set():
            time.sleep(0.05)
            with lock:
                emitted = agg.tick(now_rel())
            if emitted:
                handle_emitted(emitted)

    tick_thread: threading.Thread = threading.Thread(target=ticker, name="agg-tick", daemon=True)
    tick_thread.start()

    try:
        run_mic_deepgram_session(
            api_key=dg_key,
            language=settings.lang,
            device=device,
            on_message=on_message,
            stop=halt,
            started_perf_out=start_box,
        )
    finally:
        halt.set()
        with lock:
            leftover: list[AggregatedTurn] = agg.flush(now_rel())
        handle_emitted(leftover)
        stats.discarded_min_chars = agg.discarded_min_chars
        print_pipeline_summary(stats, jsonl_path, now_rel())
        jsonl_fp.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    colorama_init()
    args: argparse.Namespace = parse_args(argv)
    settings: PipelineSettings = load_settings(args)
    try:
        devices: list[dict[str, Any]] = list_input_devices()
    except SystemExit:
        if args.list_devices:
            raise
        devices = []
    except Exception as exc:
        print(f"asr/pipeline.py: could not query devices: {exc}", file=sys.stderr)
        devices = []
    default_idx: int | None = default_input_index()
    print_devices(devices, default_idx)
    if args.list_devices:
        return 0
    device: int | None = resolve_device(settings.device, devices)
    if device is not None:
        print(f"Using input device index {device}")
    else:
        print("Using system default input device")
    return run_pipeline(settings, device)


if __name__ == "__main__":
    raise SystemExit(main())
