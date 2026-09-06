/**
 * TraceDrawer:查看一次 task run 的 span 树
 *
 * 数据来自 GET /api/trace/{run_id}(后端已折叠成干净树,parent_span_id 均可解析)。
 * 以 antd Tree 竖向缩进渲染;点节点在下方 Descriptions 展示该 span 详情。
 *
 * 节点构成(折叠后的典型结构):
 *   main_agent(chain)
 *     ├─ llm / tool(main 层)
 *     └─ task(tool) → subagent:xxx(chain)
 *          └─ llm / tool(子智能体内部)
 */

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { TreeDataNode } from "antd";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Spin,
  Tag,
  Tree,
  Tooltip,
} from "antd";
import {
  ApartmentOutlined,
  CloudServerOutlined,
  ClusterOutlined,
  ReloadOutlined,
  RobotOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { fetchTrace } from "../lib/api";
import { formatDateTime, formatDuration, formatMs } from "../lib/format";
import type { TraceDocument, TraceSpan } from "../types";

interface TraceDataNode extends TreeDataNode {
  span: TraceSpan;
}

interface TraceDrawerProps {
  runId: string | null;
  open: boolean;
  onClose: () => void;
}

// ---- 小工具 -------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function toNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value !== "" && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

function toText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function statusColor(status: string): string {
  if (status === "completed") {
    return "success";
  }
  if (status === "failed") {
    return "error";
  }
  if (status === "cancelled") {
    return "warning";
  }
  if (status === "running") {
    return "processing";
  }
  return "default";
}

function spanStatusColor(status: string): string {
  if (status === "error") {
    return "error";
  }
  if (status === "interrupted") {
    return "warning";
  }
  return "success";
}

function formatTokens(value: number): string {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`;
  }
  return String(value);
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// ---- 树构建 -------------------------------------------------------------

/** 由扁平 spans 建嵌套树:parent 找不到即视为根(正常只有 main_agent) */
function buildTreeData(doc: TraceDocument): TraceDataNode[] {
  const nodes = new Map<string, TraceDataNode>();
  doc.spans.forEach((span) => {
    nodes.set(span.span_id, {
      key: span.span_id,
      title: <NodeTitle span={span} />,
      span,
      children: [],
    });
  });

  const roots: TraceDataNode[] = [];
  doc.spans.forEach((span) => {
    const node = nodes.get(span.span_id);
    const parent = span.parent_span_id ? nodes.get(span.parent_span_id) : undefined;
    if (node) {
      if (parent) {
        parent.children?.push(node);
      } else {
        roots.push(node);
      }
    }
  });

  const byStart = (a: TraceDataNode, b: TraceDataNode) =>
    String(a.span.start).localeCompare(String(b.span.start));
  const sortDepth = (list: TraceDataNode[]) => {
    list.sort(byStart);
    list.forEach((node) => node.children && sortDepth(node.children as TraceDataNode[]));
  };
  sortDepth(roots);
  return roots;
}

// ---- 节点展示 ------------------------------------------------------------

function KindIcon({ span }: { span: TraceSpan }) {
  if (span.kind === "llm") {
    return <RobotOutlined aria-hidden />;
  }
  if (span.kind === "tool") {
    return <ToolOutlined aria-hidden />;
  }
  if (span.name === "main_agent") {
    return <ClusterOutlined aria-hidden />;
  }
  if (span.name.startsWith("subagent")) {
    return <CloudServerOutlined aria-hidden />;
  }
  return <ApartmentOutlined aria-hidden />;
}

/** llm 节点用真实模型名(tokens.model)替代通用的 ChatOpenAI */
function nodeName(span: TraceSpan): string {
  if (span.kind === "llm") {
    const model = toText(isRecord(span.attributes.tokens) ? span.attributes.tokens.model : null);
    if (model) {
      return model;
    }
  }
  return span.name;
}

function NodeTitle({ span }: { span: TraceSpan }) {
  const tokens = isRecord(span.attributes.tokens) ? span.attributes.tokens : {};
  const total = span.kind === "llm" ? toNumber(tokens.total) : null;
  return (
    <span className="trace-node-title">
      <span className={`trace-kind-icon trace-kind-icon--${span.kind}`}>
        <KindIcon span={span} />
      </span>
      <span className="trace-node-name">{nodeName(span)}</span>
      <Tooltip title={`status: ${span.status}`}>
        <span className={`trace-status-dot trace-status-dot--${span.status}`} aria-hidden />
      </Tooltip>
      <span className="trace-node-meta">
        {total !== null ? (
          <span className="trace-node-tokens">{formatTokens(total)} tok</span>
        ) : null}
        {span.duration_ms !== null && span.duration_ms !== undefined ? (
          <span className="trace-node-duration">{formatMs(span.duration_ms)}</span>
        ) : null}
      </span>
    </span>
  );
}

// ---- 摘要区 --------------------------------------------------------------

function BudgetChips({ budget }: { budget: Record<string, unknown> | undefined }) {
  if (!budget) {
    return null;
  }
  const items: { label: string; value: string }[] = [];
  const iterations = toNumber(budget.iterations);
  const maxIterations = toNumber(budget.max_iterations);
  if (iterations !== null && maxIterations !== null) {
    items.push({ label: "迭代", value: `${iterations}/${maxIterations}` });
  }
  const toolCalls = toNumber(budget.tool_calls);
  const maxToolCalls = toNumber(budget.max_tool_calls);
  if (toolCalls !== null && maxToolCalls !== null) {
    items.push({ label: "工具", value: `${toolCalls}/${maxToolCalls}` });
  }
  const tokens = toNumber(budget.tokens);
  const maxTokens = toNumber(budget.max_tokens);
  if (tokens !== null && maxTokens !== null) {
    items.push({ label: "token", value: `${formatTokens(tokens)}/${formatTokens(maxTokens)}` });
  }
  if (items.length === 0) {
    return null;
  }
  return (
    <div className="trace-budget-chips" aria-label="预算使用">
      {items.map((item) => (
        <span className="trace-budget-chip" key={item.label}>
          <span className="trace-budget-label">{item.label}</span>
          <strong>{item.value}</strong>
        </span>
      ))}
    </div>
  );
}

function RunSummary({ doc }: { doc: TraceDocument }) {
  const counts = useMemo(() => {
    const kinds: Record<string, number> = {};
    doc.spans.forEach((span) => {
      kinds[span.kind] = (kinds[span.kind] || 0) + 1;
    });
    return kinds;
  }, [doc]);

  const start = new Date(doc.started_at).getTime();
  const end = doc.ended_at ? new Date(doc.ended_at).getTime() : Date.now();
  const totalMs = Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : null;

  return (
    <div className="trace-summary">
      <div className="trace-summary-head">
        <div className="trace-summary-id">
          <Tag color={statusColor(doc.status)}>{doc.status}</Tag>
          <span className="trace-summary-line">
            <code title={doc.run_id}>run {doc.run_id.slice(0, 8)}</code>
            <code title={doc.thread_id}>thread {doc.thread_id.slice(0, 8)}</code>
            {totalMs !== null ? <span>{formatDuration(totalMs)}</span> : null}
          </span>
        </div>
        <BudgetChips budget={doc.budget} />
      </div>
      <div className="trace-summary-meta">
        <span>{formatDateTime(doc.started_at)}</span>
        <span className="trace-kind-counts">
          {counts.chain ? <b>{counts.chain} chain</b> : null}
          {counts.llm ? <b>{counts.llm} llm</b> : null}
          {counts.tool ? <b>{counts.tool} tool</b> : null}
          <b>{doc.spans.length} spans</b>
        </span>
      </div>
    </div>
  );
}

// ---- span 详情 -----------------------------------------------------------

function detailItems(span: TraceSpan) {
  const common: { key: string; label: string; children: ReactNode }[] = [
    { key: "kind", label: "类型", children: span.kind },
    { key: "status", label: "状态", children: span.status },
    {
      key: "duration",
      label: "耗时",
      children: span.duration_ms !== null && span.duration_ms !== undefined
        ? `${formatMs(span.duration_ms)} (${span.duration_ms.toFixed(0)} ms)`
        : "—",
    },
    { key: "start", label: "开始", children: span.start ? formatDateTime(span.start) : "—" },
    {
      key: "end",
      label: "结束",
      children: span.end ? formatDateTime(span.end) : span.status === "interrupted" ? "未结束(中断)" : "—",
    },
  ];

  if (span.kind === "llm") {
    const tokens = isRecord(span.attributes.tokens) ? span.attributes.tokens : {};
    const prompt = toNumber(tokens.prompt);
    const completion = toNumber(tokens.completion);
    const total = toNumber(tokens.total);
    const model = toText(tokens.model);
    return [
      ...common,
      ...(model ? [{ key: "model", label: "模型", children: model }] : []),
      ...(prompt !== null
        ? [{ key: "prompt", label: "Prompt tokens", children: formatTokens(prompt) }]
        : []),
      ...(completion !== null
        ? [{ key: "completion", label: "Completion tokens", children: formatTokens(completion) }]
        : []),
      ...(total !== null
        ? [{ key: "total", label: "总 tokens", children: formatTokens(total) }]
        : []),
    ];
  }

  const subagentType = toText(span.attributes.subagent_type);
  const input = toText(span.attributes.input);
  const result = toText(span.attributes.result);
  const extra: { key: string; label: string; children: ReactNode }[] = [];
  if (span.name === "task") {
    extra.push({
      key: "subagent_type",
      label: "子智能体",
      children: subagentType ?? "—",
    });
  }
  if (input) {
    extra.push({ key: "input", label: "入参", children: <code className="trace-detail-code">{input}</code> });
  }
  if (result) {
    extra.push({ key: "result", label: "结果摘要", children: <code className="trace-detail-code">{result}</code> });
  }
  return [...common, ...extra];
}

// ---- 主组件 --------------------------------------------------------------

export function TraceDrawer({ runId, open, onClose }: TraceDrawerProps) {
  const [doc, setDoc] = useState<TraceDocument | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    if (!open || !runId) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    setDoc(null);
    setSelectedKey(null);

    fetchTrace(runId)
      .then((data) => {
        if (!cancelled) {
          setDoc(data);
        }
      })
      .catch((reason) => {
        if (!cancelled) {
          setError(errorText(reason));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [open, runId, reloadTick]);

  const treeData = useMemo(() => (doc ? buildTreeData(doc) : []), [doc]);
  const selectedSpan = useMemo(
    () => (doc && selectedKey ? doc.spans.find((span) => span.span_id === selectedKey) ?? null : null),
    [doc, selectedKey]
  );

  return (
    <Drawer
      className="trace-drawer"
      destroyOnClose={false}
      onClose={onClose}
      open={open}
      size="large"
      title={
        <span className="trace-drawer-title">
          调用链 Trace
          {runId ? <code>{runId.slice(0, 8)}</code> : null}
        </span>
      }
      width={760}
      extra={
        <Button
          icon={<ReloadOutlined />}
          onClick={() => setReloadTick((value) => value + 1)}
          type="text"
        >
          刷新
        </Button>
      }
    >
      {loading ? (
        <div className="trace-loading">
          <Spin />
          <span>加载 trace…</span>
        </div>
      ) : error ? (
        <Alert
          action={
            <Button size="small" onClick={() => setReloadTick((value) => value + 1)}>
              重试
            </Button>
          }
          message={error}
          showIcon
          type="error"
        />
      ) : doc ? (
        <div className="trace-body">
          <RunSummary doc={doc} />

          <div className="trace-tree-head">
            <span>span 树</span>
          </div>
          {treeData.length === 0 ? (
            <Empty description="该 run 无 span(可能为空执行)" />
          ) : (
            <Tree
              className="trace-tree"
              defaultExpandAll
              key={doc.run_id}
              onSelect={(keys) => {
                const first = keys[0];
                setSelectedKey(first === undefined ? null : String(first));
              }}
              selectedKeys={selectedKey ? [selectedKey] : []}
              showLine={{ showLeafIcon: false }}
              treeData={treeData}
            />
          )}

          {selectedSpan ? (
            <div className="trace-detail">
              <div className="trace-detail-title">
                <span className={`trace-kind-icon trace-kind-icon--${selectedSpan.kind}`}>
                  <KindIcon span={selectedSpan} />
                </span>
                <strong>{nodeName(selectedSpan)}</strong>
                <Tag color={spanStatusColor(selectedSpan.status)}>{selectedSpan.status}</Tag>
              </div>
              <Descriptions
                bordered
                column={1}
                items={detailItems(selectedSpan)}
                size="small"
              />
            </div>
          ) : null}
        </div>
      ) : null}
    </Drawer>
  );
}
