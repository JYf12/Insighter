import os

from deepagents import create_deep_agent
from dotenv import find_dotenv, load_dotenv
from langchain.agents.middleware import ModelCallLimitMiddleware, SummarizationMiddleware, ToolCallLimitMiddleware
from langchain.chat_models import init_chat_model
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv(find_dotenv())


llm = init_chat_model(
    model=os.getenv("LLM_MODEL_ID"),
    model_provider="openai",
)

checkpointer = InMemorySaver()
thread_config = {"configurable": {"thread_id": "erdaye"}}

"""
模型调用限制exit_behavior选择：
    -end：正常结束，返回一段限制提示
    -error：抛出异常，由业务层捕获处理
"""

# 1.主智能体配置中间件
main_agent = create_deep_agent(
    model=llm,
    tools=[],
    checkpointer=checkpointer,
    system_prompt="回答使用中文，调用对应的工具实现对应的功能",
    middleware=[
        ModelCallLimitMiddleware(
            thread_limit=1,  # 同一个 thread_id 下累计最多调用 1 次模型
            run_limit=1,  # 当前这次 invoke 内最多调用 1 次模型
            exit_behavior="error",  # 超限后抛出异常，便于后端统一捕获处理
        )
    ]
)

# 2.子智能体配置中间件（字典式示例）
sub_agent = {
    "model": llm,
    "tools": [],
    "checkpointer": checkpointer,
    "system_prompt": "回答使用中文，调用对应的工具实现对应的功能",
    "middleware": [
        ModelCallLimitMiddleware(
            thread_limit=1,  # 同一个 thread_id 下累计最多调用 1 次模型
            run_limit=1,  # 当前这次 invoke 内最多调用 1 次模型
            exit_behavior="error",  # 超限后抛出异常，便于后端统一捕获处理
        )
    ]
}

# 3.上下文压缩摘要中间件----在上下文快要过长之前，把一部分历史消息交给模型总结成短摘要，再把摘要放回上下文。这就是摘要中间件的作用。
context_compressor = SummarizationMiddleware(
    model=llm,
    trigger=("tokens", 4000),       # 消息累计到约 4000 token 时触发摘要(最稳妥的阈值设置是模型最大上下文窗口的2/3，或者3/4)
    keep=("messages", 15)           # 摘要后保留最近 20 条原始消息
)

main_agent_2 = create_deep_agent(
    model=llm,
    tools=[],
    checkpointer=checkpointer,
    system_prompt="回答使用中文，调用对应的工具实现对应功能",
    middleware=[context_compressor]
)

# 4.模型调用限制中间件
main_agent_3 = create_deep_agent(
    model=llm,
    tools=[],
    checkpointer=checkpointer,
    system_prompt="回答使用中文，调用对应的工具实现对应功能",
    middleware=[
        ModelCallLimitMiddleware(
            thread_limit=1,  # 同一个 thread_id 下累计最多调用 1 次模型
            run_limit=1,  # 当前这次 invoke 内最多调用 1 次模型
            exit_behavior="error",  # 超限后抛出异常，便于后端统一捕获处理
        )
    ]
)

# 5.工具调用限制中间件
"""
该中间件分为两种范围的限制：
    -全局限制：所有工具加起来最多调用多少次
    -单工具限制：指定某个工具最多调用多少次
    
工具调用exit_behavior的选择：
    -continue：阻止这次工具调用，把错误作为工具消息交回给 Agent
    -error：立刻抛异常，由业务层捕获处理
    -end：直接结束当前执行
"""
# 全局限制
global_tool_limit = ToolCallLimitMiddleware(
    thread_limit=10,  # 同一条会话线程里，所有工具累计最多调用 10 次
    run_limit=2,  # 当前这次 invoke 内，所有工具最多调用 5 次
)

search_tool_limit = ToolCallLimitMiddleware(
    tool_name="search_web",  # 只限制 search_web 这个工具
    run_limit=3,  # 当前这次 invoke 内最多搜索 3 次
    exit_behavior="error",  # 超限后直接抛异常，交给业务层处理
)

sql_tool_limit = ToolCallLimitMiddleware(
    tool_name="execute_sql",  # 只限制 execute_sql 这个工具
    run_limit=2,  # 当前这次 invoke 内最多执行 2 次 SQL
    exit_behavior="error",
)
main_agent_4 = create_deep_agent(
    model=llm,
    tools=[],
    checkpointer=checkpointer,
    system_prompt="回答使用中文，调用对应的工具实现对应功能",
    middleware=[global_tool_limit, search_tool_limit, sql_tool_limit]
)
