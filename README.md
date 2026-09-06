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

Insighter 是一个基于 **DeepAgents** 框架构建的多智能体深度研究系统。系统通过主智能体协调多个子智能体，完成复杂的深度研究任务，并提供实时流式交互的前端界面。

### 核心特性

- **多智能体协作**：主智能体统一调度网络搜索、数据库查询、RAGFlow 知识库三个子智能体
- **任务级预算 (Autonomy Budget)**：迭代轮次 / 墙钟时间 / 工具调用 / Token 四维预算，任一触达即软停止
- **Recovery Engine**：瞬态错误指数退避重试 + LLM 自纠错回注 + 外部服务熔断器
- **Task-run 级 Trace**：完整 span 树记录每次执行，前端可视化调用链复盘
- **语义缓存**：基于 SentenceTransformer + Redis 的相似查询缓存，减少重复外部 API 调用
- **实时可观测**：WebSocket 推送工具调用 / Token 消耗 / 缓存命中等实时事件，前端 EventStream 展示
- **多格式文档生成**：支持 Markdown / PDF 输出


## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|---------|------|
| Python | 3.12.x | 仅支持 3.12 |
| Node.js | >= 18 | 前端构建需要 |
| pnpm | >= 10 | 前端包管理器 |
| Docker | >= 20 | MySQL / Redis 容器化部署 |
| uv | 推荐 | Python 包管理（也可用 pip） |

## 后端启动

```bash
uvicorn app.api.server:app --host 0.0.0.0 --port 8000 --reload
```

启动后访问 `http://localhost:8000`，`--reload` 参数开启热重载，代码修改后自动重启。

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

## 环境变量说明

复制 `.env.example` 为 `.env` 后按本机实际情况调整

### LLM 配置（必填）

| 变量 | 说明 | 示例 |
|------|------|------|
| `OPENAI_BASE_URL` | 大模型 API 地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `OPENAI_API_KEY` | 大模型 API Key | `sk-xxx` |
| `LLM_MODEL_ID` | 模型标识 | `qwen-max` / `deepseek-v4-flash` |

### 联网搜索（可选）

| 变量 | 说明 | 示例 |
|------|------|------|
| `TAVILY_API_KEY` | Tavily 搜索 API Key，在 https://app.tavily.com/ 注册 | `tvly-xxx` |

### 知识库（可选）

| 变量 | 说明 | 示例 |
|------|------|------|
| `RAGFLOW_API_URL` | RAGFlow 服务地址 | `http://your-ragflow-host` |
| `RAGFLOW_API_KEY` | RAGFlow API Key | `ragflow-xxx` |

### MySQL 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MYSQL_USER` | 数据库用户名 | `root` |
| `MYSQL_PASSWORD` | 数据库密码 | `root` |
| `MYSQL_DATABASE` | 数据库名 | `deepsearch_db` |
| `MYSQL_HOST` | 数据库地址 | `localhost` |
| `MYSQL_PORT` | 端口（Docker 建议映射 3307 避免冲突） | `3307` |
| `MYSQL_CHARSET` | 字符集 | `utf8mb4` |
| `MYSQL_COLLATION` | 排序规则 | `utf8mb4_unicode_ci` |
| `MYSQL_SQL_MODE` | SQL 模式 | `TRADITIONAL` |

### Redis 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `REDIS_HOST` | Redis 地址 | `localhost` |
| `REDIS_PORT` | Redis 端口 | `6379` |
| `REDIS_DB` | Redis 数据库编号 | `0` |
| `REDIS_PASSWORD` | Redis 密码（可为空） | |

### 日志 & 持久化

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `SQLITE_CHECKPOINT_PATH` | LangGraph 检查点 SQLite 路径 | `app/data/checkpoints.db` |

### 语义缓存

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CACHE_TTL_SECONDS` | 缓存有效期（秒） | `3600` |
| `CACHE_SIMILARITY_THRESHOLD` | cosine 相似度阈值 | `0.92` |

### Autonomy Budget

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BUDGET_MAX_ITERATIONS` | 单次 run 最大 LLM 轮次 | `20` |
| `BUDGET_MAX_RUNTIME_S` | 单次 run 最大墙钟预算（秒） | `300` |
| `BUDGET_MAX_TOOL_CALLS` | 单次 run 最大工具调用次数（含子智能体内部） | `30` |
| `BUDGET_MAX_TOKENS` | 单次 run 最大累计 token | `80000` |

### Recovery Engine

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RECOVERY_MAX_RETRIES` | 瞬态错误指数退避重试上限 | `3` |
| `CIRCUIT_BREAKER_THRESHOLD` | 连续失败多少次后熔断外部服务（Tavily / RAGFlow / MySQL） | `5` |
| `CIRCUIT_BREAKER_COOLDOWN_S` | 熔断器 open 状态冷却时长（秒），到期转 half-open 探活 | `60` |

### Trace

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TRACE_MAX_RUNS` | Redis 近期窗口保留的 run 数（超出按时间淘汰） | `100` |
| `TRACE_TTL_SECONDS` | 单条 trace 保留时长（秒），默认 24h | `86400` |

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
├── app/                          # 后端核心代码
│   ├── agent/                    # 智能体 & 运行时引擎
│   │   ├── main_agent.py         # 主智能体定义（任务规划 / 子智能体调度 / 文件生成）
│   │   ├── subagents/            # 子智能体
│   │   ├── budget.py             # 任务级预算（四维：迭代/时间/工具/Token）
│   │   ├── recovery.py           # Recovery Engine（错误分类 / 重试 / 熔断器 / @resilient）
│   │   ├── run_context.py        # Task-run 上下文（RunContext / Span / RunRegistry）
│   │   ├── instrumentation.py    # 统一埋点回调（trace + budget + token，主+子智能体全覆盖）
│   │   ├── llm.py                # LLM 模型配置
│   │   └── prompts.py            # 提示词加载
│   ├── api/                      # FastAPI 服务 & WebSocket
│   │   ├── server.py             # FastAPI 应用入口 & WebSocket 路由
│   │   ├── context.py            # 请求上下文管理（ContextVar: thread_id / session_dir / run_id）
│   │   ├── metrics.py            # 指标采集器（工具/任务/Token/缓存/可靠性多维度）
│   │   └── monitor.py            # 执行监控 & WebSocket 推送
│   ├── tools/                    # 工具集
│   ├── persistence/              # 持久化层
│   ├── prompt/
│   ├── ragflow/                  # RAGFlow 知识库集成
│   └── utils/                    # 工具函数
├── frontend/                     # 前端（React 19 + Vite + Ant Design）
├── docker/                       # Docker Compose（MySQL + Redis）
└── test/                         # 测试用例
```

## 核心依赖

| 依赖 | 用途 |
|------|------|
| DeepAgents 0.5.7 | 多智能体框架核心 |
| LangGraph 1.1.10 | 智能体工作流编排 |
| LangChain 1.2.17 | LLM 应用开发框架 |
| FastAPI + WebSocket | 后端 API 与实时通信 |
| React 19 + Vite + Ant Design | 前端 UI 框架 |
| Tavily | 联网搜索 API |
| RAGFlow | 知识库检索 |
| MySQL 8.4 | 业务数据存储 |
| Redis 7 | 缓存 / 任务队列 / Trace 存储 |
| SentenceTransformers | 本地语义嵌入与缓存加速 |
| ReportLab | Markdown → PDF 转换 |

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
  <p>🚀 Taking things slowly leads to better results; Everything will eventually head in the right direction. 🎉 🔥</p>
</div>
