import { useEffect, useState } from "react";
import { GlassPanel } from "./GlassPanel";
import { api, type Paper, type UserSettings } from "../lib/api";

type Props = {
  activePaper?: Paper;
  onPaperDeleted: () => void;
  onDatabaseChanged?: () => void;
};

export function SettingsPanel({ activePaper, onPaperDeleted, onDatabaseChanged }: Props) {
  const [settings, setSettings] = useState<UserSettings | undefined>();
  const [preferenceText, setPreferenceText] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    api.settings().then((data) => {
      setSettings(data);
      setPreferenceText(data.preferenceText);
    }).catch((reason) => setMessage(reason.message));
  }, []);

  async function savePreference() {
    setLoading(true);
    setMessage("");
    try {
      const data = await api.updatePreferenceText(preferenceText);
      setSettings(data);
      setMessage("偏好已保存");
    } catch (reason) {
      setMessage((reason as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function clearChatMemory() {
    setLoading(true);
    setMessage("");
    try {
      const result = await api.clearChatMemory();
      setMessage(`已清理 ${result.deletedSessions} 个对话`);
      const data = await api.settings();
      setSettings(data);
    } catch (reason) {
      setMessage((reason as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function deleteActivePaper() {
    if (!activePaper) return;
    setLoading(true);
    setMessage("");
    try {
      await api.deletePaper(activePaper.id);
      setMessage("当前论文已从数据库删除");
      onPaperDeleted();
      const data = await api.settings();
      setSettings(data);
    } catch (reason) {
      setMessage((reason as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function deleteUnfavoritedPapers() {
    const confirmed = window.confirm("将删除当前用户未收藏的所有论文。已收藏论文会保留。继续吗？");
    if (!confirmed) return;
    setLoading(true);
    setMessage("");
    try {
      const result = await api.deleteUnfavoritedPapers();
      setMessage(`已删除 ${result.deletedPapers} 篇未收藏论文`);
      onPaperDeleted();
      onDatabaseChanged?.();
      const data = await api.settings();
      setSettings(data);
    } catch (reason) {
      setMessage((reason as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <GlassPanel className="panel settings-panel">
      <header className="settings-header">
        <div>
          <p className="eyebrow">Settings</p>
          <h2>记忆与数据库</h2>
        </div>
      </header>

      <div className="settings-body">
        <section className="settings-section">
          <h3>研究偏好</h3>
          <p>用自然语言描述你的研究兴趣。推荐文章会把数据库内论文摘要交给 AI，并按这段偏好生成推荐理由。</p>
          <textarea
            value={preferenceText}
            onChange={(event) => setPreferenceText(event.target.value)}
            placeholder="例如：我关注推理增强、RAG Agent、数学评测和低成本推理时扩展方法。"
          />
          <button onClick={savePreference} disabled={loading}>保存偏好</button>
        </section>

        <section className="settings-section">
          <h3>数据库</h3>
          <div className="stats-grid">
            <span>论文 <strong>{settings?.stats.papers ?? "-"}</strong></span>
            <span>已解析 <strong>{settings?.stats.parsedPapers ?? "-"}</strong></span>
            <span>收藏 <strong>{settings?.stats.favorites ?? "-"}</strong></span>
            <span>消息 <strong>{settings?.stats.chatMessages ?? "-"}</strong></span>
          </div>
          <button onClick={deleteActivePaper} disabled={loading || !activePaper}>删除当前论文</button>
          <button className="danger-button" onClick={deleteUnfavoritedPapers} disabled={loading}>
            删除未收藏论文
          </button>
        </section>

        <section className="settings-section">
          <h3>短期记忆</h3>
          <p>清理所有聊天 session 和消息，不会删除论文、OCR 文件或收藏夹。</p>
          <button onClick={clearChatMemory} disabled={loading}>清理聊天记录</button>
        </section>

        {message && <p className="status-pill">{message}</p>}
      </div>
    </GlassPanel>
  );
}
