# hud-router (Phase 1)

Conversation-question router for a heads-up display on smart glasses (Even Realities G2).

It takes an already-transcribed rolling window and decides whether to show the wearer a short answer. Input is text. Output is a structured decision. There is no audio, hardware, retrieval, UI, or speaker diarization.

One model call returns the full JSON. If the model emits illegal JSON, the router degrades to `should_respond=false` and does not raise.

## Requirements

- Python 3.11+
- An OpenAI-compatible chat-completions endpoint

```bash
pip install -r requirements.txt
```

The only external dependency is the official `openai` client (any compatible `base_url` works).

## Environment

Copy `.env.example` to `.env` or export the same variables. Nothing is hardcoded.

| Variable           | Required | Meaning                                              |
| ------------------ | -------- | ---------------------------------------------------- |
| `OPENAI_API_KEY`   | yes      | API key for the compatible endpoint                  |
| `ROUTER_MODEL`     | yes      | Model name (provider-specific)                       |
| `OPENAI_BASE_URL`  | no       | Override the default OpenAI URL for a proxy / local  |

`router.py` loads `.env` if present, without overwriting variables already in the environment.

## Input (`route(payload)`)

```json
{
  "recent_turns": [
    {"speaker": "OTHER", "text": "...", "ts": 1699999990},
    {"speaker": "SELF",  "text": "...", "ts": 1699999995},
    {"speaker": "UNKNOWN", "text": "...", "ts": 1700000000}
  ],
  "locale": "ja",
  "wearer_note": "optional one-liner"
}
```

- At most the last 6 turns are kept, sorted ascending by `ts`.
- Only the last turn is judged; earlier turns are context.
- `speaker` is `OTHER` / `SELF` / `UNKNOWN` (unknown labels become `UNKNOWN`).
- Missing punctuation and ASR noise are expected.

## Output

```json
{
  "should_respond": true,
  "confidence": 0.82,
  "kind": "answer",
  "reason": "...",
  "answer": "...",
  "needs_more_context": false
}
```

- `kind`: `answer` | `term` | `number` | `translation` | `none`
- `answer` is empty when `should_respond` is false
- Non-empty answers are at most 40 characters (full-width = 1). Over-length answers are truncated for HUD safety and counted as failures by `evaluate.py`
- `needs_more_context: true` forces `should_respond: false`

## Run the CLI

```bash
python cli.py <<'EOF'
{
  "recent_turns": [
    {"speaker": "UNKNOWN", "text": "CAGRって何の略ですか", "ts": 1700000000}
  ],
  "locale": "ja"
}
EOF
```

Reads one JSON object from stdin, prints one JSON object to stdout.

## Run the evaluation

```bash
python evaluate.py
python evaluate.py --no-color
python evaluate.py --cases testcases.jsonl
```

`testcases.jsonl` is the frozen Phase 1 set. Expected values are not edited to improve scores.

The report includes:

- Overall `should_respond` accuracy vs `expect`
- False-positive rate (expect false, got true), grouped by model `reason`
- False-negative rate
- Over-length answer count
- Latency p50 / p95 in milliseconds
- Per-case table (wrong rows in red)

Acceptance targets (when a live model is configured):

- False-positive rate &lt; 15%
- `should_respond` accuracy &gt; 80%
- Over-length count = 0
- p95 latency &lt; 2000 ms
