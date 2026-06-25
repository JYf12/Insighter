"""
指标采集器模块

进程内单例，记录工具调用频次/耗时/成功率、任务吞吐、Token 消耗和 WebSocket 连接状态。
所有指标通过 /api/metrics 端点暴露为 JSON。

支持会话级隔离：指标写入时自动从 ContextVar 提取 thread_id，
GET /api/metrics?thread_id=xxx 可按会话过滤，查看单个任务的独立统计。

线程安全：所有写操作使用 threading.Lock 保护。
"""

import threading
from collections import defaultdict
from typing import Any, Optional


class MetricsCollector:
    """进程内指标采集器单例

    采集维度（全局 + 按 session）：
    - 工具维度：调用次数、失败次数、错误率、平均耗时、最大耗时
    - 任务维度：启动/完成/失败/取消次数
    - Token 维度：按模型拆分的 prompt/completion/total tokens + 调用次数
    - 连接维度：WebSocket 消息数、错误数、活跃连接数
    """

    _instance: Optional["MetricsCollector"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "MetricsCollector":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._init_metrics()
                    cls._instance = obj
        return cls._instance

    def _init_metrics(self) -> None:
        """初始化所有内部计数器"""
        self._data_lock = threading.Lock()

        # ---- 工具维度（全局）----
        self._tools_invoked: dict[str, int] = defaultdict(int)
        self._tools_failed: dict[str, int] = defaultdict(int)
        self._tools_duration_sum_ms: dict[str, float] = defaultdict(float)
        self._tools_duration_max_ms: dict[str, float] = defaultdict(float)

        # ---- 工具维度（按 session）----
        # _tools_by_session[thread_id][tool_name] = {"invoked": n, "failed": n, "total_ms": f, "max_ms": f}
        self._tools_by_session: dict[str, dict[str, dict[str, Any]]] = defaultdict(
            lambda: defaultdict(lambda: {"invoked": 0, "failed": 0, "total_ms": 0.0, "max_ms": 0.0})
        )

        # ---- 任务维度 ----
        self._tasks_started: int = 0
        self._tasks_completed: int = 0
        self._tasks_failed: int = 0
        self._tasks_cancelled: int = 0

        # ---- Token 维度（全局）----
        self._token_prompt_total: int = 0
        self._token_completion_total: int = 0
        self._token_total: int = 0
        self._token_by_model: dict[str, dict[str, int]] = defaultdict(
            lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0}
        )
        self._token_call_sizes: list[int] = []  # 每次模型调用 total_tokens，用于 histogram

        # ---- Token 维度（按 session）----
        # _token_by_session[thread_id][model] = {"prompt_tokens": n, ...}
        self._token_by_session: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0})
        )

        # ---- 连接维度 ----
        self._ws_messages_sent: int = 0
        self._ws_errors: int = 0
        self._active_connections: int = 0

        # ---- 缓存维度（按 namespace）----
        self._cache_hits: dict[str, int] = defaultdict(int)
        self._cache_misses: dict[str, int] = defaultdict(int)
        self._cache_lookup_total_ms: dict[str, float] = defaultdict(float)
        self._cache_lookup_count: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # 上下文辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _current_thread_id() -> str:
        """从 ContextVar 读取当前 thread_id，失败时返回 "unknown" """
        try:
            from app.api.context import get_thread_context
            return get_thread_context() or "unknown"
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # 工具维度
    # ------------------------------------------------------------------

    def record_tool_invoked(self, tool_name: str) -> None:
        """记录一次工具调用"""
        tid = self._current_thread_id()
        with self._data_lock:
            self._tools_invoked[tool_name] += 1
            self._tools_by_session[tid][tool_name]["invoked"] += 1

    def record_tool_failed(self, tool_name: str) -> None:
        """记录一次工具失败"""
        tid = self._current_thread_id()
        with self._data_lock:
            self._tools_failed[tool_name] += 1
            self._tools_by_session[tid][tool_name]["failed"] += 1

    def record_tool_duration(self, tool_name: str, duration_ms: float) -> None:
        """记录工具执行耗时"""
        tid = self._current_thread_id()
        with self._data_lock:
            self._tools_duration_sum_ms[tool_name] += duration_ms
            if duration_ms > self._tools_duration_max_ms.get(tool_name, 0):
                self._tools_duration_max_ms[tool_name] = duration_ms

            sess = self._tools_by_session[tid][tool_name]
            sess["total_ms"] += duration_ms
            if duration_ms > sess["max_ms"]:
                sess["max_ms"] = duration_ms

    # ------------------------------------------------------------------
    # 任务维度
    # ------------------------------------------------------------------

    def record_task_started(self) -> None:
        """记录一次任务启动"""
        with self._data_lock:
            self._tasks_started += 1

    def record_task_completed(self) -> None:
        """记录一次任务完成"""
        with self._data_lock:
            self._tasks_completed += 1

    def record_task_failed(self) -> None:
        """记录一次任务失败"""
        with self._data_lock:
            self._tasks_failed += 1

    def record_task_cancelled(self) -> None:
        """记录一次任务取消"""
        with self._data_lock:
            self._tasks_cancelled += 1

    # ------------------------------------------------------------------
    # Token 维度
    # ------------------------------------------------------------------

    def record_token_usage(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """记录一次 LLM 调用的 token 消耗"""
        tid = self._current_thread_id()
        with self._data_lock:
            self._token_prompt_total += prompt_tokens
            self._token_completion_total += completion_tokens
            self._token_total += total_tokens

            gm = self._token_by_model[model]
            gm["prompt_tokens"] += prompt_tokens
            gm["completion_tokens"] += completion_tokens
            gm["total_tokens"] += total_tokens
            gm["call_count"] += 1

            sm = self._token_by_session[tid][model]
            sm["prompt_tokens"] += prompt_tokens
            sm["completion_tokens"] += completion_tokens
            sm["total_tokens"] += total_tokens
            sm["call_count"] += 1

            self._token_call_sizes.append(total_tokens)

    # ------------------------------------------------------------------
    # 连接维度
    # ------------------------------------------------------------------

    def record_ws_message_sent(self) -> None:
        """记录一条 WebSocket 消息发送"""
        with self._data_lock:
            self._ws_messages_sent += 1

    def record_ws_error(self) -> None:
        """记录一次 WebSocket 错误"""
        with self._data_lock:
            self._ws_errors += 1

    def set_active_connections(self, count: int) -> None:
        """更新活跃连接数"""
        with self._data_lock:
            self._active_connections = count

    def increment_active_connections(self) -> None:
        """websocket活跃连接 +1"""
        with self._data_lock:
            self._active_connections += 1

    def decrement_active_connections(self) -> None:
        """websocket活跃连接 -1"""
        with self._data_lock:
            self._active_connections = max(0, self._active_connections - 1)

    # ------------------------------------------------------------------
    # 缓存维度
    # ------------------------------------------------------------------

    def record_cache_hit(self, namespace: str, lookup_ms: float) -> None:
        """记录一次缓存命中"""
        with self._data_lock:
            self._cache_hits[namespace] += 1
            self._cache_lookup_total_ms[namespace] += lookup_ms
            self._cache_lookup_count[namespace] += 1

    def record_cache_miss(self, namespace: str, lookup_ms: float) -> None:
        """记录一次缓存未命中"""
        with self._data_lock:
            self._cache_misses[namespace] += 1
            self._cache_lookup_total_ms[namespace] += lookup_ms
            self._cache_lookup_count[namespace] += 1

    # ------------------------------------------------------------------
    # 快照导出
    # ------------------------------------------------------------------

    def snapshot(self, thread_id: Optional[str] = None) -> dict[str, Any]:
        """导出当前所有指标的快照（线程安全）

        :param thread_id: 可选，按会话过滤。传入时 tools/token_usage 仅返回该会话的数据。
        """
        with self._data_lock:
            # ---- 工具维度 ----
            if thread_id:
                tools = self._build_tool_snapshot_from_session(thread_id)
            else:
                tools = self._build_tool_snapshot_global()

            # ---- 任务维度（进程级） ----
            tasks = {
                "started": self._tasks_started,
                "completed": self._tasks_completed,
                "failed": self._tasks_failed,
                "cancelled": self._tasks_cancelled,
            }

            # ---- Token 维度 ----
            if thread_id:
                token_usage = self._build_token_snapshot_from_session(thread_id)
            else:
                token_usage = self._build_token_snapshot_global()

            # ---- 连接维度（进程级） ----
            websocket = {
                "messages_sent": self._ws_messages_sent,
                "errors": self._ws_errors,
                "active_connections": self._active_connections,
            }

            # ---- 缓存维度（进程级） ----
            cache = self._build_cache_snapshot()

            return {
                "tools": tools,
                "tasks": tasks,
                "token_usage": token_usage,
                "websocket": websocket,
                "cache": cache,
            }

    # ------------------------------------------------------------------
    # 内部构建方法
    # ------------------------------------------------------------------

    def _build_tool_snapshot_global(self) -> dict[str, dict[str, Any]]:
        """构建全局工具维度快照"""
        result: dict[str, dict[str, Any]] = {}
        all_tool_names = set(self._tools_invoked.keys()) | set(self._tools_failed.keys())
        for name in sorted(all_tool_names):
            invoked = self._tools_invoked.get(name, 0)
            failed = self._tools_failed.get(name, 0)
            total_ms = self._tools_duration_sum_ms.get(name, 0.0)
            max_ms = self._tools_duration_max_ms.get(name, 0.0)
            result[name] = {
                "invoked": invoked,
                "failed": failed,
                "error_rate": round(failed / invoked, 3) if invoked > 0 else 0,
                "avg_duration_ms": round(total_ms / invoked, 1) if invoked > 0 else 0,
                "max_duration_ms": max_ms,
            }
        return result

    def _build_tool_snapshot_from_session(self, thread_id: str) -> dict[str, dict[str, Any]]:
        """构建单个会话的工具维度快照"""
        session_data = self._tools_by_session.get(thread_id, {})
        result: dict[str, dict[str, Any]] = {}
        for tool_name, data in sorted(session_data.items()):
            invoked = data["invoked"]
            failed = data["failed"]
            total_ms = data["total_ms"]
            result[tool_name] = {
                "invoked": invoked,
                "failed": failed,
                "error_rate": round(failed / invoked, 3) if invoked > 0 else 0,
                "avg_duration_ms": round(total_ms / invoked, 1) if invoked > 0 else 0,
                "max_duration_ms": data["max_ms"],
            }
        return result

    def _build_token_snapshot_global(self) -> dict[str, Any]:
        """构建全局 Token 维度快照"""
        token_by_model = {
            model: {
                "prompt_tokens": data["prompt_tokens"],
                "completion_tokens": data["completion_tokens"],
                "total_tokens": data["total_tokens"],
                "call_count": data["call_count"],
            }
            for model, data in self._token_by_model.items()
        }

        token_histogram = {"count": len(self._token_call_sizes)}
        if self._token_call_sizes:
            sorted_sizes = sorted(self._token_call_sizes)
            n = len(sorted_sizes)
            token_histogram["p50"] = sorted_sizes[n // 2]
            token_histogram["p95"] = sorted_sizes[int(n * 0.95)] if n > 1 else sorted_sizes[0]
            token_histogram["max"] = sorted_sizes[-1]

        return {
            "by_model": token_by_model,
            "total_prompt_tokens": self._token_prompt_total,
            "total_completion_tokens": self._token_completion_total,
            "total_tokens": self._token_total,
            "per_call_histogram": token_histogram,
        }

    def _build_token_snapshot_from_session(self, thread_id: str) -> dict[str, Any]:
        """构建单个会话的 Token 维度快照"""
        session_data = self._token_by_session.get(thread_id, {})
        token_by_model = dict(session_data)
        total_prompt = sum(d["prompt_tokens"] for d in session_data.values())
        total_completion = sum(d["completion_tokens"] for d in session_data.values())
        total_all = sum(d["total_tokens"] for d in session_data.values())
        return {
            "by_model": token_by_model,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_all,
            "per_call_histogram": None,
        }

    def _build_cache_snapshot(self) -> dict[str, Any]:
        """构建缓存维度快照"""
        result: dict[str, dict[str, Any]] = {}
        all_namespaces = set(self._cache_hits.keys()) | set(self._cache_misses.keys())
        for ns in sorted(all_namespaces):
            hits = self._cache_hits.get(ns, 0)
            misses = self._cache_misses.get(ns, 0)
            total_lookups = hits + misses
            total_ms = self._cache_lookup_total_ms.get(ns, 0.0)
            count = self._cache_lookup_count.get(ns, 0)
            result[ns] = {
                "hits": hits,
                "misses": misses,
                "hit_rate": round(hits / total_lookups, 3) if total_lookups > 0 else 0,
                "avg_lookup_ms": round(total_ms / count, 1) if count > 0 else 0,
            }
        return {
            "by_namespace": result,
            "total_hits": sum(self._cache_hits.values()),
            "total_misses": sum(self._cache_misses.values()),
        }


# 全局单例
metrics_collector = MetricsCollector()
