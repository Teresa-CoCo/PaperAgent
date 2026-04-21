import { useState } from "react";
import { API_BASE_URL, type Paper } from "../lib/api";
import { GlassPanel } from "./GlassPanel";
import { MarkdownText } from "./MarkdownText";

type Props = {
  paper?: Paper;
  viewMode: "summary" | "markdown" | "pdf";
  selection: string;
  actionLoading: boolean;
  ocrLoading: boolean;
  ocrStatus: string;
  translation?: string;
  onViewMode: (mode: "summary" | "markdown" | "pdf") => void;
  onAnalyze: () => void;
  onOcr: () => void;
  onFavorite: () => void;
  onSelection: (text: string) => void;
  onTranslate: () => void;
};

export function PaperViewer({
  paper,
  viewMode,
  selection,
  actionLoading,
  ocrLoading,
  ocrStatus,
  translation,
  onViewMode,
  onAnalyze,
  onOcr,
  onFavorite,
  onSelection,
  onTranslate
}: Props) {
  const [markdownMode, setMarkdownMode] = useState<"preview" | "source">("preview");

  if (!paper) {
    return (
      <GlassPanel className="panel viewer empty-viewer">
        <img src="/assets/paper-signal.svg" alt="" />
        <h2>选择一篇论文开始阅读</h2>
        <p>摘要、Markdown、PDF 与划线问答会在这里展开。</p>
      </GlassPanel>
    );
  }

  const readSelection = () => {
    const text = window.getSelection()?.toString().trim() || "";
    onSelection(text);
  };

  const resolveMarkdownAsset = (src?: string) => {
    if (!src) return "";
    if (/^(https?:|data:|blob:|#|\/)/i.test(src)) return src;
    if (!paper.assetBasePath) return src;
    return `${API_BASE_URL}${paper.assetBasePath}/${src.replace(/^\.\//, "")}`;
  };

  return (
    <GlassPanel className="panel viewer">
      <header className="viewer-header">
        <div>
          <p className="eyebrow">{paper.arxivId} · {paper.category}</p>
          <h2>{paper.title}</h2>
          <p>{paper.authors.slice(0, 6).join(", ")}</p>
        </div>
        <div className="mode-tabs" role="tablist" aria-label="论文视图切换">
          {(["summary", "markdown", "pdf"] as const).map((mode) => (
            <button key={mode} className={viewMode === mode ? "active" : ""} onClick={() => onViewMode(mode)}>
              {mode === "summary" ? "总结" : mode === "markdown" ? "Markdown" : "PDF"}
            </button>
          ))}
        </div>
      </header>

      {viewMode === "summary" && (
        <article className="reader summary-reader" onMouseUp={readSelection}>
          <div className="summary-actions">
            <a href={paper.absUrl} target="_blank" rel="noreferrer">Arxiv 页面</a>
            <button onClick={onAnalyze} disabled={actionLoading}>
              {actionLoading ? "分析中" : "AI 分析"}
            </button>
            <button onClick={onOcr} disabled={ocrLoading}>
              {ocrLoading ? "OCR 中" : paper.markdownPath ? "重新 OCR" : "提交 OCR"}
            </button>
            <button onClick={onFavorite}>收藏</button>
          </div>
          {ocrStatus && <p className="status-pill">{ocrStatus}</p>}
          <h3>AI 总结</h3>
          <MarkdownText className="markdown-text summary-markdown">
            {paper.aiSummary || "还没有 AI 总结。点击 AI 分析后会写入长期数据库。"}
          </MarkdownText>
          <h3>摘要</h3>
          <MarkdownText className="markdown-text summary-markdown">{paper.abstract}</MarkdownText>
          <div className="tag-row">
            {paper.tags.map((tag) => (
              <em key={tag}>{tag}</em>
            ))}
          </div>
        </article>
      )}

      {viewMode === "markdown" && (
        <article className="reader markdown-reader" onMouseUp={readSelection}>
          {paper.markdown ? (
            <>
              <div className="markdown-toolbar">
                <div className="mode-tabs compact">
                  <button
                    className={markdownMode === "preview" ? "active" : ""}
                    onClick={() => setMarkdownMode("preview")}
                  >
                    预览
                  </button>
                  <button
                    className={markdownMode === "source" ? "active" : ""}
                    onClick={() => setMarkdownMode("source")}
                  >
                    源码
                  </button>
                </div>
              </div>
              {markdownMode === "preview" ? (
                <MarkdownText className="markdown-preview" allowRawHtml resolveImage={resolveMarkdownAsset}>
                  {paper.markdown}
                </MarkdownText>
              ) : (
                <pre>{paper.markdown}</pre>
              )}
            </>
          ) : (
            <div className="markdown-empty">
              <p>Markdown 尚未解析。点击提交 OCR 后会自动提交 PaddleOCR 任务并轮询结果。</p>
              <button onClick={onOcr} disabled={ocrLoading}>{ocrLoading ? "OCR 中" : "提交 OCR"}</button>
              {ocrStatus && <span>{ocrStatus}</span>}
            </div>
          )}
        </article>
      )}

      {viewMode === "pdf" && (
        <div className="pdf-frame">
          <iframe title={paper.title} src={paper.pdfUrl} />
        </div>
      )}

      <footer className="selection-bar">
        <span>{selection ? `已选择 ${selection.length} 个字符` : "选中 Markdown 或摘要文本后可翻译、问答"}</span>
        <button onClick={onTranslate} disabled={!selection || actionLoading}>
          翻译选区
        </button>
      </footer>
      {translation && (
        <MarkdownText className="translation-box markdown-text">
          {translation}
        </MarkdownText>
      )}
    </GlassPanel>
  );
}
