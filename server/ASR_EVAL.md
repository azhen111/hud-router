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
- A/B：`--no-fix` 关闭纠错；`--single-shot` / `--no-answer-tier` 关闭 answer 级（用 judge.answer）。
- 默认两级：judge 超时 3000ms，answer 超时 4000ms。未设 `ANSWER_MODEL` 则两级同一模型。

## 纠错层离线样本

词表 + `FIX_SIMILARITY=0.6`，无 LLM。`server/fixtures/asr_mistranscribe_samples.json` 是从佩戴者本机 `live_*.jsonl` 抽出的 24 条错听；`test_session_samples_file` 覆盖全句。未映射垃圾：`NIkkMykykM`、`LANSALSGAL`。

孤立 token（`test_ten_known_mistranscribes`）：

| ASR 错听 | 纠正为 |
| --- | --- |
| FLCK | Flex |
| JWA | JWT |
| CFCAR | Kafka |
| 库布尔netes | Kubernetes |
| GrafficQL | GraphQL |
| 线流 | 限流 |
| 回拱 | 回滚 |
| Bocker | Docker |
| pud | Pod |
| postgress | PostgreSQL |

会话原句（22/22 可映射；2 条垃圾保持原样）：

| 原句 | 纠正后 |
| --- | --- |
| 和GrafficQL怎么选 | 和GraphQL怎么选 |
| 库布尔netes里 | Kubernetes里 |
| pud | Pod |
| CFCAR消息会丢吗 | Kafka消息会丢吗 |
| GWT过期了怎么刷新 | JWT过期了怎么刷新 |
| 熔断和线流有什么区别 | 熔断和限流有什么区别 |
| 灰度发布怎么回拱 | 灰度发布怎么回滚 |
| RST和GraficQL要怎么选 | REST和GraphQL要怎么选 |
| readis | Redis |
| 科贝尔net | Kubernetes |
| CAFCAR消息会丢失吗 | Kafka消息会丢失吗 |
| GRPC和HTL | gRPC和HTTP |
| Bocker镜像太大怎么瘦身 | Docker镜像太大怎么瘦身 |
| postgress | PostgreSQL |
| hostgreatcircle和MyCircle怎么选 | PostgreSQL和MySQL怎么选 |
| ICD流水线挂了怎么查 | CI/CD流水线挂了怎么查 |
| 前端中FLCK布局怎么用 | 前端中Flex布局怎么用 |
| 前端中的FLACS | 前端中的Flex |
| JWA的三种特性有什么 | JWT的三种特性有什么 |
| 你说一下JVA的重写 | 你说一下JWT的重写 |
| JWA有哪些特性 | JWT有哪些特性 |
| Jva | JWT |

## Before（对照）

```bash
python server/live.py --lang zh --no-keyterms --log live_before_zh.jsonl
```

把 `it_questions_20.txt` 读完。Ctrl-C。

## After（加固）

```bash
python server/live.py --lang zh --log live_after_zh.jsonl
```

同样 20 句。启动日志应有 `keyterms_dg=N  fix_entries=M`（N 是 canonical，≤80；M 是纠错条目）。`N` 不应接近 90。

## zh vs multi（同一加固设置）

```bash
python server/live.py --lang zh --log live_zh.jsonl
# 读 20 句，停
python server/live.py --lang multi --log live_multi.jsonl
# 再读同一 20 句
```

## 看 jsonl

- `kind=final`：原始 FINAL / `speech_final`。
- `kind=fix`：一条确定性纠正（`original` / `fixed` / `term` / `similarity`）。
- `kind=turn`：`raw_finals` + `aggregated_text` + `fix` + `router`（judge）+ `answer` + `timings.judge_ms` / `answer_ms` / `skip_reason` / `policy`。
- `kind=policy_clear`：TTL 到期推 `・`。
- 过短：`skip_reason=too_short`（且 `error` 写明 `N < min; no keyterm hit`），`pushed=false`，没有 OpenAI 调用。命中 `terms_zh.json` 的短句不走这条。
- 策略拒绝：`skip_reason=policy_<reason>`，`policy.reason` 为 `below_hint` / `budget` / `dedup` / `over_max_two_lines`。

对比 before/after 或 zh/multi 时，只比较这 20 句对应的 `turn` 行，不要事后改 prompt 或 testcases。
