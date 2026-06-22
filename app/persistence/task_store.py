"""
Redis 任务元数据持久化层

替代原先 server.py 中内存字典 active_tasks 的任务元数据存储。
提供任务的创建、状态更新、查询和待恢复任务扫描。
"""

import os
from datetime import datetime, timezone

import redis.asyncio as redis
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

# Redis Key 命名空间前缀，避免和同一 Redis 实例中的其他应用 key 冲突
KEY_PREFIX = "insighter"

# 任务状态常量
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"


class TaskStore:
    """Redis 任务元数据存储

    Key 设计：
        insighter:task:{thread_id}  → Hash {query, status, created_at, updated_at}
        insighter:tasks:running     → Set of running thread_ids
        insighter:tasks:pending     → Set of pending thread_ids
    """

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            raise RuntimeError("TaskStore not started. Call start() first.")
        return self._redis

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """连接到 Redis"""
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", "6379"))
        db = int(os.getenv("REDIS_DB", "0"))
        password = os.getenv("REDIS_PASSWORD", None)

        self._redis = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            decode_responses=True,
        )
        await self._redis.ping()
        print(f"[TaskStore] Redis connected at {host}:{port}")

    async def stop(self) -> None:
        """关闭 Redis 连接"""
        if self._redis:
            await self._redis.aclose()
            print("[TaskStore] Redis connection closed")
            self._redis = None

    # ------------------------------------------------------------------
    # Key 构造
    # ------------------------------------------------------------------

    def _task_key(self, thread_id: str) -> str:
        return f"{KEY_PREFIX}:task:{thread_id}"

    def _running_set(self) -> str:
        return f"{KEY_PREFIX}:tasks:running"

    def _pending_set(self) -> str:
        return f"{KEY_PREFIX}:tasks:pending"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_task(self, thread_id: str, query: str) -> None:
        """创建任务记录，初始状态为 pending"""
        now = datetime.now(timezone.utc).isoformat()
        task_key = self._task_key(thread_id)
        await self._redis.hset(
            task_key,
            mapping={
                "query": query,
                "status": TASK_PENDING,
                "created_at": now,
                "updated_at": now,
            },
        )
        await self._redis.sadd(self._pending_set(), thread_id)          # 将任务的 thread_id 添加到 Redis 的一个集合中，该集合的键由 _pending_set() 方法生成，表示所有处于 pending 状态的任务。

    async def mark_running(self, thread_id: str) -> None:
        """将任务从 pending 转为 running"""
        now = datetime.now(timezone.utc).isoformat()
        task_key = self._task_key(thread_id)
        await self._redis.hset(
            task_key,
            mapping={
                "status": TASK_RUNNING,
                "updated_at": now,
            },
        )
        await self._redis.srem(self._pending_set(), thread_id)      # 从 pending 集合中移除该任务的 thread_id，表示它不再处于 pending 状态。
        await self._redis.sadd(self._running_set(), thread_id)      # 将任务的 thread_id 添加到 Redis 的一个集合中，该集合的键由 _running_set() 方法生成，表示所有处于 running 状态的任务。

    async def mark_completed(self, thread_id: str) -> None:
        """将任务标记为 completed"""
        await self._finalize_task(thread_id, TASK_COMPLETED)

    async def mark_failed(self, thread_id: str) -> None:
        """将任务标记为 failed"""
        await self._finalize_task(thread_id, TASK_FAILED)

    async def mark_cancelled(self, thread_id: str) -> None:
        """将任务标记为 cancelled"""
        await self._finalize_task(thread_id, TASK_CANCELLED)

    async def _finalize_task(self, thread_id: str, status: str) -> None:
        """任务终态的通用处理：更新状态并从 running 集合移除"""
        now = datetime.now(timezone.utc).isoformat()
        task_key = self._task_key(thread_id)
        await self._redis.hset(
            task_key,
            mapping={
                "status": status,
                "updated_at": now,
            },
        )
        await self._redis.srem(self._running_set(), thread_id)

    async def get_task(self, thread_id: str) -> dict | None:
        """获取单个任务的完整信息"""
        task_key = self._task_key(thread_id)
        data = await self._redis.hgetall(task_key)
        return data if data else None

    async def get_task_status(self, thread_id: str) -> str | None:
        """获取任务当前状态"""
        task_key = self._task_key(thread_id)
        return await self._redis.hget(task_key, "status")

    async def delete_task(self, thread_id: str) -> None:
        """删除任务记录（从所有集合中移除）"""
        task_key = self._task_key(thread_id)
        await self._redis.delete(task_key)
        await self._redis.srem(self._running_set(), thread_id)
        await self._redis.srem(self._pending_set(), thread_id)

    # ------------------------------------------------------------------
    # 启动恢复扫描
    # ------------------------------------------------------------------

    async def get_running_thread_ids(self) -> set[str]:
        """获取所有 running 状态的任务 ID（用于启动恢复 —— 这些是被中断的）"""
        return await self._redis.smembers(self._running_set())

    async def get_pending_thread_ids(self) -> set[str]:
        """获取所有 pending 状态的任务 ID（用于启动恢复 —— 这些从未启动）"""
        return await self._redis.smembers(self._pending_set())
