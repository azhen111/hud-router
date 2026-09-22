"""display_policy 单元测试：不需要眼镜硬件。"""

from __future__ import annotations

import unittest

from server.display_policy import (
    Candidate,
    DisplayPolicy,
    PolicySettings,
    format_length,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0
        self._timer_at: int | None = None
        self._cb = None

    def now_ms(self) -> int:
        return self.t

    def set_timer(self, delay_ms: int, cb) -> None:
        self._timer_at = self.t + delay_ms
        self._cb = cb

    def clear_timer(self) -> None:
        self._timer_at = None
        self._cb = None

    def advance(self, ms: int) -> None:
        target = self.t + ms
        if self._timer_at is not None and self._cb is not None and self._timer_at <= target:
            self.t = self._timer_at
            cb = self._cb
            self.clear_timer()
            cb()
        self.t = target


def make_policy(clock: FakeClock, **kwargs) -> tuple[DisplayPolicy, list[str]]:
    expired: list[str] = []
    settings = PolicySettings(**kwargs) if kwargs else PolicySettings()
    policy = DisplayPolicy(
        settings=settings,
        now_ms=clock.now_ms,
        set_timer=clock.set_timer,
        clear_timer=clock.clear_timer,
        on_expire=lambda: expired.append("clear"),
    )
    return policy, expired


class ConfBoundaries(unittest.TestCase):
    def test_below_hint_drops(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        d = p.consider(Candidate("幂等", 0.69))
        self.assertEqual(d.action, "drop")
        self.assertEqual(d.reason, "below_hint")
        self.assertEqual(p.stats.get("drop_conf"), 1)

    def test_hint_tier(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        d = p.consider(Candidate("幂等：多次执行结果相同", 0.70))
        self.assertEqual(d.action, "hint")
        self.assertEqual(d.display_text, "・?")
        self.assertTrue(d.consume_budget)

    def test_full_tier(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        d = p.consider(Candidate("幂等：多次执行结果相同", 0.85))
        self.assertEqual(d.action, "push")
        self.assertEqual(d.display_text, "幂等：多次执行结果相同")


class BudgetAndPreempt(unittest.TestCase):
    def test_second_in_window_suppressed(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        a = p.consider(Candidate("第一条答案够一眼", 0.90, ts_ms=0))
        self.assertEqual(a.action, "push")
        b = p.consider(Candidate("第二条完全不同的话", 0.91, ts_ms=1000))
        self.assertEqual(b.action, "drop")
        self.assertEqual(b.reason, "budget")
        self.assertEqual(p.stats.get("drop_budget"), 1)

    def test_preempt_once_when_margin_met(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        p.consider(Candidate("第一条答案够一眼", 0.85, ts_ms=0))
        d = p.consider(Candidate("更好的第二条答案啊", 0.96, ts_ms=500))
        self.assertEqual(d.action, "preempt")
        self.assertFalse(d.consume_budget)
        third = p.consider(Candidate("第三条再抢一次呢", 0.99, ts_ms=800))
        self.assertEqual(third.action, "drop")
        self.assertEqual(third.reason, "budget")

    def test_no_preempt_without_margin(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        p.consider(Candidate("第一条答案够一眼", 0.90, ts_ms=0))
        d = p.consider(Candidate("差一点不够抢镜", 0.95, ts_ms=10))
        self.assertEqual(d.action, "drop")
        self.assertEqual(d.reason, "budget")

    def test_window_expiry_resets_quota(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        p.consider(Candidate("第一条答案够一眼", 0.90, ts_ms=0))
        clock.t = 60_000
        d = p.consider(Candidate("窗口过后再来一条", 0.90, ts_ms=60_000))
        self.assertEqual(d.action, "push")


class Dedup(unittest.TestCase):
    def test_exact_dedup_no_budget(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        p.consider(Candidate("同一句话不要再推", 0.90, ts_ms=0))
        clock.t = 60_000
        d = p.consider(Candidate("同一句话不要再推", 0.95, ts_ms=60_000))
        self.assertEqual(d.action, "drop")
        self.assertEqual(d.reason, "dedup")
        self.assertFalse(d.consume_budget)
        self.assertEqual(p.used, 0)  # new window, dedup before consume
        self.assertEqual(p.stats.get("drop_dedup"), 1)

    def test_similar_dedup_no_budget(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        p.consider(Candidate("微服务是一种架构风格", 0.90, ts_ms=0))
        clock.t = 60_000
        d = p.consider(Candidate("微服务是一种架构风格。", 0.95, ts_ms=60_000))
        self.assertEqual(d.action, "drop")
        self.assertEqual(d.reason, "dedup")
        self.assertFalse(d.consume_budget)


class Ttl(unittest.TestCase):
    def test_ttl_clears_without_new_candidate(self) -> None:
        clock = FakeClock()
        p, expired = make_policy(clock)
        p.consider(Candidate("十条字以内的答案", 0.90, ts_ms=0))
        self.assertTrue(p.showing)
        clock.advance(10_000)
        self.assertEqual(expired, ["clear"])
        self.assertFalse(p.showing)

    def test_new_push_resets_ttl(self) -> None:
        clock = FakeClock()
        p, expired = make_policy(clock)
        p.consider(Candidate("第一条答案够一眼", 0.90, ts_ms=0))
        clock.advance(9_000)
        self.assertEqual(expired, [])
        clock.t = 9_000
        p.consider(Candidate("抢镜后的新答案行", 1.0, ts_ms=9_000))
        clock.advance(9_000)
        self.assertEqual(expired, [])
        clock.advance(1_000)
        self.assertEqual(expired, ["clear"])


class Length(unittest.TestCase):
    def test_one_line_le_28(self) -> None:
        text, tag = format_length("测" * 28, 56, 28)
        self.assertEqual(tag, "one_line")
        self.assertEqual(text, "测" * 28)
        clock = FakeClock()
        p, _ = make_policy(clock)
        d = p.consider(Candidate("测" * 28, 0.9))
        self.assertEqual(d.action, "push")
        self.assertNotIn("\n", d.display_text)

    def test_two_lines_29_to_56_not_dropped(self) -> None:
        text, tag = format_length("测" * 40, 56, 28)
        self.assertEqual(tag, "two_line")
        self.assertEqual(text, "测" * 28 + "\n" + "测" * 12)
        clock = FakeClock()
        p, _ = make_policy(clock)
        d = p.consider(Candidate("测" * 40, 0.9))
        self.assertEqual(d.action, "push")
        self.assertEqual(d.display_text.count("\n"), 1)

    def test_exactly_56_pushed_as_two_lines(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        d = p.consider(Candidate("测" * 56, 0.9))
        self.assertEqual(d.action, "push")
        self.assertEqual(d.reason, "two_line")
        self.assertEqual(d.display_text.count("\n"), 1)

    def test_over_56_drops(self) -> None:
        clock = FakeClock()
        p, _ = make_policy(clock)
        d = p.consider(Candidate("测" * 57, 0.9))
        self.assertEqual(d.action, "drop")
        self.assertEqual(d.reason, "over_max_two_lines")
        self.assertEqual(p.stats.get("drop_length"), 1)
        self.assertEqual(p.used, 0)


if __name__ == "__main__":
    unittest.main()
