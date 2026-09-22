# server — Phase 3 live 通路 + 显示策略

## Phase 3 — live 通路（ASR 加固）

`server/live.py`：眼镜 PCM 上行 → Deepgram nova-3（`keyterm`）→ `asr/aggregator.py`（默认静音 800ms）→ 短句过滤 → `route()` → `{"text": answer}` 下行上镜。

**不做：** display_policy、RAG。`router.py` / `prompts.py` / `testcases*` 不动。

**不要和** `glasses/display_server.py` **抢同一端口**（默认都是 8766）。

### nova-3 用 keyterm，不是 keywords

Nova-3 **不支持** `keywords`（HTTP 400 / 流式 WS 静默断开）。必须用 **`keyterm`**：纯词、不要 `:权重`。多个词重复参数 `keyterm=REST&keyterm=Kafka`。

- Keyterm Prompting: https://developers.deepgram.com/docs/keyterm
- Keywords 页写明 Nova-3 必须改用 Keyterm：https://developers.deepgram.com/docs/keywords
- streaming nova-3 + keywords 失败：https://github.com/deepgram/deepgram-js-sdk/issues/474

词表：`server/terms_zh.json`（约 50 个中英 IT 词）。编辑该 JSON 数组即可；不要写 `term:1.5`，不要逗号拼一条。启动时打一行 `keyterms=N from <path> (nova-3 uses keyterm, not keywords)`。`--no-keyterms` 可关，方便 A/B。

### 怎么跑（戴镜）

1. 环境变量（`.env` 或 export）：

```bash
export DEEPGRAM_API_KEY=...
export OPENAI_API_KEY=...
export ROUTER_MODEL=...
# 可选
export OPENAI_BASE_URL=https://...
export LIVE_LANG=zh
export AGG_SILENCE_MS=800
export MIN_ROUTE_CHARS=6
export LIVE_WEARER_NOTE='佩戴者是软件工程师，当前对话为 IT 技术讨论'
```

2. 启动 live 服务（电脑，与手机同一局域网）：

```bash
python server/live.py
python server/live.py --host 0.0.0.0 --port 8766 --lang zh
python server/live.py --lang multi --log live_multi.jsonl
```

重启：Ctrl-C 停掉旧进程后再跑同一条命令。改 `terms_zh.json` 后必须重启才会进 Deepgram 握手。

3. 眼镜插件：

```bash
cd glasses/app
npm install
npm run dev
# 真机扫 QR（把 <LAN> 换成电脑 IP）
npx evenhub qr --url "http://<LAN>:5173"
# 或模拟器（无真麦 / BLE）
npx evenhub-simulator http://localhost:5173
```

4. 手机页填 `ws://<电脑LAN>:8766`（灰色占位符不是值），点 **Connect**。
   `app.json` `network.whitelist` 须含该 origin（QR 开发态可能跳过，正式包必须写全，无通配符）。
5. 状态出现 `deepgram_ready` 后开麦。镜腿单击 = 暂停/恢复采集；双击 = `shutDownPageContainer(1)` 退出。
6. 对面问技术问题。jsonl 里 `kind=final` 是原始 FINAL，`kind=turn` 是聚合后的文本 + router。`should_respond=true` 时镜片出字。短于 `--min-route-chars`（默认 6）的聚合句 `skip_reason=too_short`，不调 OpenAI。

物理验收（戴上 G2、看见字）由使用者完成。本环境不编造硬件结果。A/B 清单：`server/ASR_EVAL.md`。朗读稿：`server/fixtures/it_questions_20.txt`。

### 终端

```
[00:12.30] Other  这个接口保证幂等吗
[00:13.45]   → TRIGGER  conf=0.90  lat=980ms
[00:13.45]     幂等：多次执行结果相同

[00:18.02] Self  嗯嗯明白了
[00:18.90]   → skip  conf=0.95  reason: backchannel
```

### zh vs multi（同一批话）

没有离线音频夹具。戴镜对同一批 `server/fixtures/it_questions_20.txt` 各录一份 jsonl：

```bash
python server/live.py --lang zh --log live_zh.jsonl
# 朗读 20 题，Ctrl-C
python server/live.py --lang multi --log live_multi.jsonl
# 同样 20 题，同样语速/距离
```

对比两份里 `kind=turn` 的 `raw_finals` / `aggregated_text` / `pushed`。`language=multi` 时 router `locale` 仍是 `zh`（这场是中文 IT 会）。

### jsonl

- `kind=final`：Deepgram 一条 is_final（含 `speech_final`）。
- `kind=turn`：aggregator 关窗后的一句：`aggregated_text`、`raw_finals`、`speakerRole`、`direction`、完整 `router`、`timings`、`skip_reason`。

### 旋钮

覆盖：**CLI > 环境变量 > 默认**。

| 名 | 默认 | CLI / Env | 含义 |
| --- | --- | --- | --- |
| host | `0.0.0.0` | `--host` / `LIVE_HOST` | 监听地址 |
| port | `8766` | `--port` / `LIVE_PORT` | WebSocket 端口（与插件 URL 一致） |
| lang | `zh` | `--lang` / `LIVE_LANG` | Deepgram `zh` 或 `multi` |
| locale | 由 lang 推导（`multi`→`zh`） | （随 lang） | 交给 `route()` 的 `locale` |
| wearer_note | 佩戴者是软件工程师，当前对话为 IT 技术讨论 | `--wearer-note` / `LIVE_WEARER_NOTE` | 每段都带 |
| router timeout | 1800 ms | `--router-timeout-ms` / `LIVE_ROUTER_TIMEOUT_MS` | 超时放弃，不重试 |
| DG handshake | 60 s | `--handshake-timeout-s` / `DEEPGRAM_HANDSHAKE_TIMEOUT_S` | Deepgram listen 握手 |
| agg silence | 800 ms | `--silence-ms` / `AGG_SILENCE_MS` | `TurnAggregator` 静音关窗（现有 aggregator，不另写一套） |
| min route chars | 6 | `--min-route-chars` / `MIN_ROUTE_CHARS` | 聚合后短于此不调 OpenAI |
| keyterm 词表 | `server/terms_zh.json` | `--terms` / `LIVE_TERMS_PATH` | nova-3 `keyterm` 列表 |
| 关闭 keyterm | 关 | `--no-keyterms` | A/B 基线 |
| log | `live_<时间>.jsonl` | `--log` | 会话日志路径 |
| WS URL（插件） | `ws://<页面hostname>:8766` | 手机输入框 / localStorage | 一条连接既上行 PCM 也下行文本 |

Deepgram：`nova-3`，16 kHz mono s16le（与 `asr/stream.py` / G2 `audioPcm` 一致）。`DEEPGRAM_API_KEY` 必填。

Router：`OPENAI_API_KEY` + `ROUTER_MODEL`，可选 `OPENAI_BASE_URL`。`max_retries=0`。

上行 JSON（每块 PCM）：

```json
{"type":"pcm","pcm_b64":"...","speakerRole":"other","direction":123}
```

`speakerRole`：`self`/`other`/`unknown` → router `SELF`/`OTHER`/`UNKNOWN`。`direction` 只记日志，不参与判决。

下行：`{"text":"..."}` 上镜；`{"status":"deepgram_ready"}` / `{"error":"..."}` 只上手机页。

```bash
python -m unittest server.tests.test_live -v
```

---

## Phase 2 / Milestone 4 — 显示策略（本里程碑不接线）

`display_policy.py` 夹在 router 决策和眼镜推送之间。Phase 3 M1 **不**调用它。单元测试：

```bash
python -m unittest discover -s server/tests -v
```

接到推送服务（与 live.py 分开用）：

```bash
python glasses/display_server.py --policy
```

覆盖顺序：**CLI > 环境变量 > `--policy-config` JSON > 默认**。

| 名 | 默认 | 含义 |
| --- | --- | --- |
| `POLICY_CONF_FULL` | 0.85 | ≥ 此值整段显示 |
| `POLICY_CONF_HINT` | 0.70 | HINT≤c<FULL 只显示 `POLICY_HINT_TEXT` |
| （conf < HINT） | — | 丢弃 |
| `POLICY_BUDGET_WINDOW_MS` | 60000 | 预算窗 |
| `POLICY_BUDGET_MAX` | 1 | 窗内首次通过即推并占额 |
| `POLICY_PREEMPT_MARGIN` | 0.1 | 后到者 confidence ≥ 当前+此值可抢一次镜 |
| `POLICY_DEDUP_WINDOW` | 10 | 最近已推条数 |
| `POLICY_DEDUP_THRESHOLD` | 0.85 | 规范化后精确或字符相似度 ≥ 此值丢弃，**不占预算** |
| `POLICY_TTL_MS` | 10000 | 独立定时器到期 `clearDisplay`；新推送重置 |
| `POLICY_MAX_CHARS` | 56 | 硬上限：`answer_char_len` > 56 才丢弃并计数。29–56 **不丢** |
| `POLICY_ONE_LINE_CHARS` | 28 | 可选折行阈值：≤28 一行，29–56 在 28 处折成两行（不是丢弃） |
| `POLICY_HINT_TEXT` | `・?` | hint 档显示 |
| `POLICY_CLEAR_PLACEHOLDER` | `・` | 清屏占位（无 hide API） |

长度按 Unicode code point（`len`），与 Phase 1 `MAX_ANSWER_CHARS=56` 一致。
