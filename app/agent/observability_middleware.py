"""
全链路可观测性中间件

通过 @wrap_tool_call 实现工具调用的集中式埋点、计时和指标更新。
统一接管 Phase 1 中在每个工具文件中手工添加的 perf_counter / monitor 调用，
消除重复代码。

由于项目使用 astream() 异步流式执行，中间件函数必须声明为 async 并
await handler(request)，兼容异步 handler。

工作流程：
  user request
    → Agent 决定调用工具
    → middleware.__before__
        → time.perf_counter() 开始计时
        → monitor.report_tool()
        → MetricsCollector.record_tool_invoked()
    → await handler(request)  [实际工具执行]
    → middleware.__after__  (正常返回或异常)
        → duration = time.perf_counter() - start
        → 成功: monitor.report_tool_end(), MetricsCollector.record_tool_duration()
        → 失败: monitor.report_tool_failure(), MetricsCollector.record_tool_failed()
        → logger.info/debug
    → 返回/抛出结果给 Agent
"""

import time

from langchain.agents.middleware import wrap_tool_call

from app.api.metrics import metrics_collector
from app.api.monitor import monitor
from app.utils.logger import get_logger

_logger = get_logger("observability_middleware")


@wrap_tool_call
async def observability_middleware(request, handler):
    """
    全链路可观测性工具调用中间件（异步版本）

    :param request: 工具调用请求，包含 runtime、state、tool_call_id 和 tool_call 等
    :param handler: 目标工具的异步调用器，调用 await handler(request) 才真正执行工具
    :return: 目标工具的返回结果
    """
    # 从 tool_call 中提取工具名称
    tool_name = "unknown_tool"
    if hasattr(request, "tool_call") and isinstance(request.tool_call, dict):
        tool_name = request.tool_call.get("name", "unknown_tool")

    # 前置埋点和指标更新
    start_time = time.perf_counter()
    metrics_collector.record_tool_invoked(tool_name)        # 子智能体的埋点体现在task上，其内部的工具调用并不会直接记录

    _logger.debug("工具调用开始", extra={"tool_name": tool_name})

    try:
        # ---- 真正执行目标工具 ----
        result = await handler(request)

        # 后置成功处理
        duration_ms = (time.perf_counter() - start_time) * 1000
        metrics_collector.record_tool_duration(tool_name, duration_ms)

        # 生成结果摘要（截断避免日志过长）
        result_str = str(result) if result is not None else ""
        result_summary = (result_str[:100] + "...") if len(result_str) > 100 else result_str
        # monitor.report_tool_end(tool_name, duration_ms, result_summary)  # 暂不向前端上报

        _logger.debug("工具调用完成", extra={
            "tool_name": tool_name,
            "duration_ms": round(duration_ms, 1),
        })

        return result

    except Exception as e:
        # 后置失败处理
        duration_ms = (time.perf_counter() - start_time) * 1000
        metrics_collector.record_tool_failed(tool_name)
        metrics_collector.record_tool_duration(tool_name, duration_ms)

        monitor.report_tool_failure(tool_name, duration_ms, str(e))

        _logger.error("工具调用失败", extra={
            "tool_name": tool_name,
            "duration_ms": round(duration_ms, 1),
            "error": str(e),
        })

        raise
