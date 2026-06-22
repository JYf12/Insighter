"""
任务持久化测试：验证 TaskStore（Redis）的完整 CRUD 和恢复机制

测试覆盖：
1. TaskStore 基本生命周期（连接/断开 Redis）
2. 任务状态流转：pending → running → completed/failed/cancelled
3. 恢复扫描：get_running_thread_ids / get_pending_thread_ids
4. 模拟中断恢复流程：创建任务 → 模拟中断 → 恢复扫描
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.persistence.task_store import (
    TaskStore,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_CANCELLED,
)


DB = 15  # 测试专用数据库编号，避免污染默认库


async def need_redis() -> bool:
    """检查 Redis 是否可达"""
    import redis.asyncio as redis
    try:
        r = redis.Redis(host="localhost", port=6379, db=DB, decode_responses=True)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


async def get_test_store() -> TaskStore:
    """创建并返回一个已连接的、干净的 TaskStore 测试实例"""
    import redis.asyncio as redis
    r = redis.Redis(host="localhost", port=6379, db=DB, decode_responses=True)
    await r.flushdb()

    store = TaskStore()
    store._redis = r  # 注入测试连接，绕过 start() 的环境变量读取
    return store


async def cleanup_store(store: TaskStore):
    """清理测试数据并断开连接"""
    if store._redis:
        await store._redis.flushdb()
        await store._redis.aclose()


async def test_taskstore_lifecycle():
    """测试 TaskStore 完整生命周期"""
    print("=" * 60)
    print("[TEST] 测试 1: TaskStore 生命周期 — start / stop / ping")
    print("=" * 60)

    if not await need_redis():
        print("  ⚠ Redis 不可用，跳过。请启动: docker compose up -d redis")
        return

    store = TaskStore()
    await store.start()
    print("  ✓ TaskStore.start() — connected")

    await store.stop()
    print("  ✓ TaskStore.stop() — closed")

    print("[PASS] 测试 1 通过\n")


async def test_status_flow():
    """测试任务状态流转"""
    print("=" * 60)
    print("[TEST] 测试 2: 任务状态流转 — pending → running → completed")
    print("=" * 60)

    if not await need_redis():
        print("  ⚠ Redis 不可用，跳过")
        return

    store = await get_test_store()

    # 创建任务
    await store.create_task("flow-test-1", "测试查询 1")
    status = await store.get_task_status("flow-test-1")
    assert status == TASK_PENDING, f"预期 pending，实际 {status}"
    print(f"  ✓ create_task → status = {status}")

    # 标记运行
    await store.mark_running("flow-test-1")
    status = await store.get_task_status("flow-test-1")
    assert status == TASK_RUNNING, f"预期 running，实际 {status}"
    print(f"  ✓ mark_running → status = {status}")

    # 标记完成
    await store.mark_completed("flow-test-1")
    status = await store.get_task_status("flow-test-1")
    assert status == TASK_COMPLETED, f"预期 completed，实际 {status}"
    print(f"  ✓ mark_completed → status = {status}")

    # 验证任务元数据完整
    meta = await store.get_task("flow-test-1")
    assert meta is not None, "get_task 返回 None"
    assert "created_at" in meta, "缺少 created_at"
    assert "updated_at" in meta, "缺少 updated_at"
    assert meta["query"] == "测试查询 1"
    print(f"  ✓ 任务元数据完整: {list(meta.keys())}")

    await cleanup_store(store)
    print("[PASS] 测试 2 通过\n")


async def test_status_flow_failed():
    """测试 pending → running → failed"""
    print("=" * 60)
    print("[TEST] 测试 3: 任务状态流转 — pending → running → failed")
    print("=" * 60)

    if not await need_redis():
        print("  ⚠ Redis 不可用，跳过")
        return

    store = await get_test_store()
    await store.create_task("flow-test-2", "失败场景测试")
    await store.mark_running("flow-test-2")
    await store.mark_failed("flow-test-2")
    status = await store.get_task_status("flow-test-2")
    assert status == TASK_FAILED, f"预期 failed，实际 {status}"
    print(f"  ✓ mark_failed → status = {status}")

    await cleanup_store(store)
    print("[PASS] 测试 3 通过\n")


async def test_status_flow_cancelled():
    """测试 pending → running → cancelled"""
    print("=" * 60)
    print("[TEST] 测试 4: 任务状态流转 — pending → running → cancelled")
    print("=" * 60)

    if not await need_redis():
        print("  ⚠ Redis 不可用，跳过")
        return

    store = await get_test_store()
    await store.create_task("flow-test-3", "取消场景测试")
    await store.mark_running("flow-test-3")
    await store.mark_cancelled("flow-test-3")
    status = await store.get_task_status("flow-test-3")
    assert status == TASK_CANCELLED, f"预期 cancelled，实际 {status}"
    print(f"  ✓ mark_cancelled → status = {status}")

    await cleanup_store(store)
    print("[PASS] 测试 4 通过\n")


async def test_recovery_scanning():
    """测试恢复扫描：创建不同状态的任务，验证 recovery 扫描函数"""
    print("=" * 60)
    print("[TEST] 测试 5: 恢复扫描 — get_running / get_pending")
    print("=" * 60)

    if not await need_redis():
        print("  ⚠ Redis 不可用，跳过")
        return

    store = await get_test_store()

    # 创建不同状态的任务
    await store.create_task("recover-pending-1", "未启动任务")
    # pending 状态天然就是 unstarted

    await store.create_task("recover-running-1", "运行中任务 1")
    await store.mark_running("recover-running-1")

    await store.create_task("recover-running-2", "运行中任务 2")
    await store.mark_running("recover-running-2")

    await store.create_task("recover-completed-1", "已完成任务")
    await store.mark_running("recover-completed-1")
    await store.mark_completed("recover-completed-1")

    # 验证扫描结果
    running = await store.get_running_thread_ids()
    pending = await store.get_pending_thread_ids()

    assert "recover-running-1" in running, f"running 集合中缺少 recover-running-1，runnning = {running}"
    assert "recover-running-2" in running, f"running 集合中缺少 recover-running-2，runnning = {running}"
    assert len(running) == 2, f"预期 2 个 running 任务，实际 {len(running)}"
    print(f"  ✓ running 任务: {running}")

    assert "recover-pending-1" in pending, f"pending 集合中缺少 recover-pending-1，pending = {pending}"
    assert len(pending) == 1, f"预期 1 个 pending 任务，实际 {len(pending)}"
    print(f"  ✓ pending 任务: {pending}")

    # 已完成任务不应出现在 running 或 pending 中
    assert "recover-completed-1" not in running, "已完成任务不应出现在 running"
    assert "recover-completed-1" not in pending, "已完成任务不应出现在 pending"
    print(f"  ✓ 已完成任务已从 running/pending 集合移除")

    await cleanup_store(store)
    print("[PASS] 测试 5 通过\n")


async def test_full_recovery_simulation():
    """模拟完整的中断恢复流程"""
    print("=" * 60)
    print("[TEST] 测试 6: 中断恢复完整模拟")
    print("=" * 60)

    if not await need_redis():
        print("  ⚠ Redis 不可用，跳过")
        return

    store = await get_test_store()

    # Step 1: 服务正常运行，创建并开始执行任务
    await store.create_task("recovery-sim-1", "完整的恢复模拟测试")
    print("  ✓ Step 1: 任务已创建 (pending)")

    await store.mark_running("recovery-sim-1")
    print("  ✓ Step 2: 任务开始执行 (running)")

    # Step 2.5: 验证 Redis 中的完整数据
    meta = await store.get_task("recovery-sim-1")
    assert meta["query"] == "完整的恢复模拟测试"
    assert meta["status"] == TASK_RUNNING
    print(f"  ✓ Step 2.5: Redis 中任务数据完整: {meta}")

    # Step 3: 模拟服务宕机 & 重启
    # 实际场景中：进程退出，Redis 数据保留，CheckpointManager 的 SQLite 文件保留
    print("  ✓ Step 3: ⚡ 模拟服务宕机 (进程退出，Redis 和 SQLite 数据保留)")

    # Step 4: 启动恢复扫描 (模拟 lifespan 中的 _recover_tasks)
    running_ids = await store.get_running_thread_ids()
    print(f"  ✓ Step 4: 恢复扫描发现 {len(running_ids)} 个 running 任务: {running_ids}")

    for tid in running_ids:
        task_data = await store.get_task(tid)
        assert task_data is not None
        print(f"    → 将恢复 thread_id={tid}, query={task_data['query'][:50]}...")

    # Step 5: 任务恢复成功，标记已完成
    for tid in running_ids:
        await store.mark_completed(tid)
    print("  ✓ Step 5: 所有恢复的任务标记为 completed")

    # 验证最终状态
    final = await store.get_task("recovery-sim-1")
    assert final["status"] == TASK_COMPLETED
    print(f"  ✓ Step 6: 任务最终状态 = {final['status']}")

    running_after = await store.get_running_thread_ids()
    assert len(running_after) == 0, "所有任务完成，running 集合应为空"
    print(f"  ✓ Step 7: running 集合已清空")

    await cleanup_store(store)
    print("[PASS] 测试 6 通过\n")


async def main():
    """运行所有任务持久化测试"""
    print("\n" + "=" * 60)
    print("  DeepSearch Agents - 任务持久化 (Redis) 验证测试套件")
    print("  Redis DB: 15 (测试专用，会被清空)")
    print("=" * 60 + "\n")

    # await test_taskstore_lifecycle()
    # await test_status_flow()
    # await test_status_flow_failed()
    # await test_status_flow_cancelled()
    await test_recovery_scanning()
    await test_full_recovery_simulation()

    print("=" * 60)
    print("  所有测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
