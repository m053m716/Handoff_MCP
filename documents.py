"""Documentation indexing, retrieval, drift auditing, and resource rendering."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.lifecycle import (
    ToolExecutionError,
    bool_arg,
    clamp_limit,
    optional_string,
    require_string,
)
from mcp.project import load_project_descriptor


PROJECT = load_project_descriptor()
DOC_SCOPES: dict[str, dict[str, Any]] = {
    key: dict(value) for key, value in PROJECT.doc_scopes.items()
}
DOC_SOURCE_TYPES = {"file", "manual", "inferred"}
DOC_CHUNK_TARGET_CHARS = 3200
DOC_CHUNK_MAX_CHARS = 5000
DOC_FETCH_DEFAULT_CHARS = 6000
DOC_FETCH_MAX_CHARS = 50_000
DOC_SEARCH_MAX_LIMIT = 50
DOC_DRIFT_AUDIT_SCOPE = PROJECT.doc_drift_audit_scope
DOC_DRIFT_AUDIT_MAX_ITEMS = 12
DOC_DRIFT_TIMESTAMP_SKEW_SECONDS = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_timestamp_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_doc_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:/-]+", "-", value.strip()).strip("-").lower()


def title_from_path(path: Path) -> str:
    name = path.name
    if name.lower() == "agents.md":
        return f"Agent Guidance: {path.parent.as_posix() or 'root'}"
    if name.lower() == "readme.md":
        return f"README: {path.parent.as_posix() or 'root'}"
    return name


def read_repo_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def repo_relative_posix(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix().replace("\\", "/")


def iter_doc_paths(repo_root: Path, pattern: str) -> list[Path]:
    return sorted(path for path in repo_root.glob(pattern) if path.is_file())


def git_status_for_paths(repo_root: Path, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
                "--",
                *paths,
            ],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}

    statuses: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        rel = line[3:].strip()
        if " -> " in rel:
            rel = rel.rsplit(" -> ", 1)[1]
        rel = rel.strip('"').replace("\\", "/")
        if rel:
            statuses[rel] = status
    return statuses


def path_is_under(rel_path: str, root: str) -> bool:
    rel = rel_path.strip("/").replace("\\", "/")
    normalized_root = root.strip("/").replace("\\", "/")
    return rel == normalized_root or rel.startswith(normalized_root + "/")


def split_large_text(text: str, max_chars: int = DOC_CHUNK_TARGET_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    blocks = re.split(r"(\n\s*\n)", text)
    for block in blocks:
        if current and current_len + len(block) > max_chars:
            parts.append("".join(current).strip())
            current = []
            current_len = 0
        if len(block) > DOC_CHUNK_MAX_CHARS:
            for index in range(0, len(block), max_chars):
                if current:
                    parts.append("".join(current).strip())
                    current = []
                    current_len = 0
                parts.append(block[index : index + max_chars].strip())
            continue
        current.append(block)
        current_len += len(block)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def chunk_document(content: str, default_heading: str) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    heading = default_heading
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if not text:
            buffer = []
            return
        for part in split_large_text(text):
            chunks.append({"heading": heading, "content": part})
        buffer = []

    for line in content.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and buffer:
            flush()
            heading = match.group(2).strip()
        buffer.append(line)
    flush()
    if not chunks and content.strip():
        chunks.append({"heading": default_heading, "content": content.strip()})
    return chunks


def extract_source_symbols(path: Path, *, max_symbols: int = 40) -> list[str]:
    try:
        text = read_repo_text(path)
    except OSError:
        return []
    patterns: list[tuple[str, str]] = [
        (
            "class",
            r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|sealed\s+|abstract\s+|partial\s+|final\s+|data\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ),
        (
            "object",
            r"^\s*(?:public\s+|private\s+|internal\s+)?object\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ),
        (
            "interface",
            r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+)?interface\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ),
        (
            "enum",
            r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+)?enum(?:\s+class)?\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ),
        ("fun", r"^\s*(?:suspend\s+)?fun\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        (
            "method",
            r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|virtual\s+|override\s+|async\s+|sealed\s+|partial\s+|final\s+|synchronized\s+|native\s+)+[A-Za-z0-9_<>,\[\]?.]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        ),
        ("struct", r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        ("enum", r"^\s*(?:pub\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        ("trait", r"^\s*(?:pub\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        (
            "fn",
            r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ),
        ("class", r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        ("def", r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    ]
    symbols: list[str] = []
    for line in text.splitlines():
        for kind, pattern in patterns:
            match = re.match(pattern, line)
            if match:
                symbols.append(f"{kind} {match.group(1)}")
                break
        if len(symbols) >= max_symbols:
            symbols.append("...")
            break
    return symbols


def build_scope_manifest(
    repo_root: Path, scope: str, config: dict[str, Any]
) -> str:
    lines = [
        f"# Inferred Source Manifest: {config['title']}",
        "",
        config["description"],
        "",
        "This document is generated from repository paths so agents can search for likely code locations before opening files.",
        "",
    ]
    seen: set[str] = set()
    file_count = 0
    for pattern in config["manifest_globs"]:
        for path in iter_doc_paths(repo_root, pattern):
            rel = path.relative_to(repo_root).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            file_count += 1
            symbols = extract_source_symbols(path)
            lines.append(f"## {rel}")
            if symbols:
                lines.append("")
                lines.append("Symbols: " + ", ".join(symbols))
            lines.append("")
            if file_count >= 600:
                lines.append("Manifest truncated after 600 files.")
                return "\n".join(lines).strip()
    if not seen:
        lines.append("_No source files matched the manifest patterns._")
    return "\n".join(lines).strip()


def make_snippet(text: str, terms: list[str], *, max_chars: int = 360) -> str:
    normalized = text.lower()
    positions = [normalized.find(term) for term in terms if term and normalized.find(term) >= 0]
    start = max(0, min(positions) - 120) if positions else 0
    snippet = text[start : start + max_chars].replace("\n", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if start + max_chars < len(text):
        snippet += "..."
    return snippet


def tokenize_query(query: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[A-Za-z0-9_./:-]{2,}", query)]


class DocumentationService:
    """Own repository documentation discovery, indexing, and retrieval."""

    def __init__(self, store: Any, *, project: Any | None = None) -> None:
        self.store = store
        self.project = project or PROJECT
        self.repo_root = self.project.repo_root
        self.scope_configs = {key: dict(value) for key, value in self.project.doc_scopes.items()}
        self.doc_drift_audit_scope = self.project.doc_drift_audit_scope

    def connection(self):
        return self.store.connection()

    def validate_app_scope(self, value: str | None) -> str | None:
        if not value:
            return None
        if value not in self.scope_configs:
            raise ToolExecutionError(
                "`app_scope` must be one of: " + ", ".join(sorted(self.scope_configs)) + "."
            )
        return value

    def doc_scopes_result(self) -> dict[str, Any]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.scope,
                    s.title,
                    s.description,
                    s.root_paths_json,
                    s.display_order,
                    COUNT(DISTINCT e.id) AS doc_count,
                    COUNT(c.id) AS chunk_count,
                    MAX(e.updated_at) AS latest_doc_updated_at
                FROM doc_app_scopes s
                LEFT JOIN doc_entries e ON e.app_scope = s.scope
                LEFT JOIN doc_chunks c ON c.doc_id = e.id
                GROUP BY s.scope
                ORDER BY s.display_order, s.scope
                """
            ).fetchall()
        scopes = []
        for row in rows:
            item = dict(row)
            item["root_paths"] = json.loads(item.pop("root_paths_json"))
            scopes.append(item)
        return {"scopes": scopes}

    def doc_scopes(self) -> dict[str, Any]:
        return self.doc_scopes_result()

    def doc_scope_entries(self, app_scope: str) -> dict[str, Any]:
        app_scope = self.validate_app_scope(app_scope) or ""
        self.ensure_docs_indexed()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.doc_key,
                    e.app_scope,
                    e.title,
                    e.source_type,
                    e.source_path,
                    e.summary,
                    e.updated_at,
                    COUNT(c.id) AS chunk_count
                FROM doc_entries e
                LEFT JOIN doc_chunks c ON c.doc_id = e.id
                WHERE e.app_scope = ?
                GROUP BY e.id
                ORDER BY e.source_type, e.source_path, e.title
                """,
                (app_scope,),
            ).fetchall()
        return {"app_scope": app_scope, "documents": [dict(row) for row in rows]}

    def store_doc(
        self,
        conn: Any,
        *,
        doc_key: str,
        app_scope: str,
        title: str,
        content: str,
        source_type: str,
        source_path: str = "",
        summary: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        if app_scope not in self.scope_configs:
            raise ToolExecutionError(f"Unknown app_scope: {app_scope}")
        if source_type not in DOC_SOURCE_TYPES:
            raise ToolExecutionError(
                "`source_type` must be one of: " + ", ".join(sorted(DOC_SOURCE_TYPES)) + "."
            )
        tags = tags or []
        now = utc_now()
        doc_key = safe_doc_key(doc_key)
        content_hash = hash_text(content)
        existing = conn.execute(
            "SELECT id, created_at FROM doc_entries WHERE doc_key = ?", (doc_key,)
        ).fetchone()
        if existing:
            doc_id = existing["id"]
            created_at = existing["created_at"]
            conn.execute(
                """
                UPDATE doc_entries
                SET app_scope = ?, title = ?, source_type = ?, source_path = ?,
                    summary = ?, tags_json = ?, content_hash = ?, indexed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    app_scope,
                    title,
                    source_type,
                    source_path,
                    summary,
                    json_text(tags),
                    content_hash,
                    now,
                    now,
                    doc_id,
                ),
            )
            conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        else:
            cursor = conn.execute(
                """
                INSERT INTO doc_entries(
                    doc_key, app_scope, title, source_type, source_path, summary,
                    tags_json, content_hash, indexed_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_key,
                    app_scope,
                    title,
                    source_type,
                    source_path,
                    summary,
                    json_text(tags),
                    content_hash,
                    now,
                    now,
                    now,
                ),
            )
            doc_id = cursor.lastrowid
            created_at = now

        chunks = chunk_document(content, title)
        for index, chunk in enumerate(chunks):
            search_text = "\n".join(
                [
                    app_scope,
                    title,
                    source_type,
                    source_path,
                    chunk["heading"],
                    chunk["content"],
                    " ".join(tags),
                ]
            ).lower()
            conn.execute(
                """
                INSERT INTO doc_chunks(
                    doc_id, chunk_index, heading, content, search_text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (doc_id, index, chunk["heading"], chunk["content"], search_text, now),
            )
        return {
            "doc_id": doc_id,
            "doc_key": doc_key,
            "app_scope": app_scope,
            "title": title,
            "source_type": source_type,
            "source_path": source_path,
            "chunk_count": len(chunks),
            "content_hash": content_hash,
            "created_at": created_at,
            "updated_at": now,
        }

    def doc_index_repo(self, args: dict[str, Any]) -> dict[str, Any]:
        app_scope = self.validate_app_scope(optional_string(args, "app_scope", default=""))
        include_manifests = bool_arg(args, "include_manifests", default=True)
        verbose = bool_arg(args, "verbose", default=False)
        scopes = [app_scope] if app_scope else list(self.scope_configs)
        indexed: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        with self.connection() as conn:
            for scope in scopes:
                config = self.scope_configs[scope]
                for pattern in config["doc_paths"]:
                    paths = iter_doc_paths(self.repo_root, pattern)
                    if not paths:
                        missing.append({"app_scope": scope, "pattern": pattern})
                    for path in paths:
                        rel = path.relative_to(self.repo_root).as_posix()
                        try:
                            content = read_repo_text(path)
                        except OSError as exc:
                            missing.append({"app_scope": scope, "pattern": rel, "error": str(exc)})
                            continue
                        indexed.append(
                            self.store_doc(
                                conn,
                                doc_key=f"{scope}:{rel}",
                                app_scope=scope,
                                title=title_from_path(path.relative_to(self.repo_root)),
                                content=content,
                                source_type="file",
                                source_path=rel,
                                summary=f"Indexed repository documentation file `{rel}`.",
                                tags=[scope, "repo-doc"],
                            )
                        )
                if include_manifests:
                    content = build_scope_manifest(self.repo_root, scope, config)
                    indexed.append(
                        self.store_doc(
                            conn,
                            doc_key=f"{scope}:source-manifest",
                            app_scope=scope,
                            title=f"Source Manifest: {config['title']}",
                            content=content,
                            source_type="inferred",
                            source_path=";".join(config["roots"]),
                            summary="Generated searchable manifest of likely source files and symbols.",
                            tags=[scope, "source-manifest", "inferred"],
                        )
                    )
            conn.commit()
        by_scope: dict[str, dict[str, int]] = {}
        for item in indexed:
            entry = by_scope.setdefault(item["app_scope"], {"doc_count": 0, "chunk_count": 0})
            entry["doc_count"] += 1
            entry["chunk_count"] += item["chunk_count"]
        result = {
            "indexed_count": len(indexed),
            "indexed_chunks": sum(item["chunk_count"] for item in indexed),
            "scopes": scopes,
            "by_scope": by_scope,
            "missing": missing,
        }
        if verbose:
            result["indexed"] = indexed
        return result

    def ensure_docs_indexed(self) -> None:
        with self.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM doc_entries").fetchone()[0]
        if count == 0:
            self.doc_index_repo({"include_manifests": True})

    def doc_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = require_string(args, "query", max_length=500)
        terms = tokenize_query(query)
        if not terms:
            raise ToolExecutionError("`query` must include at least one searchable term.")
        app_scope = self.validate_app_scope(optional_string(args, "app_scope", default=""))
        source_type = optional_string(args, "source_type", default="")
        if source_type and source_type not in DOC_SOURCE_TYPES:
            raise ToolExecutionError(
                "`source_type` must be one of: " + ", ".join(sorted(DOC_SOURCE_TYPES)) + "."
            )
        limit = clamp_limit(args.get("limit"), default=10, maximum=DOC_SEARCH_MAX_LIMIT)
        self.ensure_docs_indexed()
        where = ["(" + " OR ".join(["c.search_text LIKE ?"] * len(terms)) + ")"]
        params: list[Any] = [f"%{term}%" for term in terms]
        if app_scope:
            where.append("e.app_scope = ?")
            params.append(app_scope)
        if source_type:
            where.append("e.source_type = ?")
            params.append(source_type)
        sql = f"""
            SELECT c.id AS chunk_id, c.chunk_index, c.heading, c.content,
                   c.search_text, e.doc_key, e.app_scope, e.title, e.source_type,
                   e.source_path, e.summary, e.updated_at
            FROM doc_chunks c
            JOIN doc_entries e ON e.id = c.doc_id
            WHERE {' AND '.join(where)}
            LIMIT 1000
        """
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        scored: list[tuple[int, dict[str, Any]]] = []
        phrase = query.lower()
        for row in rows:
            item = dict(row)
            search_text = item.pop("search_text")
            haystack = search_text.lower()
            title = item["title"].lower()
            heading = item["heading"].lower()
            source_path = item["source_path"].lower()
            score = 0
            for term in terms:
                score += haystack.count(term)
                score += title.count(term) * 4
                score += heading.count(term) * 3
                score += source_path.count(term) * 3
            if phrase in haystack:
                score += 8
            if score <= 0:
                continue
            item["score"] = score
            item["snippet"] = make_snippet(item["content"], terms)
            item.pop("content")
            scored.append((score, item))
        scored.sort(
            key=lambda pair: (
                -pair[0],
                pair[1]["app_scope"],
                pair[1]["source_type"],
                pair[1]["doc_key"],
                pair[1]["chunk_index"],
            )
        )
        deduped: list[dict[str, Any]] = []
        seen_doc_keys: set[str] = set()
        for _, item in scored:
            if item["doc_key"] in seen_doc_keys:
                continue
            seen_doc_keys.add(item["doc_key"])
            deduped.append(item)
        results = deduped[:limit]
        return {
            "query": query,
            "app_scope": app_scope,
            "source_type": source_type or None,
            "result_count": len(results),
            "results": results,
            "hint": "Use mudra_doc_get with doc_key or chunk_id for full targeted content.",
        }

    def doc_get(self, args: dict[str, Any]) -> dict[str, Any]:
        doc_key = optional_string(args, "doc_key", default="", max_length=300)
        chunk_id = args.get("chunk_id")
        max_chars = clamp_limit(
            args.get("max_chars"),
            default=DOC_FETCH_DEFAULT_CHARS,
            maximum=DOC_FETCH_MAX_CHARS,
        )
        if max_chars < 500:
            max_chars = 500
        if not doc_key and chunk_id is None:
            raise ToolExecutionError("Provide either `doc_key` or `chunk_id`.")
        self.ensure_docs_indexed()
        with self.connection() as conn:
            if chunk_id is not None:
                try:
                    chunk_id_int = int(chunk_id)
                except (TypeError, ValueError):
                    raise ToolExecutionError("`chunk_id` must be an integer.") from None
                row = conn.execute(
                    """
                    SELECT c.id AS chunk_id, c.chunk_index, c.heading, c.content,
                           e.doc_key, e.app_scope, e.title, e.source_type,
                           e.source_path, e.summary, e.updated_at
                    FROM doc_chunks c
                    JOIN doc_entries e ON e.id = c.doc_id
                    WHERE c.id = ?
                    """,
                    (chunk_id_int,),
                ).fetchone()
                if not row:
                    return {"found": False, "chunk_id": chunk_id_int}
                item = dict(row)
                content = item["content"]
                item["content"] = content[:max_chars]
                item["truncated"] = len(content) > max_chars
                return {"found": True, "document": item}

            normalized_key = safe_doc_key(doc_key)
            row = conn.execute(
                "SELECT * FROM doc_entries WHERE doc_key = ?", (normalized_key,)
            ).fetchone()
            if not row:
                return {"found": False, "doc_key": normalized_key}
            chunks = conn.execute(
                """
                SELECT id AS chunk_id, chunk_index, heading, content
                FROM doc_chunks WHERE doc_id = ? ORDER BY chunk_index
                """,
                (row["id"],),
            ).fetchall()
        content_parts = []
        chunk_refs = []
        for chunk in chunks:
            chunk_refs.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "chunk_index": chunk["chunk_index"],
                    "heading": chunk["heading"],
                }
            )
            heading = chunk["heading"]
            content_parts.append(
                f"## {heading}\n\n{chunk['content']}" if heading else chunk["content"]
            )
        content = "\n\n".join(content_parts).strip()
        doc = dict(row)
        doc["tags"] = json.loads(doc.pop("tags_json"))
        doc.pop("id")
        doc["chunks"] = chunk_refs
        doc["content"] = content[:max_chars]
        doc["truncated"] = len(content) > max_chars
        return {"found": True, "document": doc}

    def doc_upsert(self, args: dict[str, Any]) -> dict[str, Any]:
        doc_key = require_string(args, "doc_key", max_length=300)
        app_scope = self.validate_app_scope(require_string(args, "app_scope", max_length=80))
        title = require_string(args, "title", max_length=200)
        content = require_string(args, "content", max_length=200_000)
        summary = optional_string(args, "summary", default="", max_length=4000)
        source_type = optional_string(args, "source_type", default="manual", max_length=20)
        if source_type not in {"manual", "inferred"}:
            raise ToolExecutionError("`source_type` for doc_upsert must be manual or inferred.")
        source_path = optional_string(args, "source_path", default="", max_length=1000)
        tags_value = args.get("tags", [])
        if tags_value is None:
            tags_value = []
        if not isinstance(tags_value, list) or not all(isinstance(tag, str) for tag in tags_value):
            raise ToolExecutionError("`tags` must be an array of strings when provided.")
        tags = [tag.strip()[:80] for tag in tags_value if tag.strip()]
        with self.connection() as conn:
            stored = self.store_doc(
                conn,
                doc_key=doc_key,
                app_scope=app_scope or "",
                title=title,
                content=content,
                source_type=source_type,
                source_path=source_path,
                summary=summary,
                tags=tags,
            )
            conn.commit()
        return {"document": stored}

    def docs_drift_audit(self, app_scope: str | None = None) -> dict[str, Any]:
        app_scope = self.validate_app_scope(app_scope) or self.doc_drift_audit_scope
        config = self.scope_configs[app_scope]
        with self.connection() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT doc_key, source_type, source_path, indexed_at, updated_at
                    FROM doc_entries WHERE app_scope = ? ORDER BY source_path, doc_key
                    """,
                    (app_scope,),
                ).fetchall()
            ]
        indexed_file_docs = {
            (row.get("source_path") or "").replace("\\", "/"): row
            for row in rows
            if row.get("source_type") == "file" and row.get("source_path")
        }
        latest_indexed_at = max(
            (row.get("indexed_at") or row.get("updated_at") or "" for row in rows),
            default="",
        )
        expected_paths: dict[str, Path] = {}
        for pattern in config.get("doc_paths", []):
            for path in iter_doc_paths(self.repo_root, pattern):
                expected_paths[repo_relative_posix(path, self.repo_root)] = path
        audit_pathspecs = list(
            dict.fromkeys(
                [
                    *config.get("roots", []),
                    *[pattern for pattern in config.get("doc_paths", []) if "*" not in pattern],
                ]
            )
        )
        git_status = git_status_for_paths(self.repo_root, audit_pathspecs)
        dirty_paths = sorted(git_status)
        code_roots = [
            root for root in config.get("roots", []) if not root.startswith("docs/") and root != "docs"
        ]
        dirty_code_paths = [rel for rel in dirty_paths if any(path_is_under(rel, root) for root in code_roots)]
        dirty_doc_paths = [
            rel
            for rel in dirty_paths
            if rel in expected_paths
            and (rel.lower().endswith((".md", ".dot")) or rel.lower() in {"agents.md", "readme.md"})
        ]
        advisories: list[dict[str, Any]] = []
        if not rows:
            advisories.append(
                {
                    "level": "warning",
                    "type": "empty_index",
                    "message": f"No indexed documentation entries exist for `{app_scope}`; run a docs reindex before relying on search results.",
                    "suggestion": f"python -m mcp.server --index-docs --doc-scope {app_scope}",
                }
            )
        for rel, path in sorted(expected_paths.items()):
            row = indexed_file_docs.get(rel)
            status = git_status.get(rel, "")
            if not row:
                advisories.append(
                    {
                        "level": "warning",
                        "type": "missing_index_entry",
                        "path": rel,
                        "git_status": status,
                        "message": f"`{rel}` is covered by `{app_scope}` doc paths but has no indexed file-document entry.",
                        "suggestion": f"Reindex `{app_scope}` and confirm `mudra_doc_search` can find it.",
                    }
                )
                continue
            indexed_at = row.get("indexed_at") or row.get("updated_at") or ""
            indexed_dt = parse_utc_timestamp(indexed_at)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            source_dt = datetime.fromtimestamp(mtime, timezone.utc)
            if indexed_dt and source_dt > indexed_dt + timedelta(seconds=DOC_DRIFT_TIMESTAMP_SKEW_SECONDS):
                advisories.append(
                    {
                        "level": "warning",
                        "type": "stale_index_entry",
                        "path": rel,
                        "doc_key": row.get("doc_key", ""),
                        "indexed_at": indexed_at,
                        "source_mtime": utc_timestamp_from_mtime(mtime),
                        "git_status": status,
                        "message": f"`{rel}` changed after its indexed documentation entry.",
                        "suggestion": f"Reindex `{app_scope}` after checking whether source docs also need an update.",
                    }
                )
        indexed_paths = set(indexed_file_docs)
        relevant_extensions = {".bat", ".css", ".dot", ".html", ".js", ".md", ".py"}
        for rel in dirty_paths:
            status = git_status.get(rel, "")
            if "D" in status and rel in indexed_file_docs:
                row = indexed_file_docs[rel]
                advisories.append(
                    {
                        "level": "warning",
                        "type": "deleted_indexed_source",
                        "path": rel,
                        "doc_key": row.get("doc_key", ""),
                        "indexed_at": row.get("indexed_at") or row.get("updated_at") or "",
                        "git_status": status,
                        "message": f"`{rel}` is deleted locally but still has an indexed doc entry.",
                        "suggestion": f"Reindex `{app_scope}` after the deletion is settled.",
                    }
                )
                continue
            path = self.repo_root / rel
            if rel not in expected_paths and rel not in indexed_paths and path.is_file() and path.suffix.lower() in relevant_extensions and any(path_is_under(rel, root) for root in audit_pathspecs):
                advisories.append(
                    {
                        "level": "info",
                        "type": "unindexed_changed_path",
                        "path": rel,
                        "git_status": status,
                        "message": f"`{rel}` changed under the `{app_scope}` audit paths but is not indexed as a file document.",
                        "suggestion": "Update `mcp/projects/mudra.py` if this path should be searchable as source documentation.",
                    }
                )
        if dirty_code_paths and not dirty_doc_paths:
            advisories.append(
                {
                    "level": "info",
                    "type": "docs_update_review",
                    "paths": dirty_code_paths[:DOC_DRIFT_AUDIT_MAX_ITEMS],
                    "message": "MCP code or dashboard files are changed with no changed MCP source docs detected in this worktree.",
                    "suggestion": "Review whether `docs/mcp-server`, `README.md`, or `AGENTS.md` needs a behavior/workflow update before checkout.",
                }
            )
        advisories.sort(key=lambda item: ({"warning": 0, "info": 1}.get(str(item.get("level")), 2), str(item.get("path") or item.get("type") or "")))
        truncated_count = max(0, len(advisories) - DOC_DRIFT_AUDIT_MAX_ITEMS)
        shown_advisories = advisories[:DOC_DRIFT_AUDIT_MAX_ITEMS]
        affected_paths: list[str] = []
        doc_keys: list[str] = []
        for item in shown_advisories:
            for path in [item.get("path"), *item.get("paths", [])]:
                if path and path not in affected_paths:
                    affected_paths.append(path)
            doc_key = item.get("doc_key")
            if doc_key and doc_key not in doc_keys:
                doc_keys.append(doc_key)
        suggested_todos: list[dict[str, Any]] = []
        if advisories:
            suggested_todos.append(
                {
                    "todo_key": f"{app_scope}-refresh-docs-after-drift",
                    "app_scope": app_scope,
                    "priority": "P3",
                    "title": "Refresh MCP docs after local drift audit warnings",
                    "detail": f"Docs drift audit found {len(advisories)} advisory item(s). Reindex `{app_scope}`, verify focused search, and update source docs if behavior or workflow changed.",
                    "code_paths": affected_paths[:DOC_DRIFT_AUDIT_MAX_ITEMS],
                    "doc_keys": doc_keys[:DOC_DRIFT_AUDIT_MAX_ITEMS],
                    "symbol_refs": ["docs_drift_audit", "doc_index_repo", "ProjectDescriptor.doc_scopes"],
                    "route_refs": ["GET /api/dashboard", "GET /docs/scopes", "POST /docs/index", "mudra_doc_index_repo"],
                    "test_refs": [
                        f"python -m mcp.server --index-docs --doc-scope {app_scope}",
                        f"python -m mcp.server --doc-search {app_scope} --query docs drift audit",
                    ],
                }
            )
        return {
            "app_scope": app_scope,
            "generated_at": utc_now(),
            "latest_indexed_at": latest_indexed_at,
            "is_stale": bool(advisories),
            "advisory_count": len(advisories),
            "truncated_count": truncated_count,
            "git_status_count": len(git_status),
            "advisories": shown_advisories,
            "suggested_todos": suggested_todos,
        }

    def doc_scopes_markdown(self) -> str:
        result = self.doc_scopes()
        lines = ["# Mudra Documentation Scopes", ""]
        for scope in result["scopes"]:
            roots = ", ".join(f"`{root}`" for root in scope["root_paths"])
            lines.extend(
                [
                    f"## {scope['scope']}",
                    "",
                    scope["description"],
                    "",
                    f"- Title: {scope['title']}",
                    f"- Roots: {roots}",
                    f"- Indexed docs: {scope['doc_count']}",
                    f"- Indexed chunks: {scope['chunk_count']}",
                    "",
                ]
            )
        lines.append("Use `mudra_doc_search` with `app_scope` for focused retrieval before fetching full docs.")
        return "\n".join(lines).rstrip()

    def docs_scope_markdown(self, app_scope: str) -> str:
        result = self.doc_scope_entries(app_scope)
        app_scope = result["app_scope"]
        rows = result["documents"]
        config = self.scope_configs[app_scope]
        lines = [f"# Documentation: {config['title']}", "", config["description"], ""]
        if not rows:
            lines.append("_No documentation indexed for this scope._")
            return "\n".join(lines)
        for row in rows:
            source = row["source_path"] or row["source_type"]
            lines.extend(
                [
                    f"## {row['title']}",
                    "",
                    f"- Doc key: `{row['doc_key']}`",
                    f"- Source: `{source}`",
                    f"- Type: `{row['source_type']}`",
                    f"- Chunks: {row['chunk_count']}",
                    f"- Updated: `{row['updated_at']}`",
                ]
            )
            if row["summary"]:
                lines.extend(["", row["summary"]])
            lines.append("")
        return "\n".join(lines).rstrip()

    def docs_search_markdown(self, query: str) -> str:
        result = self.doc_search({"query": query, "limit": 10})
        lines = [f"# Documentation Search: {query}", ""]
        if not result["results"]:
            lines.append("_No matching documentation chunks._")
            return "\n".join(lines)
        for item in result["results"]:
            lines.extend(
                [
                    f"## {item['title']}",
                    "",
                    f"- Scope: `{item['app_scope']}`",
                    f"- Doc key: `{item['doc_key']}`",
                    f"- Chunk id: `{item['chunk_id']}`",
                    f"- Heading: {item['heading']}",
                    "",
                    item["snippet"],
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    def doc_markdown(self, doc_key: str) -> str:
        result = self.doc_get({"doc_key": doc_key, "max_chars": DOC_FETCH_DEFAULT_CHARS})
        if not result["found"]:
            return f"# Mudra Documentation\n\nNo document found for `{doc_key}`."
        doc = result["document"]
        lines = [
            f"# {doc['title']}",
            "",
            f"- Scope: `{doc['app_scope']}`",
            f"- Doc key: `{doc['doc_key']}`",
            f"- Source: `{doc['source_path'] or doc['source_type']}`",
            f"- Type: `{doc['source_type']}`",
            f"- Truncated: `{doc['truncated']}`",
            "",
            doc["content"],
        ]
        return "\n".join(lines).rstrip()
