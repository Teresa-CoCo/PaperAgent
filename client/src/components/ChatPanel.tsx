import { GlassPanel } from "./GlassPanel";
import { MarkdownText } from "./MarkdownText";
import type { ChatMessage, ChatSession, Paper } from "../lib/api";

type Props = {
  collapsed: boolean;
  activePaper?: Paper;
  mode: "paper" | "ace";
  messages: ChatMessage[];
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
};

export function ChatPanel({
  collapsed,
  activePaper,
  mode,
  messages,
  sessions,
  activeSessionId,
  input,
  selection,
  loading,
  onToggle,
  onMode,
  onSession,
  onInput,
  onSend
}: Props) {
  return (
    <GlassPanel className={`panel chat-panel ${collapsed ? "is-collapsed" : ""}`}>
      <button className="icon-button" onClick={onToggle} aria-label="折叠右侧聊天">
        {collapsed ? "<" : ">"}
      </button>
      {!collapsed && (
        <>
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
            {messages.map((message) => (
              <div key={message.id} className={`message ${message.role}`}>
                <span>{message.role === "user" ? "你" : "Agent"}</span>
                <MarkdownText className="markdown-text message-markdown">
                  {message.content}
                </MarkdownText>
              </div>
            ))}
            {loading && messages[messages.length - 1]?.role !== "assistant" && (
              <div className="message assistant"><span>Agent</span><p>生成中...</p></div>
            )}
          </div>

          {selection && <div className="selected-snippet">{selection.slice(0, 180)}</div>}

          <div className="composer">
            <textarea
              value={input}
              placeholder={mode === "paper" ? "基于全文或选区提问" : "描述你的研究兴趣"}
              onChange={(event) => onInput(event.target.value)}
            />
            <button onClick={onSend} disabled={loading || !input.trim()}>
              发送
            </button>
          </div>
        </>
      )}
    </GlassPanel>
  );
}
