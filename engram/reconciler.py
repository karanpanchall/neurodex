"""Periodic consistency reconciler.

This is the SOURCE OF TRUTH for index consistency, not the file watcher.
Runs on session start + periodically to:
1. Detect new/modified/deleted files
2. Remove phantom entries (chunks for deleted files)
3. Re-index changed files
4. Update registry stats
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from engram.config import SKIP_DIRS, SKIP_EXTENSIONS, MAX_FILE_SIZE, EngramConfig
from engram.indexer import Indexer
from engram.registry import Registry
from engram.store import RepoStore


@dataclass
class ReconcileResult:
    files_checked: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    chunks_after: int = 0
    elapsed_seconds: float = 0.0


class Reconciler:
    """Ensures index consistency with the filesystem."""

    def __init__(
        self,
        store: RepoStore,
        indexer: Indexer,
        registry: Registry,
        config: EngramConfig,
    ) -> None:
        self._store = store
        self._indexer = indexer
        self._registry = registry
        self._config = config
        self._timer: threading.Timer | None = None
        self._running = False

    def reconcile(self, root: Path) -> ReconcileResult:
        """Full consistency scan of a project directory.

        Compares filesystem state against index state.
        """
        start = time.time()
        result = ReconcileResult()
        root = root.resolve()

        indexed_paths = self._store.get_all_file_paths()
        indexed_paths = {path for path in indexed_paths if not path.startswith("__")}

        current_files: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                dirname for dirname in dirnames
                if dirname not in SKIP_DIRS and not dirname.startswith(".")
            ]

            for filename in filenames:
                file_path = Path(dirpath) / filename
                if self._should_skip(file_path):
                    continue
                try:
                    if file_path.stat().st_size > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue

                file_hash = _quick_hash(file_path)
                current_files[str(file_path)] = file_hash
                result.files_checked += 1

        current_paths = set(current_files.keys())

        deleted = indexed_paths - current_paths
        for path in deleted:
            self._store.remove_by_file(path)
            result.files_removed += 1

        for path, file_hash in current_files.items():
            stored_hash = self._store.get_file_hash(path)
            if stored_hash is None:
                self._indexer.reindex_file(Path(path), root)
                result.files_added += 1
            elif stored_hash != file_hash:
                self._indexer.reindex_file(Path(path), root)
                result.files_updated += 1

        result.chunks_after = self._store.get_chunk_count()
        result.elapsed_seconds = time.time() - start

        self._registry.update_repo_stats(
            self._store.repo_id,
            chunk_count=result.chunks_after,
            file_count=self._store.get_file_count(),
        )

        return result

    def start_periodic(self, root: Path) -> None:
        """Start periodic reconciliation in a background thread."""
        if self._running:
            return
        self._running = True
        self._schedule_next(root)

    def stop_periodic(self) -> None:
        """Stop periodic reconciliation."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self, root: Path) -> None:
        if not self._running:
            return
        self._timer = threading.Timer(
            self._config.reconcile_interval_seconds,
            self._run_and_reschedule,
            args=[root],
        )
        self._timer.daemon = True
        self._timer.start()

    def _run_and_reschedule(self, root: Path) -> None:
        try:
            self.reconcile(root)
        except Exception:
            pass
        self._schedule_next(root)

    def _should_skip(self, path: Path) -> bool:
        if path.suffix.lower() in SKIP_EXTENSIONS:
            return True
        if path.name.startswith("."):
            return True
        for part in path.parts:
            if part in SKIP_DIRS:
                return True
        return False


def _quick_hash(path: Path) -> str:
    """Fast hash using file size + first 4KB + last 4KB.

    Much faster than full SHA-256 for change detection.
    """
    try:
        size = path.stat().st_size
        hasher = hashlib.sha256()
        hasher.update(str(size).encode())
        with open(path, "rb") as f:
            hasher.update(f.read(4096))
            if size > 8192:
                f.seek(-4096, 2)
                hasher.update(f.read(4096))
        return hasher.hexdigest()[:16]
    except OSError:
        return ""
