import { CalendarPicker } from "./CalendarPicker";
import { Panel } from "./Panel";
import type { CrawlJob, DateFilter, FavoriteFolder, OcrQuota } from "../lib/api";

export type LibraryMode = "all" | "parsed" | "favorites" | "recommendations" | "daily" | "settings";

type Props = {
  collapsed: boolean;
  categories: string[];
  activeCategory: string;
  quota?: OcrQuota;
  dateFilters: DateFilter[];
  libraryMode: LibraryMode;
  folders: FavoriteFolder[];
  activeFolderId?: number;
  newFolderName: string;
  loading: boolean;
  crawlJobs: CrawlJob[];
  onToggle: () => void;
  onCategory: (category: string) => void;
  onDateFilters: (filters: DateFilter[]) => void;
  onLibraryMode: (mode: LibraryMode) => void;
  onActiveFolder: (folderId?: number) => void;
  onNewFolderName: (value: string) => void;
  onCreateFolder: () => void;
  onCrawl: () => void;
};

export function Sidebar({
  collapsed,
  categories,
  activeCategory,
  quota,
  dateFilters,
  libraryMode,
  folders,
  activeFolderId,
  newFolderName,
  loading,
  crawlJobs,
  onToggle,
  onCategory,
  onDateFilters,
  onLibraryMode,
  onActiveFolder,
  onNewFolderName,
  onCreateFolder,
  onCrawl
}: Props) {
  const quotaRatio = quota ? Math.min(100, Math.round((quota.pagesUsed / quota.dailyLimit) * 100)) : 0;
  const parseBackendUtcTime = (value: string) => {
    const normalized = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
    return new Date(normalized);
  };
  const formatRetryTime = (value?: string) => {
    if (!value) return "";
    const date = parseBackendUtcTime(value);
    if (Number.isNaN(date.getTime())) return value.replace("T", " ").slice(0, 16);
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  };
  const formatCrawlError = (value?: string) => {
    if (!value) return "";
    return value.replace(
      /(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})/g,
      (match) => {
        const date = parseBackendUtcTime(match);
        if (Number.isNaN(date.getTime())) return match;
        return date.toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit"
        });
      }
    );
  };

  return (
    <Panel className={`sidebar ${collapsed ? "is-collapsed" : ""}`}>
      <div className="sidebar-toggle">
        <button onClick={onToggle} aria-label="折叠左侧栏目">
          {collapsed ? "→" : "←"}
        </button>
      </div>
      {!collapsed && (
        <div className="sidebar-content">
          <div className="brand-block">
            <img src="/assets/paper-signal.svg" alt="" className="brand-image" />
            <div>
              <p className="eyebrow">PaperAgent</p>
              <h1>研究流</h1>
            </div>
          </div>

          <CalendarPicker filters={dateFilters} onChange={onDateFilters} />

          <button className="primary-button" onClick={onCrawl} disabled={loading}>
            {loading ? "入队中" : dateFilters.length ? "抓取所选日期" : "手动抓取最新"}
          </button>

          {crawlJobs.length > 0 && (
            <section className="crawl-progress-box">
              <p className="section-label">抓取队列</p>
              {crawlJobs.slice(0, 3).map((job) => {
                const ratio = job.totalSteps ? Math.round((job.completedSteps / job.totalSteps) * 100) : 0;
                return (
                  <div className="crawl-job" key={job.id}>
                    <div className="crawl-job-line">
                      <strong title={`#${job.id} ${job.category}`}>#{job.id} {job.category === "all" ? "全部分类" : job.category}</strong>
                      <span>{job.status}</span>
                    </div>
                    <div className="progress-track">
                      <span style={{ width: `${ratio}%` }} />
                    </div>
                    <p>
                      {job.completedSteps}/{job.totalSteps} 步 · 新增 {job.inserted} · 更新 {job.updated}
                    </p>
                    {job.steps?.some((step) => step.nextRunAt) && (
                      <p title={job.steps.find((step) => step.nextRunAt)?.nextRunAt}>
                        等待重试 {formatRetryTime(job.steps.find((step) => step.nextRunAt)?.nextRunAt)}
                      </p>
                    )}
                    {job.errorMessage && <p className="crawl-error">{formatCrawlError(job.errorMessage)}</p>}
                  </div>
                );
              })}
            </section>
          )}

          <section className="nav-section">
            <p className="section-label">Arxiv 板块</p>
            <div className="category-list">
              {categories.map((category) => (
                <button
                  key={category}
                  className={category === activeCategory ? "category active" : "category"}
                  onClick={() => onCategory(category)}
                >
                  {category === "all" ? "全部分类" : category}
                </button>
              ))}
            </div>
          </section>

          <section className="nav-section">
            <p className="section-label">回看</p>
            <div className="category-list">
              <button className={libraryMode === "all" ? "category active" : "category"} onClick={() => onLibraryMode("all")}>
                全部论文
              </button>
              <button className={libraryMode === "parsed" ? "category active" : "category"} onClick={() => onLibraryMode("parsed")}>
                已解析
              </button>
              <button className={libraryMode === "favorites" ? "category active" : "category"} onClick={() => onLibraryMode("favorites")}>
                已收藏
              </button>
              <button className={libraryMode === "recommendations" ? "category active" : "category"} onClick={() => onLibraryMode("recommendations")}>
                推荐文章
              </button>
              <button className={libraryMode === "daily" ? "category active" : "category"} onClick={() => onLibraryMode("daily")}>
                Daily Paper
              </button>
              <button className={libraryMode === "settings" ? "category active" : "category"} onClick={() => onLibraryMode("settings")}>
                设置
              </button>
            </div>
          </section>

          {libraryMode === "favorites" && (
            <section className="favorite-box">
              <div className="folder-row">
                <select value={activeFolderId || ""} onChange={(event) => onActiveFolder(event.target.value ? Number(event.target.value) : undefined)}>
                  <option value="">全部收藏</option>
                  {folders.map((folder) => (
                    <option key={folder.id} value={folder.id}>{folder.name}</option>
                  ))}
                </select>
              </div>
              <div className="folder-row">
                <input value={newFolderName} placeholder="新建收藏夹" onChange={(event) => onNewFolderName(event.target.value)} />
                <button onClick={onCreateFolder}>新建</button>
              </div>
            </section>
          )}

          <section className="quota-box">
            <div className="quota-line">
              <span>PaddleOCR 今日额度</span>
              <strong>{quota ? `${quota.pagesUsed}/${quota.dailyLimit}` : "读取中"}</strong>
            </div>
            <div className="quota-track">
              <span style={{ width: `${quotaRatio}%` }} />
            </div>
            <p>默认按约 10 页切分处理，减少超时并保持合并顺序。</p>
          </section>
        </div>
      )}
    </Panel>
  );
}
