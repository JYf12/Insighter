"""
FastAPI 接口层与项目闭环入口

负责承接前端的任务提交、任务取消、文件上传/下载、输出文件列表查询和
WebSocket 长连接。HTTP 接口只做轻量调度，真正的 DeepAgents 执行放到后台
任务中；执行进度、工具调用和最终结果由 monitor 按 thread_id 推送给前端。

服务重启恢复：通过 Redis 持久化任务元数据 + SQLite 持久化 LangGraph 检查点，
服务重启后自动扫描并恢复中断的任务。
"""

import asyncio
import datetime
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import uvicorn
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.agent.main_agent import init_main_agent, run_deep_agent
from app.api.metrics import metrics_collector
from app.api.monitor import manager
from app.persistence.checkpoint import CheckpointManager
from app.persistence.task_store import TaskStore
from app.utils.logger import get_logger

_logger = get_logger("server")

"""
uvicorn 启动时创建一个主事件循环,多个 HTTP/WebSocket 请求通过协程（coroutine）在这个循环中交替执行,
当某个协程 await 等待 I/O 时（如数据库查询、网络请求），事件循环会切换到其他协程


"""
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    服务生命周期入口。

    启动顺序：Redis → SqliteSaver → Agent → 任务恢复
    关闭顺序（自动逆序）：Agent → SqliteSaver.close → Redis.close
    """
    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    _logger.info("WebSocket Manager bound to loop", extra={"loop_id": id(loop)})

    # 1. 初始化 Redis 任务存储
    task_store = TaskStore()
    await task_store.start()
    _app.state.task_store = task_store
    _logger.info("Redis task store initialized")

    # 2. 初始化 SQLite 检查点管理器
    checkpoint_mgr = CheckpointManager()
    await checkpoint_mgr.start()
    _app.state.checkpoint_mgr = checkpoint_mgr
    _logger.info("SQLite checkpoint manager started")

    # 3. 初始化主智能体（注入 SqliteSaver 替代 InMemorySaver）
    init_main_agent(checkpoint_mgr.checkpointer)
    _logger.info("Main agent initialized with SqliteSaver")

    # 4. 恢复中断的任务
    await _recover_tasks(task_store)
    _logger.info("Task recovery complete")

    yield

    # ---- 关闭阶段 ----
    _logger.info("Shutting down...")
    await checkpoint_mgr.stop()
    await task_store.stop()
    _logger.info("Clean shutdown complete")


# 当前文件位于 app/api/server.py，运行时目录统一收敛到 app 目录
current_dir = Path(__file__).resolve().parent           # /app/api
project_root = current_dir.parent                       # /app

app = FastAPI(title="DeepAgents API", lifespan=lifespan)            # 让 ConnectionManager 在服务启动阶段记住 FastAPI 当前的事件循环。

# 保存 thread_id -> 后台 Agent 任务，用于同一会话任务替换和主动取消
# 注意：此 dict 仅跟踪正在运行的 asyncio.Task；任务元数据（状态、查询等）由 Redis TaskStore 持久化
active_tasks: dict[str, asyncio.Task] = {}

# output 保存每个会话最终工作区，前端只允许从这里浏览和下载生成文件
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

# updated 暂存用户上传文件，run_deep_agent 启动时会复制到对应 output/session_xxx
updated_dir = project_root / "updated"
updated_dir.mkdir(exist_ok=True)

# 教学项目通常前后端分别本地启动，这里放开跨域以便 Vite 页面直接调用 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    """前端启动任务时提交的请求体。"""

    query: str
    thread_id: str = None


# ------------------------------------------------------------------
# 后台任务生命周期管理
# ------------------------------------------------------------------

def _forget_task(thread_id: str, task: asyncio.Task) -> None:
    """
    清理已结束任务的登记关系。

    done_callback 触发时，active_tasks 中可能已经被新任务替换；只有仍是同一个
    task 时才删除，避免误清理同 thread_id 下刚启动的新任务。
    """
    if active_tasks.get(thread_id) is task:
        active_tasks.pop(thread_id, None)


async def _run_task_with_lifecycle(query: str, thread_id: str, task_store: TaskStore, resume: bool = False):
    """
    带 Redis 状态跟踪的任务执行包装器。

    执行前标记 running，执行后根据结果标记 completed / failed / cancelled。
    :param resume: 是否从检查点恢复执行（中断的任务）
    """
    try:
        await task_store.mark_running(thread_id)
        await run_deep_agent(query, thread_id, resume=resume)
        await task_store.mark_completed(thread_id)
        metrics_collector.record_task_completed()                   # 统计任务完成（加一）
    except asyncio.CancelledError:
        await task_store.mark_cancelled(thread_id)
        metrics_collector.record_task_cancelled()                   # 统计任务取消（加一）
        raise
    except Exception:
        await task_store.mark_failed(thread_id)
        metrics_collector.record_task_failed()                      # 统计任务失败（加一）
        raise


async def _recover_tasks(task_store: TaskStore):
    """
    服务启动时恢复中断的任务。

    - running 状态的任务：中断于服务重启，检查点已持久化到 SQLite。
      通过 run_deep_agent(query, thread_id, resume=True) 从检查点恢复。
    - pending 状态的任务：从未启动，以原始 query 正常启动。
    """
    # 恢复 running 任务（被中断的）
    running_ids = await task_store.get_running_thread_ids()
    for thread_id in running_ids:
        task_data = await task_store.get_task(thread_id)
        query = task_data.get("query", "") if task_data else ""
        _logger.info("Resuming interrupted task", extra={"thread_id": thread_id, "query": query[:80]})

        metrics_collector.record_task_started()                                     # 统计任务启动（加一）
        task = asyncio.create_task(
            _run_task_with_lifecycle(query, thread_id, task_store, resume=True)
        )
        active_tasks[thread_id] = task
        task.add_done_callback(
            lambda finished_task, tid=thread_id: _forget_task(tid, finished_task)
        )

    if running_ids:
        _logger.info("Resumed interrupted tasks", extra={"count": len(running_ids)})

    # 恢复 pending 任务（从未启动的）
    pending_ids = await task_store.get_pending_thread_ids()
    for thread_id in pending_ids:
        task_data = await task_store.get_task(thread_id)
        query = task_data.get("query", "") if task_data else ""
        _logger.info("Starting pending task", extra={"thread_id": thread_id, "query": query[:80]})

        metrics_collector.record_task_started()                                     # 统计任务启动（加一）
        task = asyncio.create_task(
            _run_task_with_lifecycle(query, thread_id, task_store)
        )
        active_tasks[thread_id] = task
        task.add_done_callback(
            lambda finished_task, tid=thread_id: _forget_task(tid, finished_task)
        )

    if pending_ids:
        _logger.info("Started pending tasks", extra={"count": len(pending_ids)})

    if not running_ids and not pending_ids:
        _logger.info("No tasks to recover")


# ------------------------------------------------------------------
# HTTP 端点
# ------------------------------------------------------------------

@app.post("/api/task")
async def run_task(request: TaskRequest):
    """
    启动一次 DeepAgents 后台任务。

    将任务元数据持久化到 Redis，然后创建后台协程执行。
    执行进度和结果由 monitor 通过 /ws/{thread_id} 推送。
    """
    thread_id = request.thread_id or str(uuid.uuid4())
    task_store: TaskStore = app.state.task_store

    # 同一个 thread_id 只保留一个活跃任务，新任务会先取消旧任务，避免并发写同一会话目录
    old_task = active_tasks.get(thread_id)
    if old_task and not old_task.done():
        old_task.cancel()

    # 持久化任务到 Redis
    await task_store.create_task(thread_id, request.query)

    # 指标：记录任务启动
    metrics_collector.record_task_started()                                     # 统计任务启动（加一）

    # create_task 把长耗时 Agent 执行交给事件循环，接口本身不用等待最终结果
    # 使用 _run_task_with_lifecycle 包装器自动管理 Redis 中的任务状态
    task = asyncio.create_task(
        _run_task_with_lifecycle(request.query, thread_id, task_store)
    )
    active_tasks[thread_id] = task
    task.add_done_callback(lambda finished_task: _forget_task(thread_id, finished_task))

    return {"status": "started", "thread_id": thread_id}


@app.post("/api/task/{thread_id}/cancel")
async def cancel_task(thread_id: str):
    """
    取消指定 thread_id 对应的后台 Agent 任务。

    注意：取消会向 asyncio.Task 注入 CancelledError。若底层第三方工具正在执行不可中断
    的同步阻塞调用，任务可能需要等该调用返回后才会真正结束。
    """
    task_store: TaskStore = app.state.task_store
    task = active_tasks.get(thread_id)
    if not task or task.done():
        active_tasks.pop(thread_id, None)
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    # 先发出取消信号，再短暂等待协程响应；若底层阻塞中，则返回 cancelling 给前端继续展示状态
    # 注意：_run_task_with_lifecycle 内部会捕获 CancelledError 并自动更新 Redis 状态
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:              # 如果task任务已经被成功取消，则会抛出CancelledError异常
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id}      # 返回Http响应  monitor报告任务进度在main_agent中实现
    except asyncio.TimeoutError:
        return {"status": "cancelling", "thread_id": thread_id}
    except Exception as e:
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id, "message": str(e)}

    _forget_task(thread_id, task)
    return {"status": "cancelled", "thread_id": thread_id}          # HTTP 告诉前端"取消请求已处理"，WebSocket 才告诉前端"后台任务已经真正进入取消状态"。


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), thread_id: str = Form(...)):
    """
    文件上传接口 (File Upload)。

    目标：
    1. 接收用户上传的一个或多个文件。
    2. 保存到 `updated/session_{thread_id}` 目录。
    3. 供 Agent 在后续任务中读取和分析。

    Args:
        files (List[UploadFile]): 文件对象列表。
        thread_id (str): 关联的任务会话 ID。
    """
    # 上传文件先按会话隔离保存，避免不同任务读取到彼此的附件
    target_dir = updated_dir / f"session_{thread_id}"       # 下次任务发起时，会先检查该目录下是否有文件，有则复制到会话目录下
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    for file in files:
        file_path = target_dir / file.filename
        # 直接复制文件流，避免大文件一次性读入内存   好处：避免大文件一次性读入内存（分块读取时内存恒定占用很小的空间）---适用于任意大小的文件
        with file_path.open("wb") as buffer:        # 分块读取，逐块写入
            shutil.copyfileobj(file.file, buffer)       # 分块边读、边写
        saved_files.append(file.filename)               # with 块结束时，buffer 自动关闭并刷新到磁盘 ✅ 文件已保存完成

    return {"status": "uploaded", "files": saved_files}


@app.get("/api/download")
async def download_file(path: str):
    """
    文件下载接口 (File Download)。

    目标：
    1. 根据绝对路径下载文件。
    2. 严格的安全检查，防止越权访问。

    Args:
        path (str): 文件的绝对路径 (通常从 list_files 接口获取)。
    """
    try:
        # resolve 后再做 is_relative_to，防止 `../` 之类的路径穿越到 output 之外
        abs_path = Path(path).resolve()     # resolve() 会把路径规整成绝对路径
        output_abs = output_dir.resolve()

        if not abs_path.is_relative_to(output_abs):                 # is_relative_to() 检查路径是否在给定路径下
            return {"error": "拒绝访问: 只能下载输出目录下的文件"}
    except Exception:
        return {"error": "无效的路径参数"}

    if not abs_path.exists():
        return {"error": "文件不存在"}

    # FileResponse 会以流式响应返回文件内容，并让浏览器使用原文件名下载
    return FileResponse(abs_path, filename=abs_path.name)


@app.get("/api/files")
async def list_files(path: str):
    """
    文件列表查询接口 (File Explorer)。

    目标：
    1. 列出指定目录下的所有生成文件。
    2. 提供文件元数据（大小、修改时间、下载所需路径）。
    3. 严格的安全检查，防止路径遍历攻击。

    Args:
        path (str): 目标目录的绝对路径 (必须在 output 目录下)。
    """
    _logger.debug("请求文件列表", extra={"path": path})

    try:
        # 和下载接口保持同一条安全边界：前端只能查看 output 目录内部内容
        abs_path = Path(path).resolve()
        output_abs = output_dir.resolve()

        if not abs_path.is_relative_to(output_abs):
            _logger.error("拒绝访问", extra={"abs_path": str(abs_path), "output_abs": str(output_abs)})
            return {"error": "拒绝访问: 只能访问输出目录下的文件"}

    except Exception as e:
        _logger.error("路径解析失败", extra={"error": str(e)})
        return {"error": f"路径无效: {e}"}

    if not abs_path.exists():
        return {"error": "目录不存在"}

    files = []
    try:
        # 递归返回文件元数据，前端据此渲染文件列表并发起下载请求
        for file_path in abs_path.rglob("*"):
            if file_path.is_file():
                stat = file_path.stat()
                files.append(
                    {
                        "name": file_path.name,
                        "type": "file",
                        "path": str(file_path),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )

    except Exception as e:
        _logger.error("遍历文件失败", extra={"error": str(e)})
        return {"error": str(e)}

    # 最新生成的文件排在前面，方便用户优先看到本次任务产物
    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    _logger.debug("找到文件", extra={"count": len(files)})
    return {"files": files}


@app.get("/api/metrics")
async def get_metrics(thread_id: str = None):
    """
    指标查询接口 (Metrics)。

    返回工具调用、任务流转、Token 消耗和 WebSocket 连接状态的当前快照。

    查询参数:
        thread_id (str, optional): 按会话过滤工具和 Token 维度。
            不传时返回全局聚合数据。

    示例:
        GET /api/metrics                       # 全局聚合
        GET /api/metrics?thread_id=abc123      # 只返回该会话的指标 + 最近调用明细
    """
    return metrics_collector.snapshot(thread_id=thread_id)


@app.get("/api/health")
async def health_check():
    """
    健康检查接口 (Health Check)。

    用于负载均衡器和监控系统确认服务正常运行。
    """
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """
    WebSocket 实时通讯核心接口 (Real-time Communication)。

    连接建立后，ConnectionManager 会用 thread_id 保存 WebSocket。monitor 后续
    发送事件时只需要按 thread_id 查找连接，就能把进度推给对应页面。循环中的
    receive_text 用于接收前端心跳，避免连接空闲断开。
    """
    _logger.info("WebSocket 连接请求", extra={"thread_id": thread_id})

    # 连接建立后立即按 thread_id 注册，monitor 后续才能把事件定向推给当前页面
    await manager.connect(websocket, thread_id)
    metrics_collector.increment_active_connections()                # metrics_collector 记录活跃连接数(加一)

    try:
        while True:
            # 前端通常发送 ping 心跳；服务端回复 pong，顺便维持连接活跃
            data = await websocket.receive_text()
            await websocket.send_json(
                {"type": "pong", "message": f"服务端已收到: {data}"}
            )
            metrics_collector.record_ws_message_sent()              # metrics_collector 记录 WebSocket 消息发送次数（加一）

    except WebSocketDisconnect:
        # 只移除当前 WebSocket 实例，避免旧连接断开时误删同 thread_id 的新连接
        manager.disconnect(websocket, thread_id)
        metrics_collector.decrement_active_connections()            # metrics_collector 记录活跃连接数（减一）
        _logger.info("客户端已断开", extra={"thread_id": thread_id})

    except Exception as e:
        _logger.error("WebSocket 连接异常", extra={"thread_id": thread_id, "error": str(e)})
        metrics_collector.record_ws_error()                         # metrics_collector 记录 WebSocket 错误(加一)
        metrics_collector.decrement_active_connections()            # metrics_collector 记录活跃连接数（减一）
        manager.disconnect(websocket, thread_id)


if __name__ == "__main__":
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
