"""
任务级预算(Budget)模块

每个 task run 持有一个 Budget 实例,在 LLM 调用、工具调用和 astream 消费循环
的各个检查点累计消耗。任一维度触达上限即标记 exhausted,由 run_deep_agent 的
软停止逻辑接管:break astream → 用收尾 HumanMessage 在同 thread_id 重新 astream。

设计要点:
- max_tokens / max_cost 本质是"事后"量(输出 token 要等调用结束才知道),因此只能
  软停止(阻止下一轮,不硬中断当前调用)。max_cost 本期仅估算展示,不下发停止
  (决策:延后,用 max_tokens 代理)。
- max_iterations 复用 LangGraph 的 recursion_limit 作图级硬后底,软停止在其下触发。
- 所有阈值从环境变量读取,支持 per-task 覆盖。
"""

import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BudgetDimension(str, Enum):
    """触发上限的维度,用于软停止消息与指标归因"""

    ITERATIONS = "max_iterations"
    RUNTIME = "max_runtime"
    TOOL_CALLS = "max_tool_calls"
    TOKENS = "max_tokens"


@dataclass
class BudgetConfig:
    """预算阈值配置,从环境变量读取默认值,可被 per-task 覆盖"""

    max_iterations: int = 20
    max_runtime_s: float = 300.0
    max_tool_calls: int = 30
    max_tokens: int = 80000
    # 预算触发后,收尾阶段允许的最大时长(秒)。收尾阶段不再查各预算,
    # 只用这一个固定窗口给模型产出最终答复的机会,超时则强制终止。
    wrapup_grace_s: float = 30.0

    @classmethod
    def from_env(cls, **overrides) -> "BudgetConfig":
        """从环境变量构造,允许 per-task 字段覆盖"""

        def _env_int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        def _env_float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        cfg = cls(
            max_iterations=_env_int("BUDGET_MAX_ITERATIONS", cls.max_iterations),
            max_runtime_s=_env_float("BUDGET_MAX_RUNTIME_S", cls.max_runtime_s),
            max_tool_calls=_env_int("BUDGET_MAX_TOOL_CALLS", cls.max_tool_calls),
            max_tokens=_env_int("BUDGET_MAX_TOKENS", cls.max_tokens),
            wrapup_grace_s=_env_float("BUDGET_WRAPUP_GRACE_S", cls.wrapup_grace_s),
        )
        # per-task 覆盖优先于环境变量
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg


@dataclass
class Budget:
    """单次 task run 的预算账本

    线程安全:计数器累加在锁内完成,避免并发回调(子智能体子图)竞争。
    """

    config: BudgetConfig
    started_at: float = field(default_factory=time.perf_counter)

    # 累计计数器
    _iterations: int = 0
    _tool_calls: int = 0
    _tokens: int = 0
    # 哪个维度先触达上限;None 表示尚未超额
    _exhausted: Optional[BudgetDimension] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def deadline(self) -> float:
        """运行时硬截止时刻(perf_counter 时基)"""
        return self.started_at + self.config.max_runtime_s

    @property
    def exhausted(self) -> Optional[BudgetDimension]:
        """当前触达上限的维度,None 表示未超额"""
        return self._exhausted

    @property
    def remaining_runtime_s(self) -> float:
        return max(0.0, self.deadline - time.perf_counter())

    def consume_iteration(self) -> Optional[BudgetDimension]:
        """记一次 LLM 轮次,返回触发维度(若有)"""
        with self._lock:
            if self._exhausted:
                return self._exhausted
            self._iterations += 1
            if self._iterations >= self.config.max_iterations:
                self._exhausted = BudgetDimension.ITERATIONS
            return self._exhausted

    def consume_tool_call(self) -> Optional[BudgetDimension]:
        """记一次工具调用,返回触发维度(若有)"""
        with self._lock:
            if self._exhausted:
                return self._exhausted
            self._tool_calls += 1
            if self._tool_calls >= self.config.max_tool_calls:
                self._exhausted = BudgetDimension.TOOL_CALLS
            return self._exhausted

    def consume_tokens(self, total_tokens: int) -> Optional[BudgetDimension]:
        """累加一次 LLM 调用的 token,返回触发维度(若有)"""
        with self._lock:
            if self._exhausted:
                return self._exhausted
            self._tokens += max(0, int(total_tokens))
            if self._tokens >= self.config.max_tokens:
                self._exhausted = BudgetDimension.TOKENS
            return self._exhausted

    def check_runtime(self) -> Optional[BudgetDimension]:
        """检查墙钟预算是否超时(只反映墙钟,不因其他维度 exhausted 短路)。

        返回 RUNTIME 表示墙钟已超;返回 None 表示仍有时间。
        其他维度已 exhausted 不影响本方法——收尾阶段需要靠它判断墙钟是否耗尽,
        若在此短路会导致收尾 astream 在产出前就被 break(软停止空转)。
        """
        with self._lock:
            if time.perf_counter() >= self.deadline:
                # 保持"先到先得":只有尚未有其他维度触发时才记录 RUNTIME 为 exhausted
                if self._exhausted is None:
                    self._exhausted = BudgetDimension.RUNTIME
                return BudgetDimension.RUNTIME
            return None

    def snapshot(self) -> dict:
        """导出当前预算使用快照,供 trace/metrics"""
        with self._lock:
            return {
                "max_iterations": self.config.max_iterations,
                "max_runtime_s": self.config.max_runtime_s,
                "max_tool_calls": self.config.max_tool_calls,
                "max_tokens": self.config.max_tokens,
                "wrapup_grace_s": self.config.wrapup_grace_s,
                "iterations": self._iterations,
                "tool_calls": self._tool_calls,
                "tokens": self._tokens,
                "exhausted": self._exhausted.value if self._exhausted else None,
            }
