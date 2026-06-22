"""
DeepAgents Backend：StoreBackend 跨线程长期记忆

演示如何把 Agent 的文件式操作映射到 Key-Value Store
本示例使用 InMemoryStore 模拟数据库，并通过 StoreBackend 保存用户信息
线程 A 写入用户信息 -> StoreBackend 存入 Store -> 线程 B 读取同一份长期记忆
"""
import asyncio
import os
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import StoreBackend
from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.sqlite import SqliteSaver

load_dotenv(find_dotenv())


# InMemoryStore 是教学用内存 Store，进程重启后数据会丢失
# 生产环境可以替换成 RedisStore、数据库 Store 或其他持久化 Store       StoreBackend 本身更像一个适配器：它负责把“读写文件”转换成“读写 Store”。真正存到哪里，取决于传进去的 store 是什么。
# store = InMemoryStore()         # 这里选择InMemoryStore作为适配器的对象，真正的文件内容存储到内存中 程序退出后会丢失
# store = AsyncSqliteSaver.from_conn_string("./agent_memory.db")

# SqliteSaver 数据库文件路径，保存在 db/ 下，随项目一起持久化
CHECKPOINT_DB_PATH = str(r"F:\GoPro\project\deepsearch-agents\.claude\worktrees\nostalgic-kalam-0653a1\app\data\checkpoints.db")

# 确保数据库文件和父目录存在
db_path = Path(CHECKPOINT_DB_PATH)
db_path.parent.mkdir(parents=True, exist_ok=True)


def main():
    # 使用上下文管理器确保连接正确关闭
    with SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH) as checkpointer:

        llm = init_chat_model(
            model=os.getenv("LLM_MODEL_ID"),
            model_provider="openai",
        )

        main_agent = create_deep_agent(
            model=llm,
            checkpointer=checkpointer,
            system_prompt="""
            你是一个智能助手
            当用户询问信息时，请依据现有信息回答用户，如果没有足够信息，请回复“我不知道”，不要编造信息。
            """,
        )

        config_a = {"configurable": {"thread_id": "16c42ef0-af53-4968-a349-f5e048486dc3"}}
        # config_b = {"configurable": {"thread_id": "thread-b"}}

        # 第一次执行：线程 A 写入用户信息
        # result_a = main_agent.invoke(
        #     {
        #         "messages": [
        #             {
        #                 "role": "user",
        #                 "content": "2026AI金融行业趋势分析",
        #             }
        #         ]
        #     },
        #     config=config_a,
        # )
        # # print(f"\n第一次回复结果：{result_a}")
        # print(f"第一次回复结果：{result_a['messages'][-1].content}")


        # 直接读取 Store，观察持久化的数据
        print("\n读取 Store 中保存的用户信息（持久化到 SQLite）")
        print(checkpointer)

        # 第二次执行：线程 B 读取同一份用户信息
        result_b = main_agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "金融行业趋势分析报告",
                    }
                ]
            },
            config=config_a,
        )
        print(f"\n第二次回复结果：{result_b['messages'][-1].content}")


if __name__ == "__main__":
    # asyncio.run(main())
    main()
