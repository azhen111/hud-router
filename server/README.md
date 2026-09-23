# server — Phase 3 live 通路 + 显示策略

## Phase 3 — live 通路（ASR 加固）

`server/live.py`：眼镜 PCM 上行 → Deepgram nova-3（`keyterm`）→ aggregator → **`transcript_fix`（默认开）** → **judge**（`ROUTER_SYSTEM_PROMPT`，只取 should_respond / conf / kind / reason / needs_more_context）→ **answer**（`ANSWER_SYSTEM_PROMPT`，仅 trigger）→ **`display_policy`** → `{"text": ...}`。TTL 到期再推 `・`。

**两级默认开。** 未配置 `ANSWER_MODEL` 时两级用同一个 `ROUTER_MODEL`（通常 gpt-4o-mini）。Judge 的 `answer` **不上镜**。`--single-shot` / `LIVE_TWO_TIER=0` 恢复单次 judge（用 judge.answer，方便 A/B）。

**不做：** RAG、LLM 纠错、改 `ROUTER_SYSTEM_PROMPT` / testcases* / display_policy 语义。

**不要和** `glasses/display_server.py` **抢同一端口**（默认都是 8766）。

### nova-3 用 keyterm，不是 keywords

Nova-3 **不支持** `keywords`（HTTP 400 / 流式 WS 静默断开）。必须用 **`keyterm`**：纯词、不要 `:权重`。多个词重复参数 `keyterm=REST&keyterm=Kafka`。

- Keyterm Prompting: https://developers.deepgram.com/docs/keyterm
- Keywords 页写明 Nova-3 必须改用 Keyterm：https://developers.deepgram.com/docs/keywords
- streaming nova-3 + keywords 失败：https://github.com/deepgram/deepgram-js-sdk/issues/474

词表：`server/terms_zh.json`。元素可以是纯字符串，或 `{"term":"JWT","variants":["GWT","JWA"]}`。Deepgram `keyterm` 会抽出 `term` + 全部 variants（仍不要 `:权重`）。纠错层用同一张表做确定性模糊匹配。`--no-keyterms` 只关 Deepgram 词表；`--no-fix` 关纠错。

官方写明 **Nova-3 的 monolingual 和 multilingual 都可以用 `keyterm`**：https://developers.deepgram.com/docs/keyterm 。Self-hosted 2025-12-10 changelog 也写了 Nova-3 Multi 的 multilingual keyterm（最多约 500 token）：https://developers.deepgram.com/changelog/2025/12/10 。旧版 hosted/self-hosted 模型若报 `The selected Nova-3 model does not support keyterm prompting`，是模型版本问题，不是 `language=multi` 本身禁 keyterm。本仓库对 `zh` 和 `multi` 都传同一份 `keyterm` 列表，从不传 `keywords`。

### 怎么跑（戴镜）

1. 环境变量（`.env` 或 export）：

```bash
export DEEPGRAM_API_KEY=...
export OPENAI_API_KEY=...
export ROUTER_MODEL=...
# 可选
export OPENAI_BASE_URL=https://...
export LIVE_LANG=zh
export AGG_SILENCE_MS=1200
export MIN_ROUTE_CHARS=3
export LIVE_ROUTER_TIMEOUT_MS=3000
export ANSWER_MODEL=          # 空 = 与 ROUTER_MODEL 相同
export ANSWER_TIMEOUT_MS=4000
export LIVE_TWO_TIER=1        # 0 或 --single-shot = 单次 judge
export LIVE_NO_FIX=0
export FIX_SIMILARITY=0.6
export LIVE_WEARER_NOTE='佩戴者是软件工程师，当前对话为 IT 技术讨论'
```

2. 启动 live 服务（电脑，与手机同一局域网）：

```bash
python server/live.py
python server/live.py --host 0.0.0.0 --port 8766 --lang zh
python server/live.py --lang multi --log live_multi.jsonl
python server/live.py --no-policy --log live_nopolicy.jsonl
python server/live.py --no-fix --log live_nofix.jsonl
python server/live.py --single-shot --log live_singleshot.jsonl
python server/live.py --no-answer-tier --log live_singleshot.jsonl
```

启动横幅必须能看到 `router_timeout_ms=3000`（以及 `source=default|cli|env`）和 `policy=on` / `policy=off`。若 `.env` 里还留着 `LIVE_ROUTER_TIMEOUT_MS=1800`，横幅会打印 `source=env LIVE_ROUTER_TIMEOUT_MS=1800`——那不是代码默认，是环境覆盖。

重启（改默认 / 词表 / 策略后必须）：Ctrl-C 停掉旧 `python server/live.py`，再跑同一条命令。改 `terms_zh.json` 后必须重启才会进 Deepgram 握手。眼镜插件若已 Connect，断线再连一次。

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
6. 对面**或佩戴者**问技术问题都会进 `route()`。`should_respond=true` 后先走 `display_policy`（预算 / 置信 / 去重 / 长度 / TTL），通过才 `{"text": display_text}` 上镜。约 10s 后独立定时器再推 `・` 清屏（无 hide API，见 `glasses/SDK_NOTES.md`）。`--no-policy` 时 TRIGGER 直接推 **answer 级**正文（`--single-shot` 则推 judge.answer），和 M1 一样不经策略。

物理验收（戴上 G2、看见字）由使用者完成。本环境不编造硬件结果。A/B 清单：`server/ASR_EVAL.md`。朗读稿：`server/fixtures/it_questions_20.txt`。

### 终端

```
[00:12.20]   → fix  '线流' → '限流'  term=限流  sim=1.00
[00:12.30] Other  这个接口保证幂等吗
[00:13.45]   → TRIGGER  conf=0.90  judge_ms=980
[00:13.45]     judge.answer ignored  kind=term
[00:17.10]     幂等：多次执行结果相同
[00:17.10]   → policy  action=push  mode=full  ttl=10000ms
[00:27.10]   → policy_clear  text='・'

[00:18.02] Self  嗯嗯明白了
[00:18.90]   → skip  conf=0.95  reason: backchannel
```

超时行会写成：

```
→ skip  reason: router_timeout  waited=3000ms  source=default DEFAULT_ROUTER_TIMEOUT_MS=3000  (code DEFAULT_ROUTER_TIMEOUT_MS=3000)
```

### zh vs multi（文档结论，不是猜）

`--lang multi` 已经接到 Deepgram `language=multi`；router `locale` 仍是 `zh`。

官方 Codeswitching：`language=multi` + `model=nova-3`（预录和流式都支持）。  
https://developers.deepgram.com/docs/multilingual-code-switching

官方 Models 页把 nova-3 的 **`multi` 码切换集合**写成：English, Spanish, French, German, Hindi, Russian, Portuguese, Japanese, Italian, Dutch。  
**中文不在这份 `multi` 列表里。** 中文是 **单语** 码：`zh` / `zh-CN` / `zh-Hans` / `zh-TW` / `zh-Hant`。  
https://developers.deepgram.com/docs/models-languages-overview

因此：

| 场景 | 用哪个 |
| --- | --- |
| 这场中文 IT 会（夹英文术语） | 默认 **`--lang zh`**。单语 nova-3 明确支持中文；英文专有名词靠 `keyterm` 抬。 |
| 官方列出的那 10 种语言互相切换 | **`--lang multi`** |
| 想对照「中英夹杂」识别差在哪 | 同一批 `it_questions_20.txt` 各录一份 zh / multi；**不要把 multi 当成文档保证的中英混合模型** |

`keyterm` 在 multi 下：**文档写明** Nova-3 monolingual **和** multilingual 都能用（见上节 URL）。本进程对两种 lang 都传同一词表。

没有离线音频夹具。戴镜对照：

```bash
python server/live.py --lang zh --log live_zh.jsonl
# 朗读 20 题，Ctrl-C
python server/live.py --lang multi --log live_multi.jsonl
# 同样 20 题，同样语速/距离
```

对比 `kind=turn` 的 `raw_finals` / `aggregated_text` / `pushed` / `policy`。本环境不编造硬件结果。

### jsonl

- `kind=final`：Deepgram 一条 is_final（含 `speech_final`）。
- `kind=fix`：一条确定性纠正（`original` / `fixed` / `term` / `similarity`）。
- `kind=turn`：`aggregated_text`、`fix`、`router`（judge JSON，其中 `answer` 仅供对照）、`answer`（明文或 SKIP）、`timings.judge_ms` / `answer_ms`、`skip_reason`、`policy`。
- `kind=policy_clear`：TTL 到期推 `・`，不依赖下一条候选。

`policy`（router 触发后，或 `--no-policy` 本会走到策略时）字段：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 策略是否开启 |
| `allowed` | 是否允许下行 |
| `reason` | 稳定码：`below_hint` / `budget` / `dedup` / `over_max_two_lines` / `one_line` / `two_line` / `bypassed` |
| `action` | `push` / `hint` / `preempt` / `drop` |
| `mode` | `full` / `hint` / `none` |
| `confidence` | router 置信 |
| `display_text` | 实际要上镜的文本（hint 可能是 `・?`） |
| `budget_used` / `budget_max` | 当前窗已用 / 上限 |
| `ttl_ms` / `ttl_deadline_ms` | TTL 与到期墙钟 |
| `consume_budget` | 这次是否占额 |

### 旋钮

覆盖：**CLI > 环境变量 > 默认**。

| 名 | 默认 | CLI / Env | 含义 |
| --- | --- | --- | --- |
| host | `0.0.0.0` | `--host` / `LIVE_HOST` | 监听地址 |
| port | `8766` | `--port` / `LIVE_PORT` | WebSocket 端口（与插件 URL 一致） |
| lang | `zh` | `--lang` / `LIVE_LANG` | Deepgram `zh` 或 `multi` |
| locale | 由 lang 推导（`multi`→`zh`） | （随 lang） | 交给 `route()` 的 `locale` |
| wearer_note | 佩戴者是软件工程师，当前对话为 IT 技术讨论 | `--wearer-note` / `LIVE_WEARER_NOTE` | 每段都带 |
| router timeout | **3000 ms** | `--router-timeout-ms` / `LIVE_ROUTER_TIMEOUT_MS` | 超时放弃，不重试。启动横幅 + 每次 timeout skip 都打印 `waited=Nms` 和 `source=` |
| DG handshake | 60 s | `--handshake-timeout-s` / `DEEPGRAM_HANDSHAKE_TIMEOUT_S` | Deepgram listen 握手 |
| agg silence | 1200 ms | `--silence-ms` / `AGG_SILENCE_MS` | 独立 ticker 每 50ms `tick()`；最后一条 FINAL 后再等这么久就关窗，不必再来 ASR |
| min route chars | 3 | `--min-route-chars` / `MIN_ROUTE_CHARS` | 聚合后短于此且未命中 keyterm 则 `skip_reason=too_short` |
| keyterm 词表 | `server/terms_zh.json` | `--terms` / `LIVE_TERMS_PATH` | nova-3 `keyterm` 列表 |
| 关闭 keyterm | 关 | `--no-keyterms` | A/B 基线 |
| two-tier | **开** | `--single-shot` / `--no-answer-tier` / `LIVE_TWO_TIER=0` | 关则只用 judge.answer（A/B） |
| ANSWER_MODEL | =ROUTER_MODEL | `ANSWER_MODEL` | 未设则与 judge 同模型 |
| answer timeout | 4000 ms | `--answer-timeout-ms` / `ANSWER_TIMEOUT_MS` | 仅 trigger 后调用；超时丢轮、不重试 |
| transcript fix | **开** | `--no-fix` / `LIVE_NO_FIX` | 聚合后、judge 前确定性纠错 |
| FIX_SIMILARITY | 0.6 | `--fix-similarity` / `FIX_SIMILARITY` | 模糊匹配阈值 |
| display policy | **开** | `--no-policy` / `LIVE_NO_POLICY` | 关掉则 TRIGGER 直推，不经策略 |
| POLICY_CONF_FULL | 0.85 | `--conf-full` / `POLICY_CONF_FULL` | ≥ 此值整段 |
| POLICY_CONF_HINT | 0.70 | `--conf-hint` / `POLICY_CONF_HINT` | HINT≤c<FULL 只显示 `・?` |
| POLICY_BUDGET_WINDOW_MS | 60000 | `--budget-window-ms` | 先推后压；窗内额满可抢一次 |
| POLICY_BUDGET_MAX | 1 | `--budget-max` | 窗内占额次数 |
| POLICY_TTL_MS | 10000 | `--ttl-ms` | 独立 `loop.call_later`，到期推 `・` |
| POLICY_DEDUP_WINDOW | 10 | `--dedup-window` | 最近已推条数 |
| POLICY_MAX_CHARS | 56 | `--max-chars` | 超过则 `over_max_two_lines` |
| log | `live_<时间>.jsonl` | `--log` | 会话日志路径 |
| WS URL（插件） | `ws://<页面hostname>:8766` | 手机输入框 / localStorage | 一条连接既上行 PCM 也下行文本 |

Deepgram：`nova-3`，16 kHz mono s16le（与 `asr/stream.py` / G2 `audioPcm` 一致）。`DEEPGRAM_API_KEY` 必填。

Router：`OPENAI_API_KEY` + `ROUTER_MODEL`，可选 `OPENAI_BASE_URL`。`max_retries=0`。

上行 JSON（每块 PCM）：

```json
{"type":"pcm","pcm_b64":"...","speakerRole":"other","direction":123}
```

`speakerRole`：`self`/`other`/`unknown` → router `SELF`/`OTHER`/`UNKNOWN`，只记 jsonl / 终端，**不**用来丢掉 Self。`direction` 只记日志，不参与判决。

下行：`{"text":"..."}` 上镜（TTL 清屏同样走这条，内容为 `・`）；`{"status":"deepgram_ready"}` / `{"error":"..."}` 只上手机页。插件也认 `{"clear":true}`（`clearDisplay()` → 同一个 `・`），live 清屏为了和现有推送一致只发 `{"text":"・"}`。

```bash
python -m unittest server.tests.test_live -v
```

---

## Phase 3 / M2 — 显示策略（已接到 live.py）

`display_policy.py` 夹在 router 决策和眼镜推送之间。语义未改：先推后压、窗内可抢一次、去重不占预算、TTL 用调用方提供的独立定时器。live 用 `asyncio.loop.call_later`，**不是**等下一条候选才检查到期。

单元测试（含 live 接线）：

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
