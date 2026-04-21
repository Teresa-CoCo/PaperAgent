import html
import asyncio
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import httpx


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ARXIV_REQUEST_DELAY_SECONDS = 3.0
_arxiv_request_lock = asyncio.Lock()
_last_arxiv_request_at = 0.0


def _text(entry: ET.Element, name: str) -> str:
    node = entry.find(f"{ATOM}{name}")
    return " ".join((node.text or "").split()) if node is not None else ""


def _arxiv_id_and_version(raw_id: str) -> tuple[str, int]:
    identifier = raw_id.rstrip("/").split("/")[-1]
    match = re.match(r"(?P<base>.+)v(?P<version>\d+)$", identifier)
    if match:
        return match.group("base"), int(match.group("version"))
    return identifier, 1


class ArxivTool:
    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        global _last_arxiv_request_at
        async with _arxiv_request_lock:
            elapsed = time.monotonic() - _last_arxiv_request_at
            if elapsed < ARXIV_REQUEST_DELAY_SECONDS:
                await asyncio.sleep(ARXIV_REQUEST_DELAY_SECONDS - elapsed)
            response = await client.get(url, headers={"User-Agent": "PaperAgent/0.1"})
            _last_arxiv_request_at = time.monotonic()
            return response

    async def query_announced_new(self, category: str, max_results: int | None = None) -> list[dict]:
        url = f"https://arxiv.org/list/{category}/new"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await self._get(client, url)
            response.raise_for_status()
        page_papers = self._papers_from_new_page(response.text, category, max_results)
        ids = [paper["arxiv_id"] for paper in page_papers]
        if not ids:
            return await self.query(category, max_results or 20)
        try:
            enriched = await self.query_ids(ids, category)
        except httpx.HTTPError:
            return page_papers
        if not enriched:
            return page_papers
        page_by_id = {paper["arxiv_id"]: paper for paper in page_papers}
        enriched_by_id = {paper["arxiv_id"]: paper for paper in enriched}
        return [enriched_by_id.get(arxiv_id, page_by_id[arxiv_id]) for arxiv_id in ids]

    async def query_ids(self, ids: list[str], category: str | None = None) -> list[dict]:
        if not ids:
            return []
        by_id: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=30) as client:
            for index in range(0, len(ids), 100):
                chunk = ids[index : index + 100]
                id_list = ",".join(urllib.parse.quote(item) for item in chunk)
                url = f"https://export.arxiv.org/api/query?id_list={id_list}&max_results={len(chunk)}"
                response = await self._get(client, url)
                response.raise_for_status()
                root = ET.fromstring(response.text)
                for entry in root.findall(f"{ATOM}entry"):
                    paper = self._paper_from_atom(entry, category)
                    by_id[paper["arxiv_id"]] = paper
        return [by_id[arxiv_id] for arxiv_id in ids if arxiv_id in by_id]

    async def query(
        self,
        category: str,
        max_results: int = 20,
        submitted_from: date | None = None,
        submitted_to: date | None = None,
    ) -> list[dict]:
        search_parts = [f"cat:{category}"]
        if submitted_from:
            end_date = submitted_to or submitted_from
            start_token = datetime.combine(submitted_from, datetime.min.time()).strftime("%Y%m%d%H%M")
            end_token = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).strftime("%Y%m%d%H%M")
            search_parts.append(f"submittedDate:[{start_token} TO {end_token}]")
        search_query = urllib.parse.quote(" AND ".join(search_parts))
        url = (
            "https://export.arxiv.org/api/query"
            f"?search_query={search_query}&sortBy=submittedDate&sortOrder=descending"
            f"&start=0&max_results={max_results}"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            response = await self._get(client, url)
            response.raise_for_status()
        root = ET.fromstring(response.text)
        papers: list[dict] = []
        for entry in root.findall(f"{ATOM}entry"):
            papers.append(self._paper_from_atom(entry, category))
        return papers

    def _paper_from_atom(self, entry: ET.Element, requested_category: str | None = None) -> dict:
        arxiv_id, version = _arxiv_id_and_version(_text(entry, "id"))
        links = entry.findall(f"{ATOM}link")
        abs_url = ""
        for link in links:
            href = link.attrib.get("href", "")
            if "/abs/" in href:
                abs_url = href
        primary_category = entry.find(f"{ARXIV}primary_category")
        all_categories = [
            category.attrib.get("term", "")
            for category in entry.findall(f"{ATOM}category")
            if category.attrib.get("term")
        ]
        category = primary_category.attrib.get("term") if primary_category is not None else ""
        if requested_category and requested_category in all_categories:
            category = requested_category
        if not category:
            category = requested_category or ""
        pdf_identifier = f"{arxiv_id}v{version}"
        return {
            "arxiv_id": arxiv_id,
            "version": version,
            "title": _text(entry, "title"),
            "abstract": _text(entry, "summary"),
            "authors": [_text(author, "name") for author in entry.findall(f"{ATOM}author")],
            "category": category,
            "tags": all_categories,
            "pdf_url": f"https://arxiv.org/pdf/{pdf_identifier}.pdf",
            "abs_url": abs_url or f"https://arxiv.org/abs/{arxiv_id}",
            "published_at": _text(entry, "published"),
            "updated_at": _text(entry, "updated"),
            "raw_metadata": {
                "source": "arxiv_api",
                "categories": all_categories,
                "atom": ET.tostring(entry, encoding="unicode"),
            },
        }

    def _papers_from_new_page(self, html_text: str, category: str, max_results: int | None = None) -> list[dict]:
        papers: list[dict] = []
        for dt, dd in self._new_page_items(html_text, category, max_results):
            paper = self._paper_from_new_page_item(dt, dd, category)
            if paper:
                papers.append(paper)
        return papers

    def _ids_from_new_page(self, html_text: str, category: str, max_results: int | None = None) -> list[str]:
        return [paper["arxiv_id"] for paper in self._papers_from_new_page(html_text, category, max_results)]

    def _new_page_items(self, html_text: str, category: str, max_results: int | None = None) -> list[tuple[str, str]]:
        section_cutoffs = []
        for match in re.finditer(r"<li>\s*<a[^>]+href=\"#item(?P<item>\d+)\"", html_text, flags=re.S):
            section_cutoffs.append(int(match.group("item")))
        first_non_new_item = section_cutoffs[-1] if section_cutoffs else None

        items: list[tuple[str, str]] = []
        for match in re.finditer(r"<dt>(?P<dt>.*?)</dt>\s*<dd>(?P<dd>.*?)</dd>", html_text, flags=re.S):
            dt = match.group("dt")
            dd = match.group("dd")
            paper_anchor = re.search(r"<a\s+name=['\"]item(?P<item>\d+)['\"]", dt)
            if paper_anchor and first_non_new_item and int(paper_anchor.group("item")) >= first_non_new_item:
                continue
            link = re.search(
                r"<a\b(?=[^>]*\btitle=['\"]Abstract['\"])(?=[^>]*\bhref\s*=\s*['\"](?P<href>/abs/[^'\"]+)['\"])",
                dt,
            )
            if not link:
                continue
            categories = set(re.findall(r"\(([^()]+)\)", dd))
            if categories and category not in categories:
                continue
            items.append((dt, dd))
            if max_results and len(items) >= max_results:
                break
        return items

    def _paper_from_new_page_item(self, dt: str, dd: str, requested_category: str) -> dict | None:
        link = re.search(
            r"<a\b(?=[^>]*\btitle=['\"]Abstract['\"])(?=[^>]*\bhref\s*=\s*['\"](?P<href>/abs/[^'\"]+)['\"])",
            dt,
        )
        if not link:
            return None
        arxiv_id, version = _arxiv_id_and_version(link.group("href").split("/")[-1])
        categories = re.findall(r"\(([^()]+)\)", self._fragment(dd, "list-subjects"))
        primary = categories[0] if categories else requested_category
        category = requested_category if requested_category in categories else primary
        return {
            "arxiv_id": arxiv_id,
            "version": version,
            "title": self._clean_descriptor(self._fragment(dd, "list-title"), "Title:"),
            "abstract": self._clean_text(self._first_paragraph(dd)),
            "authors": self._authors_from_dd(dd),
            "category": category,
            "tags": categories,
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
            "published_at": date.today().isoformat(),
            "updated_at": date.today().isoformat(),
            "raw_metadata": {
                "source": "arxiv_new_page",
                "categories": categories,
            },
        }

    def _fragment(self, text: str, class_name: str) -> str:
        match = re.search(
            rf"<(?P<tag>div|p)[^>]*class=['\"][^'\"]*{re.escape(class_name)}[^'\"]*['\"][^>]*>(?P<body>.*?)</(?P=tag)>",
            text,
            flags=re.S,
        )
        return match.group("body") if match else ""

    def _first_paragraph(self, text: str) -> str:
        match = re.search(r"<p[^>]*class=['\"][^'\"]*mathjax[^'\"]*['\"][^>]*>(?P<body>.*?)</p>", text, flags=re.S)
        return match.group("body") if match else ""

    def _authors_from_dd(self, text: str) -> list[str]:
        authors_html = self._fragment(text, "list-authors")
        authors = [self._clean_text(author) for author in re.findall(r"<a\b[^>]*>(.*?)</a>", authors_html, flags=re.S)]
        return [author for author in authors if author]

    def _clean_descriptor(self, text: str, descriptor: str) -> str:
        cleaned = self._clean_text(text)
        return cleaned.removeprefix(descriptor).strip()

    def _clean_text(self, text: str) -> str:
        without_tags = re.sub(r"<[^>]+>", " ", text)
        return " ".join(html.unescape(without_tags).split())
