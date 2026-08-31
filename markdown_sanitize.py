"""Safe, prose-only Markdown whitespace normalization."""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.lifecycle import ToolExecutionError


SAFE_MODE = "safe"

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_LIST_RE = re.compile(r"^\s{0,3}(?:[-+*]|\d+[.)])(?:\s+|$)")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>")
_INDENTED_CODE_RE = re.compile(r"^(?: {4,}|\t)")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_LINK_LINE_RE = re.compile(r"^\s*!?\[[^\]]*\]\([^)]*\)\s*$")
_LINK_REFERENCE_RE = re.compile(r"^\s*!?\[[^\]]+\]:\s*\S+\s*$")
_HTML_LINE_RE = re.compile(r"^\s*(?:<!--|</?(?:figure|img|svg)\b)", re.IGNORECASE)


def _fence_marker(line: str) -> tuple[str, int] | None:
    match = _FENCE_RE.match(line)
    if not match:
        return None
    marker = match.group(1)
    return marker[0], len(marker)


def _is_fence_close(line: str, marker: tuple[str, int]) -> bool:
    match = _FENCE_CLOSE_RE.match(line)
    if not match:
        return False
    value = match.group(1)
    return value[0] == marker[0] and len(value) >= marker[1]


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _TABLE_SEPARATOR_RE.match(line):
        return bool(_TABLE_SEPARATOR_RE.match(line))
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _table_line_indexes(lines: list[str]) -> set[int]:
    """Protect table headers and body rows, including rows without edge pipes."""

    protected: set[int] = set()
    for index, line in enumerate(lines):
        if not _TABLE_SEPARATOR_RE.match(line):
            continue
        start = index - 1
        while start >= 0 and lines[start].strip() and "|" in lines[start]:
            start -= 1
        end = index + 1
        while end < len(lines) and lines[end].strip() and "|" in lines[end]:
            end += 1
        protected.update(range(start + 1, end))
    return protected


def _is_link_or_figure_line(line: str) -> bool:
    return bool(
        _LINK_LINE_RE.match(line)
        or _LINK_REFERENCE_RE.match(line)
        or _HTML_LINE_RE.match(line)
    )


def _is_structural_line(line: str) -> bool:
    stripped = line.strip()
    if (
        _HEADING_RE.match(line)
        or _LIST_RE.match(line)
        or _BLOCKQUOTE_RE.match(line)
        or _INDENTED_CODE_RE.match(line)
        or _is_table_line(line)
        or _is_link_or_figure_line(line)
    ):
        return True
    if re.fullmatch(r"(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,}", stripped):
        return True
    return "<br" in stripped.lower() or stripped.endswith("\\") or line.endswith("  ")


def _json_line_indexes(lines: list[str]) -> set[int]:
    """Find standalone JSON snippets without treating Markdown links as JSON."""

    protected: set[int] = set()
    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        if not stripped or stripped[0] not in "[{" or _is_link_or_figure_line(lines[index]):
            index += 1
            continue

        candidate: list[str] = []
        found_end: int | None = None
        for end in range(index, min(len(lines), index + 128)):
            if not lines[end].strip():
                break
            candidate.append(lines[end])
            try:
                json.loads("\n".join(candidate).strip())
            except (TypeError, ValueError):
                continue
            found_end = end
            break
        if found_end is None:
            index += 1
            continue
        protected.update(range(index, found_end + 1))
        index = found_end + 1
    return protected


def sanitize_markdown(text: str, *, mode: str = SAFE_MODE) -> str:
    """Join hard-wrapped prose while preserving Markdown structures."""

    if not isinstance(text, str):
        raise ToolExecutionError("`text` must be a string.")
    if mode != SAFE_MODE:
        raise ToolExecutionError("`mode` must be `safe`.")

    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    json_lines = _json_line_indexes(lines)
    table_lines = _table_line_indexes(lines)

    output: list[str] = []
    paragraph: list[str] = []
    fence: tuple[str, int] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            output.append(" ".join(part.rstrip() for part in paragraph))
            paragraph.clear()

    for index, line in enumerate(lines):
        if fence is not None:
            output.append(line)
            if _is_fence_close(line, fence):
                fence = None
            continue

        marker = _fence_marker(line)
        if marker is not None:
            flush_paragraph()
            output.append(line)
            fence = marker
            continue

        if not line.strip():
            flush_paragraph()
            output.append(line)
            continue

        if index in json_lines or index in table_lines or _is_structural_line(line):
            flush_paragraph()
            output.append(line)
            continue

        # Leading indentation can carry Markdown meaning even when it is less
        # than four spaces. Keep such lines isolated in safe mode.
        if line[:1].isspace():
            flush_paragraph()
            output.append(line)
            continue

        paragraph.append(line)

    flush_paragraph()
    return newline.join(output)


def markdown_sanitize(arguments: dict[str, Any]) -> dict[str, Any]:
    """MCP handler for ``markdown_sanitize``."""

    if not isinstance(arguments, dict):
        raise ToolExecutionError("`arguments` must be an object.")
    text = arguments.get("text")
    mode = arguments.get("mode", SAFE_MODE)
    if not isinstance(mode, str):
        raise ToolExecutionError("`mode` must be `safe`.")
    sanitized = sanitize_markdown(text, mode=mode)
    return {
        "text": sanitized,
        "mode": SAFE_MODE,
        "changed": sanitized != text,
    }


__all__ = ["SAFE_MODE", "markdown_sanitize", "sanitize_markdown"]
