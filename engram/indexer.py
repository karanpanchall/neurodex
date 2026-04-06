"""File indexing pipeline.

Orchestrates: file discovery -> hashing -> chunking -> storage.
Priority indexing: CLAUDE.md first, recent git files next, then everything else.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from engram.chunker import chunk_file
from engram.config import (
    MAX_FILE_SIZE,
    PRIORITY_FILES,
    SKIP_DIRS,
    SKIP_EXTENSIONS,
    EngramConfig,
)
from engram.languages import get_language, is_test_file, is_test_function
from engram.store import Chunk, RepoStore, make_chunk_id


@dataclass
class IndexProgress:
    """Tracks progress of an indexing operation."""
    total_files: int = 0
    indexed_files: int = 0
    skipped_files: int = 0
    chunks_created: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    @property
    def status(self) -> str:
        """Return a human-readable status string."""
        if self.total_files == 0:
            return "discovering"
        if self.indexed_files < self.total_files:
            return f"indexing ({self.indexed_files}/{self.total_files})"
        return "complete"

    @property
    def elapsed(self) -> float:
        """Return elapsed time since indexing started."""
        return time.time() - self.started_at


class Indexer:
    """Indexes a repository into a RepoStore."""

    def __init__(self, store: RepoStore, config: EngramConfig) -> None:
        self._store = store
        self._config = config

    def index_directory(
        self,
        root: Path,
        progress_callback: callable | None = None,
    ) -> IndexProgress:
        """Index all files in a directory with priority ordering.

        Priority order:
        1. CLAUDE.md, README.md, AGENTS.md
        2. Files changed in last 10 git commits
        3. src/ and app/ directories
        4. Everything else
        5. Git commit messages
        """
        progress = IndexProgress()
        root = root.resolve()

        files = self._discover_files_prioritized(root)
        progress.total_files = len(files)

        if progress_callback:
            progress_callback(progress)

        batch_size = 10
        batch_chunks = []

        for file_path in files:
            try:
                new_chunks = self._index_single_file(file_path, root)
                if new_chunks is None:
                    progress.skipped_files += 1
                else:
                    batch_chunks.extend(new_chunks)
                    progress.chunks_created += len(new_chunks)

                    if len(batch_chunks) >= batch_size:
                        self._store.add_chunks(batch_chunks)
                        batch_chunks = []

            except Exception as exc:
                progress.errors.append(f"{file_path}: {exc}")

            progress.indexed_files += 1
            if progress_callback:
                progress_callback(progress)

        if batch_chunks:
            self._store.add_chunks(batch_chunks)

        self._index_git_commits(root, progress)

        return progress

    def reindex_file(self, file_path: Path, root: Path) -> int:
        """Re-index a single file. Returns number of chunks created."""
        self._store.remove_by_file(str(file_path))
        chunks = self._index_single_file(file_path, root)
        if chunks:
            self._store.add_chunks(chunks)
            return len(chunks)
        return 0

    def remove_file(self, file_path: Path) -> int:
        """Remove all chunks for a file. Returns count removed."""
        return self._store.remove_by_file(str(file_path))

    def _index_single_file(self, file_path: Path, root: Path):
        """Index a single file. Returns list of chunks or None if skipped."""
        try:
            stat = file_path.stat()
        except OSError:
            return None

        if stat.st_size > self._config.max_file_size:
            return None

        if stat.st_size == 0:
            return None

        rel_path = str(file_path)
        file_hash = _hash_file(file_path)
        stored_hash = self._store.get_file_hash(rel_path)

        if stored_hash == file_hash:
            return None

        self._store.remove_by_file(rel_path)

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return None

        if not content.strip():
            return None

        result = chunk_file(file_path, content, file_hash, stat.st_mtime)

        edges = _extract_all_edges(content, file_path, rel_path)
        if edges:
            self._store.add_edges(rel_path, edges)

        nodes = _extract_symbol_nodes(content, file_path, rel_path)
        if nodes:
            self._store.add_nodes(rel_path, nodes)

        return result.chunks if result.chunks else None

    def _discover_files_prioritized(self, root: Path) -> list[Path]:
        """Discover files in priority order."""
        priority_1: list[Path] = []
        priority_2: list[Path] = []
        priority_3: list[Path] = []
        priority_4: list[Path] = []

        recent_files = _get_recent_git_files(root)

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                dirname for dirname in dirnames
                if dirname not in SKIP_DIRS
                and not dirname.startswith(".")
                and not _is_virtual_env(Path(dirpath) / dirname)
            ]

            dir_path = Path(dirpath)

            for fname in filenames:
                file_path = dir_path / fname

                if file_path.suffix.lower() in SKIP_EXTENSIONS:
                    continue

                if fname.startswith("."):
                    continue

                if fname in PRIORITY_FILES:
                    priority_1.append(file_path)
                elif str(file_path) in recent_files:
                    priority_2.append(file_path)
                elif any(part in str(file_path.relative_to(root)) for part in ("src/", "app/", "lib/")):
                    priority_3.append(file_path)
                else:
                    priority_4.append(file_path)

        return priority_1 + priority_2 + priority_3 + priority_4

    def _index_git_commits(self, root: Path, progress: IndexProgress) -> None:
        """Index recent git commit messages as searchable chunks."""
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-20", "--format=%H|%s|%an|%ai"],
                cwd=root, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return

        chunks = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 2:
                continue

            commit_hash = parts[0]
            message = parts[1]
            author = parts[2] if len(parts) > 2 else ""
            date = parts[3] if len(parts) > 3 else ""

            chunk_id = make_chunk_id("__commits__", None, commit_hash[:8])
            content = f"Commit {commit_hash[:8]}: {message}"
            if author:
                content += f"\nAuthor: {author}"
            if date:
                content += f"\nDate: {date}"

            chunks.append(Chunk(
                id=chunk_id,
                file_path="__commits__",
                file_hash=commit_hash,
                chunk_type="commit",
                symbol_name=commit_hash[:8],
                symbol_type=None,
                language=None,
                content=content,
                summary=message[:120],
                line_start=None,
                line_end=None,
                indexed_at=0,
                last_modified=0,
            ))

        if chunks:
            self._store.add_chunks(chunks)
            progress.chunks_created += len(chunks)


def _hash_file(file_path: Path) -> str:
    """SHA-256 hash of file contents."""
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as file_handle:
            for content_block in iter(lambda: file_handle.read(8192), b""):
                hasher.update(content_block)
    except OSError:
        return ""
    return hasher.hexdigest()[:16]


def _get_recent_git_files(root: Path) -> set[str]:
    """Get files changed in last 10 git commits."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~10", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return set()
        files = set()
        for line in result.stdout.strip().split("\n"):
            if line:
                files.add(str(root / line))
        return files
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()


def _is_virtual_env(path: Path) -> bool:
    """Detect if a directory is a Python virtual environment or similar."""
    if (path / "pyvenv.cfg").exists():
        return True
    if (path / "bin" / "python").exists() or (path / "Scripts" / "python.exe").exists():
        return True
    lib = path / "lib"
    if lib.is_dir():
        for child in lib.iterdir():
            if child.name.startswith("python") and (child / "site-packages").is_dir():
                return True
    return False


def _extract_symbol_nodes(content: str, file_path: Path, full_path: str) -> list[dict]:
    """Extract symbol-level nodes with qualified names and exact line ranges.

    Qualified name format: {file_path}::{ClassName.method_name}
    Every class, function, and method gets a node with precise line_start/end.
    """
    nodes: list[dict] = []
    ext = file_path.suffix.lower()
    lines = content.split("\n")

    language = get_language(str(file_path)) or ""
    file_is_test = is_test_file(str(file_path), language)

    if ext == ".py" or language == "python":
        _extract_python_nodes(lines, full_path, language, file_is_test, nodes)
    elif ext in (".js", ".jsx", ".ts", ".tsx") or language in ("javascript", "typescript"):
        _extract_js_ts_nodes(lines, full_path, language, file_is_test, nodes)
    elif ext == ".dart" or language == "dart":
        _extract_dart_nodes(lines, full_path, language, file_is_test, nodes)
    else:
        _extract_generic_nodes(lines, full_path, language, file_is_test, nodes)

    return nodes


def _extract_python_nodes(
    lines: list[str], file_path: str, language: str,
    file_is_test: bool, nodes: list[dict],
) -> None:
    """Extract Python classes, functions, methods with full signatures."""
    current_class: str | None = None
    current_class_indent = -1

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        class_match = re.match(r"class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", stripped)
        if class_match and indent == 0:
            current_class = class_match.group(1)
            current_class_indent = indent
            bases = class_match.group(2) or ""

            class_end = _find_block_end(lines, i, indent)

            qualified = f"{file_path}::{current_class}"
            nodes.append({
                "qualified_name": qualified,
                "kind": "class",
                "name": current_class,
                "parent_name": None,
                "line_start": i + 1,
                "line_end": class_end + 1,
                "signature": f"class {current_class}({bases})" if bases else f"class {current_class}",
                "params": bases,
                "return_type": None,
                "language": language,
                "is_test": 1 if file_is_test else 0,
            })
            continue

        if indent <= current_class_indent and stripped and not stripped.startswith("#") and not stripped.startswith("@"):
            if not re.match(r"(?:async\s+)?def\s+", stripped) and not re.match(r"class\s+", stripped):
                current_class = None
                current_class_indent = -1

        func_match = re.match(r"(async\s+)?def\s+(\w+)\s*\(", stripped)
        if func_match:
            is_async = bool(func_match.group(1))
            func_name = func_match.group(2)

            sig_lines = [stripped]
            j = i
            while ")" not in "".join(sig_lines) and j < len(lines) - 1:
                j += 1
                sig_lines.append(lines[j].strip())
            full_sig = " ".join(sig_lines)

            paren_match = re.search(r"\(([^)]*)\)", full_sig)
            params_raw = paren_match.group(1) if paren_match else ""
            ret_match = re.search(r"\)\s*->\s*([^:]+)", full_sig)
            return_type = ret_match.group(1).strip() if ret_match else ""

            is_method = current_class is not None and indent > current_class_indent
            parent = current_class if is_method else None

            if is_method:
                qualified = f"{file_path}::{current_class}.{func_name}"
                kind = "method"
            else:
                qualified = f"{file_path}::{func_name}"
                kind = "function"

            params = _compress_params_with_types(params_raw)

            async_prefix = "async " if is_async else ""
            sig = f"{async_prefix}def {func_name}({params})"
            if return_type:
                sig += f" → {return_type}"

            func_end = _find_block_end(lines, i, indent)

            is_test = 1 if (file_is_test and func_name.startswith("test")) else 0

            nodes.append({
                "qualified_name": qualified,
                "kind": kind,
                "name": func_name,
                "parent_name": parent,
                "line_start": i + 1,
                "line_end": func_end + 1,
                "signature": sig,
                "params": params,
                "return_type": return_type or None,
                "language": language,
                "is_test": is_test,
            })


def _extract_js_ts_nodes(
    lines: list[str], file_path: str, language: str,
    file_is_test: bool, nodes: list[dict],
) -> None:
    """Extract JS/TS classes and functions."""
    content = "\n".join(lines)

    for match in re.finditer(r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([^{]+))?\s*\{", content):
        name = match.group(1)
        line_start = content[:match.start()].count("\n") + 1
        line_end = _find_brace_end(content, match.end()) + 1
        sig = f"class {name}"
        if match.group(2):
            sig += f" extends {match.group(2)}"

        qualified = f"{file_path}::{name}"
        nodes.append({
            "qualified_name": qualified, "kind": "class", "name": name,
            "parent_name": None, "line_start": line_start, "line_end": line_end,
            "signature": sig, "params": None, "return_type": None,
            "language": language, "is_test": 1 if file_is_test else 0,
        })

    for match in re.finditer(
        r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([^{]+))?\s*\{",
        content,
    ):
        name = match.group(1)
        params = match.group(2).strip()
        ret = (match.group(3) or "").strip()
        line_start = content[:match.start()].count("\n") + 1
        line_end = _find_brace_end(content, match.end()) + 1

        sig = f"function {name}({params})"
        if ret:
            sig += f": {ret}"

        qualified = f"{file_path}::{name}"
        nodes.append({
            "qualified_name": qualified, "kind": "function", "name": name,
            "parent_name": None, "line_start": line_start, "line_end": line_end,
            "signature": sig, "params": params, "return_type": ret or None,
            "language": language, "is_test": 1 if file_is_test else 0,
        })

    for match in re.finditer(
        r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+)?\s*=\s*(?:async\s+)?\([^)]*\)\s*(?::\s*\w+)?\s*=>",
        content,
    ):
        name = match.group(1)
        line_start = content[:match.start()].count("\n") + 1
        qualified = f"{file_path}::{name}"
        nodes.append({
            "qualified_name": qualified, "kind": "function", "name": name,
            "parent_name": None, "line_start": line_start, "line_end": line_start + 10,
            "signature": name, "params": None, "return_type": None,
            "language": language, "is_test": 0,
        })


def _extract_dart_nodes(
    lines: list[str], file_path: str, language: str,
    file_is_test: bool, nodes: list[dict],
) -> None:
    """Extract Dart classes and functions."""
    content = "\n".join(lines)

    for match in re.finditer(r"(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?\s*\{", content):
        name = match.group(1)
        line_start = content[:match.start()].count("\n") + 1
        line_end = _find_brace_end(content, match.end()) + 1
        qualified = f"{file_path}::{name}"
        nodes.append({
            "qualified_name": qualified, "kind": "class", "name": name,
            "parent_name": None, "line_start": line_start, "line_end": line_end,
            "signature": f"class {name}", "language": language,
            "is_test": 1 if file_is_test else 0,
        })

    for match in re.finditer(r"(?:Future<[^>]+>|void|String|int|bool|dynamic|\w+)\s+(\w+)\s*\(([^)]*)\)\s*(?:async\s*)?\{", content):
        name = match.group(1)
        if name in ("if", "for", "while", "switch", "catch"):
            continue
        line_start = content[:match.start()].count("\n") + 1
        qualified = f"{file_path}::{name}"
        nodes.append({
            "qualified_name": qualified, "kind": "function", "name": name,
            "parent_name": None, "line_start": line_start, "line_end": line_start + 10,
            "signature": f"{name}({match.group(2).strip()})", "language": language,
            "is_test": 1 if file_is_test else 0,
        })


def _extract_generic_nodes(
    lines: list[str], file_path: str, language: str,
    file_is_test: bool, nodes: list[dict],
) -> None:
    """Generic extraction for languages without specific handlers."""
    content = "\n".join(lines)

    for match in re.finditer(r"class\s+(\w+)", content):
        name = match.group(1)
        line_start = content[:match.start()].count("\n") + 1
        qualified = f"{file_path}::{name}"
        nodes.append({
            "qualified_name": qualified, "kind": "class", "name": name,
            "parent_name": None, "line_start": line_start, "line_end": line_start + 20,
            "signature": f"class {name}", "language": language, "is_test": 0,
        })

    for match in re.finditer(r"(?:func|fn|def|fun|function)\s+(\w+)\s*\(([^)]*)\)", content):
        name = match.group(1)
        line_start = content[:match.start()].count("\n") + 1
        qualified = f"{file_path}::{name}"
        nodes.append({
            "qualified_name": qualified, "kind": "function", "name": name,
            "parent_name": None, "line_start": line_start, "line_end": line_start + 15,
            "signature": f"{name}({match.group(2).strip()[:60]})", "language": language, "is_test": 0,
        })


def _find_block_end(lines: list[str], start_line: int, start_indent: int) -> int:
    """Find the end of a Python block (class or function body)."""
    for i in range(start_line + 1, min(start_line + 500, len(lines))):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= start_indent and line.strip():
            return i - 1
    return min(start_line + 50, len(lines) - 1)


def _find_brace_end(content: str, start_pos: int) -> int:
    """Find the line number of the closing brace matching the one at start_pos."""
    depth = 1
    for i in range(start_pos, min(start_pos + 50000, len(content))):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[:i].count("\n") + 1
    return content[:start_pos].count("\n") + 50


def _compress_params_with_types(params_raw: str) -> str:
    """Compress Python params keeping types.

    Example: 'self, org_id: str, payload: BrandCreate' -> 'org_id: str, payload: BrandCreate'
    """
    parts = []
    for param in params_raw.split(","):
        param = param.strip()
        if not param or param in ("self", "cls"):
            continue
        if param.startswith("*") or param.startswith("**"):
            continue
        param = param.split("=")[0].strip()
        parts.append(param)
        if len(parts) >= 6:
            break
    return ", ".join(parts)


def _extract_all_edges(content: str, file_path: Path, full_path: str) -> list[dict]:
    """Extract all typed edges from file content.

    Edge types:
    - IMPORTS: from X import Y, import X, require('X')
    - CALLS: function calls to other modules (heuristic)
    - INHERITS: class Foo(Bar) -- parent class
    - TESTED_BY: test files that import production code
    - CONTAINS: class contains methods (implicit from AST, not extracted here)
    """
    edges: list[dict] = []
    ext = file_path.suffix.lower()

    import_targets = _extract_import_targets(content, file_path)
    for target in import_targets:
        edges.append({"kind": "IMPORTS", "target_symbol": target})

    if ext == ".py":
        for match in re.finditer(r"^class\s+(\w+)\s*\(([^)]+)\)", content, re.MULTILINE):
            child_class = match.group(1)
            parents_raw = match.group(2)
            for parent in parents_raw.split(","):
                parent = parent.strip().split(".")[-1]
                if parent and parent not in ("object", "ABC", "Exception", "BaseException"):
                    edges.append({
                        "kind": "INHERITS",
                        "source_symbol": child_class,
                        "target_symbol": parent,
                        "line": content[:match.start()].count("\n") + 1,
                    })

    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        for match in re.finditer(r"class\s+(\w+)\s+extends\s+(\w+)", content):
            edges.append({
                "kind": "INHERITS",
                "source_symbol": match.group(1),
                "target_symbol": match.group(2),
                "line": content[:match.start()].count("\n") + 1,
            })
        for match in re.finditer(r"class\s+(\w+).*?implements\s+([\w,\s]+)", content):
            for iface in match.group(2).split(","):
                iface = iface.strip()
                if iface:
                    edges.append({
                        "kind": "IMPLEMENTS",
                        "source_symbol": match.group(1),
                        "target_symbol": iface,
                    })

    elif ext in (".java", ".kt", ".cs"):
        for match in re.finditer(r"class\s+(\w+)\s+extends\s+(\w+)", content):
            edges.append({
                "kind": "INHERITS",
                "source_symbol": match.group(1),
                "target_symbol": match.group(2),
            })
        for match in re.finditer(r"class\s+(\w+).*?implements\s+([\w,\s]+)", content):
            for iface in match.group(2).split(","):
                iface = iface.strip()
                if iface:
                    edges.append({
                        "kind": "IMPLEMENTS",
                        "source_symbol": match.group(1),
                        "target_symbol": iface,
                    })

    lang = get_language(str(file_path)) or ""
    if is_test_file(str(file_path), lang):
        for target in import_targets:
            if not any(kw in target for kw in ("test", "mock", "fixture", "conftest", "pytest")):
                edges.append({
                    "kind": "TESTED_BY",
                    "source_symbol": file_path.stem,
                    "target_symbol": target,
                    "target_file": full_path,
                })

    if ext == ".py":
        imported_names: set[str] = set()
        for match in re.finditer(r"^from\s+\S+\s+import\s+(.+)", content, re.MULTILINE):
            for name in match.group(1).split(","):
                name = name.strip().split(" as ")[-1].strip()
                if name:
                    imported_names.add(name)

        for match in re.finditer(r"(\w+)\s*\(", content):
            called = match.group(1)
            if called in imported_names and not called[0].islower():
                edges.append({
                    "kind": "CALLS",
                    "target_symbol": called,
                    "line": content[:match.start()].count("\n") + 1,
                })

    return edges


def _extract_import_targets(content: str, file_path: Path) -> list[str]:
    """Extract import targets from file content for relationship mapping."""
    ext = file_path.suffix.lower()
    targets: list[str] = []

    if ext == ".py":
        for match in re.finditer(r"^from\s+([\w.]+)\s+import", content, re.MULTILINE):
            targets.append(match.group(1))
        for match in re.finditer(r"^import\s+([\w.]+)", content, re.MULTILINE):
            targets.append(match.group(1))
    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        for match in re.finditer(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]""", content):
            targets.append(match.group(1))
    elif ext == ".go":
        for match in re.finditer(r'"([^"]+)"', content[:2000]):
            if "/" in match.group(1):
                targets.append(match.group(1))

    return targets
