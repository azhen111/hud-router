# 28-char eval (5× testcases.jsonl + 5× testcases_zh_it.jsonl)

`evaluate.py` 需要 `OPENAI_API_KEY` 与 `ROUTER_MODEL`。本环境没有这些变量，**5+5 次 live eval 未跑**。

Follow-up（original_answer / max_retries=0 / prompt append）同样未跑 live eval。父环境 pull 后请本地跑 5× `testcases.jsonl` + 5× `testcases_zh_it.jsonl`。

配置后：

```bash
python3 evaluate.py --no-color --cases testcases.jsonl
python3 evaluate.py --no-color --cases testcases_zh_it.jsonl
```

各跑 5 次。`suppressed_over_length` 预期上升（上限 40→28），不是 FAIL 门。FP 不应差于先前基线（约 5.6%，id=16 稳定 FP）。

代码侧已改：`prompts.py` 「## The answer」那一行 40→28；`router.py` 仅 `MAX_ANSWER_CHARS` 40→28（及注释）。`testcases*` 未动。
