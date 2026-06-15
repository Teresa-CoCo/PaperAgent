import re
from dataclasses import dataclass


@dataclass
class Chunk:
    content: str
    title: str = ""
    section: str = ""


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def _split_sections(
    markdown: str,
    header_levels: list[str] | None = None,
) -> list[tuple[str, str]]:
    if header_levels is None:
        header_levels = ["#", "##", "###"]
    level_set = {len(h) for h in header_levels}

    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        content = "".join(buf).strip()
        if content:
            path = " > ".join(t for _, t in stack)
            sections.append((path, content))
        buf.clear()

    for line in markdown.split("\n"):
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level in level_set:
                flush()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, text))
                continue
        buf.append(line)
    flush()
    return sections


def _merge_chunks_with_overlap(
    parts: list[str],
    sep: str,
    chunk_size_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for part in parts:
        part_tokens = estimate_tokens(part) + (estimate_tokens(sep) if current else 0)

        if current_tokens + part_tokens > chunk_size_tokens and current:
            chunks.append(sep.join(current))

            overlap_parts: list[str] = []
            overlap_used = 0
            for p in reversed(current):
                pt = estimate_tokens(p) + (estimate_tokens(sep) if overlap_parts else 0)
                if overlap_used + pt > overlap_tokens:
                    break
                overlap_parts.insert(0, p)
                overlap_used += pt
            current = overlap_parts
            current_tokens = overlap_used

        current.append(part)
        current_tokens += part_tokens

    if current:
        chunks.append(sep.join(current))

    return chunks


def _recursive_split(
    text: str,
    chunk_size_tokens: int,
    overlap_tokens: int,
    separators: list[str] | None = None,
) -> list[str]:
    if separators is None:
        separators = ["\n\n", "\n", ".", " "]

    if estimate_tokens(text) <= chunk_size_tokens:
        return [text]

    sep = separators[0]
    parts = text.split(sep)

    if len(parts) > 1:
        return _merge_chunks_with_overlap(parts, sep, chunk_size_tokens, overlap_tokens)

    if len(separators) > 1:
        return _recursive_split(text, chunk_size_tokens, overlap_tokens, separators[1:])

    return [text]


def chunk_markdown(
    markdown: str,
    title: str = "",
    *,
    chunk_size_tokens: int = 600,
    overlap_tokens: int = 75,
    header_levels: list[str] | None = None,
) -> list[Chunk]:
    if header_levels is None:
        header_levels = ["#", "##", "###"]

    if not markdown.strip():
        return []

    sections = _split_sections(markdown, header_levels)
    chunks: list[Chunk] = []

    for section_path, content in sections:
        if not content:
            continue

        if estimate_tokens(content) <= chunk_size_tokens:
            chunks.append(Chunk(content=content, title=title, section=section_path))
        else:
            sub_texts = _recursive_split(content, chunk_size_tokens, overlap_tokens)
            for sub in sub_texts:
                chunks.append(Chunk(content=sub, title=title, section=section_path))

    return chunks
