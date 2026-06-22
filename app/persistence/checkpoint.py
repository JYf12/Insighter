"""
SQLite 检查点管理器

负责 AsyncSqliteSaver 的完整生命周期管理。
替代原先在 main_agent.py 模块级创建的 InMemorySaver，
通过 FastAPI lifespan 管理 open/close，确保服务重启后检查点不丢失。
"""

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

load_dotenv(find_dotenv())


class CheckpointManager:
    """管理 AsyncSqliteSaver 的打开 / 关闭生命周期"""

    def __init__(self) -> None:
        self._checkpointer: AsyncSqliteSaver | None = None
        self._context = None

    @property
    def checkpointer(self) -> AsyncSqliteSaver:
        """返回底层 AsyncSqliteSaver 实例，供 create_deep_agent 注入"""
        if self._checkpointer is None:
            raise RuntimeError("CheckpointManager not started. Call start() first.")
        return self._checkpointer

    async def start(self, db_path: str | None = None) -> AsyncSqliteSaver:
        """打开 SQLite 连接并通过 async context manager 初始化 AsyncSqliteSaver

        :param db_path: SQLite 数据库文件路径；默认从环境变量读取
        :return: 初始化完成的 AsyncSqliteSaver 实例
        """
        if db_path is None:
            db_path = os.getenv("SQLITE_CHECKPOINT_PATH", "app/data/checkpoints.db")

        # 确保父目录存在（如 app/data/）
        db_file = Path(db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        self._checkpointer = AsyncSqliteSaver.from_conn_string(str(db_path))
        self._context = await self._checkpointer.__aenter__()
        print(f"[Checkpoint] SQLite checkpointer opened at {db_path}")
        return self._checkpointer

    async def stop(self) -> None:
        """关闭 SQLite 连接"""
        if self._checkpointer is not None and self._context is not None:
            await self._checkpointer.__aexit__(None, None, None)
            print("[Checkpoint] SQLite checkpointer closed")
            self._checkpointer = None
            self._context = None
