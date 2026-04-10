"""Workspace management -- create, link, and resolve project groups.

A workspace groups multiple repos that should be searchable together.
Example: "MyApp" workspace contains frontend + backend + shared repos.
"""

from __future__ import annotations

from pathlib import Path

from neurodex.config import NeurodexConfig
from neurodex.project import RepoIdentity, detect_repo
from neurodex.registry import Registry, RepoRecord, WorkspaceRecord


class WorkspaceManager:
    """High-level workspace operations."""

    def __init__(self, registry: Registry, config: NeurodexConfig) -> None:
        self._registry = registry
        self._config = config

    def create_workspace(
        self, name: str, repo_paths: list[str | Path] | None = None
    ) -> WorkspaceRecord:
        """Create a workspace, optionally linking repos by their local paths."""
        repo_ids = []
        for path in (repo_paths or []):
            identity = detect_repo(Path(path))
            self._registry.upsert_repo(
                repo_id=identity.repo_id,
                name=identity.name,
                git_remote=identity.git_remote,
                local_path=str(identity.local_path),
            )
            repo_ids.append(identity.repo_id)

        return self._registry.create_workspace(name, repo_ids)

    def add_to_workspace(self, workspace_name: str, repo_path: str | Path) -> WorkspaceRecord:
        """Add a repo (by path) to an existing workspace."""
        identity = detect_repo(Path(repo_path))
        self._registry.upsert_repo(
            repo_id=identity.repo_id,
            name=identity.name,
            git_remote=identity.git_remote,
            local_path=str(identity.local_path),
        )
        return self._registry.add_repo_to_workspace(workspace_name, identity.repo_id)

    def list_workspaces(self) -> list[WorkspaceRecord]:
        return self._registry.list_workspaces()

    def get_workspace(self, name: str) -> WorkspaceRecord | None:
        return self._registry.get_workspace(name)

    def get_workspace_repos(self, name: str) -> list[RepoRecord]:
        """Get all repo records in a workspace."""
        workspace = self._registry.get_workspace(name)
        if not workspace:
            return []
        repos = []
        for rid in workspace.repo_ids:
            repo = self._registry.get_repo(rid)
            if repo:
                repos.append(repo)
        return repos

    def delete_workspace(self, name: str) -> None:
        self._registry.delete_workspace(name)

    def suggest_workspace_for(self, repo_path: str | Path) -> list[RepoRecord]:
        """Suggest repos that might belong in the same workspace."""
        identity = detect_repo(Path(repo_path))
        return self._registry.suggest_workspace(identity.repo_id)

    def resolve_search_targets(
        self,
        cwd: str,
        workspace: str | None = None,
        repo_id: str | None = None,
    ) -> dict:
        """Determine which repo DBs to search.

        Returns:
            dict with 'repo_ids' list and 'status' info
        """
        if workspace:
            matched_workspace = self._registry.get_workspace(workspace)
            if not matched_workspace:
                return {
                    "status": "error",
                    "message": f"Workspace '{workspace}' not found",
                    "repo_ids": [],
                }
            return {
                "status": "resolved",
                "workspace": workspace,
                "repo_ids": matched_workspace.repo_ids,
            }

        if repo_id:
            repo = self._registry.get_repo(repo_id)
            if not repo:
                return {
                    "status": "error",
                    "message": f"Repo '{repo_id}' not found",
                    "repo_ids": [],
                }
            return {
                "status": "resolved",
                "repo_id": repo_id,
                "repo_ids": [repo_id],
            }

        context = self._registry.resolve_context(cwd)

        if context["status"] == "resolved":
            repo_ids = context.get("workspace_repo_ids", [context["repo_id"]])
            return {
                "status": "resolved",
                "repo_id": context["repo_id"],
                "workspace": context.get("workspace"),
                "repo_ids": repo_ids,
            }

        return {**context, "repo_ids": []}
