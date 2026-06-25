"""
Tavily 网络搜索工具模块

封装 internet_search 工具，供网络搜索子智能体检索互联网公开信息
工具内部会先通过 monitor 上报调用参数，再请求 Tavily API 返回结构化搜索结果

注意：计时/埋点/指标更新已由 observability_middleware 统一接管，
工具文件不需要再手工调用 time.perf_counter() 或 monitor.report_tool_end/failure()。
"""

import json
import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient

from app.api.monitor import monitor
from app.persistence.cache_store import check_cache, save_to_cache, init_cache
from app.utils.logger import get_logger

load_dotenv()

_logger = get_logger("tavily_tool")


# TavilyClient 是实际访问搜索服务的客户端；模块级复用可避免每次工具调用重复初始化
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# 缓存命名空间，与其他工具隔离
CACHE_NAMESPACE = "tavily"


def _build_cache_query(query: str, topic: str, max_results: int) -> str:
    """构造缓存查找用的复合查询文本，避免不同搜索配置互相污染"""
    return f"{query}|||{topic}|||{max_results}"


# @tool 会把函数签名和 docstring 暴露给 DeepAgents，模型据此决定是否调用以及如何填参
@tool
async def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
):
    """
    根据用户问题检索互联网公开信息

    注意：本工具只用于外部公开网页、新闻、政策等信息，不用于查询业务数据库或 RAGFlow 私有知识库
    :param query: 搜索关键词或自然语言问题
    :param topic: 搜索主题，可选 news、finance、general
    :param max_results: 返回的最大结果数
    :param include_raw_content: 是否返回网页原文内容；False 返回摘要，True 尝试返回更完整正文
    :return: Tavily 返回的结构化搜索结果
    """
    # 埋点：工具一被调用，前端就能看到本次搜索参数（中间件接管计时和 end/failure 事件）
    monitor.report_tool(
        tool_name="网络搜索工具",
        args={
            "query": query,
            "topic": topic,
            "max_results": max_results,
            "include_raw_content": include_raw_content,
        },
    )

    # 1. 语义缓存检查
    cache_query = _build_cache_query(query, topic, max_results)
    cached = await check_cache(CACHE_NAMESPACE, cache_query)
    if cached is not None:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            _logger.warning("Cache result JSON parse failed, falling back to real search")
            # 继续执行真实搜索

    # 2. Tavily 返回 query、results、title、url、content 等结构化字段，后续由子智能体阅读并汇总
    result = tavily_client.search(
        query=query,
        topic=topic,
        max_results=max_results,
        include_raw_content=include_raw_content,
    )

    # 3. 存入缓存（异步，不阻塞返回）
    try:
        result_json = json.dumps(result, ensure_ascii=False, default=str)
        await save_to_cache(CACHE_NAMESPACE, cache_query, result_json)
    except Exception as e:
        _logger.debug("Failed to cache search result", extra={"error": str(e)})

    return result


if __name__ == "__main__":
    import asyncio
    from pprint import pprint

    async def main():
        await init_cache()  # 初始化缓存
        # 本地调试入口：直接运行本文件可验证 TAVILY_API_KEY 和 Tavily API 是否可用
        result = await internet_search.ainvoke(
            # {"query": "2026中国节假日放假安排表，我好想放假啊"}
            {"query": "中国2026节假日汇总"}
        )
        pprint(result)

    asyncio.run(main())
