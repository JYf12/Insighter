"""
Task-run 上下文(RunContext)

承载一次 task run 的全部状态:身份(run_id/trace_id)、预算(Budget)、trace span 缓冲。
InstrumentationCallback 与软停止逻辑共享同一个 RunContext 实例,避免 metrics↔monitor
式的状态分裂。

- run_id:本次执行尝试的唯一 ID(同 thread_id 可多次 run:首次 + 恢复续跑)
- trace_id:= run_id,作为 span 树根
- 经 ContextVar 在 LangGraph 子图/工具链路中传播(与现有 thread_id 同机制)
- RunRegistry:进程内活跃 run 索引 + Redis 镜像,供跨 run/重启查询
"""

import threading
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.agent.budget import Budget, BudgetConfig
from app.api.context import set_run_context
from app.utils.logger import get_logger

_logger = get_logger("run_context")


# ------------------------------------------------------------------
# Span
# ------------------------------------------------------------------


@dataclass
class Span:
    """单个 trace span,序列化后写入 TraceStore"""

    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: str  # "llm" | "tool" | "chain" | "subagent"
    start_perf: float  # perf_counter 时基,用于算 duration
    start_iso: str
    end_iso: Optional[str] = None
    duration_ms: Optional[float] = None
    status: str = "ok"  # "ok" | "error"
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start": self.start_iso,
            "end": self.end_iso,
            "duration_ms": round(self.duration_ms, 2) if self.duration_ms is not None else None,
            "status": self.status,
            "attributes": self.attributes,
        }


# ------------------------------------------------------------------
# RunContext
# ------------------------------------------------------------------


class RunContext:
    """一次 task run 的上下文对象"""

    def __init__(self, thread_id: str, budget_config: Optional[BudgetConfig] = None, run_id: Optional[str] = None) -> None:
        self.run_id: str = run_id or str(uuid.uuid4())
        self.trace_id: str = self.run_id
        self.thread_id: str = thread_id
        self.budget: Budget = Budget(config=budget_config or BudgetConfig.from_env())
        self.started_at_perf: float = time.perf_counter()
        self.started_at_iso: str = datetime.now(timezone.utc).isoformat()
        self.ended_at_iso: Optional[str] = None
        self.status: str = "running"

        # 已闭合的 span 缓冲(open_span/close_span 时填充)
        self._spans: list[Span] = []
        # 进行中的 span:langchain run_id -> Span(含起始时间)
        self._open: dict[str, Span] = {}
        self._lock = threading.Lock()

    # -- span 生命周期 --

    def open_span(
        self,
        span_id: str,
        parent_span_id: Optional[str],
        name: str,
        kind: str,
        attributes: Optional[dict] = None,
    ) -> Span:
        """开启一个 span;parent 为 None 时挂到 run 根(trace_id)"""
        span = Span(
            trace_id=self.trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id or self.trace_id,
            name=name,
            kind=kind,
            start_perf=time.perf_counter(),
            start_iso=datetime.now(timezone.utc).isoformat(),
            attributes=attributes or {},
        )
        with self._lock:
            self._open[span_id] = span
        return span

    def close_span(
        self,
        span_id: str,
        status: str = "ok",
        attributes: Optional[dict] = None,
    ) -> Optional[Span]:
        """闭合一个 span,移入缓冲并计算 duration"""
        with self._lock:
            span = self._open.pop(span_id, None)
            if span is None:
                # 未追踪的 span(如未 open 即 close),忽略
                return None
            if attributes:
                span.attributes.update(attributes)
            span.status = status
            span.end_iso = datetime.now(timezone.utc).isoformat()
            span.duration_ms = (time.perf_counter() - span.start_perf) * 1000
            self._spans.append(span)
            return span

    def add_attribute(self, span_id: str, key: str, value: Any) -> None:
        """向进行中的 span 追加属性(token 等)"""
        with self._lock:
            span = self._open.get(span_id)
            if span is not None:
                span.attributes[key] = value

    # -- 状态 --

    def finalize(self, status: str) -> None:
        """标记 run 终态,并兜底落盘所有未闭合的 span

        run 异常/取消时,顶层 graph/agent 的 on_chain_end 可能不会触发,导致
        对应 span 一直卡在 _open;这里把它们标记为 interrupted 后移入缓冲,
        保证失败 run 的 trace 结构也完整可复盘。
        """
        with self._lock:
            abandoned = list(self._open.keys())
            for span_id in abandoned:
                span = self._open.pop(span_id)
                span.status = "interrupted"
                span.end_iso = datetime.now(timezone.utc).isoformat()
                span.duration_ms = (time.perf_counter() - span.start_perf) * 1000
                self._spans.append(span)
            if abandoned:
                _logger.warning(
                    "Finalize drained open spans",
                    extra={"run_id": self.run_id, "count": len(abandoned)},
                )
        self.status = status
        self.ended_at_iso = datetime.now(timezone.utc).isoformat()

    def span_dicts(self) -> list[dict[str, Any]]:
        """导出已闭合 span(序列化),供 TraceStore"""
        with self._lock:
            return [s.to_dict() for s in self._spans]

    def collapsed_span_dicts(self) -> list[dict[str, Any]]:
        """导出折叠后的干净 span 树

        折叠规则(针对 LangChain/LangGraph 的匿名 wrapper 层):
        1. 删除叶子 chain(无子节点,纯噪音)。
        2. 折叠单子节点 chain(只有一个孩子):移除该 chain,把唯一孩子直接挂到它的父节点。
           反复执行,直到没有可折叠的 chain。
        3. 保留分叉 chain(多子节点)作为结构节点,并重命名:
           - 挂在 trace 根下的 → "main_agent"(主智能体图)
           - 挂在 task 工具下的 → 该 task 的 subagent_type(子智能体子图)

        这样 68 个 span 会收敛为"main_agent → llm/tool + task → 子智能体 → llm/tool",
        直观反映"主 Agent → 子 Agent → 子 Agent 工具"的调用链。
        """
        with self._lock:
            spans = [s.to_dict() for s in self._spans]

        # 反复折叠叶子/单子 chain(操作副本上的 parent_span_id,不污染原始 Span)
        while True:
            by_id = {s["span_id"]: s for s in spans}
            children: dict[str, list[dict]] = {}
            for s in spans:
                children.setdefault(s["parent_span_id"], []).append(s)

            to_remove: set[str] = set()
            for s in spans:
                if s["kind"] != "chain":
                    continue
                ch = children.get(s["span_id"], [])
                if len(ch) == 0:
                    to_remove.add(s["span_id"])  # 叶子 chain
                elif len(ch) == 1:
                    to_remove.add(s["span_id"])  # 单子 chain:唯一孩子上提
                    ch[0]["parent_span_id"] = s["parent_span_id"]

            if not to_remove:
                break
            spans = [s for s in spans if s["span_id"] not in to_remove]

        # 命名保留的分叉 chain
        for s in spans:
            if s["kind"] != "chain":
                continue
            if s["parent_span_id"] == self.trace_id:
                s["name"] = "main_agent"
                continue
            parent = next((x for x in spans if x["span_id"] == s["parent_span_id"]), None)
            if parent is not None and parent["kind"] == "tool" and parent["name"] == "task":
                sub = (parent.get("attributes") or {}).get("subagent_type")
                s["name"] = f"subagent:{sub}" if sub else "subagent"

        return spans

    def snapshot(self) -> dict[str, Any]:
        """导出 run 元信息 + 预算快照"""
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "started_at": self.started_at_iso,
            "ended_at": self.ended_at_iso,
            "budget": self.budget.snapshot(),
            "span_count": len(self._spans),
        }


# ------------------------------------------------------------------
# ContextVar:当前活跃 RunContext
# ------------------------------------------------------------------

_run_context_ctx: ContextVar[Optional[RunContext]] = ContextVar("run_context", default=None)


def set_current_run_context(rc: RunContext) -> Token[Optional[RunContext]]:
    """绑定当前请求链路的 RunContext,同时同步 run_id 到 context.py 的 ContextVar"""
    set_run_context(rc.run_id)  # 供 metrics 等仅需 id 的模块取用
    return _run_context_ctx.set(rc)


def get_current_run_context() -> Optional[RunContext]:
    """深层回调/工具取当前 RunContext(未设置时返回 None,调用方需自行兜底)"""
    return _run_context_ctx.get()


def reset_run_context(token: Token[Optional[RunContext]]) -> None:
    _run_context_ctx.reset(token)


# ------------------------------------------------------------------
# RunRegistry:进程内活跃 run 索引
# ------------------------------------------------------------------


class RunRegistry:
    """进程内 run_id -> RunContext 索引(并发安全)

    供 trace flush 与跨 run 查询;Redis 侧的持久化由 TraceStore 负责。
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunContext] = {}
        self._lock = threading.Lock()

    def register(self, rc: RunContext) -> None:
        with self._lock:
            self._runs[rc.run_id] = rc

    def unregister(self, run_id: str) -> Optional[RunContext]:
        with self._lock:
            return self._runs.pop(run_id, None)

    def get(self, run_id: str) -> Optional[RunContext]:
        with self._lock:
            return self._runs.get(run_id)

    def active_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [rc.snapshot() for rc in self._runs.values() if rc.status == "running"]


# 全局单例
run_registry = RunRegistry()
