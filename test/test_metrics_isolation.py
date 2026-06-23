"""Test MetricsCollector global vs session isolation"""
import sys
from pathlib import Path

# Ensure project root is on path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.api.metrics import metrics_collector

# Manually inject a thread_id through ContextVar
from app.api.context import set_thread_context

def test_metrics():
    print("=== Testing MetricsCollector ===")

    # Simulate task execution with thread_id
    set_thread_context("test-session-1")

    # Record some tool invocations
    metrics_collector.record_tool_invoked("internet_search")
    metrics_collector.record_tool_invoked("internet_search")
    metrics_collector.record_tool_invoked("execute_sql")
    metrics_collector.record_tool_duration("internet_search", 150.0)
    metrics_collector.record_tool_duration("internet_search", 200.0)
    metrics_collector.record_tool_duration("execute_sql", 300.0)
    metrics_collector.record_tool_failed("execute_sql")

    # Record some token usage
    metrics_collector.record_token_usage("test-model", 100, 50, 150)
    metrics_collector.record_token_usage("test-model", 200, 80, 280)

    # Check global snapshot
    global_snap = metrics_collector.snapshot()
    print("\n--- Global snapshot ---")
    print(f"tools: {global_snap['tools']}")
    print(f"token_usage total_tokens: {global_snap['token_usage']['total_tokens']}")

    # Check session snapshot
    session_snap = metrics_collector.snapshot(thread_id="test-session-1")
    print("\n--- Session snapshot (test-session-1) ---")
    print(f"tools: {session_snap['tools']}")
    print(f"token_usage total_tokens: {session_snap['token_usage']['total_tokens']}")

    # Verify global is NOT empty
    assert global_snap['tools'], "Global tools should NOT be empty!"
    assert global_snap['token_usage']['total_tokens'] == 430, f"Expected 430, got {global_snap['token_usage']['total_tokens']}"
    assert global_snap['tools']['internet_search']['invoked'] == 2
    assert global_snap['tools']['execute_sql']['invoked'] == 1

    # Verify session matches
    assert session_snap['tools']['internet_search']['invoked'] == 2
    assert session_snap['tools']['execute_sql']['invoked'] == 1

    print("\nAll assertions passed!")
    print("  Global and session counters both work correctly.")

if __name__ == "__main__":
    test_metrics()
