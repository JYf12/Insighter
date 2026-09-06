"""
大模型初始化模块

负责从 .env 中读取模型配置，并创建项目统一复用的模型对象
后续主智能体和子智能体都从这里导入 model，避免在多个文件里重复加载环境变量

Token 记账与 trace/budget 埋点已统一由 InstrumentationCallback 承担,
经 astream 的 config["callbacks"] 注入,由 LangGraph 传播到主 Agent + 子智能体子图
+ 所有 ToolNode(覆盖子智能体内部)。本模块不再单独挂载回调,避免与 config 回调重复触发。
"""

import os

from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

# find_dotenv 会从当前目录向上查找 .env，适合脚本和 Web 服务从不同入口启动的场景
load_dotenv(find_dotenv())

# 使用 OpenAI 兼容协议接入模型；具体模型名由 .env 中的 LLM_QWEN_MAX 控制
model = init_chat_model(
    model=os.getenv("LLM_MODEL_ID"),
    model_provider="openai",
)

