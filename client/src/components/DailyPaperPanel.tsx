import { useMemo, useState } from "react";
import { GlassPanel } from "./GlassPanel";
import { MarkdownText } from "./MarkdownText";
import type { DailyPaperEntry, DailyPaperRun } from "../lib/api";

type Props = {
  categories: string[];
  selectedCategories: string[];
  targetDate: string;
  maxResults: number;
  entries: DailyPaperEntry[];
  runs: DailyPaperRun[];
  loading: boolean;
  generating: boolean;
  error?: string;
  onCategories: (categories: string[]) => void;
  onTargetDate: (value: string) => void;
  onMaxResults: (value: number) => void;
  onGenerate: () => void;
  onStopRun: (runId: number) => void;
};

export function DailyPaperPanel({
  categories,
  selectedCategories,
  targetDate,
  maxResults,
  entries,
  runs,
  loading,
  generating,
  error,
  onCategories,
  onTargetDate,
  onMaxResults,
  onGenerate,
  onStopRun
}: Props) {
  const [expandedIds, setExpandedIds] = useState<number[]>([]);
  const selectedSet = useMemo(() => new Set(selectedCategories), [selectedCategories]);

  function toggleCategory(category: string) {
    if (selectedSet.has(category)) {
      onCategories(selectedCategories.filter((item) => item !== category));
      return;
    }
    onCategories([...selectedCategories, category]);
  }

  function toggleExpanded(id: number) {
    setExpandedIds((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  return (
    <GlassPanel className="panel daily-paper-panel">
      <div className="daily-paper-shell">
        <header className="daily-paper-header">
          <div>
            <p className="eyebrow">Daily Paper</p>
            <h2>前一日论文日刊</h2>
            <p>抓取指定领域上一天论文，转 Markdown，写入 RAG，并生成缩略版与详细版总结。</p>
          </div>
          <button className="primary-button" onClick={onGenerate} disabled={generating || selectedCategories.length === 0}>
            {generating ? "生成中" : "生成 Daily Paper"}
          </button>
        </header>

        <section className="daily-paper-controls">
          <label className="daily-control">
            <span>目标日期</span>
            <input type="date" value={targetDate} onChange={(event) => onTargetDate(event.target.value)} />
          </label>
          <label className="daily-control">
            <span>每个领域最多抓取</span>
            <input
              type="number"
              min={1}
              max={2000}
              value={maxResults}
              onChange={(event) => onMaxResults(Number(event.target.value) || 12)}
            />
          </label>
        </section>

        <section className="daily-paper-controls">
          <div className="daily-fieldset">
            <span>领域多选</span>
            <div className="category-list">
              {categories.filter((item) => item !== "all").map((category) => (
                <button
                  key={category}
                  className={selectedSet.has(category) ? "category active" : "category"}
                  onClick={() => toggleCategory(category)}
                >
                  {category}
                </button>
              ))}
            </div>
          </div>
        </section>

        {runs.length > 0 && (
          <section className="daily-run-strip">
            {runs.slice(0, 4).map((run) => {
              const ratio = run.totalPapers ? Math.round((run.completedPapers / run.totalPapers) * 100) : 0;
              return (
                <article className="daily-run-card" key={run.id}>
                  <div className="crawl-job-line">
                    <strong>#{run.id} · {run.targetDate}</strong>
                    <span>{run.status}</span>
                  </div>
                  <p>{run.categories.join(", ")}</p>
                  <div className="quota-track">
                    <span style={{ width: `${ratio}%` }} />
                  </div>
                  <p>{run.completedPapers}/{run.totalPapers} 篇 · 新增 {run.inserted} · 更新 {run.updated}</p>
                  {(run.status === "queued" || run.status === "running") && (
                    <button className="run-stop-button" onClick={() => onStopRun(run.id)}>
                      停止任务
                    </button>
                  )}
                  {run.errorMessage && <p className="crawl-error">{run.errorMessage}</p>}
                </article>
              );
            })}
          </section>
        )}

        {loading && (
          <div className="skeleton-stack">
            <span />
            <span />
            <span />
          </div>
        )}
        {error && <p className="error-text">{error}</p>}
        {!loading && entries.length === 0 && (
          <div className="empty-state">
            <p>当前日期和领域还没有 Daily Paper。选择领域后点击上方按钮生成。</p>
          </div>
        )}

        <section className="daily-paper-list">
          {entries.map((entry) => {
            const expanded = expandedIds.includes(entry.id);
            return (
              <article className="daily-paper-card" key={entry.id}>
                <div className="daily-paper-card-head">
                  <div>
                    <span className="paper-meta">{entry.targetDate} · {entry.category} · {entry.arxivId}</span>
                    <h3>{entry.title}</h3>
                    <p className="paper-date">
                      {entry.authors.slice(0, 6).join(", ")}
                    </p>
                  </div>
                  <div className="daily-paper-links">
                    <a href={entry.absUrl} target="_blank" rel="noreferrer">Arxiv</a>
                    <a href={entry.pdfUrl} target="_blank" rel="noreferrer">PDF</a>
                  </div>
                </div>

                <div className="tag-row">
                  <em>{entry.status}</em>
                  <em>{entry.ragDocumentCount} chunks</em>
                  {entry.tags.slice(0, 4).map((tag) => (
                    <em key={tag}>{tag}</em>
                  ))}
                </div>
                {entry.errorMessage && <p className="crawl-error">{entry.errorMessage}</p>}

                <section className="daily-summary-block">
                  <div className="daily-summary-head">
                    <strong>缩略版</strong>
                    <button onClick={() => toggleExpanded(entry.id)}>
                      {expanded ? "收起详细版" : "展开详细版"}
                    </button>
                  </div>
                  <MarkdownText className="markdown-text">
                    {entry.shortSummary || entry.abstract}
                  </MarkdownText>
                </section>

                {expanded && (
                  <section className="daily-summary-block detailed">
                    <div className="daily-summary-head">
                      <strong>详细版</strong>
                    </div>
                    <MarkdownText className="markdown-text">
                      {entry.longSummary || entry.abstract}
                    </MarkdownText>
                  </section>
                )}
              </article>
            );
          })}
        </section>
      </div>
    </GlassPanel>
  );
}
