"""
Budget 软停止触发测试(纯逻辑,无需 Redis)

验证四个维度的预算触达上限逻辑:iterations / tool_calls / tokens / runtime。
"""

import os
import sys
import time

# 让测试在无 .env 时也能跑(走代码内默认值)
os.environ.setdefault("BUDGET_MAX_ITERATIONS", "3")
os.environ.setdefault("BUDGET_MAX_TOOL_CALLS", "2")
os.environ.setdefault("BUDGET_MAX_TOKENS", "10")
os.environ.setdefault("BUDGET_MAX_RUNTIME_S", "0.3")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.budget import Budget, BudgetConfig, BudgetDimension  # noqa: E402


def test_budget_config_overrides():
    cfg = BudgetConfig.from_env(max_iterations=99, max_tokens=50)
    assert cfg.max_iterations == 99
    assert cfg.max_tokens == 50
    # 未覆盖的字段沿用环境变量
    assert cfg.max_tool_calls == 2


def test_iterations_exhaustion():
    b = Budget(config=BudgetConfig(max_iterations=3, max_runtime_s=60, max_tool_calls=100, max_tokens=100000))
    assert b.consume_iteration() is None
    assert b.consume_iteration() is None
    dim = b.consume_iteration()  # 第 3 次触达
    assert dim == BudgetDimension.ITERATIONS
    # 超额后 consume 不再变化,exhausted 稳定
    assert b.consume_iteration() == BudgetDimension.ITERATIONS


def test_tool_calls_exhaustion():
    b = Budget(config=BudgetConfig(max_iterations=100, max_runtime_s=60, max_tool_calls=2, max_tokens=100000))
    assert b.consume_tool_call() is None
    assert b.consume_tool_call() == BudgetDimension.TOOL_CALLS


def test_tokens_exhaustion():
    b = Budget(config=BudgetConfig(max_iterations=100, max_runtime_s=60, max_tool_calls=100, max_tokens=10))
    assert b.consume_tokens(4) is None
    assert b.consume_tokens(6) == BudgetDimension.TOKENS  # 累计 10 触达


def test_runtime_exhaustion():
    b = Budget(config=BudgetConfig(max_iterations=100, max_runtime_s=0.2, max_tool_calls=100, max_tokens=100000))
    assert b.check_runtime() is None
    time.sleep(0.25)
    assert b.check_runtime() == BudgetDimension.RUNTIME


def test_check_runtime_not_short_circuited_by_other_dimension():
    """其他维度先 exhausted 时,check_runtime 在墙钟未超时仍应返回 None(否则收尾阶段会空转)"""
    b = Budget(config=BudgetConfig(max_iterations=1, max_runtime_s=60, max_tool_calls=100, max_tokens=100000))
    assert b.consume_iteration() == BudgetDimension.ITERATIONS  # exhausted = ITERATIONS
    # 墙钟远未超时,不应被 ITERATIONS 短路返回非 None
    assert b.check_runtime() is None


def test_first_dimension_wins():
    """先触达的维度锁定 exhausted,后续维度不再覆盖"""
    b = Budget(config=BudgetConfig(max_iterations=1, max_runtime_s=60, max_tool_calls=100, max_tokens=100000))
    b.consume_iteration()  # 触达 iterations
    # 之后即使工具调用超额,exhausted 仍是 iterations
    assert b.consume_tool_call() == BudgetDimension.ITERATIONS


def test_snapshot():
    b = Budget(config=BudgetConfig(max_iterations=2, max_runtime_s=60, max_tool_calls=2, max_tokens=100))
    b.consume_iteration()
    b.consume_tool_call()
    snap = b.snapshot()
    assert snap["iterations"] == 1
    assert snap["tool_calls"] == 1
    assert snap["exhausted"] is None


if __name__ == "__main__":
    test_budget_config_overrides()
    test_iterations_exhaustion()
    test_tool_calls_exhaustion()
    test_tokens_exhaustion()
    test_runtime_exhaustion()
    test_check_runtime_not_short_circuited_by_other_dimension()
    test_first_dimension_wins()
    test_snapshot()
    print("PASS: budget tests passed")
