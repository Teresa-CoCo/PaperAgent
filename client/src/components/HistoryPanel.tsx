import type { ChatSession } from "../lib/api";

type Props = {
  open: boolean;
  sessions: ChatSession[];
  activeSessionId: string;
  onClose: () => void;
  onSelect: (session: ChatSession) => void;
  onNewSession: () => void;
  onDeleteSession: (session: ChatSession) => void;
};

function formatDate(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.replace("T", " ").slice(0, 16);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function missionLabel(session: ChatSession) {
  const mission = session.latestMission;
  if (!mission) return "";
  if (mission.status === "queued") return "排队中";
  if (mission.status === "running") return "后台执行中";
  if (mission.status === "done") return "后台完成";
  return "后台失败";
}

export function HistoryPanel({ open, sessions, activeSessionId, onClose, onSelect, onNewSession, onDeleteSession }: Props) {
  return (
    <aside className={`history-panel ${open ? "is-open" : ""}`} aria-hidden={!open}>
      <div className="history-head">
        <div>
          <p className="eyebrow">History</p>
          <h2>会话历史</h2>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭历史">×</button>
      </div>

      <button type="button" className="history-new-button" onClick={onNewSession}>
        新会话
      </button>

      <div className="history-list">
        {sessions.length === 0 && <p className="muted">还没有会话。</p>}
        {sessions.map((session) => {
          const status = missionLabel(session);
          return (
            <div
              key={session.id}
              role="button"
              tabIndex={0}
              className={session.id === activeSessionId ? "history-item active" : "history-item"}
              onClick={() => onSelect(session)}
              onKeyDown={(event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                onSelect(session);
              }}
            >
              <span className="history-row">
                <strong>{session.title || (session.scope === "paper" ? "Paper Chat" : "Ace Chat")}</strong>
                <span>{formatDate(session.updatedAt || session.createdAt)}</span>
              </span>
              <span className="history-meta">
                {session.scope === "paper" ? "Paper" : "Ace"}
                {status ? ` · ${status}` : ""}
              </span>
              <span className="history-preview">
                {session.latestMission?.status === "failed"
                  ? session.latestMission.errorMessage || "后台任务失败"
                  : session.preview || session.latestMission?.message || "无消息"}
              </span>
              <span
                role="button"
                tabIndex={0}
                className="history-item-delete"
                onClick={(event) => {
                  event.stopPropagation();
                  onDeleteSession(session);
                }}
                onKeyDown={(event) => {
                  if (event.key !== "Enter" && event.key !== " ") return;
                  event.preventDefault();
                  event.stopPropagation();
                  onDeleteSession(session);
                }}
              >
                删除
              </span>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
