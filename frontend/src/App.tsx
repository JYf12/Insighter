import {
  ApiOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { Alert, App as AntApp, Button } from "antd";
import { useEffect, useRef, useState } from "react";
import { ChatComposer } from "./components/ChatComposer";
import { ConversationThread } from "./components/ConversationThread";
import type { ChatTurn } from "./components/ConversationThread";
import { TraceDrawer } from "./components/TraceDrawer";
import { API_BASE_URL, WS_BASE_URL } from "./lib/config";
import { useDeepAgentSession } from "./hooks/useDeepAgentSession";
import type { ConnectionState, UploadedItem } from "./types";

function connectionLabel(state: ConnectionState): string {
  const labels: Record<ConnectionState, string> = {
    connecting: "Connecting",
    connected: "Connected",
    reconnecting: "Reconnecting",
    closed: "Closed"
  };
  return labels[state];
}

function createTurn(content: string): ChatTurn {
  return {
    id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}`,
    content,
    events: [],
    files: [],
    isRunning: true,
    result: "",
    timestamp: new Date().toISOString()
  };
}

export default function App() {
  const { message } = AntApp.useApp();
  const [query, setQuery] = useState("");
  const [stagedItems, setStagedItems] = useState<UploadedItem[]>([]);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const streamRef = useRef<HTMLElement | null>(null);
  const session = useDeepAgentSession();
  // ?trace=<run_id> 深链可直接打开某次 run 的调用链 Drawer(便于回放/联调)
  const [traceRunId, setTraceRunId] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("trace")
  );

  useEffect(() => {
    setTurns((previous) => {
      if (previous.length === 0) {
        return previous;
      }

      const latestTurn = previous[previous.length - 1];
      const nextLatestTurn = {
        ...latestTurn,
        events: session.events,
        files: session.files,
        isRunning: session.isRunning,
        result: session.result
      };

      return [...previous.slice(0, -1), nextLatestTurn];
    });
  }, [session.events, session.files, session.isRunning, session.result]);

  useEffect(() => {
    const streamNode = streamRef.current;
    if (!streamNode) {
      return;
    }

    window.requestAnimationFrame(() => {
      streamNode.scrollTo({
        top: streamNode.scrollHeight,
        behavior: "smooth"
      });
    });
  }, [turns]);

  async function handleSubmit() {
    const cleanQuery = query.trim();
    if (!cleanQuery) {
      message.warning("Please enter a research task");
      return;
    }

    const nextTurn = createTurn(cleanQuery);
    setTurns((previous) => [...previous, nextTurn]);
    setQuery("");

    try {
      const response = await session.submitTask(cleanQuery);
      if (response.run_id) {
        // 把本次 run 的 ID 挂到对应消息上,完成后即可打开调用链 trace
        setTurns((previous) =>
          previous.map((turn) =>
            turn.id === nextTurn.id ? { ...turn, runId: response.run_id } : turn
          )
        );
      }
      message.success("Task started — progress will appear in the conversation");
    } catch (error) {
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === nextTurn.id
            ? {
                ...turn,
                isRunning: false,
                result: error instanceof Error ? error.message : "Task failed to start"
              }
            : turn
        )
      );
      message.error(error instanceof Error ? error.message : "Task failed to start");
    }
  }

  async function handleCancel() {
    try {
      const response = await session.cancelCurrentTask();
      message.info(response.status === "cancelling" ? "Cancelling… waiting for current call to finish" : "Task cancelled");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "Failed to cancel task");
    }
  }

  async function handleUpload(items: UploadedItem[]) {
    try {
      const response = await session.uploadFiles(items);
      setStagedItems([]);
      message.success(`Uploaded ${response.files.length} file(s)`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "Upload failed");
    }
  }

  function handleNewSession() {
    session.resetSession();
    setTurns([]);
    setQuery("");
    setStagedItems([]);
  }

  const online = session.connectionState === "connected";

  return (
    <div className="chat-app-shell min-h-dvh">
      <aside className="chat-sidebar" aria-label="Session info">
        <div className="sidebar-brand">
          <span className="panel-kicker">INSIGHTER</span>
          <h1>Insighter</h1>
          <p>Multi-agent deep research console</p>
        </div>

        <Button className="new-chat-button" block onClick={handleNewSession}>
          New Research
        </Button>

        <div className="sidebar-section">
          <span className="sidebar-label">THREAD</span>
          <strong className="thread-id" title={session.threadId}>
            {session.threadId.slice(0, 8)}
          </strong>
        </div>

        <div className="sidebar-status-list">
          <div className={`sidebar-status ${online ? "sidebar-status--online" : "sidebar-status--warn"}`}>
            <ApiOutlined aria-hidden />
            <span>WebSocket</span>
            <strong>{connectionLabel(session.connectionState)}</strong>
          </div>
          <div className="sidebar-status">
            <BranchesOutlined aria-hidden />
            <span>Assistants</span>
            <strong>{session.stats.assistantEvents}</strong>
          </div>
          <div className="sidebar-status">
            <ToolOutlined aria-hidden />
            <span>Tool calls</span>
            <strong>{session.stats.toolEvents}</strong>
          </div>
          <div className={session.stats.errorEvents > 0 ? "sidebar-status sidebar-status--error" : "sidebar-status"}>
            <CloseCircleOutlined aria-hidden />
            <span>Errors</span>
            <strong>{session.stats.errorEvents}</strong>
          </div>
        </div>

        <div className="sidebar-section">
          <span className="sidebar-label">AGENTS</span>
          <ul className="agent-mini-list">
            <li>
              <CloudServerOutlined aria-hidden />
              Web Search Agent
            </li>
            <li>
              <DatabaseOutlined aria-hidden />
              Database Agent
            </li>
            <li>
              <FileSearchOutlined aria-hidden />
              RAGFlow Agent
            </li>
          </ul>
        </div>

        <div className="sidebar-section sidebar-endpoints">
          <span className="sidebar-label">ENDPOINTS</span>
          <code>{API_BASE_URL}</code>
          <code>{WS_BASE_URL}</code>
        </div>
      </aside>

      <main className="chat-main">
        <header className="chat-topbar">
          <div>
            <span className="panel-kicker">WORKSPACE</span>
            <h2>Research Chat</h2>
          </div>
          <div className={`run-indicator ${session.isRunning ? "run-indicator--live" : ""}`}>
            {session.isRunning ? <BranchesOutlined aria-hidden /> : <CheckCircleOutlined aria-hidden />}
            {session.isRunning ? "Researching" : "Ready"}
          </div>
        </header>

        {session.lastError ? (
          <Alert
            className="chat-alert"
            message={session.lastError}
            showIcon
            type="error"
          />
        ) : null}

        <section className="chat-stream-panel" ref={streamRef}>
          <ConversationThread
            onUseExample={setQuery}
            onViewTrace={setTraceRunId}
            turns={turns}
          />
        </section>

        <ChatComposer
          isCancelling={session.isCancelling}
          isRunning={session.isRunning}
          isUploading={session.isUploading}
          onCancel={handleCancel}
          onNewSession={handleNewSession}
          onQueryChange={setQuery}
          onStagedItemsChange={setStagedItems}
          onSubmit={handleSubmit}
          onUpload={handleUpload}
          query={query}
          stagedItems={stagedItems}
          uploadedItems={session.uploadedItems}
        />
      </main>

      <TraceDrawer
        onClose={() => setTraceRunId(null)}
        open={Boolean(traceRunId)}
        runId={traceRunId}
      />
    </div>
  );
}
