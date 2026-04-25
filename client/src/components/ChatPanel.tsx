import { Panel } from "./Panel";
import { MarkdownText } from "./MarkdownText";
import type { ChatMessage, ChatSession, Paper, ToolCallInfo } from "../lib/api";

type Props = {
  collapsed: boolean;
  activePaper?: Paper;
  mode: "paper" | "ace";
  messages: ChatMessage[];
  toolCalls: ToolCallInfo[];
  sessions: ChatSession[];
  activeSessionId: string;
  input: string;
  selection: string;
  loading: boolean;
  onToggle: () => void;
  onMode: (mode: "paper" | "ace") => void;
  onSession: (sessionId: string) => void;
  onInput: (value: string) => void;
  onSend: () => void;
  onApproveToolCall: (toolCallId: string, approved: boolean) => void;
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
  mode,
  messages,
  toolCalls,
  sessions,
  activeSessionId,
  input,
  selection,
  loading,
  onToggle,
  onMode,
  onSession,
  onInput,
  onSend,
  onApproveToolCall
}: Props) {
  const pendingApproval = toolCalls.find((tc) => tc.summary?.startsWith("⚠️ 需要批准"));

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
              <h2>{mode === "paper" ? "Paper Chat" : "Ace Chat"}</h2>
            </div>
            <div className="mode-tabs compact">
              <button className={mode === "paper" ? "active" : ""} onClick={() => onMode("paper")}>
                Paper
              </button>
              <button className={mode === "ace" ? "active" : ""} onClick={() => onMode("ace")}>
                Ace
              </button>
            </div>
          </div>

          <div className="chat-context">
            {mode === "paper" && activePaper ? activePaper.title : "探索研究方向、检索网页并推荐数据库内论文"}
          </div>

          <div className="session-list">
            {sessions.slice(0, 8).map((session) => (
              <button
                key={session.id}
                className={session.id === activeSessionId ? "session-chip active" : "session-chip"}
                onClick={() => onSession(session.id)}
              >
                {session.title || session.scope}
              </button>
            ))}
          </div>

          <div className="message-list">
            {messages.length === 0 && <p className="muted">对论文选区提问，或让 Ace 推荐下一批论文。</p>}
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
            <textarea
              value={input}
              placeholder={mode === "paper" ? "基于全文或选区提问" : "描述你的研究兴趣"}
              onChange={(event) => onInput(event.target.value)}
              onKeyDown={handleKeyDown}
            />
            <button onClick={onSend} disabled={loading || !input.trim()}>
              发送
            </button>
          </div>
        </div>
      )}
    </Panel>
  );
}
