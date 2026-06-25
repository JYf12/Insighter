"""
语义缓存存储模块

基于 Redis + SentenceTransformer 实现语义相似查询缓存。
通过本地 Embedding 模型将查询文本转为 384 维向量，在 Redis 中按 namespace
查找余弦相似度最高的历史缓存，超过阈值则直接返回，避免重复调用外部 API。

架构：
    工具调用 → check_cache(namespace, query)
        ├── 计算 embedding → 加载 namespace 全部缓存 → cosine 比对
        ├── 命中 (cosine ≥ threshold) → 返回缓存结果 + 上报 cache_hit
        └── 未命中 → 执行真实工具 → save_to_cache(namespace, query, result)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import redis.asyncio as redis
from dotenv import find_dotenv, load_dotenv

from app.utils.logger import get_logger

load_dotenv(find_dotenv())

_logger = get_logger("cache_store")

# Redis Key 前缀，与 task_store 共用 insighter 命名空间
KEY_PREFIX = "insighter:cache"

# 默认配置
DEFAULT_CACHE_TTL_SECONDS = 3600
DEFAULT_SIMILARITY_THRESHOLD = 0.92

# SentenceTransformer 模型名（首次使用自动从 HuggingFace Hub 下载到本地缓存）
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _hash_query(query: str) -> str:
    """对查询文本做短哈希，用作 Redis Hash field"""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


class SemanticCacheStore:
    """Redis 驱动的语义缓存存储

    生命周期与 TaskStore 一致：FastAPI lifespan 中调用 start() / stop()。
    start() 时惰性加载 SentenceTransformer 模型（约 80 MB），常驻内存。
    """

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._model: Any = None  # SentenceTransformer 实例，start() 时加载

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """连接 Redis 并加载 Embedding 模型"""
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

        # SentenceTransformer 加载是同步阻塞 I/O（读磁盘 / 下载模型），
        # 放到线程池避免阻塞 FastAPI 事件循环
        loop = asyncio.get_running_loop()
        _logger.info("Loading embedding model (this may take a few seconds on first run)...")
        t0 = time.perf_counter()
        self._model = await loop.run_in_executor(None, self._load_model)
        elapsed = time.perf_counter() - t0
        _logger.info("Embedding model loaded", extra={"model": EMBEDDING_MODEL_NAME, "load_time_s": round(elapsed, 1)})

    @staticmethod
    def _load_model() -> Any:
        """在单独线程中加载 SentenceTransformer（阻塞调用）"""
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(EMBEDDING_MODEL_NAME)

    async def stop(self) -> None:
        """关闭 Redis 连接"""
        if self._redis:
            await self._redis.aclose()
            _logger.info("Cache store Redis connection closed")
            self._redis = None
        self._model = None

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            raise RuntimeError("SemanticCacheStore not started. Call start() first.")
        return self._redis

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def _get_embedding(self, text: str) -> np.ndarray:
        """计算查询文本的 384 维向量（线程池执行，避免阻塞事件循环）"""
        if self._model is None:
            raise RuntimeError("Embedding model not loaded. Call start() first.")
        loop = asyncio.get_running_loop()
        embedding: np.ndarray = await loop.run_in_executor(
            None, lambda: self._model.encode(text, convert_to_numpy=True)
        )
        return embedding

    # ------------------------------------------------------------------
    # 缓存读写
    # ------------------------------------------------------------------

    def _cache_key(self, namespace: str) -> str:
        return f"{KEY_PREFIX}:{namespace}"

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """计算两个向量的余弦相似度，返回 [0, 1] 之间的浮点数"""
        dot = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(dot / (norm_a * norm_b))

    def _get_threshold(self) -> float:
        """从环境变量读取相似度阈值"""
        try:
            return float(os.getenv("CACHE_SIMILARITY_THRESHOLD", str(DEFAULT_SIMILARITY_THRESHOLD)))
        except ValueError:
            return DEFAULT_SIMILARITY_THRESHOLD

    def _get_ttl(self) -> int:
        """从环境变量读取缓存 TTL（秒）"""
        try:
            return int(os.getenv("CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS)))
        except ValueError:
            return DEFAULT_CACHE_TTL_SECONDS

    async def lookup(self, namespace: str, query: str) -> tuple[bool, Optional[str]]:
        """查找语义相似缓存

        :param namespace: 缓存命名空间（如 "tavily" / "ragflow"），隔离不同工具
        :param query: 当前查询文本
        :return: (hit: bool, result: str | None)
        """
        t0 = time.perf_counter()
        threshold = self._get_threshold()

        # 1. 计算当前查询的 embedding
        query_embedding = await self._get_embedding(query)

        # 2. 加载该 namespace 全部缓存条目
        cache_key = self._cache_key(namespace)
        all_entries = await self.redis.hgetall(cache_key)

        if not all_entries:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            _logger.debug(
                "Cache miss (empty namespace)",
                extra={"namespace": namespace, "lookup_ms": round(elapsed_ms, 1)},
            )
            return False, None

        # 3. 逐条计算余弦相似度，找最高分
        best_score = 0.0
        best_result: Optional[str] = None
        best_query: Optional[str] = None

        for field_key, value_json in all_entries.items():
            try:
                entry = json.loads(value_json)
                stored_embedding = np.array(entry["embedding"], dtype=np.float32)
                score = self._cosine_similarity(query_embedding, stored_embedding)
                if score > best_score:
                    best_score = score
                    best_result = entry["result"]
                    best_query = entry.get("query", "")
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                _logger.warning("Skipping corrupted cache entry", extra={"field": field_key, "error": str(e)})
                continue

        elapsed_ms = (time.perf_counter() - t0) * 1000

        if best_score >= threshold and best_result is not None:
            _logger.info(
                "Cache HIT",
                extra={
                    "namespace": namespace,
                    "query": query[:80],
                    "matched_query": (best_query or "")[:80],
                    "similarity": round(best_score, 4),
                    "lookup_ms": round(elapsed_ms, 1),
                },
            )
            return True, best_result

        _logger.debug(
            "Cache miss (below threshold)",
            extra={
                "namespace": namespace,
                "query": query[:80],
                "best_similarity": round(best_score, 4),
                "threshold": threshold,
                "lookup_ms": round(elapsed_ms, 1),
            },
        )
        return False, None

    async def store(self, namespace: str, query: str, result: str) -> None:
        """存储新的缓存条目

        :param namespace: 缓存命名空间
        :param query: 查询文本
        :param result: 查询结果（JSON 字符串或纯文本）
        """
        t0 = time.perf_counter()

        # 1. 计算 embedding
        embedding = await self._get_embedding(query)

        # 2. 构造条目
        entry = {
            "query": query,
            "result": result,
            "embedding": embedding.tolist(),
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }

        # 3. 写入 Redis Hash
        cache_key = self._cache_key(namespace)
        field = _hash_query(query)
        ttl = self._get_ttl()

        await self.redis.hset(cache_key, field, json.dumps(entry, ensure_ascii=False))
        await self.redis.expire(cache_key, ttl)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        _logger.info(
            "Cache stored",
            extra={
                "namespace": namespace,
                "query": query[:80],
                "field": field,
                "ttl_s": ttl,
                "store_ms": round(elapsed_ms, 1),
            },
        )


# ------------------------------------------------------------------
# 单例访问
# ------------------------------------------------------------------

_cache_store: Optional[SemanticCacheStore] = None


async def init_cache() -> SemanticCacheStore:
    """初始化语义缓存存储（在 FastAPI lifespan 中调用）"""
    global _cache_store
    if _cache_store is not None:
        _logger.warning("Cache store already initialized, reusing existing instance")
        return _cache_store
    _cache_store = SemanticCacheStore()
    await _cache_store.start()
    return _cache_store


def get_cache_store() -> SemanticCacheStore:
    """获取全局缓存存储单例"""
    if _cache_store is None:
        raise RuntimeError(
            "SemanticCacheStore not initialized. "
            "Ensure init_cache() was called during FastAPI lifespan startup."
        )
    return _cache_store


# ------------------------------------------------------------------
# 工具层便捷函数
# ------------------------------------------------------------------

async def check_cache(namespace: str, query: str) -> Optional[str]:
    """工具层调用：查询语义缓存

    供 tavily_tool / ragflow_tools 在真正执行前调用。

    :param namespace: 缓存命名空间
    :param query: 查询文本
    :return: 命中时返回缓存结果；未命中返回 None
    """
    try:
        store = get_cache_store()
    except RuntimeError:
        # 缓存未初始化（例如脚本模式），直接返回 miss
        return None

    try:
        t0 = time.perf_counter()
        hit, result = await store.lookup(namespace, query)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 上报监控事件 + 更新指标
        try:
            from app.api.monitor import monitor
            from app.api.metrics import metrics_collector

            if hit:
                monitor.report_cache_hit(namespace, query, elapsed_ms)              # 上报缓存命中事件
                metrics_collector.record_cache_hit(namespace, elapsed_ms)           # 记录缓存命中指标（加一）
            else:
                monitor.report_cache_miss(namespace, query, elapsed_ms)             # 上报缓存未命中事件
                metrics_collector.record_cache_miss(namespace, elapsed_ms)          # 记录缓存未命中指标（加一）
        except Exception:
            pass

        return result if hit else None
    except Exception as e:
        _logger.warning(
            "Cache lookup failed, falling back to real execution",
            extra={"namespace": namespace, "error": str(e)},
        )
        return None


async def save_to_cache(namespace: str, query: str, result: str) -> None:
    """工具层调用：保存查询结果到缓存

    供 tavily_tool / ragflow_tools 在成功执行后调用。

    :param namespace: 缓存命名空间
    :param query: 查询文本
    :param result: 查询结果（已序列化为字符串的 JSON 或纯文本）
    """
    try:
        store = get_cache_store()
    except RuntimeError:
        return

    try:
        await store.store(namespace, query, result)
    except Exception as e:
        _logger.warning(
            "Cache store failed",
            extra={"namespace": namespace, "error": str(e)},
        )
