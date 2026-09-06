"""
Recovery Engine 测试(纯逻辑,无需 Redis)

验证 @resilient 装饰器:
- 瞬态错误:退避重试后成功
- 可纠错错误:不重试,返回结构化错误串给 LLM
- 永久错误:不重试,返回错误串
- 资源错误:上抛(交 Budget/顶层)
并验证错误分类 classify()。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.agent.recovery as recovery  # noqa: E402
from app.agent.recovery import classify, resilient, ErrorKind  # noqa: E402

# 测试中把退避压到极小,避免重试拉长测试时间
recovery._backoff_delay = lambda attempt, base=0.5, cap=8.0: 0.001


def test_classify_transient():
    assert classify(ConnectionRefusedError("connection refused")) == ErrorKind.TRANSIENT
    assert classify(TimeoutError("request timed out")) == ErrorKind.TRANSIENT
    assert classify(ConnectionError("503 service unavailable")) == ErrorKind.TRANSIENT


def test_classify_permanent():
    assert classify(ValueError("缺失数据库核心配置：user")) == ErrorKind.PERMANENT


def test_classify_correctable():
    # 未匹配永久消息的 ValueError 视为可纠错参数错误
    assert classify(ValueError("bad argument")) == ErrorKind.CORRECTABLE


def test_classify_resource():
    assert classify(MemoryError()) == ErrorKind.RESOURCE


def test_resilient_transient_retry_then_success():
    calls = {"n": 0}

    @resilient(tool_name="t_retry", max_retries=3, use_breaker=False)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError("connection refused")
        return "ok"

    result = asyncio.run(flaky())
    assert result == "ok"
    assert calls["n"] == 3  # 2 次失败 + 1 次成功


def test_resilient_correctable_returns_string():
    @resilient(tool_name="t_corr", max_retries=3, use_breaker=False)
    async def bad_sql():
        raise ValueError("bad argument: invalid column")

    result = asyncio.run(bad_sql())
    assert "[工具执行失败:t_corr]" in result
    assert "correctable" in result


def test_resilient_permanent_returns_string():
    @resilient(tool_name="t_perm", max_retries=3, use_breaker=False)
    async def no_config():
        raise ValueError("缺失数据库核心配置：user")

    result = asyncio.run(no_config())
    assert "permanent" in result


def test_resilient_resource_reraises():
    @resilient(tool_name="t_res", max_retries=3, use_breaker=False)
    async def oom():
        raise MemoryError()

    try:
        asyncio.run(oom())
        assert False, "应抛出 MemoryError 交由顶层处理"
    except MemoryError:
        pass


def test_resilient_sync_tool():
    """验证 sync 工具同样走 @resilient"""

    @resilient(tool_name="t_sync", max_retries=2, use_breaker=False)
    def sync_flaky():
        raise TimeoutError("timed out")

    result = sync_flaky()
    # 瞬态耗尽重试后转 correctable 回注
    assert "工具执行失败" in result


def test_resilient_breaker_short_circuits():
    """连续失败达阈值后熔断器 open,后续直接返回不可用串"""

    @resilient(tool_name="t_break", max_retries=0, use_breaker=True)
    async def always_fail():
        raise ConnectionRefusedError("refused")

    # 多次调用累积失败直至 open(threshold 默认 5)
    for _ in range(6):
        asyncio.run(always_fail())

    breaker = recovery.get_breaker("t_break")
    assert breaker.state == "open"

    # open 后再调用:不触达函数体,直接返回不可用串
    called = {"n": 0}

    @resilient(tool_name="t_break", max_retries=0, use_breaker=True)
    async def should_not_run():
        called["n"] += 1
        return "ok"

    result = asyncio.run(should_not_run())
    assert "暂不可用" in result or "熔断" in result
    assert called["n"] == 0  # 函数体未被调用


if __name__ == "__main__":
    test_classify_transient()
    test_classify_permanent()
    test_classify_correctable()
    test_classify_resource()
    test_resilient_transient_retry_then_success()
    test_resilient_correctable_returns_string()
    test_resilient_permanent_returns_string()
    test_resilient_resource_reraises()
    test_resilient_sync_tool()
    test_resilient_breaker_short_circuits()
    # 熔断器状态在全局注册表,清掉避免影响后续
    print("PASS: recovery tests passed")
