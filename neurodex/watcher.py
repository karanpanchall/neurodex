"""File system watcher for real-time index updates.

Uses watchdog to monitor project directories. Debounces changes
within a 2-second window before triggering re-indexing.

IMPORTANT: This is an optimization, NOT the source of truth.
The reconciler (reconciler.py) is the consistency guarantee.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from neurodex.config import SKIP_DIRS, SKIP_EXTENSIONS, EngramConfig
from neurodex.indexer import Indexer
from neurodex.store import RepoStore


class _DebouncedHandler(FileSystemEventHandler):
    """Collects file change events and processes them in batches."""

    def __init__(
        self,
        indexer: Indexer,
        root: Path,
        debounce_seconds: float = 2.0,
    ) -> None:
        self._indexer = indexer
        self._root = root
        self._debounce = debounce_seconds
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        path = Path(event.src_path)

        if self._should_skip(path):
            return

        with self._lock:
            if event.event_type == "deleted":
                self._pending[str(path)] = "deleted"
            else:
                self._pending[str(path)] = "modified"

            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        """Process all pending changes."""
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()

        for path_str, event_type in pending.items():
            path = Path(path_str)
            try:
                if event_type == "deleted":
                    self._indexer.remove_file(path)
                else:
                    if path.exists():
                        self._indexer.reindex_file(path, self._root)
            except Exception:
                pass

    def _should_skip(self, path: Path) -> bool:
        """Check if a path should be skipped."""
        if path.suffix.lower() in SKIP_EXTENSIONS:
            return True
        if path.name.startswith("."):
            return True
        for part in path.parts:
            if part in SKIP_DIRS or part.startswith("."):
                return True
        return False


class FileWatcher:
    """Watches a project directory for changes and updates the index."""

    def __init__(
        self,
        root: Path,
        indexer: Indexer,
        config: EngramConfig,
    ) -> None:
        self._root = root.resolve()
        self._handler = _DebouncedHandler(
            indexer, self._root, config.watcher_debounce_seconds
        )
        self._observer = Observer()
        self._running = False

    def start(self) -> None:
        """Start watching for file changes."""
        if self._running:
            return
        self._observer.schedule(self._handler, str(self._root), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        self._running = True

    def stop(self) -> None:
        """Stop watching."""
        if not self._running:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running
