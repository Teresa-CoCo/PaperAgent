import { useEffect, useRef } from "react";
import { Panel } from "./Panel";
import { MarkdownText } from "./MarkdownText";
import type { AgentActivity, AgentInfo, ChatMessage, Paper, ToolCallInfo } from "../lib/api";

type Props = {
  collapsed: boolean;
  activePaper?: Paper;
  messages: ChatMessage[];
  agents: AgentInfo[];
  agentActivities: AgentActivity[];
  toolCalls: ToolCallInfo[];
  input: string;
  selection: string;
  loading: boolean;
  attachments: Paper[];
  mentionResults: Paper[];
  mentionOpen: boolean;
  onToggle: () => void;
  onHistory: () => void;
  onInput: (value: string) => void;
  onSend: () => void;
  onSubmitMission: () => void;
  onApproveToolCall: (toolCallId: string, approved: boolean) => void;
  onAttachPaper: (paper: Paper) => void;
  onRemoveAttachment: (paperId: number) => void;
};

function ToolCallCard({ toolCall }: { toolCall: ToolCallInfo }) {
  let statusLabel = "";
  let statusClass = "";
  switch (toolCall.status) {
    case "running":
      statusLabel = "运行中...";
      statusClass = "running";
      break;
    case "success":
      statusLabel = "完成";
      statusClass = "success";
      break;
    case "error":
      statusLabel = "失败";
      statusClass = "error";
      break;
    case "denied":
      statusLabel = "已拒绝";
      statusClass = "denied";
      break;
  }

  let argsDisplay = "";
  try {
    const parsed = JSON.parse(toolCall.arguments);
    argsDisplay = parsed.command || parsed.query || JSON.stringify(parsed).slice(0, 120);
  } catch {
    argsDisplay = toolCall.arguments.slice(0, 120);
  }

  return (
    <div className={`tool-call-card ${statusClass}`}>
      <div className="tool-call-header">
        <span className="tool-call-icon">
          {toolCall.status === "running" ? "⚡" : toolCall.status === "success" ? "✓" : toolCall.status === "denied" ? "✗" : "⚠"}
        </span>
        <span className="tool-call-name">{toolCall.name}</span>
        <span className={`tool-call-status ${statusClass}`}>{statusLabel}</span>
      </div>
      <div className="tool-call-args">{argsDisplay}</div>
      {toolCall.summary && <div className="tool-call-summary">{toolCall.summary}</div>}
    </div>
  );
}

function AgentDock({ agents, activities }: { agents: AgentInfo[]; activities: AgentActivity[] }) {
  const activityByKey = new Map(activities.map((activity) => [activity.agentKey, activity]));
  return (
    <div className="agent-dock" aria-label="Paper Ace Paper agents">
      {agents.map((agent) => {
        const activity = activityByKey.get(agent.key);
        return (
          <div key={agent.key} className={`agent-pill ${activity?.status || "idle"}`} title={agent.purpose}>
            <span className="agent-dot" />
            <span>{agent.name.replace(" Agent", "")}</span>
            {activity && <small>{activity.status === "running" ? "工作中" : "已核验"}</small>}
          </div>
        );
      })}
    </div>
  );
}

function ApprovalDialog({ toolCall, onApprove, onDeny }: {
  toolCall: ToolCallInfo;
  onApprove: () => void;
  onDeny: () => void;
}) {
  return (
    <div className="approval-overlay">
      <div className="approval-dialog">
        <div className="approval-header">⚠️ 需要批准</div>
        <div className="approval-command-label">Shell 命令需要您的批准才能执行：</div>
        <pre className="approval-command">{toolCall.summary?.replace("⚠️ 需要批准: ", "") || ""}</pre>
        <div className="approval-actions">
          <button className="approval-deny" onClick={onDeny}>拒绝</button>
          <button className="approval-approve" onClick={onApprove}>批准执行</button>
        </div>
      </div>
    </div>
  );
}

export function ChatPanel({
  collapsed,
  activePaper,
  messages,
  agents,
  agentActivities,
  toolCalls,
  input,
  selection,
  loading,
  attachments,
  mentionResults,
  mentionOpen,
  onToggle,
  onHistory,
  onInput,
  onSend,
  onSubmitMission,
  onApproveToolCall,
  onAttachPaper,
  onRemoveAttachment
}: Props) {
  const pendingApproval = toolCalls.find((tc) => tc.summary?.startsWith("⚠️ 需要批准"));
  const messageListRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = messageListRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
  }, [messages, toolCalls, agentActivities, loading]);

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSend();
    }
  };

  return (
    <Panel className={`chat-panel ${collapsed ? "is-collapsed" : ""}`}>
      {pendingApproval && (
        <ApprovalDialog
          toolCall={pendingApproval}
          onApprove={() => onApproveToolCall(pendingApproval.toolCallId, true)}
          onDeny={() => onApproveToolCall(pendingApproval.toolCallId, false)}
        />
      )}
      <div className="chat-toggle">
        <button onClick={onToggle} aria-label="折叠右侧聊天">
          {collapsed ? "←" : "→"}
        </button>
      </div>
      {!collapsed && (
        <div className="chat-content">
          <div className="chat-head">
            <div>
              <p className="eyebrow">Chat</p>
              <h2>Paper Ace Paper</h2>
            </div>
            <div className="chat-head-actions">
              <button type="button" className="history-open-button" onClick={onHistory}>
                历史
              </button>
            </div>
          </div>

          <div className="chat-context">
            {activePaper ? activePaper.title : "统一入口：论文 RAG、数据库检索、arXiv/Web 搜索、推荐与核验"}
          </div>

          <AgentDock agents={agents} activities={agentActivities} />

          <div className="message-list" ref={messageListRef}>
            {messages.length === 0 && <p className="muted">对论文、选区、研究方向或最新论文提问；系统会显示正在使用的 Agent 和工具。</p>}
            {messages.map((message, index) => (
              <div key={message.id}>
                <div className={`message ${message.role}`}>
                  <span>{message.role === "user" ? "你" : "Agent"}</span>
                  <MarkdownText className="markdown-text message-markdown">
                    {message.content}
                  </MarkdownText>
                </div>
                {/* Show tool calls after the last assistant message */}
                {message.role === "assistant" && index === messages.length - 1 && toolCalls.length > 0 && (
                  <div className="tool-calls-group">
                    {toolCalls.map((tc) => (
                      <ToolCallCard key={tc.toolCallId} toolCall={tc} />
                    ))}
                  </div>
                )}
              </div>
            ))}
            {loading && !toolCalls.length && (
              <div className="message assistant"><span>Agent</span><p>生成中...</p></div>
            )}
          </div>

          {selection && <div className="selected-snippet">{selection.slice(0, 180)}</div>}

          <div className="composer">
            {attachments.length > 0 && (
              <div className="chat-attachments">
                {attachments.map((paper) => (
                  <button
                    key={paper.id}
                    type="button"
                    className="attachment-chip"
                    onClick={() => onRemoveAttachment(paper.id)}
                    title={paper.title}
                  >
                    @{paper.arxivId || paper.title}
                  </button>
                ))}
              </div>
            )}
            <textarea
              value={input}
              placeholder="询问论文、方向、推荐或最新进展；输入 @论文/作者/arXiv 编号附加参考"
              onChange={(event) => onInput(event.target.value)}
              onKeyDown={handleKeyDown}
            />
            {mentionOpen && mentionResults.length > 0 && (
              <div className="mention-menu">
                {mentionResults.slice(0, 6).map((paper) => (
                  <button
                    key={paper.id}
                    type="button"
                    className="mention-option"
                    onClick={() => onAttachPaper(paper)}
                  >
                    <span className="mention-title">{paper.title}</span>
                    <span className="mention-meta">
                      {paper.arxivId}
                      {paper.authors?.length ? ` · ${paper.authors.slice(0, 2).join(", ")}` : ""}
                    </span>
                  </button>
                ))}
              </div>
            )}
            <div className="composer-actions">
              <button
                type="button"
                className="secondary-send-button"
                onClick={onSubmitMission}
                disabled={loading || !input.trim()}
              >
                后台
              </button>
              <button onClick={onSend} disabled={loading || !input.trim()}>
                发送
              </button>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
