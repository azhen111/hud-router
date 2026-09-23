# Live ASR 加固 — 戴镜对照清单

本环境不能戴 G2，**不要编造**识别结果。下面给佩戴者录 `live_*.jsonl`。

词表 / keyterm：`server/terms_zh.json`。朗读稿：`server/fixtures/it_questions_20.txt`（20 道 IT 问句）。

## 共同条件

- 同一房间、同一距离、同一语速。
- 插件连 `ws://<LAN>:8766`，开麦后再读。
- 需要：`DEEPGRAM_API_KEY`、`OPENAI_API_KEY`、`ROUTER_MODEL`。
- 默认加固：keyterm 开、`--silence-ms 1200`、`--min-route-chars 3`（命中词表豁免短句）、router timeout **3000ms**（启动横幅 `router_timeout_ms=3000`）、**policy 开**。Self 发话也会进 `route()`。
- A/B：`--lang multi`（Deepgram `language=multi` + 同一 keyterm；router locale 仍 zh）。官方 `multi` 码切换列表不含中文，见 `server/README.md`。
- A/B：`--no-policy` 关闭显示策略。

## Before（对照）

```bash
python server/live.py --lang zh --no-keyterms --log live_before_zh.jsonl
```

把 `it_questions_20.txt` 读完。Ctrl-C。

## After（加固）

```bash
python server/live.py --lang zh --log live_after_zh.jsonl
```

同样 20 句。启动日志应有一行 `keyterms=N from ... (nova-3 uses keyterm, not keywords)`。

## zh vs multi（同一加固设置）

```bash
python server/live.py --lang zh --log live_zh.jsonl
# 读 20 句，停
python server/live.py --lang multi --log live_multi.jsonl
# 再读同一 20 句
```

## 看 jsonl

- `kind=final`：原始 FINAL / `speech_final`。
- `kind=turn`：`raw_finals` + `aggregated_text` + `router` / `skip_reason` / **`policy`**。
- `kind=policy_clear`：TTL 到期推 `・`。
- 过短：`skip_reason=too_short`（且 `error` 写明 `N < min; no keyterm hit`），`pushed=false`，没有 OpenAI 调用。命中 `terms_zh.json` 的短句不走这条。
- 策略拒绝：`skip_reason=policy_<reason>`，`policy.reason` 为 `below_hint` / `budget` / `dedup` / `over_max_two_lines`。

对比 before/after 或 zh/multi 时，只比较这 20 句对应的 `turn` 行，不要事后改 prompt 或 testcases。
