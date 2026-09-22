# asr — Deepgram streaming + turn pipeline (Phase 2)

## Milestone 1 — observation (`asr/stream.py`)

Microphone → Deepgram WebSocket streaming ASR → colorized terminal + raw jsonl.

Does **not** import Phase 1 routing. Still the M1 CLI:

```bash
python asr/stream.py
python asr/stream.py --list-devices
python asr/stream.py --device 1 --lang zh
```

## Milestone 2 — turns + router (`asr/pipeline.py`)

Mic → Deepgram → `aggregator` → `window` → Phase 1 `route()` → terminal + `session_*.jsonl`.

Speaker is always `UNKNOWN` (no diarization). No glasses/BLE/frontend, no RAG, no display dedupe.

```bash
python asr/pipeline.py
python asr/pipeline.py --lang zh --silence-ms 800 --window-turns 6
python asr/pipeline.py --config asr/config.example.json
```

Needs `DEEPGRAM_API_KEY` plus Phase 1 `OPENAI_API_KEY` / `ROUTER_MODEL` / optional `OPENAI_BASE_URL`.

### Terminal

```
[00:12.30] TURN (2 segs, 2.1s)  这个接口保证幂等吗
[00:13.45]   → TRIGGER  conf=0.90  kind=term  lat=980ms
[00:13.45]     幂等：多次执行结果相同的性质
[00:13.45]     reason: direct question about API property

[00:18.02] TURN (1 seg, 0.8s)  嗯嗯明白了
[00:18.90]   → skip  conf=0.95  reason: backchannel
```

Each kept turn is one JSON line in `session_<YYYYMMDD_HHMMSS>.jsonl`: turn fields + full router `to_dict()` + `latency_ms` + `timeout`. There is **no** cache/short-circuit on previous router results.

`ROUTER_TIMEOUT_MS` (default 1800): if `route()` exceeds this, the turn is skipped, counted as a timeout, and the pipeline keeps running.

e2e latency = wall time from turn close until `route()` returns (or timeout).

## Parameters

Override order: **CLI flag > environment variable > `--config` JSON > default**.

| Name | Default | CLI | Env | Meaning |
| --- | --- | --- | --- | --- |
| `AGG_SILENCE_MS` | 800 | `--silence-ms` | `AGG_SILENCE_MS` | Silence after last FINAL ends the turn (from last FINAL audio-end / recv) |
| `AGG_MAX_TURN_MS` | 15000 | `--max-turn-ms` | `AGG_MAX_TURN_MS` | Force-close if the open turn grows this long |
| `AGG_MIN_CHARS` | 4 | `--min-chars` | `AGG_MIN_CHARS` | Shorter turns are discarded (not sent to the router) |
| `AGG_USE_SPEECH_FINAL` | true | `--use-speech-final` / `--no-speech-final` | `AGG_USE_SPEECH_FINAL` | If true, `speech_final=true` **or** silence timeout ends a turn |
| `WINDOW_TURNS` | 6 | `--window-turns` | `WINDOW_TURNS` | Rolling last-N turns in the `route()` payload |
| `ROUTER_TIMEOUT_MS` | 1800 | `--router-timeout-ms` | `ROUTER_TIMEOUT_MS` | Skip the turn if the router exceeds this (no crash) |
| language | `zh` | `--lang` | `ASR_LANG` | Deepgram language (`zh`, later `ja`) |
| locale | from lang | `--locale` | `ASR_LOCALE` | Phase 1 `locale` field |
| device | system default | `--device` | `ASR_DEVICE` | Mic index or name substring |

Example `asr/config.example.json`:

```json
{
  "AGG_SILENCE_MS": 800,
  "AGG_MAX_TURN_MS": 15000,
  "AGG_MIN_CHARS": 4,
  "AGG_USE_SPEECH_FINAL": true,
  "WINDOW_TURNS": 6,
  "ROUTER_TIMEOUT_MS": 1800,
  "lang": "zh",
  "locale": "zh"
}
```

## Tests

```bash
python -m unittest discover -s asr/tests -v
```

Aggregator only (constructed FINAL sequences; no mic): silence, max-turn, min-chars, speech_final.

## Deepgram model (M1/M2 share `nova-3`)

**`nova-3`** — current general-purpose streaming ASR (not Flux). Supports `zh` and `ja`. Connect sets `model`, `language`, `encoding=linear16`, `sample_rate=16000`, `channels=1`, `interim_results=true`, `punctuate=true`. **Endpointing is not tuned.**

`DeepgramPcmSession` in `stream.py` is the same listen options without a mic (PCM bytes in). `server/live.py` uses it. Handshake wait is 60s. M1/M2 CLI (`run_mic_deepgram_session`) is unchanged.

## Install

```bash
pip install -r requirements.txt
```

`deepgram-sdk`, `sounddevice`, `colorama`, plus Phase 1 `openai`. Windows `sounddevice` wheels include PortAudio; on Linux install `libportaudio2`.

```bash
export DEEPGRAM_API_KEY=your-key
export OPENAI_API_KEY=your-key
export ROUTER_MODEL=your-model
```

## M1 terminal / jsonl (unchanged)

```
[00:03.42] INTERIM  这个接口保证幂
[00:04.18] FINAL    这个接口保证幂等吗
[00:04.18]   ^ speech_final=true  duration=1.85s  confidence=0.92
```

Raw Deepgram messages go to `transcript_<YYYYMMDD_HHMMSS>.jsonl`.

M1 ASR latency: `FINAL_recv − (session_start + Deepgram.start + duration)`.
