"""
主智能体组装与异步执行模块

负责把模型、主提示词、文件类工具和三个专家子智能体组装成 DeepAgent，
并提供 run_deep_agent 作为后续 API 层调用的统一入口。运行时还会为每个
session_id 创建独立工作目录，并把工具调用、子智能体调用和最终结果推送给前端。
"""

import asyncio
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from deepagents import create_deep_agent

from app.agent.instrumentation import instrumentation_callback
from app.agent.llm import model
from app.agent.prompts import main_agent_content
from app.agent.run_context import (
    RunContext,
    reset_run_context,
    run_registry,
    set_current_run_context,
)
from app.agent.subagents.database_query_agent import database_query_agent
from app.agent.subagents.knowledge_base_agent import knowledge_base_agent
from app.agent.subagents.network_search_agent import network_search_agent
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.api.monitor import monitor
from app.agent.budget import BudgetConfig
from app.persistence.trace_store import get_trace_store
from app.utils.logger import get_logger

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tool import read_file_content

_logger = get_logger("main_agent")

# 主智能体实例在 FastAPI 生命周期中由 init_main_agent() 初始化
# 使用 SqliteSaver（或其他注入的 checkpointer）替代原先硬编码的 InMemorySaver
_main_agent = None


def init_main_agent(checkpointer):
    """
    使用指定的 checkpointer 初始化主智能体

    在 FastAPI lifespan 启动阶段调用，注入 AsyncSqliteSaver / InMemorySaver 等实例。
    原先 InMemorySaver() 硬编码已被移除，checkpointer 由上层生命周期管理。

    create_summarization_tool_middleware中间件赋予主智能体工具化压缩上下文的能力
    """
    global _main_agent
    _main_agent = create_deep_agent(
        model=model,
        system_prompt=main_agent_content["system_prompt"],
        tools=[generate_markdown, convert_md_to_pdf, read_file_content],
        checkpointer=checkpointer,
        subagents=[database_query_agent, network_search_agent, knowledge_base_agent],
    )
    _logger.info("Agent initialized with injected checkpointer (instrumentation via callbacks)")


def get_main_agent():
    """获取当前主智能体实例"""
    if _main_agent is None:
        raise RuntimeError(
            "MainAgent not initialized. "
            "Ensure init_main_agent() was called during FastAPI lifespan startup."
        )
    return _main_agent

# 当前文件位于 app/agent/main_agent.py，parents[1] 即 app 目录
project_root_path = Path(__file__).parents[1].resolve()

# 软停止收尾指令：预算触达上限时注入，让模型用已有信息产出阶段性成果而非硬中断
WRAP_UP_PROMPT = (
    "【预算提示】本次任务的 {dim} 预算已接近上限。请立即停止调用工具，"
    "基于已收集到的信息完成最终总结与交付，不要再发起新的工具调用或子智能体任务。"
)

# 硬超时倍率：在 max_runtime 之上再给 1.5 倍窗口兜底（含收尾阶段），超时则强制终止
_HARD_TIMEOUT_FACTOR = 1.5


async def run_deep_agent(task_query, session_id, resume=False, budget_config=None, run_id=None):
    """
    异步流式执行主智能体

    API 层会为每次任务传入用户问题和 session_id。本函数负责准备会话目录、
    复制上传文件、写入 ContextVar，并在流式执行过程中把关键事件上报给前端。
    :param task_query: 前端提交的原始任务问题
    :param session_id: 当前任务 ID，同时用于 thread_id、输出目录和 WebSocket 定向推送
    :param resume: 是否为恢复执行；True 时跳过路径指令注入，传入 None 从检查点恢复
    :param budget_config: per-task 预算覆盖（None 时用环境变量默认值）
    :param run_id: 本次执行尝试的 ID（None 时自动生成）；与 trace_id 一致，同 thread 可多 run
    """
    _logger.info(
        "开始执行会话",
        extra={"session_id": session_id, "resume": resume},
    )

    # 每个会话独立使用 output/session_{session_id}，避免不同用户的产物互相覆盖
    session_dir = project_root_path / "output" / f"session_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # 前端和工具使用绝对路径；提示词里只给模型相对路径，降低模型误用系统绝对路径的概率
    session_dir_str = str(session_dir).replace("\\", "/")
    relative_session_dir_str = str(session_dir.relative_to(project_root_path)).replace("\\", "/")

    # 上传文件先落在 updated/session_{session_id}，执行前复制到本次 output 工作目录
    # 这样读文件工具和生成文件工具都只需要围绕同一个 session_dir 工作
    updated_dir_path = project_root_path / "updated" / f"session_{session_id}"
    updated_info_prompt = ""
    if updated_dir_path.exists():
        files = [f.name for f in updated_dir_path.iterdir() if f.is_file()]
        if files:
            for filename in files:
                # copy2 会保留上传文件的修改时间、权限等元数据，便于后续排查文件来源
                shutil.copy2(updated_dir_path / filename, session_dir / filename)

            # 把上传文件列表注入用户消息，提醒模型先调用 read_file_content 获取附件内容
            updated_info_prompt = (
                "\n    [已上传文件] 已加载到工作目录:\n"
                + "\n".join([f"- {f}" for f in files])
                + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
            )

    # ContextVar 让深层工具无需显式传参，也能拿到当前会话目录和 WebSocket thread_id
    session_dir_token = set_session_context(session_dir_str)
    session_id_token = set_thread_context(session_id)

    # 前端拿到工作目录后，可以展示本次任务生成的 Markdown/PDF 等产物
    monitor.report_session_dir(session_dir_str)

    # 建 RunContext：承载本次 run 的预算账本与 trace span 缓冲，经 ContextVar 传播到子智能体
    run_ctx = RunContext(thread_id=session_id, budget_config=budget_config, run_id=run_id)
    run_registry.register(run_ctx)
    run_context_token = set_current_run_context(run_ctx)

    # checkpointer 依赖 thread_id 区分会话记忆；同一 session_id 会复用同一条执行上下文
    # callbacks 经 LangGraph 传播到主 Agent + 子智能体子图 + 所有 ToolNode（覆盖子智能体内部）
    # recursion_limit 作为 max_iterations 的图级硬后底（= max_iterations + 5），防软停止漏触发时无限循环
    config = {
        "configurable": {"thread_id": session_id},
        "callbacks": [instrumentation_callback],
        "recursion_limit": run_ctx.budget.config.max_iterations + 5,
    }

    # 工作环境指令是运行时动态补充的，约束模型只在当前会话目录读写文件
    # 恢复执行时跳过指令注入，因为上一次执行时已经注入了相同的指令
    if resume:
        path_instruction = ""
    else:
        path_instruction = f"""
    【工作环境指令】
    工作目录: {relative_session_dir_str}
    {updated_info_prompt}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir_str}/filename'
    2. 读取已上传的文件时，请直接将文件名（例如：'开篇.txt'）作为 filename 参数传入（read_file_content）读取工具，不要带上任何目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """

    # 追踪子智能体调用的起始时间，用于在 model node 检测完成后上报 subagent_end
    _assistant_start_times: dict[str, float] = {}

    # 恢复执行时传入 None 让 LangGraph 从最后一个 checkpoint 自动恢复；正常执行传入完整消息
    if resume:
        stream_input = None
    else:
        stream_input = {
            "messages": [{"role": "user", "content": task_query + path_instruction}]
        }

    async def _consume(stream_input_arg, *, is_wrapup=False):
        """消费 astream 流；每 chunk 查墙钟预算与已触达标志，触达即 break（软停止）

        :param is_wrapup: 收尾阶段；True 时不再因 budget.exhausted 二次软停止（防递归）
        """
        async for chunk in get_main_agent().astream(stream_input_arg, config=config):
            # 墙钟预算周期性检查 + 已触达上限检查
            if run_ctx.budget.check_runtime() or (run_ctx.budget.exhausted and not is_wrapup):
                break
            # chunk 形如 {“model”: {“messages”: [...]}}，这里主要关心模型最新消息
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                messages = state["messages"]
                if messages and isinstance(messages, list):
                    last_msg = messages[-1]
                    if node_name == "model":
                        if last_msg.tool_calls:
                            # DeepAgents 调用子智能体时，本质上会产生名为 task 的工具调用
                            for tool_call in last_msg.tool_calls:
                                if tool_call["name"] == "task":
                                    # 子智能体调用单独上报，前端可以展示"正在调用哪个专家助手"
                                    subagent_type = tool_call["args"]["subagent_type"]
                                    monitor.report_assistant(
                                        subagent_type,
                                        {"description": tool_call["args"]["description"]},
                                    )
                                    _assistant_start_times[subagent_type] = time.perf_counter()
                        elif last_msg.content:
                            # 当 model node 产出最终回复时，检查是否有子智能体刚完成
                            if _assistant_start_times:
                                for assistant_name, start_time in list(_assistant_start_times.items()):
                                    duration_ms = (time.perf_counter() - start_time) * 1000
                                    monitor.report_assistant_end(assistant_name, duration_ms)
                                    del _assistant_start_times[assistant_name]
                            # 模型没有继续调用工具时，最新文本内容就是本轮可反馈给前端的结果
                            _logger.info(f"主智能体本轮结果: {str(last_msg.content)[:100]}")
                            monitor.report_task_result(last_msg.content)

    try:
        # 两阶段执行：正常消费 → 若预算触达则注入收尾指令再消费一轮
        hard_cap = run_ctx.budget.config.max_runtime_s * _HARD_TIMEOUT_FACTOR
        await asyncio.wait_for(_consume(stream_input), timeout=hard_cap)

        if run_ctx.budget.exhausted:
            dim = run_ctx.budget.exhausted.value
            _logger.info("预算触达上限,注入收尾指令", extra={"dimension": dim, "thread_id": session_id})
            monitor._emit("budget_soft_stop", f"预算触达 {dim},进入收尾阶段", {"dimension": dim})
            wrap_up = {"messages": [{"role": "user", "content": WRAP_UP_PROMPT.format(dim=dim)}]}
            # 收尾阶段使用剩余的墙钟预算(至少 10s),不再二次软停止
            remaining = max(10.0, hard_cap - (time.perf_counter() - run_ctx.budget.started_at))
            await asyncio.wait_for(_consume(wrap_up, is_wrapup=True), timeout=remaining)

        run_ctx.finalize("completed")
    except asyncio.TimeoutError:
        # 硬超时兜底：收尾阶段也超时或主阶段 stall，强制终止
        _logger.error("硬超时兜底触发", extra={"thread_id": session_id})
        monitor._emit("error", f"任务硬超时(max_runtime×{_HARD_TIMEOUT_FACTOR}),强制终止")
        run_ctx.finalize("failed")
    except asyncio.CancelledError:
        monitor.report_task_cancelled()
        run_ctx.finalize("cancelled")
        raise
    except Exception as e:
        # 异步执行异常也走 monitor，保证前端能收到明确错误事件
        _logger.error("执行主智能发生异常", extra={"error": str(e)})
        monitor._emit("error", f"执行主智能发生异常信息：{str(e)}")
        run_ctx.finalize("failed")
    finally:
        # 持久化本次 run 的完整 trace span 树到 Redis 近期窗口（失败不影响终态）
        try:
            await get_trace_store().save_trace(
                run_id=run_ctx.run_id,
                thread_id=session_id,
                spans=run_ctx.collapsed_span_dicts(),
                status=run_ctx.status,
                started_at=run_ctx.started_at_iso,
                ended_at=run_ctx.ended_at_iso or datetime.now(timezone.utc).isoformat(),
                budget_snapshot=run_ctx.budget.snapshot(),
            )
        except Exception as trace_err:
            _logger.warning("Trace flush failed", extra={"run_id": run_ctx.run_id, "error": str(trace_err)})
        run_registry.unregister(run_ctx.run_id)
        # 任务结束后恢复 ContextVar，避免本次会话目录/thread_id 残留到后续请求
        reset_session_context(session_dir_token, session_id_token)
        reset_run_context(run_context_token)


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        run_deep_agent("从网络查询机器人信息，并生成Markdown文件", "test_session_001")
    )
