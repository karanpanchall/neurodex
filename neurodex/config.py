"""NEURODEX configuration management.

Data stored at ~/.local/share/neurodex/
Config at ~/.config/neurodex/config.toml (optional)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "neurodex"
_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "neurodex"

SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".pnpm", "bower_components",
    ".venv", "venv", "env", "site-packages", "__pycache__",
    ".eggs", ".egg-info", ".tox", ".nox",
    "dist", "build", ".next", ".nuxt", ".output", ".turbo",
    "target", ".gradle", ".mvn",
    ".git", ".svn", ".hg", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "coverage", ".coverage", "htmlcov",
    "Pods", ".dart_tool", ".pub-cache",
    "ephemeral", "Generated", "DerivedData",
    ".terraform", ".serverless", "vendor",
    ".env", ".cache", "tmp", ".temp",
})

SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".class", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp", ".tiff",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".pptx",
    ".db", ".sqlite", ".sqlite3",
    ".lock", ".sum",
    ".min.js", ".min.css", ".map",
    ".pbxproj", ".xcworkspacedata", ".plist", ".storyboard", ".xib",
    ".g.dart", ".freezed.dart", ".mocks.dart",
    ".csv", ".parquet", ".pickle", ".pkl", ".npy",
    ".db-shm", ".db-wal", ".db-journal",
    ".pem", ".cer", ".crt", ".key", ".p12", ".jks",
    ".apk", ".ipa", ".aab", ".dex",
    ".xcworkspacedata", ".xcscheme",
    ".gradle", ".gradlew",
    ".bin", ".dat", ".raw",
})

MAX_FILE_SIZE: int = 100_000

PRIORITY_FILES: tuple[str, ...] = (
    "CLAUDE.md", "AGENTS.md", "README.md", "readme.md",
    "CONTRIBUTING.md", "ARCHITECTURE.md",
)


@dataclass(frozen=True)
class EngramConfig:
    """Immutable configuration for NEURODEX."""

    data_dir: Path = field(default_factory=lambda: _DEFAULT_DATA_DIR)
    config_dir: Path = field(default_factory=lambda: _DEFAULT_CONFIG_DIR)
    max_file_size: int = MAX_FILE_SIZE
    reconcile_interval_seconds: int = 300
    watcher_debounce_seconds: float = 2.0
    bm25_score_threshold: float = 0.0

    @property
    def registry_db_path(self) -> Path:
        return self.data_dir / "registry.db"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    def repo_db_path(self, repo_id: str) -> Path:
        return self.repos_dir / f"{repo_id}.db"

    def ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> EngramConfig:
    """Load config, respecting environment variable overrides."""
    data_dir = Path(os.environ.get("NEURODEX_DATA_DIR", _DEFAULT_DATA_DIR))
    config_dir = Path(os.environ.get("NEURODEX_CONFIG_DIR", _DEFAULT_CONFIG_DIR))
    return EngramConfig(data_dir=data_dir, config_dir=config_dir)
