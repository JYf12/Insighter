"""
统一埋点与预算检查回调(InstrumentationCallback)

subsume token_tracker + observability_middleware 的埋点职责,作为唯一 instrumentation 入口。
经 astream config["callbacks"] 注入,由 LangGraph 传播到主 Agent + 所有子智能体子图 +
所有 ToolNode,从而覆盖子智能体内部(当前 wrap_tool_call middleware 覆盖不到处)。

关键洞察:LangChain callback 每个钩子自带 run_id + parent_run_id,span 父子树天然可得,
无需自建 ContextVar 传播父子关系;trace_id 取自 RunContext(= run_id)。

职责:
- on_chat_model_start/end → 开/闭 LLM span + 计 iteration + 记 token + 查 token budget
- on_tool_start/end/error  → 开/闭 tool span + 计 tool_call + 查 tool_call budget + 错误分类(埋点)
- 预算触达上限时一次性记录 metric + 推 monitor,软停止本身由 run_deep_agent 的 astream 循环执行
"""

from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.agent.run_context import get_current_run_context
from app.api.metrics import metrics_collector
from app.api.monitor import monitor
from app.utils.logger import get_logger

_logger = get_logger("instrumentation")


def _extract_tool_name(serialized: Optional[dict]) -> str:
    """从 serialized 提取工具/模型名,失败回退 unknown"""
    if not serialized:
        return "unknown"
    return serialized.get("name") or serialized.get("id") or "unknown"


def _report_budget_if_newly_exhausted(rc, was: Optional[str], dim) -> None:
    """预算从 None → 某维度时,恰好记录一次 metric + monitor"""
    if dim and not was:
        metrics_collector.record_budget_exceeded(dim.value)
        monitor._emit(
            "budget_exceeded",
            f"预算触达上限:{dim.value}",
            {"dimension": dim.value, "budget": rc.budget.snapshot()},
        )


class InstrumentationCallback(BaseCallbackHandler):
    """统一 trace + budget + token 回调(主+子智能体全覆盖)"""

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        name = _extract_tool_name(serialized)
        rc.open_span(
            span_id=str(run_id),
            parent_span_id=str(parent_run_id) if parent_run_id else None,
            name=name,
            kind="llm",
        )
        # 计 iteration 并查预算(软停止由 astream 循环接管)
        was = rc.budget.exhausted
        dim = rc.budget.consume_iteration()
        _report_budget_if_newly_exhausted(rc, was, dim)

    def on_llm_end(self, response: LLMResult, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        span_id = str(run_id)

        # ---- token 记账(沿用 token_tracker 的 provider 兼容逻辑)----
        prompt_tokens = completion_tokens = total_tokens = 0
        model_name = "unknown"
        llm_output = response.llm_output
        if llm_output:
            token_usage = llm_output.get("token_usage", {}) or {}
            prompt_tokens = (
                token_usage.get("prompt_tokens", 0) or token_usage.get("input_tokens", 0) or 0
            )
            completion_tokens = (
                token_usage.get("completion_tokens", 0) or token_usage.get("output_tokens", 0) or 0
            )
            total_tokens = (
                token_usage.get("total_tokens", 0) or (prompt_tokens + completion_tokens)
            )
            model_name = llm_output.get("model_name", "unknown")

            metrics_collector.record_token_usage(
                model=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
            monitor.report_token_usage(model_name, prompt_tokens, completion_tokens, total_tokens)

            # 查 token 预算
            was = rc.budget.exhausted
            dim = rc.budget.consume_tokens(total_tokens)
            _report_budget_if_newly_exhausted(rc, was, dim)

        rc.add_attribute(
            span_id,
            "tokens",
            {"prompt": prompt_tokens, "completion": completion_tokens, "total": total_tokens, "model": model_name},
        )
        rc.close_span(span_id, status="ok")

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        rc.close_span(str(run_id), status="error", attributes={"error": str(error)})
        _logger.error("LLM 调用失败", extra={"error": str(error)})

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        name = _extract_tool_name(serialized)
        inputs = kwargs.get("inputs") or {}
        attrs = {"input": str(inputs)[:200]}
        # task 工具 = 子智能体委派;单独提取 subagent_type 供 trace 折叠时命名子智能体子图
        if name == "task" and isinstance(inputs, dict) and inputs.get("subagent_type"):
            attrs["subagent_type"] = inputs["subagent_type"]
        rc.open_span(
            span_id=str(run_id),
            parent_span_id=str(parent_run_id) if parent_run_id else None,
            name=name,
            kind="tool",
            attributes=attrs,
        )
        # 计 tool_call 并查预算
        was = rc.budget.exhausted
        dim = rc.budget.consume_tool_call()
        _report_budget_if_newly_exhausted(rc, was, dim)

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        out_str = str(output) if output is not None else ""
        result_summary = (out_str[:100] + "...") if len(out_str) > 100 else out_str
        span = rc.close_span(str(run_id), status="ok", attributes={"result": result_summary})
        if span is not None:
            monitor.report_tool_end(span.name, span.duration_ms or 0.0, result_summary)

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        span = rc.close_span(str(run_id), status="error", attributes={"error": str(error)})
        # 错误分类(仅用于埋点/监控;实际重试由 @resilient 装饰器在工具层执行)
        try:
            from app.agent.recovery import classify

            kind = classify(error, span.name if span else "")
        except Exception:
            kind = None
        if span is not None:
            duration_ms = span.duration_ms or 0.0
            monitor.report_tool_failure(span.name, duration_ms, str(error))
            metrics_collector.record_tool_failed(span.name)
            metrics_collector.record_tool_duration(span.name, duration_ms)
        _logger.error(
            "工具调用失败",
            extra={"tool": span.name if span else "unknown", "kind": kind.value if kind else None, "error": str(error)},
        )

    # ------------------------------------------------------------------
    # Chain 调用(结构层)
    #
    # LangGraph 的 agent 节点 / 子智能体子图 / 工具执行节点各自是一个 chain 级
    # runnable。工具与 LLM span 的 parent_run_id 指向这些 chain,若我们不开 chain
    # span,它们的父节点就永远不存在 → trace 树无法连接。这里补齐这一层,让每个
    # 工具/LLM 的父引用都能解析到真实 span(树结构由此自然连通)。
    # chain span 仅作结构载体,不消耗预算、不计指标。
    # ------------------------------------------------------------------

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        name = _extract_tool_name(serialized)
        rc.open_span(
            span_id=str(run_id),
            parent_span_id=str(parent_run_id) if parent_run_id else None,
            name=name if name != "unknown" else "chain",
            kind="chain",
        )

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        rc.close_span(str(run_id), status="ok")

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        rc = get_current_run_context()
        if rc is None:
            return
        rc.close_span(str(run_id), status="error", attributes={"error": str(error)})


# 全局单例(经 astream config["callbacks"] 与 model.callbacks 注入)
instrumentation_callback = InstrumentationCallback()
