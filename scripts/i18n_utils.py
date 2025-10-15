from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


PLACEHOLDER_RE = re.compile(r"\[\[[A-Z]+_\d+\]\]")


class PlaceholderManager:
    """Tracks non-translatable fragments that are replaced by stable placeholders."""

    def __init__(self) -> None:
        self._items: List[Tuple[str, str]] = []
        self._counter = 0

    def add(self, kind: str, original: str) -> str:
        self._counter += 1
        token = f"[[{kind.upper()}_{self._counter}]]"
        self._items.append((token, original))
        return token

    def restore(self, text: str) -> str:
        for token, original in self._items:
            text = text.replace(token, original)
        return text

    @property
    def items(self) -> List[Tuple[str, str]]:
        return list(self._items)


def sanitize_text(raw_text: str) -> Tuple[str, PlaceholderManager]:
    """Replace inline non-translatable fragments by placeholders."""

    manager = PlaceholderManager()
    text = raw_text

    # Inline code ``code``
    text = re.sub(
        r"`([^`]+)`",
        lambda m: manager.add("CODE", m.group(0)),
        text,
    )

    # Markdown links and images [text](url "title")
    link_pattern = re.compile(
        r"(!)?\[(?P<text>[^\]]+)\]\((?P<url>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)"
    )

    def replace_link(match: re.Match[str]) -> str:
        prefix = "!" if match.group(1) else ""
        inner_text = match.group("text")
        url = match.group("url")
        title = match.group("title") or ""
        url_token = manager.add("URL", url)
        title_token = manager.add("TITLE", title) if title else ""
        return f"{prefix}[{inner_text}]({url_token}{title_token})"

    text = link_pattern.sub(replace_link, text)

    # Raw URLs
    text = re.sub(
        r"(?P<url>https?://[^\s)]+)",
        lambda m: manager.add("URL", m.group("url")),
        text,
    )

    # HTML tags or autolinks <...>
    def replace_angle(match: re.Match[str]) -> str:
        value = match.group(0)
        kind = "URL" if value.startswith("<http") else "HTML"
        return manager.add(kind, value)

    text = re.sub(r"<[^>]+>", replace_angle, text)

    # Variables {placeholder}
    text = re.sub(
        r"\{[^{}\s][^{}]*\}",
        lambda m: manager.add("VAR", m.group(0)),
        text,
    )

    return text, manager


def _normalise_slug_text(text: str) -> str:
    text = PLACEHOLDER_RE.sub(" ", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def slugify(text: str, default: str = "section") -> str:
    slug = _normalise_slug_text(text)
    return slug or default


@dataclass
class Segment:
    identifier: str
    file_path: str
    start_line: int
    block_type: str
    msgid: str
    placeholders: List[Tuple[str, str]] = field(default_factory=list)
    context_path: str = "root"
    order: int = 0
    metadata: Dict[str, object] = field(default_factory=dict)

    def restore_placeholders(self, text: str) -> str:
        for token, original in self.placeholders:
            text = text.replace(token, original)
        return text


def collect_markdown_segments(markdown: str, rel_path: Path) -> List[Segment]:
    lines = markdown.splitlines()
    segments: List[Segment] = []

    heading_stack: List[Tuple[int, str]] = []
    slug_counters: Dict[Tuple[int, str], int] = {}
    block_counters: Dict[Tuple[str, str], int] = {}

    paragraph_lines: List[str] = []
    paragraph_start: Optional[int] = None

    in_fence = False
    fence_marker: Optional[str] = None

    def current_context() -> str:
        if not heading_stack:
            return "root"
        return "#".join(entry[1] for entry in heading_stack)

    order = 0

    def register_segment(
        *,
        block_type: str,
        start_line: int,
        content: str,
        metadata: Optional[Dict[str, object]] = None,
    ) -> None:
        nonlocal order
        sanitized, manager = sanitize_text(content)
        if not sanitized.strip():
            return
        ctx = current_context()
        if block_type == "heading":
            identifier = f"{rel_path.as_posix()}#{ctx}#title"
        else:
            key = (ctx, block_type)
            block_counters[key] = block_counters.get(key, 0) + 1
            suffix_map = {
                "paragraph": "p",
                "list_item": "li",
                "blockquote": "q",
            }
            suffix = suffix_map.get(block_type, block_type)
            identifier = f"{rel_path.as_posix()}#{ctx}#{suffix}-{block_counters[key]}"
        order += 1
        segments.append(
            Segment(
                identifier=identifier,
                file_path=rel_path.as_posix(),
                start_line=start_line,
                block_type=block_type,
                msgid=sanitized.strip(),
                placeholders=manager.items,
                context_path=current_context(),
                order=order,
                metadata=metadata or {},
            )
        )

    def flush_paragraph() -> None:
        nonlocal paragraph_lines, paragraph_start
        if not paragraph_lines:
            return
        text = "\n".join(paragraph_lines).strip("\n")
        if text.strip():
            register_segment(
                block_type="paragraph",
                start_line=paragraph_start or 1,
                content=text,
                metadata={"lines": list(paragraph_lines)},
            )
        paragraph_lines = []
        paragraph_start = None

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        line_no = i + 1

        fence_match = re.match(r"^(\s*)(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(2)
            if not in_fence:
                flush_paragraph()
                in_fence = True
                fence_marker = marker
            elif fence_marker == marker:
                in_fence = False
                fence_marker = None
            i += 1
            continue

        if in_fence:
            i += 1
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            raw_heading = heading_match.group(2).rstrip("#").strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            base_slug = slugify(raw_heading)
            slug_key = (level, base_slug)
            count = slug_counters.get(slug_key, 0)
            slug_counters[slug_key] = count + 1
            slug = f"{base_slug}-{count + 1}" if count else base_slug
            heading_stack.append((level, f"h{level}-{slug}"))
            register_segment(
                block_type="heading",
                start_line=line_no,
                content=raw_heading,
                metadata={"level": level, "slug": slug},
            )
            i += 1
            continue

        list_match = re.match(r"^(\s*)([-+*]|\d+\.)\s+(.*)$", line)
        if list_match:
            flush_paragraph()
            indent = list_match.group(1)
            marker = list_match.group(2)
            item_lines = [list_match.group(3)]
            item_start = line_no
            base_indent = len(indent.expandtabs(4))
            i += 1
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()
                next_indent = len(next_line[: len(next_line) - len(next_line.lstrip(" "))])
                if (
                    re.match(r"^(\s*)([-+*]|\d+\.)\s+", next_line)
                    and next_indent <= base_indent
                ):
                    break
                if next_stripped == "":
                    item_lines.append("")
                    i += 1
                    continue
                if next_indent > base_indent:
                    item_lines.append(next_line[base_indent:])
                    i += 1
                    continue
                break

            register_segment(
                block_type="list_item",
                start_line=item_start,
                content="\n".join(item_lines).strip("\n"),
                metadata={"indent": indent, "marker": marker},
            )
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines: List[str] = []
            quote_start = line_no
            while i < len(lines):
                q_line = lines[i]
                if q_line.strip().startswith(">"):
                    quote_lines.append(q_line.lstrip()[1:].lstrip())
                    i += 1
                    continue
                if q_line.strip() == "":
                    quote_lines.append("")
                    i += 1
                    continue
                break
            register_segment(
                block_type="blockquote",
                start_line=quote_start,
                content="\n".join(quote_lines).strip("\n"),
                metadata={},
            )
            continue

        if stripped == "":
            flush_paragraph()
            i += 1
            continue

        if not paragraph_lines:
            paragraph_start = line_no
        paragraph_lines.append(line)
        i += 1

    flush_paragraph()
    return segments


def iter_markdown_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.md")):
        if path.is_file():
            yield path

