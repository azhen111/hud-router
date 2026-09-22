# server — Phase 3 live 通路 + 显示策略

## Phase 3 / Milestone 1 — 最小直播通路

`server/live.py`：眼镜 PCM 上行 → Deepgram 流式 ASR → 每个 `speech_final=true` 立刻 `route()` → `{"text": answer}` 下行上镜。

**不做：** display_policy（无预算 / 置信档 / TTL / 去重）、aggregator、RAG。误触发这一轮可以，用来看真实行为。

**不要和** `glasses/display_server.py` **抢同一端口**（默认都是 8766）。

### 怎么跑（戴镜）

1. 环境变量（`.env` 或 export）：

```bash
export DEEPGRAM_API_KEY=...
export OPENAI_API_KEY=...
export ROUTER_MODEL=...
# 可选
export OPENAI_BASE_URL=https://...
export LIVE_LANG=zh
export LIVE_WEARER_NOTE='佩戴者是软件工程师，当前对话为 IT 技术讨论'
```

2. 启动 live 服务（电脑，与手机同一局域网）：

```bash
python server/live.py
python server/live.py --host 0.0.0.0 --port 8766 --lang zh
```

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
6. 对面问技术问题。终端和 `live_*.jsonl` 各记一条 final；`should_respond=true` 时镜片出字。

物理验收（戴上 G2、看见字）由使用者完成。本环境不编造硬件结果。

### 终端

```
[00:12.30] Other  这个接口保证幂等吗
[00:13.45]   → TRIGGER  conf=0.90  lat=980ms
[00:13.45]     幂等：多次执行结果相同

[00:18.02] Self  嗯嗯明白了
[00:18.90]   → skip  conf=0.95  reason: backchannel
```

### jsonl

每个 `speech_final` 一段一行：`transcript`、`speakerRole`、`direction`、完整 `router` 返回、`timings`（router / downlink / total）。这是以后调参的唯一证据。

### 旋钮

覆盖：**CLI > 环境变量 > 默认**。

| 名 | 默认 | CLI / Env | 含义 |
| --- | --- | --- | --- |
| host | `0.0.0.0` | `--host` / `LIVE_HOST` | 监听地址 |
| port | `8766` | `--port` / `LIVE_PORT` | WebSocket 端口（与插件 URL 一致） |
| lang | `zh` | `--lang` / `LIVE_LANG` | Deepgram 语言（`zh` / `ja` / …） |
| locale | 由 lang 推导 | （随 lang） | 交给 `route()` 的 `locale` |
| wearer_note | 佩戴者是软件工程师，当前对话为 IT 技术讨论 | `--wearer-note` / `LIVE_WEARER_NOTE` | 每段都带 |
| router timeout | 1800 ms | `--router-timeout-ms` / `LIVE_ROUTER_TIMEOUT_MS` | 超时放弃，不重试 |
| DG handshake | 60 s | `--handshake-timeout-s` / `DEEPGRAM_HANDSHAKE_TIMEOUT_S` | Deepgram listen 握手 |
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
