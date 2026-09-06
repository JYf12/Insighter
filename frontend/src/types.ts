export type ConnectionState = "connecting" | "connected" | "reconnecting" | "closed";

export type MonitorEventName =
  | "session_created"
  | "tool_start"
  | "assistant_call"
  | "task_result"
  | "task_cancelled"
  | "error"
  | string;

export interface MonitorMessage {
  type: "monitor_event";
  event: MonitorEventName;
  message: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface PongMessage {
  type: "pong";
  message: string;
}

export type SocketMessage = MonitorMessage | PongMessage;

export interface TaskResponse {
  status: "started" | string;
  thread_id: string;
  /** 本次执行尝试的唯一 ID,与后端 trace 一一对应;同 thread 可多次 run */
  run_id?: string;
}

export interface CancelTaskResponse {
  status: "cancelled" | "cancelling" | string;
  thread_id: string;
  message?: string;
}

export interface UploadResponse {
  status: "uploaded" | string;
  files: string[];
}

export interface OutputFile {
  name: string;
  type: "file" | string;
  path: string;
  size: number;
  mtime: number;
}

export interface FileListResponse {
  files?: OutputFile[];
  error?: string;
}

export interface UploadedItem {
  uid: string;
  name: string;
  size: number;
  raw: File;
}

// ---- Task-run 级 Trace ----

export interface TraceSpan {
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  name: string;
  kind: "llm" | "tool" | "chain" | string;
  start: string;
  end: string | null;
  duration_ms: number | null;
  status: string;
  attributes: Record<string, unknown>;
}

export interface TraceDocument {
  run_id: string;
  thread_id: string;
  status: string;
  started_at: string;
  ended_at: string;
  budget?: Record<string, unknown>;
  spans: TraceSpan[];
}

export interface TraceSummary {
  run_id: string;
  thread_id: string;
  status: string;
  started_at: string;
  ended_at?: string;
  span_count: number;
}

export interface TraceListResponse {
  traces?: TraceSummary[];
}
