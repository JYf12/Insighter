"""
Recovery Engine:执行中错误的分类与针对性恢复策略

v1 范围(已确认决策):瞬态重试(指数退避)+ LLM 自纠错(错误回注)+ 熔断器。

设计要点:
- 错误分类 ErrorKind 决定策略:
  - TRANSIENT(网络/429/5xx/DB断连)→ 指数退避 + jitter 重试,仍失败转 Correctable。
  - CORRECTABLE(无效SQL/坏参数/工具未找到/context溢出)→ 不重试,格式化为结构化
    错误串作为工具结果返回,让 LLM 下一轮自纠错(正式化 db_tools 既有模式)。
  - PERMANENT(401/403/缺配置)→ 快速失败,返回错误串给 LLM,不上抛中断 run。
  - RESOURCE(budget/OOM)→ 上抛,透传给 Budget 软停止 / 顶层处理。
- 熔断器 CircuitBreaker 仅对 TRANSIENT/PERMANENT(服务侧)失败计数;CORRECTABLE
  (调用方失误)不计入,避免 LLM 的坏 SQL 错误把外部服务熔断。
- @resilient 装饰器作用于工具函数实现层(非 callback,因重试需真正再执行),
  同时支持 sync 与 async 工具。
"""

import asyncio
import functools
import inspect
import os
import random
import time
from enum import Enum
from typing import Any, Callable, Optional

from app.utils.logger import get_logger

_logger = get_logger("recovery")

# 收尾错误串模板:回注 LLM 让其自纠错或改用替代方案
_ERROR_RETURN_TEMPLATE = "[工具执行失败:{tool}] {kind}: {error}"


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    CORRECTABLE = "correctable"
    PERMANENT = "permanent"
    RESOURCE = "resource"


# ------------------------------------------------------------------
# 错误分类
# ------------------------------------------------------------------

# 已知瞬态异常类型名(字符串匹配,避免硬依赖具体包)
_TRANSIENT_EXC_NAMES = {
    "TimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "ConnectionRefusedError",
    "OSError",
    "ChunkedEncodingError",
    "IncompleteRead",
    "ConnectTimeout",
    "ReadTimeout",
    "ProtocolError",
}
# 已知永久异常类型名(鉴权/配置类)
_PERMANENT_EXC_NAMES = {
    "AuthenticationError",
    "PermissionError",
    "UnauthorizedError",
    "AccessDeniedError",
    "ValueError",  # 缺失核心配置等(如 db_tools get_db_config 抛出)
}
# 已知可纠错异常类型名(调用方参数/SQL 错误)
_CORRECTABLE_EXC_NAMES = {
    "ProgrammingError",  # mysql.connector: SQL 语法/坏表名
    "IntegrityError",  # 外键/唯一约束冲突
    "OperationalError",  # 部分可纠错的运行时 SQL 错误
}


def _exc_name(exc: BaseException) -> str:
    return type(exc).__name__


def classify(exc: BaseException, tool_name: str = "") -> ErrorKind:
    """根据异常类型与消息判断错误类别

    判定顺序:Resource → Transient → Permanent → Correctable(兜底)。
    调用方失误优先归为 Correctable,确保错误回注 LLM 自纠错而非盲目重试。
    """
    name = _exc_name(exc)
    msg = str(exc).lower()

    # Resource:预算/内存类,上抛交由 Budget 软停止
    if isinstance(exc, MemoryError):
        return ErrorKind.RESOURCE
    if "budget" in msg or "exhausted" in msg or "max_" in msg:
        return ErrorKind.RESOURCE

    # Transient:网络/限流/5xx/DB连接
    if name in _TRANSIENT_EXC_NAMES:
        return ErrorKind.TRANSIENT
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return ErrorKind.TRANSIENT
    if "timed out" in msg or "timeout" in msg:
        return ErrorKind.TRANSIENT
    if "connection" in msg and ("refused" in msg or "reset" in msg or "closed" in msg or "unreachable" in msg):
        return ErrorKind.TRANSIENT
    if "502" in msg or "503" in msg or "504" in msg or "service unavailable" in msg or "bad gateway" in msg:
        return ErrorKind.TRANSIENT
    if name in ("OperationalError",) and ("connection" in msg or "lost" in msg or "gone away" in msg or "2006" in msg or "2013" in msg or "2003" in msg):
        # mysql.connector OperationalError 中的连接类错误码(2003/2006/2013)属瞬态
        return ErrorKind.TRANSIENT

    # Permanent:鉴权/配置缺失
    if name in _PERMANENT_EXC_NAMES:
        # ValueError 既可能是缺配置(永久),也可能是参数错误(可纠错);按消息细分
        if "配置" in str(exc) or "config" in msg or "缺失" in str(exc) or "api_key" in msg or "api key" in msg:
            return ErrorKind.PERMANENT
        # 否则当作可纠错参数错误
        return ErrorKind.CORRECTABLE
    if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg or "invalid api key" in msg:
        return ErrorKind.PERMANENT

    # Correctable:SQL/参数类
    if name in _CORRECTABLE_EXC_NAMES:
        return ErrorKind.CORRECTABLE
    if "context window" in msg or "context length" in msg or "maximum context" in msg or "too long" in msg:
        return ErrorKind.CORRECTABLE
    if "not found" in msg and "table" in msg:
        return ErrorKind.CORRECTABLE

    # 兜底:无法归类视为可纠错(回注 LLM,最安全的行为)
    return ErrorKind.CORRECTABLE


# ------------------------------------------------------------------
# 熔断器
# ------------------------------------------------------------------


class CircuitBreaker:
    """单工具熔断器:closed → open(连续失败达阈值)→ cooldown 后 half-open 探活

    线程安全:状态读写在锁内。跨 task run 共享(同一外部服务的健康度是全局的)。
    """

    _STATES = ("closed", "open", "half_open")

    def __init__(
        self,
        name: str,
        threshold: Optional[int] = None,
        cooldown_s: Optional[float] = None,
    ) -> None:
        self.name = name
        self.threshold = threshold or int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "5"))
        self.cooldown_s = cooldown_s or float(os.getenv("CIRCUIT_BREAKER_COOLDOWN_S", "60"))
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._lock = __import__("threading").Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        """open 状态冷却到期后转 half_open,放行一次探活(调用方持锁)"""
        if self._state == "open" and (time.monotonic() - self._opened_at) >= self.cooldown_s:
            self._state = "half_open"

    def allow(self) -> bool:
        """是否放行本次调用;open 期间直接短路"""
        with self._lock:
            self._maybe_half_open()
            return self._state in ("closed", "half_open")

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"

    def on_failure(self) -> None:
        with self._lock:
            self._maybe_half_open()
            self._failures += 1
            if self._state == "half_open":
                # 探活失败,重新打开
                self._state = "open"
                self._opened_at = time.monotonic()
            elif self._failures >= self.threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                _logger.warning(
                    "Circuit breaker opened",
                    extra={"breaker": self.name, "failures": self._failures},
                )

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "threshold": self.threshold,
                "cooldown_s": self.cooldown_s,
            }


# 全局熔断器注册表:tool_name -> CircuitBreaker
_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = __import__("threading").Lock()


def get_breaker(tool_name: str) -> CircuitBreaker:
    """获取或创建某工具的熔断器(全局共享)"""
    with _breakers_lock:
        if tool_name not in _breakers:
            _breakers[tool_name] = CircuitBreaker(tool_name)
        return _breakers[tool_name]


def all_breakers_snapshot() -> dict:
    """导出所有熔断器状态快照,供 /api/metrics"""
    with _breakers_lock:
        return {name: b.snapshot() for name, b in _breakers.items()}


# ------------------------------------------------------------------
# 退避
# ------------------------------------------------------------------


def _backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """指数退避 + jitter:delay = min(cap, base * 2^attempt) * (0.5 + 0.5 * random)"""
    delay = min(cap, base * (2 ** attempt))
    return delay * (0.5 + 0.5 * random.random())


# ------------------------------------------------------------------
# @resilient 装饰器
# ------------------------------------------------------------------


def _format_error(tool_name: str, kind: ErrorKind, exc: BaseException) -> str:
    """生成回注 LLM 的结构化错误串"""
    return _ERROR_RETURN_TEMPLATE.format(tool=tool_name, kind=kind.value, error=str(exc))


def resilient(
    tool_name: Optional[str] = None,
    max_retries: Optional[int] = None,
    use_breaker: bool = True,
) -> Callable[[Callable], Callable]:
    """工具函数的恢复装饰器

    - use_breaker=True 时,open 状态直接返回"服务暂不可用"串给 LLM(不发起调用)。
    - TRANSIENT:指数退避重试 max_retries 次;成功即止;耗尽则转 Correctable 回注。
    - CORRECTABLE/PERMANENT:不重试,返回结构化错误串让 LLM 自纠错。
    - RESOURCE:上抛,交由 Budget/顶层软停止处理。

    同时支持 sync 与 async 被装饰函数。
    """
    retries = max_retries if max_retries is not None else int(os.getenv("RECOVERY_MAX_RETRIES", "3"))

    def decorator(func: Callable) -> Callable:
        name = tool_name or func.__name__
        breaker = get_breaker(name) if use_breaker else None

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await _execute_with_recovery_async(func, name, breaker, retries, args, kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            return _execute_with_recovery_sync(func, name, breaker, retries, args, kwargs)

        return sync_wrapper

    return decorator


def _unavailable_msg(tool_name: str) -> str:
    return _format_error(tool_name, ErrorKind.TRANSIENT, _BreakerOpenError(tool_name))


class _BreakerOpenError(Exception):
    """熔断器开启时的占位异常,仅用于生成回注 LLM 的错误串"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"{tool_name} 服务暂不可用(熔断中),请稍后重试或改用替代方案")


async def _execute_with_recovery_async(func, name, breaker, retries, args, kwargs):
    from app.api.metrics import metrics_collector

    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):  # 首次 + retries 次重试
        if breaker is not None and not breaker.allow():
            _logger.info("Breaker open, short-circuit", extra={"tool": name})
            metrics_collector.record_breaker_trip(name)
            return _unavailable_msg(name)
        try:
            result = await func(*args, **kwargs)
            if breaker is not None:
                breaker.on_success()
            return result
        except BaseException as exc:  # noqa: BLE001 - 需捕获所有以分类
            kind = classify(exc, name)
            last_exc = exc
            # Resource 上抛
            if kind == ErrorKind.RESOURCE:
                raise
            # 服务侧失败计入熔断器
            if breaker is not None and kind in (ErrorKind.TRANSIENT, ErrorKind.PERMANENT):
                breaker.on_failure()
            metrics_collector.record_recovery_retry(name)
            # 只有 TRANSIENT 且还有重试机会才退避重试
            if kind == ErrorKind.TRANSIENT and attempt < retries:
                delay = _backoff_delay(attempt)
                _logger.warning(
                    "Transient error, retrying",
                    extra={"tool": name, "attempt": attempt + 1, "delay_s": round(delay, 2), "error": str(exc)},
                )
                await asyncio.sleep(delay)
                continue
            # CORRECTABLE / PERMANENT / 重试耗尽:回注 LLM
            _logger.warning(
                "Tool returning error to LLM",
                extra={"tool": name, "kind": kind.value, "error": str(exc)},
            )
            return _format_error(name, kind, exc)
    # 理论不可达:循环内必返回;兜底
    return _format_error(name, classify(last_exc, name) if last_exc else ErrorKind.CORRECTABLE, last_exc or RuntimeError("unknown"))


def _execute_with_recovery_sync(func, name, breaker, retries, args, kwargs):
    from app.api.metrics import metrics_collector

    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        if breaker is not None and not breaker.allow():
            _logger.info("Breaker open, short-circuit", extra={"tool": name})
            metrics_collector.record_breaker_trip(name)
            return _unavailable_msg(name)
        try:
            result = func(*args, **kwargs)
            if breaker is not None:
                breaker.on_success()
            return result
        except BaseException as exc:  # noqa: BLE001
            kind = classify(exc, name)
            last_exc = exc
            if kind == ErrorKind.RESOURCE:
                raise
            if breaker is not None and kind in (ErrorKind.TRANSIENT, ErrorKind.PERMANENT):
                breaker.on_failure()
            metrics_collector.record_recovery_retry(name)
            if kind == ErrorKind.TRANSIENT and attempt < retries:
                delay = _backoff_delay(attempt)
                _logger.warning(
                    "Transient error, retrying",
                    extra={"tool": name, "attempt": attempt + 1, "delay_s": round(delay, 2), "error": str(exc)},
                )
                time.sleep(delay)
                continue
            _logger.warning(
                "Tool returning error to LLM",
                extra={"tool": name, "kind": kind.value, "error": str(exc)},
            )
            return _format_error(name, kind, exc)
    return _format_error(name, classify(last_exc, name) if last_exc else ErrorKind.CORRECTABLE, last_exc or RuntimeError("unknown"))
