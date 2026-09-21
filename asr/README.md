# asr — Deepgram streaming observation (Phase 2 / Milestone 1)

Microphone → Deepgram WebSocket streaming ASR → colorized terminal + raw jsonl.

This directory is an **observation tool only**. It does not import Phase 1 routing (`router.py`, `prompts.py`, `cli.py`, `evaluate.py`) and does not aggregate dialog turns.

## Deepgram model

**`nova-3`** (same string as the `model=` query parameter).

Why this name:

- Deepgram’s current **general-purpose** streaming ASR. No built-in turn detection (that is Flux). Milestone 1 is observation, so we want transcription, not agent turn-taking.
- Officially supports Mandarin Simplified as `zh` / `zh-CN` / `zh-Hans`, and Japanese as `ja` (so `--lang ja` later does not require a model change).
- `nova-2` / `nova-2-general` also speak `zh`, but `nova-3` is the current recommended general streaming model for meetings / captioning / far-field audio.
- Flux (`flux-general-en` / `flux-general-multi`) is for voice agents and does not list Mandarin `zh`.

Connect parameters we set: `model`, `language`, `encoding=linear16`, `sample_rate=16000`, `channels=1`, `interim_results=true`, `punctuate=true`. Everything else is left at Deepgram defaults — **endpointing is not tuned**.

## Install

Python 3.11+. Windows-friendly (`colorama` for ANSI; `sounddevice` wheels bundle PortAudio on Windows).

From the repo root:

```bash
pip install -r requirements.txt
```

Dependencies added for this tool: `deepgram-sdk`, `sounddevice`, `colorama`.

On Windows the `sounddevice` wheel includes PortAudio. On Linux install `libportaudio2` first.

Set the key (never hardcode):

```bash
# PowerShell
$env:DEEPGRAM_API_KEY = "your-key"

# cmd
set DEEPGRAM_API_KEY=your-key

# bash / git-bash
export DEEPGRAM_API_KEY=your-key
```

A `.env` file with `DEEPGRAM_API_KEY=` is also loaded if present (does not overwrite a real env var).

## Run

From the repo root, default system mic, Chinese:

```bash
python asr/stream.py
```

Useful flags:

```bash
python asr/stream.py --list-devices
python asr/stream.py --device 1
python asr/stream.py --device "Microphone"
python asr/stream.py --lang zh
python asr/stream.py --lang ja
```

On startup the tool always prints available **input** devices (and marks the default). Audio is **16000 Hz, mono, 16-bit PCM** (Even G2 aligned).

## Terminal lines

```
[00:03.42] INTERIM  这个接口保证幂
[00:04.18] FINAL    这个接口保证幂等吗
[00:04.18]   ^ speech_final=true  duration=1.85s  confidence=0.92
```

- Timestamp is **session wall time** since the mic stream started (`[mm:ss.ms]`).
- INTERIM = yellow, FINAL = green, meta = cyan (`colorama`).
- Every Deepgram WebSocket message is appended **unfiltered** as one JSON line to `transcript_<YYYYMMDD_HHMMSS>.jsonl` in the current working directory.

## Ctrl-C summary

- Total session duration
- FINAL segment count
- Gaps between adjacent FINAL **receive** times: p50 / p95
- FINAL transcript character counts: p50 / p95 / max
- Fraction of FINALs with `speech_final=true`
- Latency **audio-end → FINAL receive**: p50 / p95

Latency definition (also commented in `stream.py`):

`latency = t_final_recv − (session_start + Deepgram.start + Deepgram.duration)`

`start` and `duration` are Deepgram’s audio-clock fields (seconds from the first byte we sent). Because the mic is streamed in real time, that equals “when that utterance’s last sample was captured.” The remainder is Deepgram’s **default** endpointing plus network and decode. We do not set `endpointing` / `utterance_end_ms` / `vad_events`.
