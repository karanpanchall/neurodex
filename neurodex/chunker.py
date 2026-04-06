"""Multi-strategy chunking for code, docs, and insights.

Three distinct chunking strategies:
1. Code chunker — tree-sitter AST-aware, chunks at function/class boundaries
2. Doc chunker — markdown heading-boundary splitting
3. Insight chunker — for explicitly saved insights (passthrough)

Falls back gracefully when tree-sitter parsers aren't available.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from neurodex.languages import EXTENSION_MAP, get_config, get_definition_types, has_ast_support, get_language
from neurodex.store import Chunk, make_chunk_id

CHARS_PER_TOKEN = 4
MAX_CHUNK_TOKENS = 2000
MAX_WHOLE_FILE_TOKENS = 2000
MAX_WHOLE_DOC_TOKENS = 4000


@dataclass
class ChunkResult:
    """Result of chunking a file."""
    chunks: list[Chunk] = field(default_factory=list)
    language: str | None = None
    strategy: str = "unknown"


def chunk_file(
    file_path: Path,
    content: str,
    file_hash: str,
    file_mtime: float,
) -> ChunkResult:
    """Chunk a file using the best available strategy.

    Dispatches to code, doc, or fallback chunker based on file extension.
    """
    language = get_language(str(file_path))

    if language in ("markdown", "rst", "text"):
        return _chunk_doc(file_path, content, file_hash, file_mtime, language)

    if language and has_ast_support(language):
        result = _chunk_code_treesitter(file_path, content, file_hash, file_mtime, language)
        if result.chunks:
            return result

    return _chunk_blocks(file_path, content, file_hash, file_mtime, language)


def chunk_insight(
    content: str,
    tags: list[str] | None = None,
    repo_id: str = "",
) -> Chunk:
    """Create a chunk from an explicitly saved insight."""
    chunk_id = make_chunk_id(f"insight:{repo_id}", None, hashlib.md5(content.encode()).hexdigest()[:8])
    tag_str = ", ".join(tags) if tags else None
    return Chunk(
        id=chunk_id,
        file_path="__insights__",
        file_hash="",
        chunk_type="insight",
        symbol_name=tag_str,
        symbol_type=None,
        language=None,
        content=content,
        summary=content[:120] if len(content) > 120 else content,
        line_start=None,
        line_end=None,
        indexed_at=0,
        last_modified=0,
    )


def _chunk_doc(
    file_path: Path,
    content: str,
    file_hash: str,
    file_mtime: float,
    language: str | None,
) -> ChunkResult:
    """Chunk markdown/docs at heading boundaries."""
    token_est = len(content) // CHARS_PER_TOKEN
    path_str = str(file_path)

    if token_est <= MAX_WHOLE_DOC_TOKENS:
        chunk_id = make_chunk_id(path_str, 1, None)
        summary = _extract_first_line(content)
        return ChunkResult(
            chunks=[Chunk(
                id=chunk_id, file_path=path_str, file_hash=file_hash,
                chunk_type="doc", symbol_name=file_path.name,
                symbol_type=None, language=language,
                content=content, summary=summary,
                line_start=1, line_end=content.count("\n") + 1,
                indexed_at=0, last_modified=file_mtime,
            )],
            language=language,
            strategy="whole-file",
        )

    chunks: list[Chunk] = []
    sections = _split_markdown_sections(content)

    for heading, section_content, start_line in sections:
        if not section_content.strip():
            continue
        chunk_id = make_chunk_id(path_str, start_line, heading)
        chunks.append(Chunk(
            id=chunk_id, file_path=path_str, file_hash=file_hash,
            chunk_type="doc", symbol_name=heading,
            symbol_type="section", language=language,
            content=section_content, summary=heading,
            line_start=start_line,
            line_end=start_line + section_content.count("\n"),
            indexed_at=0, last_modified=file_mtime,
        ))

    return ChunkResult(chunks=chunks, language=language, strategy="doc")


def _split_markdown_sections(content: str) -> list[tuple[str, str, int]]:
    """Split markdown by headings. Returns (heading, content, line_number)."""
    lines = content.split("\n")
    sections: list[tuple[str, str, int]] = []
    current_heading = "Top"
    current_lines: list[str] = []
    current_start = 1

    for i, line in enumerate(lines, 1):
        if re.match(r"^#{1,4}\s+", line):
            if current_lines:
                sections.append((
                    current_heading,
                    "\n".join(current_lines),
                    current_start,
                ))
            current_heading = line.lstrip("#").strip()
            current_lines = [line]
            current_start = i
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, "\n".join(current_lines), current_start))

    return sections


def _chunk_code_treesitter(
    file_path: Path,
    content: str,
    file_hash: str,
    file_mtime: float,
    language: str,
) -> ChunkResult:
    """AST-aware code chunking using tree-sitter."""
    try:
        import tree_sitter as ts
    except ImportError:
        return ChunkResult(strategy="tree-sitter-unavailable")

    ts_language = _load_treesitter_language(language)
    if not ts_language:
        return ChunkResult(strategy="tree-sitter-no-language")

    path_str = str(file_path)
    token_est = len(content) // CHARS_PER_TOKEN

    module_doc = _extract_module_docstring(content, language)
    basename = file_path.stem
    parent_dir = file_path.parent.name
    file_context = f"{parent_dir}/{file_path.name}"

    if token_est <= MAX_WHOLE_FILE_TOKENS:
        symbols = _extract_symbols_from_tree(content, ts_language, language)
        symbol_names = ", ".join(sym[0] for sym in symbols[:5])
        summary_parts = [file_context]
        if module_doc:
            summary_parts.append(module_doc[:200])
        if symbol_names:
            summary_parts.append(f"Defines: {symbol_names}")
        summary = " | ".join(summary_parts)
        sym = f"{file_context} {symbol_names}" if symbol_names else file_context

        chunk_id = make_chunk_id(path_str, 1, None)
        return ChunkResult(
            chunks=[Chunk(
                id=chunk_id, file_path=path_str, file_hash=file_hash,
                chunk_type="code", symbol_name=sym,
                symbol_type="module", language=language,
                content=content, summary=summary,
                line_start=1, line_end=content.count("\n") + 1,
                indexed_at=0, last_modified=file_mtime,
            )],
            language=language,
            strategy="tree-sitter",
        )

    parser = ts.Parser(ts_language)
    tree = parser.parse(content.encode())
    chunks = _extract_chunks_from_tree(
        tree, content, path_str, file_hash, file_mtime, language,
        module_doc=module_doc, file_context=file_context,
    )

    if not chunks:
        return ChunkResult(strategy="tree-sitter-no-chunks")

    return ChunkResult(chunks=chunks, language=language, strategy="tree-sitter")


def _load_treesitter_language(language: str):
    """Load a tree-sitter language, returning the Language object or None.

    Handles tree-sitter 0.22 (Language from .so), 0.23+ (language() -> Language),
    and 0.25+ (language() -> PyCapsule that needs wrapping).
    """
    cfg = get_config(language)
    if not cfg or not cfg.treesitter_module:
        return None

    try:
        import importlib
        import tree_sitter as ts

        mod = importlib.import_module(cfg.treesitter_module)
        if not hasattr(mod, "language"):
            return None

        raw_lang = mod.language()

        if hasattr(ts, "Language") and not isinstance(raw_lang, ts.Language):
            try:
                return ts.Language(raw_lang)
            except TypeError:
                return raw_lang
        return raw_lang
    except (ImportError, AttributeError, TypeError, OSError):
        return None


def _extract_symbols_from_tree(content: str, ts_language, language: str) -> list[tuple[str, str]]:
    """Extract (name, type) pairs from a tree-sitter parse."""
    try:
        import tree_sitter as ts
    except ImportError:
        return []

    parser = ts.Parser(ts_language)
    tree = parser.parse(content.encode())

    symbols: list[tuple[str, str]] = []
    node_types = _get_definition_node_types(language)

    def walk(node):
        if node.type in node_types:
            name = _get_node_name(node, content)
            if name:
                symbols.append((name, node.type))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols


def _extract_chunks_from_tree(
    tree, content: str, file_path: str,
    file_hash: str, file_mtime: float, language: str,
    module_doc: str | None = None, file_context: str = "",
) -> list[Chunk]:
    """Extract function/class chunks from a parsed tree."""
    chunks: list[Chunk] = []
    lines = content.split("\n")
    node_types = _get_definition_node_types(language)

    def walk(node, parent_name: str | None = None):
        if node.type in node_types:
            name = _get_node_name(node, content)
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            chunk_content = "\n".join(lines[node.start_point[0]:node.end_point[0] + 1])

            if len(chunk_content) // CHARS_PER_TOKEN > MAX_CHUNK_TOKENS:
                chunk_content = "\n".join(lines[node.start_point[0]:node.start_point[0] + 5])
                end_line = start_line + 4

            symbol_type = _node_type_to_symbol_type(node.type)
            docstring = _extract_docstring(node, content, language)

            summary_parts = [file_context] if file_context else []
            if docstring:
                summary_parts.append(docstring[:200])
            else:
                summary_parts.append(f"{symbol_type}: {name}")
            if module_doc and not docstring:
                summary_parts.append(f"(module: {module_doc[:80]})")
            summary = " | ".join(summary_parts)

            qualified_name = f"{file_context} {name}" if file_context else name

            chunk_id = make_chunk_id(file_path, start_line, name)
            chunks.append(Chunk(
                id=chunk_id, file_path=file_path, file_hash=file_hash,
                chunk_type="code", symbol_name=qualified_name,
                symbol_type=symbol_type, language=language,
                content=chunk_content, summary=summary,
                line_start=start_line, line_end=end_line,
                indexed_at=0, last_modified=file_mtime,
            ))

            for child in node.children:
                walk(child, name)
        else:
            for child in node.children:
                walk(child, parent_name)

    walk(tree.root_node)

    import_chunk = _extract_imports(content, file_path, file_hash, file_mtime, language, lines)
    if import_chunk:
        chunks.append(import_chunk)

    return chunks


def _get_definition_node_types(language: str) -> set[str]:
    """Get tree-sitter node types that represent definitions, from language config."""
    return set(get_definition_types(language))


def _get_node_name(node, content: str) -> str | None:
    """Extract the name from a definition node."""
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier", "type_identifier"):
            return content[child.start_byte:child.end_byte]
    return None


def _node_type_to_symbol_type(node_type: str) -> str:
    """Map a tree-sitter node type to a symbol type string."""
    if "class" in node_type:
        return "class"
    if "method" in node_type:
        return "method"
    if "function" in node_type or "arrow" in node_type:
        return "function"
    if "interface" in node_type:
        return "interface"
    if "enum" in node_type:
        return "enum"
    if "type" in node_type or "struct" in node_type:
        return "type"
    if "trait" in node_type or "impl" in node_type:
        return "trait"
    return "definition"


def _extract_module_docstring(content: str, language: str) -> str | None:
    """Extract the module-level docstring from file content."""
    if language == "python":
        stripped = content.lstrip()
        for quote in ['"""', "'''"]:
            if stripped.startswith(quote):
                end = stripped.find(quote, len(quote))
                if end != -1:
                    return stripped[len(quote):end].strip()
        for quote in ['"', "'"]:
            if stripped.startswith(quote) and not stripped.startswith(quote * 3):
                end = stripped.find(quote, 1)
                if end != -1:
                    return stripped[1:end].strip()
    elif language in ("javascript", "typescript"):
        stripped = content.lstrip()
        if stripped.startswith("/**"):
            end = stripped.find("*/")
            if end != -1:
                doc = stripped[3:end].strip()
                lines = [line.lstrip(" *").strip() for line in doc.split("\n")]
                return " ".join(line for line in lines if line)
    lines = content.split("\n")
    comment_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            comment_lines.append(stripped.lstrip("#/ ").strip())
        elif stripped == "" and not comment_lines:
            continue
        else:
            break
    if comment_lines:
        return " ".join(comment_lines)
    return None


def _extract_docstring(node, content: str, language: str) -> str | None:
    """Try to extract a docstring/comment from a definition node."""
    if language == "python":
        for child in node.children:
            if child.type == "block":
                for block_child in child.children:
                    if block_child.type == "expression_statement":
                        for expr_child in block_child.children:
                            if expr_child.type == "string":
                                doc = content[expr_child.start_byte:expr_child.end_byte]
                                return doc.strip('"""').strip("'''").strip()
                    break
    return None


def _extract_imports(
    content: str, file_path: str, file_hash: str,
    file_mtime: float, language: str, lines: list[str],
) -> Chunk | None:
    """Extract the import block as a separate chunk."""
    import_lines: list[str] = []
    import_end = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if language == "python":
            if stripped.startswith(("import ", "from ")):
                import_lines.append(line)
                import_end = i
        elif language in ("javascript", "typescript"):
            if stripped.startswith(("import ", "require(", "const ")) and "require" in stripped:
                import_lines.append(line)
                import_end = i
        elif language == "go":
            if stripped.startswith("import"):
                import_lines.append(line)
                import_end = i
        elif language == "rust":
            if stripped.startswith("use "):
                import_lines.append(line)
                import_end = i
        elif language == "java":
            if stripped.startswith("import "):
                import_lines.append(line)
                import_end = i

    if not import_lines:
        return None

    import_content = "\n".join(import_lines)
    chunk_id = make_chunk_id(file_path, 0, "__imports__")
    return Chunk(
        id=chunk_id, file_path=file_path, file_hash=file_hash,
        chunk_type="code", symbol_name="__imports__",
        symbol_type="imports", language=language,
        content=import_content, summary=f"Imports ({len(import_lines)} lines)",
        line_start=1, line_end=import_end + 1,
        indexed_at=0, last_modified=file_mtime,
    )


def _chunk_blocks(
    file_path: Path,
    content: str,
    file_hash: str,
    file_mtime: float,
    language: str | None,
) -> ChunkResult:
    """Fallback chunker: split by blank-line-separated blocks.

    Used when tree-sitter isn't available or for config/data files.
    """
    path_str = str(file_path)
    token_est = len(content) // CHARS_PER_TOKEN
    file_context = f"{file_path.parent.name}/{file_path.name}"
    module_doc = _extract_module_docstring(content, language or "")

    if token_est <= MAX_WHOLE_FILE_TOKENS:
        summary_parts = [file_context]
        if module_doc:
            summary_parts.append(module_doc[:200])
        else:
            first = _extract_first_line(content)
            if first:
                summary_parts.append(first)
        summary = " | ".join(summary_parts)

        chunk_id = make_chunk_id(path_str, 1, None)
        return ChunkResult(
            chunks=[Chunk(
                id=chunk_id, file_path=path_str, file_hash=file_hash,
                chunk_type="code" if language else "doc",
                symbol_name=file_context, symbol_type="module",
                language=language, content=content,
                summary=summary,
                line_start=1, line_end=content.count("\n") + 1,
                indexed_at=0, last_modified=file_mtime,
            )],
            language=language,
            strategy="whole-file",
        )

    chunks: list[Chunk] = []
    blocks = re.split(r"\n\s*\n", content)
    current_line = 1

    for content_block in blocks:
        content_block = content_block.strip()
        if not content_block:
            current_line += 1
            continue

        block_lines = content_block.count("\n") + 1
        chunk_id = make_chunk_id(path_str, current_line, None)
        chunks.append(Chunk(
            id=chunk_id, file_path=path_str, file_hash=file_hash,
            chunk_type="code" if language else "doc",
            symbol_name=None, symbol_type=None,
            language=language, content=content_block,
            summary=_extract_first_line(content_block),
            line_start=current_line,
            line_end=current_line + block_lines - 1,
            indexed_at=0, last_modified=file_mtime,
        ))
        current_line += block_lines + 1

    return ChunkResult(chunks=chunks, language=language, strategy="block")


def _extract_first_line(content: str) -> str:
    """Extract first non-empty line as a summary."""
    for line in content.split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return ""
