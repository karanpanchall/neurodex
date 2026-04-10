"""Repo detection and identity.

Identity resolution:
1. git remote origin URL hash (stable across clones/renames)
2. git root path hash (fallback if no remote)
3. directory path hash (fallback if not a git repo)
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoIdentity:
    """Uniquely identifies a repository."""

    repo_id: str
    name: str
    git_root: Path | None
    git_remote: str | None
    local_path: Path

    @property
    def is_git(self) -> bool:
        return self.git_root is not None


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _hash_id(value: str) -> str:
    """Generate a 12-char hex hash from a string."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def detect_repo(path: Path | str) -> RepoIdentity:
    """Detect repository identity from a directory path.

    Resolution order:
    1. sha256(git_remote_origin_url)[:12] -- stable across clones
    2. sha256(git_root_absolute_path)[:12] -- fallback if no remote
    3. sha256(dir_absolute_path)[:12] -- fallback if not git
    """
    path = Path(path).resolve()

    git_root_str = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
    git_root = Path(git_root_str) if git_root_str else None

    git_remote: str | None = None
    if git_root:
        git_remote = _run_git(["remote", "get-url", "origin"], cwd=git_root)

    if git_remote:
        normalized = git_remote.rstrip("/").removesuffix(".git").lower()
        repo_id = _hash_id(normalized)
    elif git_root:
        repo_id = _hash_id(str(git_root))
    else:
        repo_id = _hash_id(str(path))

    # Prefer the name from the git remote URL — a local clone might live in
    # a directory with a stale name after a rename. Fall back to directory.
    name: str | None = None
    if git_remote:
        last = git_remote.rstrip("/").split("/")[-1]
        last = last.removesuffix(".git")
        if last:
            name = last
    if not name:
        name = (git_root or path).name

    return RepoIdentity(
        repo_id=repo_id,
        name=name,
        git_root=git_root,
        git_remote=git_remote,
        local_path=path,
    )
