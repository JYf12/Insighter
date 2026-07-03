<div align='center'>
  <h1 style="margin-top: 15px;"> Insighter - 多智能体研究系统</h1>
  <h4><b>deepsearch-agents</b></h4>
</div>

<div align='center'>

![AI](https://img.shields.io/badge/AI-Agent-00c853?style=flat)
![DeepAgents](https://img.shields.io/badge/DeepAgents-0.5.7-1C3C3C.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688.svg?logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB.svg?logo=react&logoColor=black)
</div>

<p align="center">
  <img alt="Multi-Agent for Insight logo" src="assets/logo.png" width="400px">
</p>

---

## 项目简介

Insighter 是一个基于 **DeepAgents** 框架构建的多智能体深度研究系统。系统通过主智能体协调多个子智能体（联网搜索、知识库问答、数据库查询），完成复杂的深度研究任务，并提供实时流式交互的前端界面。


## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|---------|------|
| Python | 3.12.x | 仅支持 3.12 |
| Node.js | >= 18 | 前端构建需要 |
| pnpm | >= 10 | 前端包管理器 |
| Docker | >= 20 | MySQL / Redis 容器化部署 |
| uv | 推荐 | Python 包管理（也可用 pip） |

## 快速开始

### 1. 克隆项目

```bash
git clone <repo-url>
cd deepsearch-agents
```

### 2. 配置环境变量

```bash
# 复制后端环境变量模板
cp .env.example .env

# 复制前端环境变量模板
cp frontend/.env.example frontend/.env
```

编辑 `.env` 文件，填入必要的 API 密钥：

```ini
# 大模型配置（必填）
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=你的大模型_API_KEY
LLM_QWEN_MAX=qwen-max

# 联网搜索（可选）
TAVILY_API_KEY=你的_TAVILY_API_KEY

# 知识库（可选）
RAGFLOW_API_URL=http://your-ragflow-host
RAGFLOW_API_KEY=ragflow-your-api-key
```

### 3. 启动基础设施（MySQL + Redis）

```bash
cd docker
docker compose up -d
```

> MySQL 首次启动时会自动导入 `docker/mysql/mysql.sql` 初始化数据。端口默认映射为 `3307`，避免与本机已有 MySQL 冲突。

### 4. 安装后端依赖并启动

使用 **uv**（推荐）：

```bash
# 安装 uv（如未安装）
pip install uv

# 安装依赖
uv sync

# 启动后端服务
uvicorn app.api.server:app --host 0.0.0.0 --port 8000 --reload
```

或使用 **pip**：

```bash
pip install -r requirements.txt
uvicorn app.api.server:app --host 0.0.0.0 --port 8000 --reload
```

后端服务启动后访问：`http://localhost:8000`

### 5. 安装前端依赖并启动

```bash
cd frontend
pnpm install
pnpm dev
```

前端默认连接后端地址 `http://localhost:8000`，如需修改请编辑 `frontend/.env`。

## 项目结构

```
deepsearch-agents/
├── app/                        # 后端核心代码
│   ├── agent/                  # 智能体定义
│   │   ├── main_agent.py       # 主智能体
│   │   ├── subagents/          # 子智能体（联网搜索 / 知识库 / 数据库）
│   │   ├── llm.py              # LLM 配置
│   │   └── prompts.py          # 提示词管理
│   ├── api/                    # FastAPI 服务 & WebSocket
│   ├── tools/                  # 工具集（搜索/数据库/文件/知识库）
│   ├── persistence/            # 持久化（检查点/缓存/任务存储）
│   ├── ragflow/                # RAGFlow 知识库集成
│   └── utils/                  # 工具函数
├── frontend/                   # 前端（React + Vite + Tailwind）
├── docker/                     # Docker Compose（MySQL + Redis）
├── docs/                       # 文档与示例图片
├── examples/                   # 框架使用示例
└── test/                       # 测试用例
```

## 核心依赖

| 依赖 | 用途 |
|------|------|
| DeepAgents 0.5.7 | 多智能体框架核心 |
| LangGraph 1.1.10 | 智能体工作流编排 |
| LangChain 1.2.17 | LLM 应用开发框架 |
| FastAPI + WebSocket | 后端 API 与实时通信 |
| React 19 + Vite 7 | 前端 UI 框架 |
| Tavily | 联网搜索 API |
| RAGFlow | 知识库检索 |
| MySQL 8.4 | 业务数据存储 |
| Redis 7 | 缓存与任务队列 |
| SentenceTransformers | 本地语义嵌入与缓存加速 |

## 开发说明

### 前端构建

```bash
cd frontend
pnpm build      # 生产构建
pnpm preview    # 预览生产构建
```

### 测试

```bash
# 在项目根目录执行
pytest test/
```

---

<div align='center'>
  <p>🚀 ✨ 更多更新即将同步，敬请期待…… 🎉 🔥</p>
</div>
