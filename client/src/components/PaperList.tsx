import type { Paper } from "../lib/api";
import { Panel } from "./Panel";
import { MarkdownText } from "./MarkdownText";

type Props = {
  papers: Paper[];
  activePaperId?: number;
  selectedPaperIds: number[];
  batchLoading?: boolean;
  loading: boolean;
  error?: string;
  title: string;
  onSelect: (paper: Paper) => void;
  onSearch: (query: string) => void;
  onToggleSelected: (paperId: number) => void;
  onSelectAll: () => void;
  onClearSelected: () => void;
  onBatchAnalyze: () => void;
  onBatchOcr: () => void;
  onBatchFavorite: () => void;
};

export function PaperList({
  papers,
  activePaperId,
  selectedPaperIds,
  batchLoading,
  loading,
  error,
  title,
  onSelect,
  onSearch,
  onToggleSelected,
  onSelectAll,
  onClearSelected,
  onBatchAnalyze,
  onBatchOcr,
  onBatchFavorite
}: Props) {
  const selectedSet = new Set(selectedPaperIds);

  return (
    <Panel className="paper-list-panel">
      <section className="paper-list">
        <div className="list-toolbar">
          <div>
            <p className="eyebrow">Library</p>
            <h2>{title}</h2>
          </div>
          <input
            className="search-input"
            placeholder="搜索标题、摘要或总结"
            onChange={(event) => onSearch(event.target.value)}
          />
        </div>

        <div className="batch-toolbar">
          <span>已选 {selectedPaperIds.length} / {papers.length}</span>
          <button onClick={onSelectAll} disabled={papers.length === 0 || batchLoading}>全选</button>
          <button onClick={onClearSelected} disabled={selectedPaperIds.length === 0 || batchLoading}>清空</button>
          <button onClick={onBatchAnalyze} disabled={selectedPaperIds.length === 0 || batchLoading}>
            批量 AI 分析
          </button>
          <button onClick={onBatchOcr} disabled={selectedPaperIds.length === 0 || batchLoading}>
            批量提交 OCR
          </button>
          <button onClick={onBatchFavorite} disabled={selectedPaperIds.length === 0 || batchLoading}>
            批量收藏
          </button>
        </div>

        {loading && (
          <div className="skeleton-stack">
            <span />
            <span />
            <span />
          </div>
        )}
        {error && <p className="error-text">{error}</p>}
        {!loading && papers.length === 0 && (
          <div className="empty-state">
            <img src="/assets/paper-signal.svg" alt="" />
            <p>当前板块还没有论文。点击左侧抓取最新内容。</p>
          </div>
        )}

        <div className="paper-items">
          {papers.map((paper) => (
            <article
              key={paper.id}
              className={`${paper.id === activePaperId ? "paper-item active" : "paper-item"} ${selectedSet.has(paper.id) ? "selected" : ""}`}
              onClick={() => onSelect(paper)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onSelect(paper);
              }}
              role="button"
              tabIndex={0}
            >
              <label className="paper-select" onClick={(event) => event.stopPropagation()}>
                <input
                  type="checkbox"
                  checked={selectedSet.has(paper.id)}
                  onChange={() => onToggleSelected(paper.id)}
                />
                <span>选择</span>
              </label>
              <span className="paper-meta">{paper.category} · v{paper.version}</span>
              <strong>{paper.title}</strong>
              <span className="paper-date">{paper.publishedAt ? `发表 ${paper.publishedAt.slice(0, 10)}` : "暂无发表日期"}</span>
              <MarkdownText className="paper-summary markdown-text" compact>
                {paper.aiSummary || paper.abstract}
              </MarkdownText>
              <span className="tag-row">
                {paper.recommendationTags?.slice(0, 3).map((tag) => (
                  <em className="recommend-tag" key={tag}>{tag}</em>
                ))}
                {paper.tags.slice(0, 4).map((tag) => (
                  <em key={tag}>{tag}</em>
                ))}
              </span>
              {paper.recommendationReason && <span className="paper-reason">{paper.recommendationReason}</span>}
            </article>
          ))}
        </div>
      </section>
    </Panel>
  );
}
