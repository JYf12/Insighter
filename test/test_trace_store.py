"""
TraceStore 测试(Redis 近期窗口)

Redis 不可用时软跳过(与现有 test/ 风格一致)。
验证 save_trace / get_trace / list_runs / 近期窗口淘汰。
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.persistence.trace_store import TraceStore  # noqa: E402


def _spans(run_id):
    """构造一棵小型 span 树:run 根 → tool span → 子 LLM span"""
    return [
        {
            "trace_id": run_id,
            "span_id": "root",
            "parent_span_id": run_id,
            "name": "internet_search",
            "kind": "tool",
            "start": "2026-08-23T00:00:00Z",
            "end": "2026-08-23T00:00:01Z",
            "duration_ms": 1000.0,
            "status": "ok",
            "attributes": {"input": "query"},
        },
        {
            "trace_id": run_id,
            "span_id": "child-llm",
            "parent_span_id": "root",
            "name": "qwen-max",
            "kind": "llm",
            "start": "2026-08-23T00:00:00Z",
            "end": "2026-08-23T00:00:00.5Z",
            "duration_ms": 500.0,
            "status": "ok",
            "attributes": {"tokens": {"prompt": 100, "completion": 20, "total": 120}},
        },
    ]


async def main():
    store = TraceStore()
    store._max_runs = 5  # 小窗口便于验证淘汰
    try:
        await store.start()
    except Exception as e:
        print(f"⏭️  Redis 不可用,跳过 trace 测试: {e}")
        return

    try:
        # save + get
        run_id = str(uuid.uuid4())
        await store.save_trace(
            run_id=run_id,
            thread_id="thread-1",
            spans=_spans(run_id),
            status="completed",
            started_at="2026-08-23T00:00:00Z",
            ended_at="2026-08-23T00:00:01Z",
            budget_snapshot={"iterations": 2, "tokens": 120},
        )
        trace = await store.get_trace(run_id)
        assert trace is not None, "trace 应可读回"
        assert trace["run_id"] == run_id
        assert trace["thread_id"] == "thread-1"
        assert trace["status"] == "completed"
        assert len(trace["spans"]) == 2
        # 父子关系保留
        assert trace["spans"][1]["parent_span_id"] == "root"
        print("PASS: save/get_trace 通过")

        # list_runs 按 thread
        run2 = str(uuid.uuid4())
        await store.save_trace(run2, "thread-1", _spans(run2), "failed", "s", "e")
        runs = await store.list_runs(thread_id="thread-1")
        assert len(runs) >= 2
        assert runs[0]["span_count"] >= 1
        print("PASS: list_runs(thread) 通过")

        # 近期窗口淘汰:写入超过 max_runs 条后只保留最近 5
        for i in range(8):
            rid = str(uuid.uuid4())
            await store.save_trace(rid, "thread-trim", _spans(rid), "completed", "s", "e")
        recent = await store.list_runs(thread_id="thread-trim", limit=100)
        assert len(recent) <= 5, f"淘汰后应 <=5,实际 {len(recent)}"
        print("PASS: 近期窗口淘汰通过")
    finally:
        await store.stop()


if __name__ == "__main__":
    asyncio.run(main())
    print("PASS: trace_store tests passed")
