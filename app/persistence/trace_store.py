"""
Task-run 级 Trace 持久化层

将一次 task run 的完整 span 树写入 Redis 近期窗口,支持事后复盘失败 run。
重启不丢,新增 /api/trace/{run_id} 与 /api/traces?thread_id= 查询。

Key 设计(复用 insighter 命名空间):
    insighter:trace:{run_id}        → JSON {run_id, thread_id, status, started_at, ended_at, spans[]}
    insighter:trace:recent          → ZSET(run_id, score=ended_at ts),全局近期窗口
    insighter:trace:thread:{tid}    → ZSET(run_id, score=ended_at ts),按 thread 列 run
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import redis.asyncio as redis
from dotenv import find_dotenv, load_dotenv

from app.utils.logger import get_logger

load_dotenv(find_dotenv())

_logger = get_logger("trace_store")

KEY_PREFIX = "insighter:trace"


class TraceStore:
    """Redis 驱动的 trace 持久化(近期窗口策略)"""

    def __init__(self) -> None:
        self._redis: Optional[redis.Redis] = None
        # 近期窗口保留条数与 TTL,可由环境变量覆盖
        self._max_runs = int(os.getenv("TRACE_MAX_RUNS", "100"))
        self._ttl_s = int(os.getenv("TRACE_TTL_SECONDS", "86400"))  # 默认 24h

    async def start(self) -> None:
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
        _logger.info("TraceStore connected", extra={"host": host, "port": port, "db": db})

    async def stop(self) -> None:
        if self._redis:
            await self._redis.aclose()
            _logger.info("TraceStore Redis connection closed")
            self._redis = None

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            raise RuntimeError("TraceStore not started. Call start() first.")
        return self._redis

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def save_trace(
        self,
        run_id: str,
        thread_id: str,
        spans: list[dict[str, Any]],
        status: str,
        started_at: str,
        ended_at: str,
        budget_snapshot: Optional[dict] = None,
    ) -> None:
        """持久化一次 run 的完整 span 树

        :param spans: 已序列化为 dict 的 span 列表(含 span_id/parent_span_id/name/kind/...)
        """
        ts = datetime.now(timezone.utc).isoformat()
        key = f"{KEY_PREFIX}:{run_id}"
        payload = json.dumps(
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "budget": budget_snapshot or {},
                "spans": spans,
            },
            ensure_ascii=False,
            default=str,
        )

        pipe = self.redis.pipeline()
        pipe.set(key, payload, ex=self._ttl_s)
        pipe.zadd(f"{KEY_PREFIX}:recent", {run_id: datetime.now(timezone.utc).timestamp()})
        pipe.expire(f"{KEY_PREFIX}:recent", self._ttl_s)
        pipe.zadd(f"{KEY_PREFIX}:thread:{thread_id}", {run_id: datetime.now(timezone.utc).timestamp()})
        pipe.expire(f"{KEY_PREFIX}:thread:{thread_id}", self._ttl_s)
        await pipe.execute()

        # 近期窗口淘汰:只保留最近 max_runs 条
        await self._trim_recent()

        _logger.debug(
            "Trace saved",
            extra={"run_id": run_id, "thread_id": thread_id, "span_count": len(spans), "status": status},
        )

    async def _trim_recent(self) -> None:
        """裁剪全局近期窗口与各 thread 窗口至 max_runs 条"""
        total = await self.redis.zcard(f"{KEY_PREFIX}:recent")
        if total > self._max_runs:
            # 删除最早的 (total - max_runs) 条,并清理其 trace key
            victims = await self.redis.zrange(f"{KEY_PREFIX}:recent", 0, total - self._max_runs - 1)
            if victims:
                pipe = self.redis.pipeline()
                for vid in victims:
                    pipe.delete(f"{KEY_PREFIX}:{vid}")
                pipe.zremrangebyscore(f"{KEY_PREFIX}:recent", 0, total - self._max_runs - 1)
                await pipe.execute()

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def get_trace(self, run_id: str) -> Optional[dict[str, Any]]:
        """获取单次 run 的完整 span 树"""
        data = await self.redis.get(f"{KEY_PREFIX}:{run_id}")
        if not data:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            _logger.warning("Corrupt trace JSON", extra={"run_id": run_id})
            return None

    async def list_runs(self, thread_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        """列出近期 run(thread_id 给定时按 thread 过滤),新在前"""
        limit = max(1, min(limit, self._max_runs))
        if thread_id:
            key = f"{KEY_PREFIX}:thread:{thread_id}"
        else:
            key = f"{KEY_PREFIX}:recent"
        # ZREVRANGE:按 score 降序(新在前)取 limit 条
        run_ids = await self.redis.zrevrange(key, 0, limit - 1)
        if not run_ids:
            return []
        # 批量取元数据(仅 run_id/thread_id/status/started_at/ended_at,不含 spans)
        pipe = self.redis.pipeline()
        for rid in run_ids:
            pipe.get(f"{KEY_PREFIX}:{rid}")
        raws = await pipe.execute()

        results: list[dict[str, Any]] = []
        for rid, raw in zip(run_ids, raws):
            if not raw:
                continue
            try:
                doc = json.loads(raw)
            except json.JSONDecodeError:
                continue
            results.append(
                {
                    "run_id": doc.get("run_id", rid),
                    "thread_id": doc.get("thread_id"),
                    "status": doc.get("status"),
                    "started_at": doc.get("started_at"),
                    "ended_at": doc.get("ended_at"),
                    "span_count": len(doc.get("spans", [])),
                }
            )
        return results


# ------------------------------------------------------------------
# 单例
# ------------------------------------------------------------------

_trace_store: Optional[TraceStore] = None


async def init_trace_store() -> TraceStore:
    """初始化 trace 存储(FastAPI lifespan 中调用)"""
    global _trace_store
    if _trace_store is not None:
        return _trace_store
    _trace_store = TraceStore()
    await _trace_store.start()
    return _trace_store


def get_trace_store() -> TraceStore:
    if _trace_store is None:
        raise RuntimeError("TraceStore not initialized. Call init_trace_store() during lifespan.")
    return _trace_store
