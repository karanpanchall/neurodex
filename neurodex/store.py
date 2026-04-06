"""Per-repo SQLite FTS5 storage.

Each repo gets its own SQLite DB with:
- chunks table: stores code/doc/insight chunks with metadata
- chunks_fts: FTS5 virtual table for BM25 full-text search
- meta: key-value store for repo-level metadata
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Chunk:
    id: str
    file_path: str
    file_hash: str
    chunk_type: str
    symbol_name: str | None
    symbol_type: str | None
    language: str | None
    content: str
    summary: str | None
    line_start: int | None
    line_end: int | None
    indexed_at: float
    last_modified: float


@dataclass
class SearchResult:
    chunk: Chunk
    bm25_score: float
    repo_id: str
    repo_name: str


def _check_fts5(conn: sqlite3.Connection) -> bool:
    """Check if FTS5 is available in this SQLite build."""
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_test USING fts5(x)")
        conn.execute("DROP TABLE _fts5_test")
        return True
    except sqlite3.OperationalError:
        return False


class RepoStore:
    """SQLite FTS5 store for a single repo's indexed data."""

    def __init__(self, db_path: Path, repo_id: str, repo_name: str) -> None:
        self.repo_id = repo_id
        self.repo_name = repo_name
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._has_fts5 = _check_fts5(self._conn)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                chunk_type TEXT NOT NULL,
                symbol_name TEXT,
                symbol_type TEXT,
                language TEXT,
                content TEXT NOT NULL,
                summary TEXT,
                line_start INTEGER,
                line_end INTEGER,
                indexed_at REAL NOT NULL,
                last_modified REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_file_path ON chunks(file_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_symbol_name ON chunks(symbol_name);
            CREATE INDEX IF NOT EXISTS idx_chunks_chunk_type ON chunks(chunk_type);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS nodes (
                qualified_name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                file_path TEXT NOT NULL,
                name TEXT NOT NULL,
                parent_name TEXT,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                signature TEXT,
                params TEXT,
                return_type TEXT,
                language TEXT,
                is_test INTEGER DEFAULT 0,
                extra TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
            CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
            CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
            CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_name);

            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_symbol TEXT,
                target_file TEXT,
                target_symbol TEXT NOT NULL,
                line INTEGER,
                UNIQUE(kind, source_file, source_symbol, target_symbol)
            );

            CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_file);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_symbol);
            CREATE INDEX IF NOT EXISTS idx_edges_target_file ON edges(target_file);

            CREATE VIEW IF NOT EXISTS relationships AS
                SELECT source_file, target_symbol AS target_module, kind AS relationship_type
                FROM edges WHERE kind = 'IMPORTS';
        """)

        if self._has_fts5:
            exists = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
            ).fetchone()
            if not exists:
                self._conn.executescript("""
                    CREATE VIRTUAL TABLE chunks_fts USING fts5(
                        content, summary, symbol_name, file_path,
                        content='chunks', content_rowid='rowid'
                    );

                    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                        INSERT INTO chunks_fts(rowid, content, summary, symbol_name, file_path)
                        VALUES (new.rowid, new.content, new.summary, new.symbol_name, new.file_path);
                    END;

                    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, content, summary, symbol_name, file_path)
                        VALUES ('delete', old.rowid, old.content, old.summary, old.symbol_name, old.file_path);
                    END;

                    CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, content, summary, symbol_name, file_path)
                        VALUES ('delete', old.rowid, old.content, old.summary, old.symbol_name, old.file_path);
                        INSERT INTO chunks_fts(rowid, content, summary, symbol_name, file_path)
                        VALUES (new.rowid, new.content, new.summary, new.symbol_name, new.file_path);
                    END;
                """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add_chunks(self, chunks: list[Chunk]) -> int:
        """Add chunks in a batch. Returns number inserted."""
        if not chunks:
            return 0
        now = time.time()
        inserted = 0
        with self._conn:
            for chunk in chunks:
                self._conn.execute(
                    """INSERT OR REPLACE INTO chunks
                    (id, file_path, file_hash, chunk_type, symbol_name, symbol_type,
                     language, content, summary, line_start, line_end, indexed_at, last_modified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk.id, chunk.file_path, chunk.file_hash, chunk.chunk_type,
                        chunk.symbol_name, chunk.symbol_type, chunk.language,
                        chunk.content, chunk.summary, chunk.line_start, chunk.line_end,
                        now, chunk.last_modified,
                    ),
                )
                inserted += 1
        return inserted

    def remove_by_file(self, file_path: str) -> int:
        """Remove all chunks for a file. Returns count removed."""
        cursor = self._conn.execute(
            "DELETE FROM chunks WHERE file_path=?", (file_path,)
        )
        self._conn.commit()
        return cursor.rowcount

    def remove_by_id(self, chunk_id: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE id=?", (chunk_id,))
        self._conn.commit()

    def get_file_hash(self, file_path: str) -> str | None:
        """Get the stored hash for a file (to detect changes)."""
        row = self._conn.execute(
            "SELECT file_hash FROM chunks WHERE file_path=? LIMIT 1", (file_path,)
        ).fetchone()
        return row["file_hash"] if row else None

    def get_all_file_paths(self) -> set[str]:
        """Get all unique file paths in the store."""
        rows = self._conn.execute(
            "SELECT DISTINCT file_path FROM chunks"
        ).fetchall()
        return {row["file_path"] for row in rows}

    def get_chunk_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM chunks").fetchone()
        return row["cnt"]

    def get_file_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT file_path) as cnt FROM chunks"
        ).fetchone()
        return row["cnt"]

    def search_bm25(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Full-text search using BM25 ranking.

        FTS5 BM25 scores are negative — more negative = more relevant.
        """
        if not self._has_fts5:
            return self._search_fallback(query, limit)

        safe_query = _escape_fts5_query(query)
        if not safe_query.strip():
            return []

        try:
            rows = self._conn.execute(
                """SELECT c.*, bm25(chunks_fts, 1.0, 2.0, 3.0, 1.5) as score
                FROM chunks_fts fts
                JOIN chunks c ON c.rowid = fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY bm25(chunks_fts, 1.0, 2.0, 3.0, 1.5)
                LIMIT ?""",
                (safe_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return self._search_fallback(query, limit)

        return [
            SearchResult(
                chunk=_row_to_chunk(row),
                bm25_score=row["score"],
                repo_id=self.repo_id,
                repo_name=self.repo_name,
            )
            for row in rows
        ]

    def search_symbols(self, pattern: str, limit: int = 50) -> list[Chunk]:
        """Search indexed symbols (function/class names) by pattern."""
        rows = self._conn.execute(
            """SELECT * FROM chunks
            WHERE symbol_name IS NOT NULL AND symbol_name LIKE ?
            ORDER BY symbol_name
            LIMIT ?""",
            (f"%{pattern}%", limit),
        ).fetchall()
        return [_row_to_chunk(row) for row in rows]

    def _search_fallback(self, query: str, limit: int) -> list[SearchResult]:
        """LIKE-based fallback when FTS5 is not available."""
        terms = query.split()
        if not terms:
            return []

        conditions = []
        params: list[str] = []
        for term in terms:
            conditions.append("(content LIKE ? OR summary LIKE ? OR symbol_name LIKE ?)")
            params.extend([f"%{term}%", f"%{term}%", f"%{term}%"])

        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM chunks WHERE {where} ORDER BY last_modified DESC LIMIT ?",
            (*params, limit),
        ).fetchall()

        return [
            SearchResult(
                chunk=_row_to_chunk(row),
                bm25_score=0.0,
                repo_id=self.repo_id,
                repo_name=self.repo_name,
            )
            for row in rows
        ]

    def add_nodes(self, file_path: str, nodes: list[dict]) -> int:
        """Add symbol-level nodes for a file. Replaces existing nodes for that file."""
        with self._conn:
            self._conn.execute("DELETE FROM nodes WHERE file_path=?", (file_path,))
            for node in nodes:
                self._conn.execute(
                    """INSERT OR REPLACE INTO nodes
                    (qualified_name, kind, file_path, name, parent_name,
                     line_start, line_end, signature, params, return_type,
                     language, is_test, extra)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        node["qualified_name"], node["kind"], file_path, node["name"],
                        node.get("parent_name"), node["line_start"], node["line_end"],
                        node.get("signature"), node.get("params"), node.get("return_type"),
                        node.get("language"), node.get("is_test", 0), node.get("extra"),
                    ),
                )
        return len(nodes)

    def get_nodes_in_file(self, file_path: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE file_path=? ORDER BY line_start",
            (file_path,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_nodes_at_lines(self, file_path: str, start: int, end: int) -> list[dict]:
        """Find nodes whose line range overlaps [start, end]."""
        rows = self._conn.execute(
            """SELECT * FROM nodes
            WHERE file_path=? AND line_start <= ? AND line_end >= ?
            ORDER BY line_start""",
            (file_path, end, start),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_node(self, qualified_name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE qualified_name=?", (qualified_name,),
        ).fetchone()
        return dict(row) if row else None

    def get_all_nodes(self, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM nodes WHERE kind=? ORDER BY file_path, line_start",
                (kind,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM nodes ORDER BY file_path, line_start"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_node_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM nodes").fetchone()
        return row["cnt"]

    def add_edges(self, source_file: str, edges: list[dict]) -> None:
        """Store typed edges for a file.

        Each edge dict has: kind, source_symbol (optional), target_file (optional),
        target_symbol, line (optional).
        Edge kinds: IMPORTS, CALLS, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON
        """
        with self._conn:
            self._conn.execute("DELETE FROM edges WHERE source_file=?", (source_file,))
            for edge in edges:
                self._conn.execute(
                    """INSERT OR IGNORE INTO edges
                    (kind, source_file, source_symbol, target_file, target_symbol, line)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (edge["kind"], source_file, edge.get("source_symbol"),
                     edge.get("target_file"), edge["target_symbol"], edge.get("line")),
                )

    def get_edges_from(self, file_path: str, kind: str | None = None) -> list[dict]:
        """Get edges originating from a file, optionally filtered by kind."""
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE source_file=? AND kind=?",
                (file_path, kind),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE source_file=?", (file_path,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_edges_to(self, target: str, kind: str | None = None) -> list[dict]:
        """Get edges pointing to a target (symbol or file), optionally filtered by kind."""
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE (target_symbol LIKE ? OR target_file LIKE ?) AND kind=?",
                (f"%{target}%", f"%{target}%", kind),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE target_symbol LIKE ? OR target_file LIKE ?",
                (f"%{target}%", f"%{target}%"),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_imports(self, file_path: str) -> list[str]:
        """Get modules imported by a file (convenience wrapper)."""
        rows = self._conn.execute(
            "SELECT target_symbol FROM edges WHERE source_file=? AND kind='IMPORTS'",
            (file_path,),
        ).fetchall()
        return [row["target_symbol"] for row in rows]

    def get_importers(self, module_name: str) -> list[str]:
        """Get files that import a given module."""
        rows = self._conn.execute(
            "SELECT source_file FROM edges WHERE target_symbol LIKE ? AND kind='IMPORTS'",
            (f"%{module_name}%",),
        ).fetchall()
        return [row["source_file"] for row in rows]

    def get_callers(self, symbol_name: str) -> list[dict]:
        """Get all call sites for a symbol."""
        rows = self._conn.execute(
            "SELECT source_file, source_symbol, line FROM edges WHERE target_symbol LIKE ? AND kind='CALLS'",
            (f"%{symbol_name}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_all_references(self, symbol_name: str) -> dict:
        """Find every place a symbol is referenced -- the cascade map.

        Searches through:
        1. Edges (IMPORTS, CALLS, INHERITS) -- structural references
        2. Chunk content -- text occurrences (catches type annotations, string refs, etc.)
        3. Nodes -- symbols with matching names

        Returns grouped results with exact file:line locations.
        """
        results: dict[str, list[dict]] = {
            "defined_in": [],
            "imported_by": [],
            "called_by": [],
            "inherited_by": [],
            "type_annotated_in": [],
            "referenced_in": [],
        }

        defined = self._conn.execute(
            "SELECT * FROM nodes WHERE name=?", (symbol_name,)
        ).fetchall()
        for definition in defined:
            definition_dict = dict(definition)
            results["defined_in"].append({
                "file": definition_dict["file_path"],
                "qualified": definition_dict["qualified_name"],
                "line_start": definition_dict["line_start"],
                "line_end": definition_dict["line_end"],
                "signature": definition_dict.get("signature", ""),
            })

        import_edges = self._conn.execute(
            "SELECT DISTINCT source_file FROM edges WHERE target_symbol LIKE ? AND kind='IMPORTS'",
            (f"%{symbol_name}%",),
        ).fetchall()
        for record in import_edges:
            results["imported_by"].append({"file": record["source_file"]})

        call_edges = self._conn.execute(
            "SELECT source_file, source_symbol, line FROM edges WHERE target_symbol LIKE ? AND kind='CALLS'",
            (f"%{symbol_name}%",),
        ).fetchall()
        for record in call_edges:
            results["called_by"].append(dict(record))

        inherit_edges = self._conn.execute(
            "SELECT source_file, source_symbol FROM edges WHERE target_symbol=? AND kind='INHERITS'",
            (symbol_name,),
        ).fetchall()
        for record in inherit_edges:
            results["inherited_by"].append(dict(record))

        if self._has_fts5:
            try:
                text_refs = self._conn.execute(
                    """SELECT DISTINCT c.file_path, c.line_start, c.line_end, c.symbol_name
                    FROM chunks_fts fts
                    JOIN chunks c ON c.rowid = fts.rowid
                    WHERE chunks_fts MATCH ?
                    LIMIT 50""",
                    (f'"{symbol_name}"',),
                ).fetchall()
            except Exception:
                text_refs = []
        else:
            text_refs = self._conn.execute(
                "SELECT DISTINCT file_path, line_start, line_end, symbol_name FROM chunks WHERE content LIKE ? LIMIT 50",
                (f"%{symbol_name}%",),
            ).fetchall()

        known_files = set()
        for category in ["defined_in", "imported_by", "called_by", "inherited_by"]:
            for item in results[category]:
                known_files.add(item.get("file", item.get("source_file", "")))

        for record in text_refs:
            ref_file = record["file_path"]
            if ref_file not in known_files and not ref_file.startswith("__"):
                results["referenced_in"].append({
                    "file": ref_file,
                    "lines": f"{record['line_start']}-{record['line_end']}",
                    "context": record["symbol_name"][:50] if record["symbol_name"] else "",
                })
                known_files.add(ref_file)

        total = sum(len(value) for value in results.values())
        unique_files = len(known_files)

        return {
            "symbol": symbol_name,
            "total_references": total,
            "unique_files": unique_files,
            **results,
        }

    def get_test_edges(self, file_path: str) -> list[dict]:
        """Get TESTED_BY edges for code in a file."""
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE (source_file=? OR target_file=?) AND kind='TESTED_BY'",
            (file_path, file_path),
        ).fetchall()
        return [dict(row) for row in rows]

    def impact_bfs(
        self,
        file_path: str,
        max_depth: int = 3,
        max_nodes: int = 50,
        changed_lines: tuple[int, int] | None = None,
    ) -> list[dict]:
        """Symbol-level bidirectional BFS for blast-radius analysis.

        If changed_lines is given, starts from the exact symbols at those lines.
        Otherwise starts from all symbols in the file.

        Returns affected symbols with distance, edge kind, and exact location.
        """
        if changed_lines:
            seeds = self.get_nodes_at_lines(file_path, changed_lines[0], changed_lines[1])
        else:
            seeds = self.get_nodes_in_file(file_path)

        if not seeds:
            return self._impact_bfs_file_level(file_path, max_depth, max_nodes)

        visited: set[str] = set()
        visited_files: set[str] = {file_path}
        queue: list[tuple[dict, int, str, str]] = []
        results: list[dict] = []

        seed_names = set()
        for seed in seeds:
            visited.add(seed["qualified_name"])
            seed_names.add(seed["name"])
            if seed.get("parent_name"):
                seed_names.add(seed["parent_name"])

        for seed_name in seed_names:
            forward_rows = [dict(row) for row in self._conn.execute(
                "SELECT * FROM edges WHERE source_file=? AND source_symbol=?",
                (file_path, seed_name),
            ).fetchall()]
            for row in forward_rows:
                target_nodes = self._resolve_edge_target(row["target_symbol"], row.get("target_file"))
                for target_node in target_nodes:
                    if target_node["qualified_name"] not in visited:
                        queue.append((target_node, 1, row["kind"], "forward"))

            reverse_rows = [dict(row) for row in self._conn.execute(
                "SELECT * FROM edges WHERE target_symbol LIKE ? AND source_file != ?",
                (f"%{seed_name}%", file_path),
            ).fetchall()]
            for row in reverse_rows:
                caller_nodes = self.get_nodes_in_file(row["source_file"])
                if row.get("line"):
                    precise = self.get_nodes_at_lines(row["source_file"], row["line"], row["line"])
                    if precise:
                        caller_nodes = precise
                for caller_node in caller_nodes[:3]:
                    if caller_node["qualified_name"] not in visited:
                        queue.append((caller_node, 1, row["kind"], "reverse"))

        while queue and len(results) < max_nodes:
            node, depth, via_kind, direction = queue.pop(0)
            qualified_name = node["qualified_name"]

            if qualified_name in visited:
                continue
            visited.add(qualified_name)

            node_file = node["file_path"]
            is_new_file = node_file not in visited_files
            visited_files.add(node_file)

            results.append({
                "file": node_file,
                "symbol": node["name"],
                "qualified_name": qualified_name,
                "kind": node["kind"],
                "line_start": node["line_start"],
                "line_end": node["line_end"],
                "signature": node.get("signature", ""),
                "distance": depth,
                "via": via_kind,
                "direction": direction,
                "is_new_file": is_new_file,
            })

            if depth >= max_depth:
                continue

            name = node["name"]
            parent = node.get("parent_name")
            search_names = [name]
            if parent:
                search_names.append(parent)

            for search_name in search_names:
                forward_edges = [dict(row) for row in self._conn.execute(
                    "SELECT * FROM edges WHERE source_file=? AND (source_symbol=? OR source_symbol IS NULL)",
                    (node_file, search_name),
                ).fetchall()]
                for row in forward_edges:
                    targets = self._resolve_edge_target(row["target_symbol"], row.get("target_file"))
                    for target in targets[:2]:
                        if target["qualified_name"] not in visited:
                            queue.append((target, depth + 1, row["kind"], direction))

                reverse_edges = [dict(row) for row in self._conn.execute(
                    "SELECT * FROM edges WHERE target_symbol LIKE ? AND source_file != ?",
                    (f"%{search_name}%", node_file),
                ).fetchall()]
                for row in reverse_edges[:5]:
                    callers = self.get_nodes_in_file(row["source_file"])
                    for caller in callers[:2]:
                        if caller["qualified_name"] not in visited:
                            queue.append((caller, depth + 1, row["kind"], "reverse"))

        return results

    def _resolve_edge_target(self, target_symbol: str, target_file: str | None) -> list[dict]:
        """Resolve an edge target to actual nodes in the database."""
        if target_file:
            matched_nodes = self._conn.execute(
                "SELECT * FROM nodes WHERE file_path=? AND name=?",
                (target_file, target_symbol.split(".")[-1]),
            ).fetchall()
            if matched_nodes:
                return [dict(node) for node in matched_nodes]

        name = target_symbol.split(".")[-1]
        matched_nodes = self._conn.execute(
            "SELECT * FROM nodes WHERE name=? LIMIT 3",
            (name,),
        ).fetchall()
        if matched_nodes:
            return [dict(node) for node in matched_nodes]

        module_path = target_symbol.replace(".", "/")
        matched_nodes = self._conn.execute(
            "SELECT * FROM nodes WHERE file_path LIKE ? LIMIT 3",
            (f"%{module_path}%",),
        ).fetchall()
        return [dict(node) for node in matched_nodes]

    def _impact_bfs_file_level(self, file_path: str, max_depth: int, max_nodes: int) -> list[dict]:
        """Fallback file-level BFS when no nodes exist."""
        visited: set[str] = {file_path}
        queue: list[tuple[str, int]] = [(file_path, 0)]
        results: list[dict] = []

        while queue and len(results) < max_nodes:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            forward = self._conn.execute(
                "SELECT DISTINCT target_file, target_symbol, kind FROM edges WHERE source_file=?",
                (current,),
            ).fetchall()

            file_stem = current.rsplit("/", 1)[-1].removesuffix(".py") if "/" in current else current
            reverse = self._conn.execute(
                "SELECT DISTINCT source_file, kind FROM edges WHERE target_symbol LIKE ?",
                (f"%{file_stem}%",),
            ).fetchall()

            for row in forward:
                target_file = row["target_file"]
                if target_file and target_file not in visited:
                    visited.add(target_file)
                    queue.append((target_file, depth + 1))
                    results.append({
                        "file": target_file, "distance": depth + 1,
                        "via": row["kind"], "direction": "forward",
                    })

            for row in reverse:
                source_file = row["source_file"]
                if source_file not in visited:
                    visited.add(source_file)
                    queue.append((source_file, depth + 1))
                    results.append({
                        "file": source_file, "distance": depth + 1,
                        "via": row["kind"], "direction": "reverse",
                    })

        return results

    def get_edge_stats(self) -> dict[str, int]:
        """Get count of edges by kind."""
        rows = self._conn.execute(
            "SELECT kind, COUNT(*) as cnt FROM edges GROUP BY kind"
        ).fetchall()
        return {row["kind"]: row["cnt"] for row in rows}

    def add_relationships(self, source_file: str, targets: list[str]) -> None:
        """Legacy wrapper -- converts imports to typed edges."""
        edges = [{"kind": "IMPORTS", "target_symbol": target} for target in targets]
        self.add_edges(source_file, edges)

    def trace_dependencies(self, file_path: str, depth: int = 2) -> dict:
        """Trace import chain from a file. Returns tree of dependencies."""
        visited: set[str] = set()

        def _trace(current_path: str, remaining_depth: int) -> dict:
            if remaining_depth <= 0 or current_path in visited:
                return {}
            visited.add(current_path)
            imports = self.get_imports(current_path)
            result = {}
            for imp in imports:
                matching = self._conn.execute(
                    "SELECT DISTINCT file_path FROM chunks WHERE file_path LIKE ?",
                    (f"%{imp.replace('.', '/')}%",),
                ).fetchall()
                for match in matching:
                    result[match["file_path"]] = _trace(match["file_path"], remaining_depth - 1)
                if not matching:
                    result[imp] = {}
            return result

        return {file_path: _trace(file_path, depth)}

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None


def make_chunk_id(file_path: str, line_start: int | None, symbol_name: str | None) -> str:
    """Deterministic chunk ID from file path + location."""
    parts = f"{file_path}:{line_start or 0}:{symbol_name or ''}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    return Chunk(
        id=row["id"],
        file_path=row["file_path"],
        file_hash=row["file_hash"],
        chunk_type=row["chunk_type"],
        symbol_name=row["symbol_name"],
        symbol_type=row["symbol_type"],
        language=row["language"],
        content=row["content"],
        summary=row["summary"],
        line_start=row["line_start"],
        line_end=row["line_end"],
        indexed_at=row["indexed_at"],
        last_modified=row["last_modified"],
    )


def _escape_fts5_query(query: str) -> str:
    """Turn a user query into a safe FTS5 query.

    FTS5 uses a specific query syntax. We convert plain words to
    an OR-joined query to be forgiving. Quoted phrases are passed through.
    """
    if any(op in query for op in ['"', "AND", "OR", "NOT", "NEAR"]):
        return query

    words = [word.strip() for word in query.split() if word.strip()]
    if not words:
        return ""

    escaped = " OR ".join(f'"{word}"' for word in words)
    return escaped
