"""
DeepAgents Backend：StoreBackend 跨线程长期记忆

演示如何把 Agent 的文件式操作映射到 Key-Value Store
本示例使用 InMemoryStore 模拟数据库，并通过 StoreBackend 保存用户信息
线程 A 写入用户信息 -> StoreBackend 存入 Store -> 线程 B 读取同一份长期记忆
"""

import os

from deepagents import create_deep_agent
from deepagents.backends import StoreBackend
from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.store.memory import InMemoryStore

load_dotenv(find_dotenv())


# InMemoryStore 是教学用内存 Store，进程重启后数据会丢失
# 生产环境可以替换成 RedisStore、数据库 Store 或其他持久化 Store       StoreBackend 本身更像一个适配器：它负责把“读写文件”转换成“读写 Store”。真正存到哪里，取决于传进去的 store 是什么。
store = InMemoryStore()         # 这里选择InMemoryStore作为适配器的对象，真正的文件内容存储到内存中 程序退出后会丢失


llm = init_chat_model(
    model=os.getenv("LLM_MODEL_ID"),
    model_provider="openai",
)


# StoreBackend 会把文件路径映射成 Store 中的 key，把文件内容映射成 value
# 这里要求 Agent 把用户重要信息写入 user_profile.txt
# 底层实际不会写本地文件，而是写入上面的 store
main_agent = create_deep_agent(
    model=llm,
    store=store,                                # 表示具体存到哪个 Store 实例中
    backend=StoreBackend,                       # 表示文件系统操作走 Store
    system_prompt="""
    你是一个智能助手
    当用户提供重要个人信息时，请保存到 user_profile.txt
    当用户询问个人信息时，请从 user_profile.txt 中读取
    """,
)


# 使用两个不同 thread_id 模拟跨线程或跨会话
# Backend 保存的是长期文件数据，不依赖同一个 thread_id 才能读取
config_a = {"configurable": {"thread_id": "thread-a"}}
config_b = {"configurable": {"thread_id": "thread-b"}}


# 第一次执行：线程 A 写入用户信息
result_a = main_agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "我是乌萨奇，我今年 16 岁",
            }
        ]
    },
    config=config_a,
)
print(f"第一次回复结果：{result_a['messages'][-1].content}")

"""
执行流程：
    用户提问--主智能体调用write_file工具写入个人信息--工具返回
"""


# 直接读取 Store，观察 StoreBackend 写入的底层数据
# DeepAgents 文件系统默认使用 filesystem 命名空间保存文件式内容                key:文件名     value:写入内容
print("读取 Store 中保存的用户信息")
items = store.search(("filesystem",))
for item in items:
    print(f"key = {item.key}")
    print(f"value = {item.value}")


# 第二次执行：线程 B 读取同一份用户信息
# 这说明 Backend 存储和 checkpointer 的线程状态不是一回事
result_b = main_agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "我叫什么，我的年龄是多少",
            }
        ]
    },
    config=config_b,
)
print(f"第二次回复结果：{result_b['messages'][-1].content}")            # 跨线程读取store中保存的长期记忆
