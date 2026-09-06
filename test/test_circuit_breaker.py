"""
CircuitBreaker 状态机测试(纯逻辑,无需 Redis)

验证 closed → open(连续失败达阈值)→ cooldown 后 half-open → 成功回 closed / 失败回 open。
"""

import os
import sys
import time

os.environ.setdefault("CIRCUIT_BREAKER_THRESHOLD", "3")
os.environ.setdefault("CIRCUIT_BREAKER_COOLDOWN_S", "0.2")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.recovery import CircuitBreaker  # noqa: E402


def test_closed_to_open():
    b = CircuitBreaker("svc", threshold=3, cooldown_s=0.2)
    assert b.allow()  # closed 放行
    b.on_failure()
    b.on_failure()
    assert b.allow()  # 仍 closed(2 < 3)
    b.on_failure()  # 第 3 次 → open
    assert b.state == "open"
    assert not b.allow()  # open 短路


def test_open_to_half_open_to_closed():
    b = CircuitBreaker("svc", threshold=2, cooldown_s=0.2)
    b.on_failure()
    b.on_failure()
    assert b.state == "open"
    time.sleep(0.25)  # 冷却到期
    # 冷却后 allow() 转入 half_open 放行探活
    assert b.allow()
    assert b.state == "half_open"
    b.on_success()  # 探活成功 → closed
    assert b.state == "closed"
    assert b.snapshot()["failures"] == 0


def test_half_open_failure_reopens():
    b = CircuitBreaker("svc", threshold=2, cooldown_s=0.2)
    b.on_failure()
    b.on_failure()
    assert b.state == "open"
    time.sleep(0.25)
    b.allow()  # → half_open
    b.on_failure()  # 探活失败 → 重新 open
    assert b.state == "open"


def test_success_resets_failures():
    b = CircuitBreaker("svc", threshold=3, cooldown_s=0.2)
    b.on_failure()
    b.on_failure()
    b.on_success()  # 成功重置
    assert b.snapshot()["failures"] == 0
    assert b.state == "closed"


def test_correctable_does_not_trip_via_decorator():
    """@resilient 不应因可纠错错误熔断(仅 TRANSIENT/PERMANENT 计数)"""
    import app.agent.recovery as recovery
    from app.agent.recovery import resilient, ErrorKind

    recovery._backoff_delay = lambda attempt, base=0.5, cap=8.0: 0.001

    # 用独立 breaker(独立 name)
    @resilient(tool_name="cb_correctable_only", max_retries=0, use_breaker=True)
    async def bad():
        raise ValueError("bad argument")

    import asyncio

    for _ in range(10):
        asyncio.run(bad())
    breaker = recovery.get_breaker("cb_correctable_only")
    assert breaker.state == "closed"  # 可纠错不计入,未熔断


if __name__ == "__main__":
    test_closed_to_open()
    test_open_to_half_open_to_closed()
    test_half_open_failure_reopens()
    test_success_resets_failures()
    test_correctable_does_not_trip_via_decorator()
    print("PASS: circuit breaker tests passed")
