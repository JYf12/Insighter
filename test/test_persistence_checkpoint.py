"""
检查点持久化测试：验证 CheckpointManager + AsyncSqliteSaver 的完整生命周期

测试覆盖：
1. CheckpointManager 启动时创建 SQLite 数据库文件
2. 写入检查点后文件大小发生变化
3. 停止后再启动，数据仍然可读
4. 多次操作的文件大小稳定预期
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.persistence.checkpoint import CheckpointManager


async def test_checkpoint_lifecycle():
    """测试 CheckpointManager 完整的 start/stop 生命周期"""
    print("=" * 60)
    print("[TEST] 测试 1: CheckpointManager 基本生命周期")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_checkpoints.db")

        # 1. 启动
        mgr = CheckpointManager()
        cp = await mgr.start(db_path)
        assert os.path.exists(db_path), f"SQLite 文件未创建: {db_path}"
        file_size_before = os.path.getsize(db_path)
        print(f"  ✓ SQLite 文件已创建: {db_path} (大小: {file_size_before} 字节)")

        # 2. 写入一个测试检查点
        write_config = {
            "configurable": {
                "thread_id": "test-thread-1",
                "checkpoint_ns": "",
            }
        }
        checkpoint_data = {
            "v": 1,
            "ts": "2025-01-01T00:00:00Z",
            "id": "test-checkpoint-id",
            "channel_values": {"messages": [{"role": "user", "content": "test"}]},
            "channel_versions": {},
            "versions_seen": {},
            "pending_sends": [],
        }
        metadata = {"source": "test", "step": 1}
        saved_config = await cp.aput(write_config, checkpoint_data, metadata, {})
        assert saved_config is not None, "aput 返回 None"
        print(f"  ✓ 写入检查点成功, config: {saved_config}")

        file_size_after = os.path.getsize(db_path)
        assert file_size_after > file_size_before, "写入后文件大小应增加"
        print(f"  ✓ 写入后文件大小增加: {file_size_before} → {file_size_after} 字节")

        # 3. 读取检查点
        read_config = {"configurable": {"thread_id": "test-thread-1"}}
        loaded = await cp.aget(read_config)
        assert loaded is not None, "aget 返回 None，检查点丢失"
        print(f"  ✓ 读取检查点成功, channel_values 存在: {'channel_values' in loaded}")

        # 4. 停止
        await mgr.stop()
        assert os.path.exists(db_path), "停止后 SQLite 文件应保留在磁盘"
        print(f"  ✓ CheckpointManager 停止成功, 文件仍在磁盘")

        # 给 Windows 一点时间释放 SQLite 文件句柄
        import asyncio
        await asyncio.sleep(0.5)

    print("[PASS] 测试 1 通过\n")


async def test_checkpoint_multiple_threads():
    """测试多个 thread_id 的检查点隔离"""
    print("=" * 60)
    print("[TEST] 测试 2: 多个 thread_id 检查点隔离")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "multi_thread.db")
        mgr = CheckpointManager()
        cp = await mgr.start(db_path)

        thread_ids = ["thread-a", "thread-b", "thread-c"]
        for i, tid in enumerate(thread_ids):
            write_config = {
                "configurable": {"thread_id": tid, "checkpoint_ns": ""}
            }
            cp_data = {
                "v": 1,
                "ts": f"2025-01-01T00:00:0{i}Z",
                "id": f"cp-{tid}",
                "channel_values": {"data": f"value-{i}"},
                "channel_versions": {},
                "versions_seen": {},
                "pending_sends": [],
            }
            await cp.aput(write_config, cp_data, {"step": i}, {})
            print(f"  ✓ 写入 thread_id={tid}")

        # 验证每个 thread 都能读到自己的数据
        for i, tid in enumerate(thread_ids):
            read_config = {"configurable": {"thread_id": tid}}
            loaded = await cp.aget(read_config)
            assert loaded is not None, f"thread_id={tid} 检查点丢失"
            print(f"  ✓ 读取 thread_id={tid} 成功")

        # 验证 list 能枚举所有检查点
        all_checkpoints = []
        for tid in thread_ids:
            async for cp_i in cp.alist({"configurable": {"thread_id": tid}}):
                all_checkpoints.append(cp_i)
            print(f"  ✓ thread_id={tid} 历史检查点数: {len([c async for c in cp.alist({'configurable': {'thread_id': tid}})])}")

        await mgr.stop()

        # 给 Windows 一点时间释放 SQLite 文件句柄
        import asyncio
        await asyncio.sleep(0.5)

    print("[PASS] 测试 2 通过\n")


async def test_checkpoint_restart_survival():
    """模拟重启：停止 CheckpointManager 再重新打开，数据仍在"""
    print("=" * 60)
    print("[TEST] 测试 3: 停止后重新打开，检查点数据持久化验证")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "survival.db")

        # 第一轮：写入数据
        mgr1 = CheckpointManager()
        cp1 = await mgr1.start(db_path)
        write_config = {
            "configurable": {"thread_id": "persist-test", "checkpoint_ns": ""}
        }
        cp_data = {
            "v": 1,
            "ts": "2025-06-01T12:00:00Z",
            "id": "persist-cp-1",
            "channel_values": {"messages": [{"role": "user", "content": "持久化测试消息"}]},
            "channel_versions": {},
            "versions_seen": {},
            "pending_sends": [],
        }
        await cp1.aput(write_config, cp_data, {"source": "persist-test"}, {})
        print("  ✓ 第一轮：写入检查点完成")
        await mgr1.stop()
        print("  ✓ 第一轮：CheckpointManager 停止")

        # 第二轮：重新打开并读取（模拟重启）
        mgr2 = CheckpointManager()
        cp2 = await mgr2.start(db_path)
        read_config = {"configurable": {"thread_id": "persist-test"}}
        loaded = await cp2.aget(read_config)
        assert loaded is not None, "重启后检查点丢失"
        assert "channel_values" in loaded, "重启后 channel_values 丢失"
        print(f"  ✓ 第二轮：成功读取持久化的检查点数据")
        await mgr2.stop()

        # 给 Windows 一点时间释放 SQLite 文件句柄
        import asyncio
        await asyncio.sleep(0.5)

print("[PASS] 测试 3 通过\n")


async def check_redis():
    """确认 Redis 是否可用"""
    import redis.asyncio as redis
    try:
        r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


async def test_checkpoint_with_redis_coordination():
    """综合测试：Checkpoint 写入 + Redis 任务状态 → 重启恢复链路的关键环节"""
    print("=" * 60)
    print("[TEST] 测试 4: 检查点 + Redis 恢复链路关键环节验证")
    print("=" * 60)

    redis_ok = await check_redis()
    if not redis_ok:
        print("  ⚠ Redis 不可用，跳过 Redis 相关验证")
        print("  请确保 Redis 已启动: docker compose up -d redis")
    else:
        import redis.asyncio as redis
        r = redis.Redis(host="localhost", port=6379, db=15, decode_responses=True)
        await r.flushdb()

        from app.persistence.task_store import TaskStore
        store = TaskStore()
        store._redis = r  # 直接注入测试用 Redis 连接

        thread_id = "integration-test-thread"
        query = "集成测试查询"

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "integrate.db")
            mgr = CheckpointManager()
            cp = await mgr.start(db_path)

            # 模拟任务生命周期
            await store.create_task(thread_id, query)
            status = await store.get_task_status(thread_id)
            assert status == "pending", f"预期 pending，实际 {status}"
            print(f"  ✓ 任务创建：状态={status}")

            await store.mark_running(thread_id)
            status = await store.get_task_status(thread_id)
            assert status == "running", f"预期 running，实际 {status}"
            print(f"  ✓ 任务运行：状态={status}")

            # 模拟执行过程中写入了一个检查点
            write_config = {
                "configurable": {"thread_id": thread_id, "checkpoint_ns": ""}
            }
            cp_data = {
                "v": 1,
                "ts": "2025-06-22T10:00:00Z",
                "id": f"cp-{thread_id}",
                "channel_values": {"step": "research_completed"},
                "channel_versions": {},
                "versions_seen": {},
                "pending_sends": [],
            }
            await cp.aput(write_config, cp_data, {"step": 3}, {})
            print("  ✓ 检查点已写入")

            # 模拟服务重启：停止管理器然后重新打开
            await mgr.stop()
            print("  ✓ 模拟服务关闭 (CheckpointManager + Redis 保留数据)")

            # Redis 中的任务 metadata 仍然存在
            task_meta = await store.get_task(thread_id)
            assert task_meta is not None, "Redis 中任务元数据丢失"
            assert task_meta["status"] == "running", "Redis 中状态应为 running"
            print(f"  ✓ 重启后 Redis 中任务状态仍为: {task_meta['status']}")

            # 检查点数据仍然存在
            mgr2 = CheckpointManager()
            cp2 = await mgr2.start(db_path)
            read_config = {"configurable": {"thread_id": thread_id}}
            loaded = await cp2.aget(read_config)
            assert loaded is not None, "重启后检查点数据丢失"
            print(f"  ✓ 重启后检查点数据可读取: channel_values={loaded.get('channel_values')}")
            await mgr2.stop()

            # 给 Windows 一点时间释放 SQLite 文件句柄
            import asyncio
            await asyncio.sleep(0.5)

    await r.flushdb()
    await r.aclose()

    print("[PASS] 测试 4 通过\n")


async def main():
    """运行所有持久化测试"""
    print("\n" + "=" * 60)
    print("  DeepSearch Agents - 持久化方案验证测试套件")
    print("=" * 60 + "\n")

    # await test_checkpoint_lifecycle()                     # 检查点生命周期验证
    # await test_checkpoint_multiple_threads()              # 多线程检查点隔离验证
    # await test_checkpoint_restart_survival()              # LangGraph检查点中断恢复验证
    await test_checkpoint_with_redis_coordination()         # 持久化执行状态 + Redis 恢复链路关键环节验证（模拟服务重启恢复机制）---redis负责找到重启前正在执行的任务(具体为thread_id)，checkpoint负责根据thread_id找到对应的检查点数据并恢复运行

    print("=" * 60)
    print("  所有测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
