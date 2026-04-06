"""Global project registry.

Single SQLite DB at ~/.local/share/neurodex/registry.db
Tracks all indexed repos, workspaces, and path-to-repo mappings.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from neurodex.config import EngramConfig


@dataclass
class RepoRecord:
    id: str
    name: str
    git_remote: str | None
    local_paths: list[str]
    last_indexed: float
    chunk_count: int
    file_count: int


@dataclass
class WorkspaceRecord:
    id: str
    name: str
    repo_ids: list[str]
    created_at: float


class Registry:
    """Global registry of all indexed repos and workspaces."""

    def __init__(self, config: EngramConfig) -> None:
        self._config = config
        config.ensure_dirs()
        self._db_path = config.registry_db_path
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS repos (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                git_remote TEXT,
                local_paths TEXT NOT NULL DEFAULT '[]',
                last_indexed REAL NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                repo_ids TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS path_mappings (
                local_path TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                workspace_id TEXT,
                FOREIGN KEY (repo_id) REFERENCES repos(id)
            );
        """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert_repo(
        self,
        repo_id: str,
        name: str,
        git_remote: str | None,
        local_path: str,
    ) -> RepoRecord:
        """Register or update a repo. Adds local_path to known paths."""
        existing = self.get_repo(repo_id)
        if existing:
            paths = set(existing.local_paths)
            paths.add(local_path)
            self._conn.execute(
                "UPDATE repos SET name=?, git_remote=?, local_paths=? WHERE id=?",
                (name, git_remote, json.dumps(sorted(paths)), repo_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO repos (id, name, git_remote, local_paths) VALUES (?, ?, ?, ?)",
                (repo_id, name, git_remote, json.dumps([local_path])),
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO path_mappings (local_path, repo_id) VALUES (?, ?)",
            (local_path, repo_id),
        )
        self._conn.commit()
        return self.get_repo(repo_id)  # type: ignore[return-value]

    def get_repo(self, repo_id: str) -> RepoRecord | None:
        row = self._conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
        if not row:
            return None
        return RepoRecord(
            id=row["id"],
            name=row["name"],
            git_remote=row["git_remote"],
            local_paths=json.loads(row["local_paths"]),
            last_indexed=row["last_indexed"],
            chunk_count=row["chunk_count"],
            file_count=row["file_count"],
        )

    def update_repo_stats(
        self, repo_id: str, chunk_count: int, file_count: int
    ) -> None:
        self._conn.execute(
            "UPDATE repos SET chunk_count=?, file_count=?, last_indexed=? WHERE id=?",
            (chunk_count, file_count, time.time(), repo_id),
        )
        self._conn.commit()

    def list_repos(self) -> list[RepoRecord]:
        rows = self._conn.execute("SELECT * FROM repos ORDER BY name").fetchall()
        return [
            RepoRecord(
                id=row["id"],
                name=row["name"],
                git_remote=row["git_remote"],
                local_paths=json.loads(row["local_paths"]),
                last_indexed=row["last_indexed"],
                chunk_count=row["chunk_count"],
                file_count=row["file_count"],
            )
            for row in rows
        ]

    def find_repo_by_path(self, local_path: str) -> str | None:
        """Find repo_id for a given local path."""
        row = self._conn.execute(
            "SELECT repo_id FROM path_mappings WHERE local_path=?", (local_path,)
        ).fetchone()
        return row["repo_id"] if row else None

    def delete_repo(self, repo_id: str) -> None:
        self._conn.execute("DELETE FROM path_mappings WHERE repo_id=?", (repo_id,))
        self._conn.execute("DELETE FROM repos WHERE id=?", (repo_id,))
        for workspace in self.list_workspaces():
            if repo_id in workspace.repo_ids:
                new_ids = [rid for rid in workspace.repo_ids if rid != repo_id]
                self._conn.execute(
                    "UPDATE workspaces SET repo_ids=? WHERE id=?",
                    (json.dumps(new_ids), workspace.id),
                )
        self._conn.commit()
        db_path = self._config.repo_db_path(repo_id)
        if db_path.exists():
            db_path.unlink()

    def create_workspace(self, name: str, repo_ids: list[str] | None = None) -> WorkspaceRecord:
        workspace_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._conn.execute(
            "INSERT INTO workspaces (id, name, repo_ids, created_at) VALUES (?, ?, ?, ?)",
            (workspace_id, name, json.dumps(repo_ids or []), now),
        )
        for rid in (repo_ids or []):
            self._conn.execute(
                "UPDATE path_mappings SET workspace_id=? WHERE repo_id=?",
                (workspace_id, rid),
            )
        self._conn.commit()
        return WorkspaceRecord(id=workspace_id, name=name, repo_ids=repo_ids or [], created_at=now)

    def add_repo_to_workspace(self, workspace_name: str, repo_id: str) -> WorkspaceRecord:
        workspace = self.get_workspace(workspace_name)
        if not workspace:
            raise ValueError(f"Workspace '{workspace_name}' not found")
        if repo_id not in workspace.repo_ids:
            workspace.repo_ids.append(repo_id)
            self._conn.execute(
                "UPDATE workspaces SET repo_ids=? WHERE id=?",
                (json.dumps(workspace.repo_ids), workspace.id),
            )
            self._conn.execute(
                "UPDATE path_mappings SET workspace_id=? WHERE repo_id=?",
                (workspace.id, repo_id),
            )
            self._conn.commit()
        return workspace

    def get_workspace(self, name: str) -> WorkspaceRecord | None:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        return WorkspaceRecord(
            id=row["id"],
            name=row["name"],
            repo_ids=json.loads(row["repo_ids"]),
            created_at=row["created_at"],
        )

    def get_workspace_by_id(self, workspace_id: str) -> WorkspaceRecord | None:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        if not row:
            return None
        return WorkspaceRecord(
            id=row["id"],
            name=row["name"],
            repo_ids=json.loads(row["repo_ids"]),
            created_at=row["created_at"],
        )

    def list_workspaces(self) -> list[WorkspaceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM workspaces ORDER BY name"
        ).fetchall()
        return [
            WorkspaceRecord(
                id=row["id"],
                name=row["name"],
                repo_ids=json.loads(row["repo_ids"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def delete_workspace(self, name: str) -> None:
        workspace = self.get_workspace(name)
        if workspace:
            self._conn.execute(
                "UPDATE path_mappings SET workspace_id=NULL WHERE workspace_id=?",
                (workspace.id,),
            )
            self._conn.execute("DELETE FROM workspaces WHERE id=?", (workspace.id,))
            self._conn.commit()

    def resolve_context(self, cwd: str) -> dict:
        """Resolve which repo/workspace to use based on current working directory.

        Returns a dict with:
        - status: "resolved" | "disambiguation_needed" | "unindexed"
        - repo_id, workspace, etc. depending on status
        """
        repo_id = self.find_repo_by_path(cwd)
        if repo_id:
            repo = self.get_repo(repo_id)
            for workspace in self.list_workspaces():
                if repo_id in workspace.repo_ids:
                    return {
                        "status": "resolved",
                        "repo_id": repo_id,
                        "repo_name": repo.name if repo else None,
                        "workspace": workspace.name,
                        "workspace_repo_ids": workspace.repo_ids,
                    }
            return {
                "status": "resolved",
                "repo_id": repo_id,
                "repo_name": repo.name if repo else None,
                "workspace": None,
            }

        for repo in self.list_repos():
            for known_path in repo.local_paths:
                if cwd.startswith(known_path):
                    return {
                        "status": "resolved",
                        "repo_id": repo.id,
                        "repo_name": repo.name,
                        "workspace": None,
                    }

        repos = self.list_repos()
        workspaces = self.list_workspaces()
        if not repos:
            return {
                "status": "unindexed",
                "message": "No projects indexed yet. Run `neurodex init` to index the current directory.",
            }

        return {
            "status": "disambiguation_needed",
            "message": "Current directory is not indexed. Choose an existing project or create a new index.",
            "options": [
                {
                    "id": record.id,
                    "name": record.name,
                    "paths": record.local_paths,
                    "git_remote": record.git_remote,
                    "chunks": record.chunk_count,
                }
                for record in repos
            ],
            "workspaces": [
                {"name": workspace.name, "repos": workspace.repo_ids}
                for workspace in workspaces
            ],
        }

    def suggest_workspace(self, repo_id: str) -> list[RepoRecord]:
        """Find repos that might belong in the same workspace.

        Heuristic: repos in the same parent directory or same git org.
        """
        repo = self.get_repo(repo_id)
        if not repo:
            return []

        suggestions = []
        for other in self.list_repos():
            if other.id == repo_id:
                continue

            for my_path in repo.local_paths:
                for their_path in other.local_paths:
                    if Path(my_path).parent == Path(their_path).parent:
                        suggestions.append(other)
                        break

            if repo.git_remote and other.git_remote:
                my_org = _extract_org(repo.git_remote)
                their_org = _extract_org(other.git_remote)
                if my_org and their_org and my_org == their_org:
                    if other not in suggestions:
                        suggestions.append(other)

        return suggestions


def _extract_org(remote_url: str) -> str | None:
    """Extract org/owner from a git remote URL."""
    if ":" in remote_url and "@" in remote_url:
        path_part = remote_url.split(":")[-1]
        parts = path_part.strip("/").split("/")
        return parts[0] if parts else None
    parts = remote_url.rstrip("/").split("/")
    if len(parts) >= 2:
        return parts[-2]
    return None
