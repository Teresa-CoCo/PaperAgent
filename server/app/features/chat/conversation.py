import re
from dataclasses import dataclass
from datetime import date

from app.features.tools.llm import ChatMessage


@dataclass
class ChatConversationBuilder:
    papers: object

    def history_to_chat_messages(self, rows: list[dict]) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for row in rows:
            role = row.get("role")
            content = (row.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append(ChatMessage(role=role, content=content[:6000]))
        return messages

    def attachment_papers(self, paper_ids: list[int] | None, exclude_paper_id: int | None = None) -> list[dict]:
        if not paper_ids:
            return []
        filtered = [paper_id for paper_id in paper_ids if paper_id != exclude_paper_id]
        return self.papers.get_papers_by_ids(filtered[:6])

    def format_attachment_block(self, paper_ids: list[int] | None, exclude_paper_id: int | None = None) -> str:
        papers = self.attachment_papers(paper_ids, exclude_paper_id=exclude_paper_id)
        if not papers:
            return ""
        lines = ["用户附加了这些论文，请优先将它们作为额外上下文："]
        for index, paper in enumerate(papers, start=1):
            authors = ", ".join((paper.get("authors") or [])[:4])
            summary = (paper.get("aiSummary") or paper.get("abstract") or "").replace("\n", " ").strip()
            lines.append(
                f"{index}. [{paper['id']}] {paper['title']} (arXiv: {paper.get('arxivId', '')})"
                + (f" | Authors: {authors}" if authors else "")
                + (f"\n   摘要: {summary[:400]}" if summary else "")
            )
        return "\n".join(lines)

    def paper_ace_user_prompt(
        self,
        message: str,
        paper_id: int | None,
        selection: str | None,
        context_chunks: list[str],
        attachment_paper_ids: list[int] | None = None,
    ) -> str:
        focused_paper = ""
        if paper_id:
            try:
                paper = self.papers.get_papers_by_ids([paper_id])[0]
                focused_paper = (
                    "当前界面焦点论文：\n"
                    f"- DB paper_id={paper['id']} | {paper['title']} | arXiv: {paper.get('arxivId', '')}\n"
                    f"- 摘要: {(paper.get('aiSummary') or paper.get('abstract') or '')[:900]}\n"
                )
            except IndexError:
                focused_paper = f"当前界面焦点论文 ID：{paper_id}（数据库未返回详情）\n"
        selection_block = f"\n用户当前选中的原文片段：\n{selection[:3000]}\n" if selection else ""
        attachment_block = self.format_attachment_block(attachment_paper_ids, exclude_paper_id=paper_id)
        retrieval_block = "\n---\n".join(context_chunks) if context_chunks else "无当前焦点论文 RAG 片段；如需要证据，请调用 search_database/search_rag_database/arxiv_search/web_search。"
        return (
            f"用户当前请求：{message}\n"
            f"{focused_paper}"
            f"{selection_block}"
            f"{attachment_block}\n"
            "当前焦点论文 RAG 预取片段：\n"
            f"{retrieval_block}"
        )[:16000]

    def paper_conversation_messages(self, session_history: list[dict], current_prompt: str) -> list[ChatMessage]:
        messages = [
            ChatMessage(
                "system",
                (
                    f"你是 Paper Chat。今天的日期是 {date.today().isoformat()}。\n"
                    "你要延续当前会话，不要忽略同一会话里前面的问答。"
                    "优先基于当前论文的检索片段回答；如果用户附加了其他论文，把它们视为次级参考。"
                    "用中文回答；不确定时明确说明；引用时优先写 arXiv 编号或论文标题。"
                ),
            )
        ]
        messages.extend(self.history_to_chat_messages(session_history[:-1]))
        messages.append(ChatMessage("user", current_prompt))
        return messages


def refs_from_tool_results(tool_results: list[dict]) -> list[str]:
    refs: list[str] = []
    for item in tool_results:
        result = item.get("result", {})
        if "paper_id" in result:
            refs.append(f"paper_id={result['paper_id']}")
        for paper in result.get("results", []) if isinstance(result.get("results"), list) else []:
            if paper.get("id"):
                refs.append(f"paper_id={paper['id']}")
            if paper.get("arxivId"):
                refs.append(f"arXiv:{paper['arxivId']}")
            if paper.get("absUrl"):
                refs.append(paper["absUrl"])
            if paper.get("url"):
                refs.append(paper["url"])
        for folder in result.get("folders", []) if isinstance(result.get("folders"), list) else []:
            if folder.get("id"):
                refs.append(f"favorite_folder={folder['id']}")
    return refs


def available_source_refs(
    paper_id: int | None,
    attachment_paper_ids: list[int] | None,
    context_chunks: list[str],
    results: list[object],
) -> list[str]:
    refs: list[str] = []
    if paper_id:
        refs.append(f"paper_id={paper_id}")
    for attached_id in attachment_paper_ids or []:
        refs.append(f"paper_id={attached_id}")
    text = "\n".join(context_chunks + [getattr(result, "content", "") for result in results])
    refs.extend(f"arXiv:{match}" for match in re.findall(r"arXiv:\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", text, flags=re.I))
    refs.extend(re.findall(r"https?://[^\s)\]]+", text))
    for result in results:
        refs.extend(refs_from_tool_results(getattr(result, "tool_results", [])))
    return sorted(set(refs))


def citation_report(answer: str, source_refs: list[str]) -> str:
    factual_lines = [
        line.strip()
        for line in answer.splitlines()
        if len(line.strip()) > 30 and not line.lstrip().startswith(("引用检查", "References", "来源"))
    ]
    if not factual_lines:
        return ""
    unsupported = [line for line in factual_lines if not line_has_known_ref(line, source_refs)]
    if not unsupported:
        return ""
    sample = "；".join(line[:90] for line in unsupported[:3])
    return f"发现 {len(unsupported)} 条可能缺少显式来源绑定的陈述，已保留但需要人工核验：{sample}"


def line_has_known_ref(line: str, source_refs: list[str]) -> bool:
    markers = re.findall(r"\[([^\]]+)\]", line)
    if not markers or not source_refs:
        return False
    normalized_refs = {ref.lower() for ref in source_refs}
    for marker in markers:
        normalized_marker = marker.lower()
        if any(ref in normalized_marker or normalized_marker in ref for ref in normalized_refs):
            return True
    return False
