export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const USER_ID = import.meta.env.VITE_USER_ID || "local-user";

export type StreamEvent =
  | { type: "text"; content: string }
  | { type: "tool_start"; toolCallId: string; name: string; arguments: string }
  | { type: "tool_result"; toolCallId: string; name: string; summary: string }
  | { type: "approval"; toolCallId: string; command: string; reason: string }
  | { type: "done" }
  | { type: "error"; message: string };

export type ToolCallInfo = {
  toolCallId: string;
  name: string;
  arguments: string;
  status: "running" | "success" | "error" | "denied";
  summary?: string;
};

export type Paper = {
  id: number;
  arxivId: string;
  version: number;
  title: string;
  authors: string[];
  abstract: string;
  aiSummary: string;
  category: string;
  tags: string[];
  pdfUrl: string;
  absUrl: string;
  markdownPath?: string;
  markdown?: string;
  assetBasePath?: string;
  publishedAt?: string;
  updatedAt?: string;
  analyzedAt?: string;
  recommendationReason?: string;
  recommendationTags?: string[];
};

export type OcrQuota = {
  date: string;
  pagesUsed: number;
  dailyLimit: number;
};

export type ChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  selection?: string;
  createdAt: string;
};

export type ChatSession = {
  id: string;
  scope: "paper" | "ace";
  paperId?: number;
  title: string;
  updatedAt: string;
};

export type UserSettings = {
  userId: string;
  preferenceText: string;
  keywords: string[];
  homeCategories: string[];
  stats: {
    papers: number;
    parsedPapers: number;
    chatMessages: number;
    favorites: number;
  };
};

export type DateFilter = {
  start: string;
  end?: string;
};

export type OcrJob = {
  jobId?: string;
  status: "pending" | "running" | "done" | "failed";
  markdownPath?: string;
  pagesTotal?: number;
  pagesExtracted?: number;
};

export type FavoriteFolder = {
  id: number;
  name: string;
  createdAt: string;
};

export type CrawlStep = {
  id: number;
  category: string;
  targetDate?: string;
  source: "arxiv_new" | "arxiv_api";
  status: "queued" | "running" | "done" | "failed";
  fetched: number;
  inserted: number;
  updated: number;
  errorMessage?: string;
  attemptCount?: number;
  nextRunAt?: string;
  startedAt?: string;
  finishedAt?: string;
};

export type CrawlJob = {
  id: number;
  category: string;
  dateFilters: DateFilter[];
  maxResults: number;
  status: "queued" | "running" | "done" | "partial" | "failed";
  totalSteps: number;
  completedSteps: number;
  fetched: number;
  inserted: number;
  updated: number;
  errorMessage?: string;
  createdAt: string;
  startedAt?: string;
  updatedAt: string;
  finishedAt?: string;
  steps?: CrawlStep[];
};

export type DailyPaperRun = {
  id: number;
  targetDate: string;
  categories: string[];
  maxResults: number;
  status: "queued" | "running" | "done" | "partial" | "failed" | "cancelled";
  totalPapers: number;
  completedPapers: number;
  inserted: number;
  updated: number;
  errorMessage?: string;
  createdAt: string;
  startedAt?: string;
  updatedAt: string;
  finishedAt?: string;
};

export type DailyPaperEntry = {
  id: number;
  runId?: number;
  paperId: number;
  targetDate: string;
  category: string;
  title: string;
  authors: string[];
  abstract: string;
  shortSummary: string;
  longSummary: string;
  status: "queued" | "ready" | "failed";
  errorMessage?: string;
  markdownPath?: string;
  ragCollection?: string;
  ragDocumentCount: number;
  arxivId: string;
  version: number;
  pdfUrl: string;
  absUrl: string;
  publishedAt?: string;
  updatedAt?: string;
  tags: string[];
};

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": USER_ID,
      ...(init.headers || {})
    }
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data?.error?.message || `Request failed: ${response.status}`);
  }
  return data as T;
}

export const api = {
  config: () => request<{ categories: string[]; ocrDailyPageLimit: number; ocrChunkPages: number }>("/api/config"),
  quota: () => request<OcrQuota>("/api/quota/ocr"),
  listPapers: (category?: string, query?: string, options: { parsed?: boolean; dateFilters?: DateFilter[] } = {}) => {
    const params = new URLSearchParams();
    if (category && category !== "all") params.set("category", category);
    if (query) params.set("query", query);
    if (options.parsed !== undefined) params.set("parsed", String(options.parsed));
    if (options.dateFilters?.length) {
      params.set(
        "dates",
        options.dateFilters.map((item) => item.end ? `${item.start}..${item.end}` : item.start).join(",")
      );
    }
    return request<{ items: Paper[] }>(`/api/papers?${params.toString()}`);
  },
  getPaper: (id: number) => request<Paper>(`/api/papers/${id}`),
  deletePaper: (id: number) =>
    request<{ deleted: number }>(`/api/papers/${id}`, {
      method: "DELETE"
    }),
  crawl: (category: string, maxResults = 20, dateFilters: DateFilter[] = []) =>
    request<CrawlJob>("/api/papers/crawl", {
      method: "POST",
      body: JSON.stringify({ category, maxResults, dateFilters })
    }),
  crawlJobs: () => request<{ items: CrawlJob[] }>("/api/papers/crawl/jobs"),
  crawlJob: (id: number) => request<CrawlJob>(`/api/papers/crawl/jobs/${id}`),
  dailyPapers: (targetDate?: string, categories: string[] = []) => {
    const params = new URLSearchParams();
    if (targetDate) params.set("targetDate", targetDate);
    if (categories.length) params.set("categories", categories.join(","));
    return request<{ items: DailyPaperEntry[] }>(`/api/daily-papers?${params.toString()}`);
  },
  dailyPaperRuns: () => request<{ items: DailyPaperRun[] }>("/api/daily-papers/runs"),
  dailyPaperRun: (id: number) => request<DailyPaperRun>(`/api/daily-papers/runs/${id}`),
  cancelDailyPaperRun: (id: number) =>
    request<DailyPaperRun>(`/api/daily-papers/runs/${id}/cancel`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  generateDailyPaper: (payload: { categories: string[]; targetDate: string; maxResults?: number }) =>
    request<DailyPaperRun>("/api/daily-papers/runs", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  submitOcr: (paperId: number) =>
    request<OcrJob>(`/api/papers/${paperId}/ocr`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  pollOcr: (paperId: number, jobId: string) =>
    request<OcrJob>(`/api/papers/${paperId}/ocr/${jobId}/poll`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  analyze: (paperId: number) =>
    request<{ paperId: number; analysis: Record<string, unknown> }>(`/api/papers/${paperId}/analyze`, {
      method: "POST",
      body: JSON.stringify({ force: true })
    }),
  translate: (paperId: number, selection: string, context: string) =>
    request<{ translation: string }>(`/api/papers/${paperId}/translate`, {
      method: "POST",
      body: JSON.stringify({ selection, context })
    }),
  createSession: (scope: "paper" | "ace", paperId?: number, title = "") =>
    request<{ id: string }>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ scope, paperId, title })
    }),
  listSessions: () => request<{ items: ChatSession[] }>("/api/chat/sessions"),
  listMessages: (sessionId: string) => request<{ items: ChatMessage[] }>(`/api/chat/sessions/${sessionId}/messages`),
  streamMessage: async (
    sessionId: string,
    payload: { message: string; paperId?: number; selection?: string; attachmentPaperIds?: number[]; mode: "paper" | "ace" },
    onEvent: (event: StreamEvent) => void
  ) => {
    const response = await fetch(`${API_BASE_URL}/api/chat/sessions/${sessionId}/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-Id": USER_ID
      },
      body: JSON.stringify(payload)
    });
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data?.error?.message || `Request failed: ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        const remaining = buffer.trim();
        if (remaining) onEvent({ type: "text", content: remaining });
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          const event = JSON.parse(trimmed) as StreamEvent;
          onEvent(event);
        } catch {
          onEvent({ type: "text", content: trimmed });
        }
      }
    }
  },
  approveToolCall: (sessionId: string, toolCallId: string, approved: boolean) =>
    request<{ status: string }>(`/api/chat/sessions/${sessionId}/tools/${toolCallId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approved })
    }),
  recommendations: () => request<{ items: Paper[] }>("/api/users/recommendations"),
  settings: () => request<UserSettings>("/api/users/settings"),
  updatePreferenceText: (text: string) =>
    request<UserSettings>("/api/users/settings/preferences", {
      method: "PUT",
      body: JSON.stringify({ text })
    }),
  clearChatMemory: () =>
    request<{ deletedSessions: number }>("/api/users/settings/chat-memory", {
      method: "DELETE"
    }),
  deleteUnfavoritedPapers: () =>
    request<{ deletedPapers: number }>("/api/users/settings/unfavorited-papers", {
      method: "DELETE"
    }),
  favoriteFolders: () => request<{ items: FavoriteFolder[] }>("/api/users/favorites/folders"),
  createFavoriteFolder: (name: string) =>
    request<FavoriteFolder>("/api/users/favorites/folders", {
      method: "POST",
      body: JSON.stringify({ name })
    }),
  favoritePaper: (paperId: number, folderId?: number) =>
    request<{ paperId: number; folderId: number }>("/api/users/favorites", {
      method: "POST",
      body: JSON.stringify({ paperId, folderId })
    }),
  favoritePapers: (folderId?: number) => {
    const params = new URLSearchParams();
    if (folderId) params.set("folderId", String(folderId));
    return request<{ items: Paper[] }>(`/api/users/favorites?${params.toString()}`);
  }
};
