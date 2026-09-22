# server — 显示策略（Phase 2 / Milestone 4）

`display_policy.py` 夹在 router 决策和眼镜推送之间。把误触发变成排序：低置信丢掉，窗口里默认只亮一条，相似的不占额度，超时自己清。

不接 ASR / 检索 / 说话人向量。单元测试不需要硬件：

```bash
python -m unittest discover -s server/tests -v
```

## 接到推送服务

```bash
python glasses/display_server.py --policy
python glasses/display_server.py --policy --file glasses/fixtures/display_test.txt --pause
python glasses/display_server.py --policy --policy-config server/config.example.json
```

每行可以是纯文本（默认 `--confidence 1.0`）或 JSON：

```json
{"text": "幂等：多次执行结果相同", "confidence": 0.91, "kind": "term"}
```

策略 `push` / `hint` / `preempt` 时服务器发 `{"text":"..."}`。TTL 到期发 `{"clear": true}`，插件走 `clearDisplay()`（可配置占位符，默认 `・`）。文档没有 hide API，空串不能清屏。

不带 `--policy` 时行为与 M3 相同：每行原样 `{"text":...}`。

覆盖顺序：**CLI > 环境变量 > `--policy-config` JSON > 默认**。

## 旋钮

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

长度按 Unicode code point（`len`），与 Phase 1 `MAX_ANSWER_CHARS=56` 一致。`POLICY_MAX_CHARS` 是丢弃上限，不是单行宽；单行折行用 `POLICY_ONE_LINE_CHARS`（默认 28）。去重：NFKC + 去空白 + lower，再 `SequenceMatcher.ratio()`。
