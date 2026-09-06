"""
[已弃用] Token 记账职责已被 app/agent/instrumentation.py 的 InstrumentationCallback.on_llm_end 吸收。

新方案经 astream 的 config["callbacks"] 注入,统一覆盖主+子智能体的 LLM 调用 token 采集,
并叠加 trace span 与预算检查。llm.py 不再单独挂载本回调,避免与 config 回调重复触发。
本文件保留仅供历史参考。

---

LangChain Token 追踪器

通过挂载到 model.callbacks 上，利用 LangChain 的 Callback 层级传播机制，
自动捕获主智能体和所有子智能体的 LLM 调用，实现全链路 Token 覆盖。

注意：astream 只产出主智能体的消息流，子智能体的 LLM 调用发生在 DeepAgents
子图内部，主循环完全不可见。因此不能从 main_agent.py 的 model node 提取，
必须在模型层面挂载 BaseCallbackHandler。
"""

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.api.metrics import metrics_collector
from app.api.monitor import monitor


class LangChainTokenTracker(BaseCallbackHandler):
    """
    捕获所有 LLM 调用的 token 用量。

    通过挂载到 model.callbacks 上，主智能体和所有子智能体的
    模型调用都会经过 on_llm_end，实现全链路覆盖。
    """

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        """
        LLM 调用完成时触发，从 LLMResult.llm_output 中提取 token_usage。

        兼容不同模型 provider 的字段名差异：
        - OpenAI 风格：prompt_tokens / completion_tokens / total_tokens
        - 部分自定义模型：input_tokens / output_tokens
        """
        llm_output = response.llm_output
        if not llm_output:
            return

        token_usage = llm_output.get("token_usage", {})
        if not token_usage:
            return

        # 防御性兼容两种字段命名
        prompt_tokens = (
            token_usage.get("prompt_tokens", 0)
            or token_usage.get("input_tokens", 0)
            or 0
        )
        completion_tokens = (
            token_usage.get("completion_tokens", 0)
            or token_usage.get("output_tokens", 0)
            or 0
        )
        total_tokens = (
            token_usage.get("total_tokens", 0)
            or (prompt_tokens + completion_tokens)
        )

        model_name = llm_output.get("model_name", "unknown")

        # 更新指标采集器（始终记录，不受前端影响）
        metrics_collector.record_token_usage(
            model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        # 可选推送到前端（WebSocket）
        # monitor.report_token_usage(
        #     model=model_name,
        #     prompt_tokens=prompt_tokens,
        #     completion_tokens=completion_tokens,
        #     total_tokens=total_tokens,
        # )
