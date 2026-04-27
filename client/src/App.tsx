import { useEffect, useMemo, useRef, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { DailyPaperPanel } from "./components/DailyPaperPanel";
import { HistoryPanel } from "./components/HistoryPanel";
import { PaperList } from "./components/PaperList";
import { PaperViewer } from "./components/PaperViewer";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar, type LibraryMode } from "./components/Sidebar";
import { api, type AgentActivity, type AgentInfo, type ChatMessage, type ChatMission, type ChatSession, type CrawlJob, type DailyPaperEntry, type DailyPaperRun, type DateFilter, type FavoriteFolder, type OcrQuota, type Paper, type StreamEvent, type ThinkingItem, type ToolCallInfo } from "./lib/api";

type ViewMode = "summary" | "markdown" | "pdf";
type ChatMode = "paper_ace";

function yesterdayString() {
  const target = new Date();
  target.setDate(target.getDate() - 1);
  return target.toISOString().slice(0, 10);
}

export default function App() {
  const [categories, setCategories] = useState<string[]>(["cs.AI"]);
  const [category, setCategory] = useState("cs.AI");
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selectedPaperIds, setSelectedPaperIds] = useState<number[]>([]);
  const [activePaper, setActivePaper] = useState<Paper | undefined>();
  const [quota, setQuota] = useState<OcrQuota | undefined>();
  const [viewMode, setViewMode] = useState<ViewMode>("summary");
  const [chatMode] = useState<ChatMode>("paper_ace");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState<string>("");
  const [input, setInput] = useState("");
  const [selection, setSelection] = useState("");
  const [translation, setTranslation] = useState("");
  const [dateFilters, setDateFilters] = useState<DateFilter[]>([]);
  const [libraryMode, setLibraryMode] = useState<LibraryMode>("all");
  const [folders, setFolders] = useState<FavoriteFolder[]>([]);
  const [crawlJobs, setCrawlJobs] = useState<CrawlJob[]>([]);
  const [dailyPapers, setDailyPapers] = useState<DailyPaperEntry[]>([]);
  const [dailyRuns, setDailyRuns] = useState<DailyPaperRun[]>([]);
  const [dailyCategories, setDailyCategories] = useState<string[]>(["cs.AI"]);
  const [dailyTargetDate, setDailyTargetDate] = useState(yesterdayString);
  const [dailyMaxResults, setDailyMaxResults] = useState(12);
  const [activeFolderId, setActiveFolderId] = useState<number | undefined>();
  const [newFolderName, setNewFolderName] = useState("");
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [paperLoading, setPaperLoading] = useState(false);
  const [dailyLoading, setDailyLoading] = useState(false);
  const [crawlLoading, setCrawlLoading] = useState(false);
  const [dailyGenerating, setDailyGenerating] = useState(false);
  const [messagesBySession, setMessagesBySession] = useState<Record<string, ChatMessage[]>>({});
  const [toolCallsBySession, setToolCallsBySession] = useState<Record<string, ToolCallInfo[]>>({});
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [agentActivitiesBySession, setAgentActivitiesBySession] = useState<Record<string, AgentActivity[]>>({});
  const [thinkingBySession, setThinkingBySession] = useState<Record<string, ThinkingItem[]>>({});
  const [sessionLoading, setSessionLoading] = useState<Record<string, boolean>>({});
  const [chatAttachments, setChatAttachments] = useState<Paper[]>([]);
  const [mentionResults, setMentionResults] = useState<Paper[]>([]);
  const [mentionOpen, setMentionOpen] = useState(false);
  const streamingSessionsRef = useRef<Set<string>>(new Set());
  const [actionLoading, setActionLoading] = useState(false);
  const [batchLoading, setBatchLoading] = useState(false);
  const [ocrLoading, setOcrLoading] = useState(false);
  const [ocrStatus, setOcrStatus] = useState("");
  const [error, setError] = useState<string>("");
  const [query, setQuery] = useState("");

  const gridClass = useMemo(() => {
    const flags = [];
    if (leftCollapsed) flags.push("left-collapsed");
    if (rightCollapsed) flags.push("right-collapsed");
    if (libraryMode === "daily") flags.push("daily-focus");
    return flags.join(" ");
  }, [leftCollapsed, rightCollapsed, libraryMode]);

  useEffect(() => {
    api.config().then((data) => {
      setCategories(data.categories);
      setCategory(data.categories[0] || "cs.AI");
      setDailyCategories([data.categories.find((item) => item !== "all") || data.categories[0] || "cs.AI"]);
    }).catch((reason) => setError(reason.message));
    api.quota().then(setQuota).catch(() => undefined);
    api.favoriteFolders().then((data) => setFolders(data.items)).catch(() => undefined);
    api.listSessions().then((data) => setSessions(data.items)).catch(() => undefined);
    api.agents().then((data) => setAgents(data.items)).catch(() => undefined);
    api.crawlJobs().then((data) => setCrawlJobs(data.items)).catch(() => undefined);
    api.dailyPaperRuns().then((data) => setDailyRuns(data.items)).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (libraryMode === "daily") {
      void loadDailyPapers();
      return;
    }
    void loadPapers(category, query);
  }, [category, query, dateFilters, libraryMode, activeFolderId, dailyTargetDate, dailyCategories]);

  useEffect(() => {
    const visibleIds = new Set(papers.map((paper) => paper.id));
    setSelectedPaperIds((ids) => ids.filter((id) => visibleIds.has(id)));
  }, [papers]);

  useEffect(() => {
    void ensureSession(chatMode, activePaper);
  }, [chatMode]);

  useEffect(() => {
    const mentionQuery = extractMentionQuery(input);
    if (mentionQuery === null) {
      setMentionResults([]);
      setMentionOpen(false);
      return;
    }

    const trimmed = mentionQuery.trim();
    if (!trimmed) {
      const seeds = [activePaper, ...papers].filter(Boolean) as Paper[];
      const unique = Array.from(new Map(seeds.map((paper) => [paper.id, paper])).values()).slice(0, 6);
      setMentionResults(unique);
      setMentionOpen(unique.length > 0);
      return;
    }

    const timer = window.setTimeout(async () => {
      try {
        const result = await api.listPapers(undefined, trimmed);
        setMentionResults(result.items.slice(0, 6));
        setMentionOpen(result.items.length > 0);
      } catch {
        setMentionResults([]);
        setMentionOpen(false);
      }
    }, 180);

    return () => window.clearTimeout(timer);
  }, [input, activePaper, papers]);

  useEffect(() => {
    const hasActiveJob = crawlJobs.some((job) => job.status === "queued" || job.status === "running");
    if (!hasActiveJob) return;
    const timer = window.setInterval(async () => {
      const latest = await api.crawlJobs();
      const active = latest.items.filter((job) => job.status === "queued" || job.status === "running");
      const detailed = await Promise.all(active.map((job) => api.crawlJob(job.id).catch(() => job)));
      const detailedById = new Map(detailed.map((job) => [job.id, job]));
      setCrawlJobs(latest.items.map((item) => detailedById.get(item.id) || item));
      if (!latest.items.some((job) => job.status === "queued" || job.status === "running")) {
        await loadPapers(category, query);
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [crawlJobs, category, query]);

  useEffect(() => {
    const hasActiveRun = dailyRuns.some((run) => run.status === "queued" || run.status === "running");
    if (!hasActiveRun) return;
    const timer = window.setInterval(async () => {
      const latest = await api.dailyPaperRuns();
      setDailyRuns(latest.items);
      if (libraryMode === "daily") {
        await loadDailyPapers();
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [dailyRuns, libraryMode, dailyTargetDate, dailyCategories]);

  useEffect(() => {
    const activeMissions = sessions
      .map((session) => session.latestMission)
      .filter((mission): mission is ChatMission => !!mission && (mission.status === "queued" || mission.status === "running"));
    if (!activeMissions.length) return;
    const timer = window.setInterval(async () => {
      const missionStatuses = await Promise.all(activeMissions.map((mission) => api.getMission(mission.id).catch(() => mission)));
      const completed = missionStatuses.filter((mission) => mission.status === "done" || mission.status === "failed");
      const latest = await api.listSessions();
      setSessions(latest.items);
      for (const mission of completed) {
        const data = await api.listMessages(mission.sessionId);
        setMessagesBySession((prev) => ({ ...prev, [mission.sessionId]: data.items }));
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [sessions]);

  async function loadPapers(nextCategory = category, nextQuery = query) {
    if (libraryMode === "settings" || libraryMode === "daily") return;
    setPaperLoading(true);
    setError("");
    try {
      const data = await loadCurrentLibrary(nextCategory, nextQuery);
      setPapers(data.items);
      if (data.items.length > 0 && (!activePaper || !data.items.some((item) => item.id === activePaper.id))) {
        await selectPaper(data.items[0]);
      } else if (data.items.length === 0) {
        setActivePaper(undefined);
      }
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setPaperLoading(false);
    }
  }

  async function loadDailyPapers(nextTargetDate = dailyTargetDate, nextCategories = dailyCategories) {
    setDailyLoading(true);
    setError("");
    try {
      const [paperData, runData] = await Promise.all([
        api.dailyPapers(nextTargetDate, nextCategories),
        api.dailyPaperRuns()
      ]);
      setDailyPapers(paperData.items);
      setDailyRuns(runData.items);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setDailyLoading(false);
    }
  }

  function loadCurrentLibrary(nextCategory = category, nextQuery = query) {
    if (libraryMode === "recommendations") {
      return api.recommendations();
    }
    if (libraryMode === "favorites") {
      return api.favoritePapers(activeFolderId);
    }
    return api.listPapers(nextCategory, nextQuery, {
      parsed: libraryMode === "parsed" ? true : undefined,
      dateFilters
    });
  }

  async function selectPaper(paper: Paper) {
    setError("");
    setTranslation("");
    setSelection("");
    try {
      const full = await api.getPaper(paper.id);
      setActivePaper(full);
      setViewMode("summary");
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function ensureSession(mode: ChatMode, paper?: Paper) {
    try {
      const latest = await api.listSessions();
      const existing = latest.items.find((item) => item.scope === mode);
      if (existing) {
        await selectSession(existing.id);
        setSessions(latest.items);
        return;
      }
      const session = await api.createSession(mode, paper?.id, "Paper Ace Paper");
      setSessionId(session.id);
      setSessions([{ id: session.id, scope: mode, paperId: paper?.id, title: "Paper Ace Paper", updatedAt: new Date().toISOString() }, ...latest.items]);
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function selectSession(nextSessionId: string) {
    setSessionId(nextSessionId);
    if (messagesBySession[nextSessionId]) {
      return; // Already have cached messages
    }
    const data = await api.listMessages(nextSessionId);
    setMessagesBySession((prev) => ({ ...prev, [nextSessionId]: data.items }));
  }

  async function openHistorySession(session: ChatSession) {
    setError("");
    try {
      if ((session.scope === "paper" || session.scope === "paper_ace") && session.paperId && activePaper?.id !== session.paperId) {
        const paper = await api.getPaper(session.paperId);
        setActivePaper(paper);
        setViewMode("summary");
      }
      await selectSession(session.id);
      setHistoryOpen(false);
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function createNewChatSession() {
    setError("");
    try {
      const title = "Paper Ace Paper";
      const session = await api.createSession(chatMode, activePaper?.id, title);
      const next: ChatSession = {
        id: session.id,
        scope: chatMode,
        paperId: activePaper?.id,
        title,
        preview: "",
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString()
      };
      setSessionId(session.id);
      setMessagesBySession((prev) => ({ ...prev, [session.id]: [] }));
      setToolCallsBySession((prev) => ({ ...prev, [session.id]: [] }));
      setThinkingBySession((prev) => ({ ...prev, [session.id]: [] }));
      setAgentActivitiesBySession((prev) => ({ ...prev, [session.id]: [] }));
      setSessions((items) => [next, ...items]);
      setHistoryOpen(false);
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function deleteHistorySession(session: ChatSession) {
    const label = session.title || "Paper Ace Paper";
    const confirmed = window.confirm(`删除「${label}」这条会话历史？此操作不能撤销。`);
    if (!confirmed) return;
    setError("");
    try {
      await api.deleteSession(session.id);
      const remaining = sessions.filter((item) => item.id !== session.id);
      setSessions(remaining);
      setMessagesBySession((prev) => {
        const next = { ...prev };
        delete next[session.id];
        return next;
      });
      setToolCallsBySession((prev) => {
        const next = { ...prev };
        delete next[session.id];
        return next;
      });
      setThinkingBySession((prev) => {
        const next = { ...prev };
        delete next[session.id];
        return next;
      });
      setAgentActivitiesBySession((prev) => {
        const next = { ...prev };
        delete next[session.id];
        return next;
      });
      if (session.id === sessionId) {
        const nextSession = remaining[0];
        if (nextSession) {
          await openHistorySession(nextSession);
        } else {
          setSessionId("");
          setMessagesBySession({});
          setToolCallsBySession({});
          setThinkingBySession({});
          setAgentActivitiesBySession({});
        }
      }
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function crawlLatest() {
    setCrawlLoading(true);
    setError("");
    try {
      const job = await api.crawl(category, 20, dateFilters);
      setCrawlJobs((items) => [job, ...items.filter((item) => item.id !== job.id)]);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setCrawlLoading(false);
    }
  }

  async function generateDailyPaper() {
    if (dailyCategories.length === 0) return;
    setDailyGenerating(true);
    setError("");
    try {
      const run = await api.generateDailyPaper({
        categories: dailyCategories,
        targetDate: dailyTargetDate,
        maxResults: dailyMaxResults
      });
      setDailyRuns((items) => [run, ...items.filter((item) => item.id !== run.id)]);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setDailyGenerating(false);
    }
  }

  async function stopDailyPaperRun(runId: number) {
    setError("");
    try {
      const run = await api.cancelDailyPaperRun(runId);
      setDailyRuns((items) => items.map((item) => item.id === runId ? run : item));
      if (libraryMode === "daily") {
        await loadDailyPapers();
      }
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function createFolder() {
    if (!newFolderName.trim()) return;
    try {
      const folder = await api.createFavoriteFolder(newFolderName);
      setFolders((items) => items.some((item) => item.id === folder.id) ? items : [folder, ...items]);
      setActiveFolderId(folder.id);
      setNewFolderName("");
      setLibraryMode("favorites");
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function favoriteActivePaper(folderId?: number) {
    if (!activePaper) return;
    try {
      const targetFolderId = folderId || activeFolderId || folders[0]?.id;
      await api.favoritePaper(activePaper.id, targetFolderId);
      setLibraryMode("favorites");
      const data = await api.favoritePapers(targetFolderId);
      setPapers(data.items);
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  useEffect(() => {
    const handleSelectionChange = () => {
      if (!window.getSelection()?.toString().trim()) {
        setSelection("");
      }
    };
    document.addEventListener("selectionchange", handleSelectionChange);
    return () => document.removeEventListener("selectionchange", handleSelectionChange);
  }, []);

  async function analyzePaper() {
    if (!activePaper) return;
    setActionLoading(true);
    setError("");
    try {
      await api.analyze(activePaper.id);
      await selectPaper(activePaper);
      await loadPapers(category, query);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setActionLoading(false);
    }
  }

  async function translateSelection() {
    if (!activePaper || !selection) return;
    setActionLoading(true);
    setError("");
    try {
      const data = await api.translate(activePaper.id, selection, activePaper.abstract);
      setTranslation(data.translation);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setActionLoading(false);
    }
  }

  async function submitOcrAndPoll() {
    if (!activePaper) return;
    setOcrLoading(true);
    setOcrStatus("提交 OCR 任务中");
    setError("");
    try {
      const job = await api.submitOcr(activePaper.id);
      const jobId = job.jobId;
      if (!jobId) {
        throw new Error("OCR job id missing");
      }
      let status = job.status;
      for (let index = 0; index < 120 && status !== "done"; index += 1) {
        setOcrStatus(status === "pending" ? "OCR 排队中" : "OCR 解析中");
        await new Promise((resolve) => window.setTimeout(resolve, 5000));
        const result = await api.pollOcr(activePaper.id, jobId);
        status = result.status;
        if (result.pagesExtracted || result.pagesTotal) {
          setOcrStatus(`OCR 解析中 ${result.pagesExtracted || 0}/${result.pagesTotal || "?"} 页`);
        }
      }
      if (status !== "done") {
        throw new Error("OCR 仍在运行，请稍后再次提交或轮询");
      }
      setOcrStatus("OCR 完成，正在加载 Markdown");
      await selectPaper(activePaper);
      setViewMode("markdown");
      setQuota(await api.quota());
    } catch (reason) {
      setError((reason as Error).message);
      setOcrStatus("");
    } finally {
      setOcrLoading(false);
    }
  }

  function toggleSelectedPaper(paperId: number) {
    setSelectedPaperIds((ids) => ids.includes(paperId) ? ids.filter((id) => id !== paperId) : [...ids, paperId]);
  }

  function selectAllVisiblePapers() {
    setSelectedPaperIds(papers.map((paper) => paper.id));
  }

  async function batchAnalyzeSelected() {
    if (!selectedPaperIds.length) return;
    setBatchLoading(true);
    setError("");
    try {
      for (let index = 0; index < selectedPaperIds.length; index += 1) {
        setOcrStatus(`AI 分析中 ${index + 1}/${selectedPaperIds.length}`);
        await api.analyze(selectedPaperIds[index]);
      }
      setOcrStatus("批量 AI 分析完成");
      await loadPapers(category, query);
      if (activePaper) await selectPaper(activePaper);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBatchLoading(false);
    }
  }

  async function batchFavoriteSelected() {
    if (!selectedPaperIds.length) return;
    setBatchLoading(true);
    setError("");
    try {
      const targetFolderId = activeFolderId || folders[0]?.id;
      for (const paperId of selectedPaperIds) {
        await api.favoritePaper(paperId, targetFolderId);
      }
      setFolders((await api.favoriteFolders()).items);
      setOcrStatus(`已收藏 ${selectedPaperIds.length} 篇论文`);
      if (libraryMode === "favorites") {
        setPapers((await api.favoritePapers(targetFolderId)).items);
      }
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBatchLoading(false);
    }
  }

  async function batchOcrSelected() {
    if (!selectedPaperIds.length) return;
    setBatchLoading(true);
    setOcrLoading(true);
    setError("");
    try {
      for (let index = 0; index < selectedPaperIds.length; index += 1) {
        const paperId = selectedPaperIds[index];
        setOcrStatus(`提交 OCR ${index + 1}/${selectedPaperIds.length}`);
        const job = await api.submitOcr(paperId);
        const jobId = job.jobId;
        if (!jobId) throw new Error(`论文 ${paperId} OCR job id missing`);
        let status = job.status;
        for (let pollIndex = 0; pollIndex < 120 && status !== "done"; pollIndex += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 5000));
          const result = await api.pollOcr(paperId, jobId);
          status = result.status;
          setOcrStatus(`OCR ${index + 1}/${selectedPaperIds.length} · ${result.pagesExtracted || 0}/${result.pagesTotal || "?"} 页`);
          if (status === "failed") throw new Error(`论文 ${paperId} OCR 失败`);
        }
        if (status !== "done") throw new Error(`论文 ${paperId} OCR 仍在运行`);
      }
      setOcrStatus("批量 OCR 完成");
      setQuota(await api.quota());
      await loadPapers(category, query);
      if (activePaper) await selectPaper(activePaper);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBatchLoading(false);
      setOcrLoading(false);
    }
  }

  function sendMessage() {
    if (!sessionId || !input.trim()) return;
    const sid = sessionId;
    const content = input;
    const attachmentPaperIds = chatAttachments.map((paper) => paper.id);
    sendMessageInner(sid, content, attachmentPaperIds);
  }

  async function submitBackgroundMission() {
    if (!sessionId || !input.trim()) return;
    const sid = sessionId;
    const content = input;
    const attachmentPaperIds = chatAttachments.map((paper) => paper.id);
    setError("");
    setInput("");
    setMentionResults([]);
    setMentionOpen(false);
    setChatAttachments([]);
    try {
      const mission = await api.submitMission(sid, {
        message: content,
        paperId: activePaper?.id,
        selection,
        attachmentPaperIds,
        mode: chatMode
      });
      const latest = await api.listSessions();
      setSessions(latest.items.map((item) => item.id === sid ? { ...item, latestMission: mission } : item));
      setHistoryOpen(true);
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  function sendMessageInner(sid: string, content: string, attachmentPaperIds: number[]) {
    if (streamingSessionsRef.current.has(sid)) return; // Already streaming
    streamingSessionsRef.current.add(sid);
    setSessionLoading((prev) => ({ ...prev, [sid]: true }));
    setError("");
    setInput("");
    setMentionResults([]);
    setMentionOpen(false);
    setChatAttachments([]);

    const userMessage: ChatMessage = {
      id: -Date.now(),
      role: "user",
      content,
      selection,
      createdAt: new Date().toISOString()
    };
    const assistantMessage: ChatMessage = {
      id: -Date.now() - 1,
      role: "assistant",
      content: "",
      createdAt: new Date().toISOString()
    };
    const pendingToolCalls: ToolCallInfo[] = [];

    setMessagesBySession((prev) => ({
      ...prev,
      [sid]: [...(prev[sid] || []), userMessage, assistantMessage]
    }));
    setToolCallsBySession((prev) => ({ ...prev, [sid]: [] }));
    setAgentActivitiesBySession((prev) => ({ ...prev, [sid]: [] }));
    setThinkingBySession((prev) => ({ ...prev, [sid]: [] }));

    // Run stream in background — don't await
    api.streamMessage(
      sid,
      { message: content, paperId: activePaper?.id, selection, attachmentPaperIds, mode: chatMode },
      (event: StreamEvent) => {
        handleStreamEvent(sid, assistantMessage.id, pendingToolCalls, event);
      }
    )
      .then(async () => {
        await onStreamDone(sid);
      })
      .catch((reason: Error) => {
        handleStreamError(sid, reason);
      });
  }

  function handleStreamEvent(sid: string, assistantId: number, pendingToolCalls: ToolCallInfo[], event: StreamEvent) {
    switch (event.type) {
      case "text":
        setMessagesBySession((prev) => {
          const msgs = prev[sid] || [];
          return {
            ...prev,
            [sid]: msgs.map((m) =>
              m.id === assistantId ? { ...m, content: m.content + event.content } : m
            )
          };
        });
        break;
      case "agent_start": {
        const activity: AgentActivity = {
          agentKey: event.agentKey,
          agentName: event.agentName,
          status: "running",
          summary: event.summary
        };
        setAgentActivitiesBySession((prev) => ({
          ...prev,
          [sid]: [...(prev[sid] || []).filter((item) => item.agentKey !== event.agentKey), activity]
        }));
        break;
      }
      case "thinking": {
        const item: ThinkingItem = {
          id: `${Date.now()}-${event.agentKey}-${Math.random().toString(16).slice(2)}`,
          agentKey: event.agentKey,
          agentName: event.agentName,
          content: event.content,
          createdAt: new Date().toISOString()
        };
        setThinkingBySession((prev) => ({
          ...prev,
          [sid]: [...(prev[sid] || []), item]
        }));
        break;
      }
      case "agent_result":
        setAgentActivitiesBySession((prev) => ({
          ...prev,
          [sid]: (prev[sid] || []).map((item) =>
            item.agentKey === event.agentKey
              ? { ...item, status: "done", summary: event.summary }
              : item
          )
        }));
        break;
      case "tool_start": {
        const info: ToolCallInfo = {
          toolCallId: event.toolCallId,
          name: event.name,
          arguments: event.arguments,
          status: "running"
        };
        pendingToolCalls.push(info);
        setToolCallsBySession((prev) => ({
          ...prev,
          [sid]: [...(prev[sid] || []), info]
        }));
        break;
      }
      case "tool_result": {
        setToolCallsBySession((prev) => ({
          ...prev,
          [sid]: (prev[sid] || []).map((tc) =>
            tc.toolCallId === event.toolCallId
              ? { ...tc, status: "success", summary: event.summary }
              : tc
          )
        }));
        break;
      }
      case "approval": {
        // Store approval info for the dialog
        setToolCallsBySession((prev) => ({
          ...prev,
          [sid]: (prev[sid] || []).map((tc) =>
            tc.toolCallId === event.toolCallId
              ? { ...tc, status: "running", summary: `⚠️ 需要批准: ${event.command}` }
              : tc
          )
        }));
        break;
      }
      case "error":
        setMessagesBySession((prev) => {
          const msgs = prev[sid] || [];
          return {
            ...prev,
            [sid]: msgs.map((m) =>
              m.id === assistantId
                ? { ...m, content: m.content + `\n\n[错误] ${event.message}` }
                : m
            )
          };
        });
        break;
      case "done":
        break;
    }
  }

  async function onStreamDone(sid: string) {
    streamingSessionsRef.current.delete(sid);
    setSessionLoading((prev) => ({ ...prev, [sid]: false }));
    const [messageData, sessionData] = await Promise.all([
      api.listMessages(sid),
      api.listSessions()
    ]);
    setMessagesBySession((prev) => ({ ...prev, [sid]: messageData.items }));
    setToolCallsBySession((prev) => ({ ...prev, [sid]: [] }));
    setSessions(sessionData.items);
  }

  function handleStreamError(sid: string, reason: Error) {
    streamingSessionsRef.current.delete(sid);
    setSessionLoading((prev) => ({ ...prev, [sid]: false }));
    setError(reason.message);
  }

  function handleApproveToolCall(toolCallId: string, approved: boolean) {
    setToolCallsBySession((prev) => ({
      ...prev,
      [sessionId]: (prev[sessionId] || []).map((tc) =>
        tc.toolCallId === toolCallId
          ? { ...tc, status: approved ? "running" : "denied", summary: approved ? tc.summary : "❌ 已拒绝" }
          : tc
      )
    }));
    api.approveToolCall(sessionId, toolCallId, approved).catch(() => undefined);
  }

  function extractMentionQuery(value: string) {
    const lastAt = value.lastIndexOf("@");
    if (lastAt === -1) return null;
    if (lastAt > 0 && /\S/.test(value[lastAt - 1])) return null;
    const tail = value.slice(lastAt + 1);
    if (tail.includes("\n")) return null;
    return tail;
  }

  function attachPaperFromMention(paper: Paper) {
    setChatAttachments((items) => items.some((item) => item.id === paper.id) ? items : [...items, paper]);
    setInput((value) => {
      const lastAt = value.lastIndexOf("@");
      if (lastAt === -1) return value;
      return value.slice(0, lastAt).replace(/\s+$/, " ").trimStart();
    });
    setMentionResults([]);
    setMentionOpen(false);
  }

  function removeChatAttachment(paperId: number) {
    setChatAttachments((items) => items.filter((paper) => paper.id !== paperId));
  }

  return (
    <div className={`app-shell ${gridClass}`}>
      <Sidebar
        collapsed={leftCollapsed}
        categories={categories}
        activeCategory={category}
        quota={quota}
        dateFilters={dateFilters}
        libraryMode={libraryMode}
        folders={folders}
        crawlJobs={crawlJobs}
        activeFolderId={activeFolderId}
        newFolderName={newFolderName}
        loading={crawlLoading}
        onToggle={() => setLeftCollapsed((value) => !value)}
        onCategory={setCategory}
        onDateFilters={setDateFilters}
        onLibraryMode={setLibraryMode}
        onActiveFolder={setActiveFolderId}
        onNewFolderName={setNewFolderName}
        onCreateFolder={createFolder}
        onCrawl={crawlLatest}
      />

      <section className={libraryMode === "settings" ? "center-stage settings-stage" : libraryMode === "daily" ? "center-stage daily-stage" : "center-stage"}>
        {libraryMode === "settings" ? (
          <SettingsPanel
            activePaper={activePaper}
            onPaperDeleted={() => setActivePaper(undefined)}
            onDatabaseChanged={() => {
              setSelectedPaperIds([]);
              void loadPapers(category, query);
            }}
          />
        ) : libraryMode === "daily" ? (
          <DailyPaperPanel
            categories={categories}
            selectedCategories={dailyCategories}
            targetDate={dailyTargetDate}
            maxResults={dailyMaxResults}
            entries={dailyPapers}
            runs={dailyRuns}
            loading={dailyLoading}
            generating={dailyGenerating}
            error={error}
            onCategories={setDailyCategories}
            onTargetDate={setDailyTargetDate}
            onMaxResults={setDailyMaxResults}
            onGenerate={generateDailyPaper}
            onStopRun={stopDailyPaperRun}
          />
        ) : (
          <>
            <PaperList
              papers={papers}
              activePaperId={activePaper?.id}
              selectedPaperIds={selectedPaperIds}
              batchLoading={batchLoading}
              loading={paperLoading}
              error={error}
              title={libraryMode === "parsed" ? "已解析" : libraryMode === "favorites" ? "已收藏" : libraryMode === "recommendations" ? "推荐文章" : "论文摘要"}
              onSelect={selectPaper}
              onSearch={setQuery}
              onToggleSelected={toggleSelectedPaper}
              onSelectAll={selectAllVisiblePapers}
              onClearSelected={() => setSelectedPaperIds([])}
              onBatchAnalyze={batchAnalyzeSelected}
              onBatchOcr={batchOcrSelected}
              onBatchFavorite={batchFavoriteSelected}
            />
            <PaperViewer
              paper={activePaper}
              viewMode={viewMode}
              selection={selection}
              actionLoading={actionLoading}
              ocrLoading={ocrLoading}
              ocrStatus={ocrStatus}
              translation={translation}
              onViewMode={setViewMode}
              onAnalyze={analyzePaper}
              onOcr={submitOcrAndPoll}
              onFavorite={() => favoriteActivePaper()}
              onSelection={setSelection}
              onTranslate={translateSelection}
            />
          </>
        )}
      </section>

      <ChatPanel
        collapsed={rightCollapsed}
        activePaper={activePaper}
        messages={messagesBySession[sessionId] || []}
        agents={agents}
        agentActivities={agentActivitiesBySession[sessionId] || []}
        thinkingItems={thinkingBySession[sessionId] || []}
        toolCalls={toolCallsBySession[sessionId] || []}
        input={input}
        selection={selection}
        loading={!!sessionLoading[sessionId]}
        attachments={chatAttachments}
        mentionResults={mentionResults}
        mentionOpen={mentionOpen}
        onToggle={() => setRightCollapsed((value) => !value)}
        onHistory={() => setHistoryOpen(true)}
        onInput={setInput}
        onSend={sendMessage}
        onSubmitMission={submitBackgroundMission}
        onApproveToolCall={(toolCallId, approved) => handleApproveToolCall(toolCallId, approved)}
        onAttachPaper={attachPaperFromMention}
        onRemoveAttachment={removeChatAttachment}
      />
      <HistoryPanel
        open={historyOpen}
        sessions={sessions}
        activeSessionId={sessionId}
        onClose={() => setHistoryOpen(false)}
        onSelect={openHistorySession}
        onNewSession={createNewChatSession}
        onDeleteSession={deleteHistorySession}
      />
    </div>
  );
}
