"""Project Brain Generator.

Builds a compressed, complete project representation that an LLM
processes in one shot. Not a search engine — a pre-built mental model.

The brain is structured for how LLMs actually read:
- Dense, no fluff, every token carries information
- Hierarchical: project → modules → symbols → connections
- Signatures not code — the LLM reads actual files only when editing
- Session memory baked in — quirks, decisions, gotchas from past work

Output format is a compact structured text (~8-15k tokens for a 300-file project)
served as a single MCP tool response on session start.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

from engram.config import EngramConfig
from engram.store import RepoStore


@dataclass
class ModuleBrain:
    """Compressed representation of a single module (directory)."""
    name: str
    description: str
    files: list[str]
    symbols: list[str]
    precise_symbols: list[str] = field(default_factory=list)
    model_fields: list[str] = field(default_factory=list)
    config_values: list[str] = field(default_factory=list)
    imports_from: set[str] = field(default_factory=set)
    imported_by: set[str] = field(default_factory=set)


@dataclass
class ProjectBrain:
    """Complete compressed project representation."""
    name: str
    description: str
    stack: str
    modules: dict[str, ModuleBrain] = field(default_factory=dict)
    graph: list[str] = field(default_factory=list)
    key_flows: list[str] = field(default_factory=list)
    memory: list[str] = field(default_factory=list)
    generated_at: float = 0.0
    token_estimate: int = 0


def generate_brain(store: RepoStore, config: EngramConfig) -> ProjectBrain:
    """Generate a complete project brain from indexed data."""
    brain = ProjectBrain(
        name=store.repo_name,
        description="",
        stack="",
        generated_at=time.time(),
    )

    brain.description, brain.stack = _extract_project_description(store)

    module_chunks = _group_by_module(store)

    for module_name, chunks in sorted(module_chunks.items()):
        brain_module = _build_module_brain(module_name, chunks)
        brain.modules[module_name] = brain_module

    _enrich_with_nodes(store, brain)

    _build_dependency_graph(store, brain)

    brain.key_flows = _detect_key_flows(store, brain)

    brain.memory = _load_insights(store)

    rendered = render_brain(brain, store)
    brain.token_estimate = len(rendered) // 4

    return brain


def render_brain(brain: ProjectBrain, store: RepoStore | None = None) -> str:
    """Render brain as compact structured text optimized for LLM consumption.

    This is NOT markdown, NOT JSON. It's a dense format that LLMs
    actually process fully because every line carries information.
    """
    lines: list[str] = []

    lines.append(f"PROJECT: {brain.name}")
    if brain.description:
        lines.append(f"  {brain.description}")
    if brain.stack:
        lines.append(f"  Stack: {brain.stack}")
    lines.append("")

    lines.append("MODULES:")
    for name, mod in sorted(brain.modules.items()):
        if not mod.symbols and not mod.description and len(mod.files) > 10:
            non_code = sum(1 for f in mod.files if f.endswith((".json", ".html", ".css", ".svg")))
            if non_code > len(mod.files) * 0.7:
                lines.append(f"  [{name}] ({len(mod.files)} data files)")
                schema_sample = _get_data_dir_schema(store, name, mod.files)
                if schema_sample:
                    lines.append(f"    schema: {schema_sample}")
                lines.append("")
                continue
        lines.append(f"  [{name}] {mod.description}")
        lines.append(f"    files: {', '.join(mod.files)}")
        display_symbols = mod.precise_symbols if mod.precise_symbols else mod.symbols
        if display_symbols:
            for sym in display_symbols[:20]:
                lines.append(f"    · {sym}")
        if mod.model_fields:
            for model_field in mod.model_fields[:8]:
                lines.append(f"    § {model_field}")
        if mod.config_values:
            lines.append(f"    config: {', '.join(mod.config_values[:6])}")
        if mod.imports_from:
            lines.append(f"    uses: {', '.join(sorted(mod.imports_from))}")
        lines.append("")

    if brain.graph:
        lines.append("DEPENDENCIES:")
        for chain in brain.graph[:15]:
            lines.append(f"  {chain}")
        lines.append("")

    if brain.key_flows:
        lines.append("KEY FLOWS (entry point → execution path):")
        for flow in brain.key_flows[:15]:
            lines.append(f"  {flow}")
        lines.append("")

    if brain.memory:
        lines.append("MEMORY (from past sessions):")
        for mem in brain.memory:
            lines.append(f"  ! {mem}")
        lines.append("")

    return "\n".join(lines)


def render_brain_for_repo(
    repo_id: str,
    repo_name: str,
    config: EngramConfig,
) -> str | None:
    """Generate and render brain for a specific repo. Returns None if not indexed."""
    db_path = config.repo_db_path(repo_id)
    if not db_path.exists():
        return None

    store = RepoStore(db_path, repo_id, repo_name)
    try:
        brain = generate_brain(store, config)
        return render_brain(brain, store)
    finally:
        store.close()


def _enrich_with_nodes(store: RepoStore, brain: ProjectBrain) -> None:
    """Replace regex-extracted symbols with precise node data from the nodes table.

    Each symbol becomes: "filename:line ClassName.method(params) → ReturnType"
    giving the LLM exact file + line + signature for every symbol.
    """
    all_nodes = store.get_all_nodes()
    if not all_nodes:
        return

    module_nodes: dict[str, list[dict]] = defaultdict(list)
    for node in all_nodes:
        mod = _path_to_module(node["file_path"])
        module_nodes[mod].append(node)

    for mod_name, nodes in module_nodes.items():
        if mod_name not in brain.modules:
            continue

        brain_module = brain.modules[mod_name]
        precise: list[str] = []

        nodes_sorted = sorted(nodes, key=lambda node: (node["file_path"], node["line_start"]))

        for node in nodes_sorted:
            filename = Path(node["file_path"]).name
            kind = node["kind"]
            name = node["name"]
            sig = node.get("signature") or name
            line = node["line_start"]
            line_end = node["line_end"]
            parent = node.get("parent_name")

            if parent:
                display = f"{filename}:{line}-{line_end} {parent}.{name}"
            else:
                display = f"{filename}:{line}-{line_end} {sig}"

            if len(display) > 120:
                display = display[:117] + "..."

            precise.append(display)

        brain_module.precise_symbols = precise[:25]


def _get_data_dir_schema(store: RepoStore, module_name: str, files: list[str]) -> str:
    """Extract schema (top-level keys) from the first JSON file in a data directory."""
    json_files = [f for f in files if f.endswith(".json")]
    if not json_files:
        return ""

    row = store._conn.execute(
        """SELECT content FROM chunks
        WHERE file_path LIKE ? AND file_path LIKE ?
        LIMIT 1""",
        (f"%{module_name}%", f"%{json_files[0]}"),
    ).fetchone()

    if not row:
        return ""

    try:
        data = json.loads(row["content"])
        if isinstance(data, dict):
            schema_parts = []
            for key, value in list(data.items())[:10]:
                if isinstance(value, str):
                    schema_parts.append(f'{key}: "{value[:30]}..."' if len(str(value)) > 30 else f'{key}: "{value}"')
                elif isinstance(value, list):
                    schema_parts.append(f"{key}: [{type(value[0]).__name__ if value else 'any'}×{len(value)}]")
                elif isinstance(value, dict):
                    sub_keys = list(value.keys())[:4]
                    schema_parts.append(f"{key}: {{{', '.join(sub_keys)}...}}")
                else:
                    schema_parts.append(f"{key}: {value}")
            return "{" + ", ".join(schema_parts) + "}"
    except (json.JSONDecodeError, Exception):
        pass

    return ""


def _extract_project_description(store: RepoStore) -> tuple[str, str]:
    """Extract project description and tech stack from CLAUDE.md or README."""
    description = ""
    stack = ""

    rows = store._conn.execute(
        """SELECT content, summary FROM chunks
        WHERE file_path LIKE '%CLAUDE.md'
        ORDER BY line_start ASC LIMIT 3"""
    ).fetchall()

    if rows:
        content = rows[0]["content"]
        for para in content.split("\n\n"):
            stripped = para.strip().lstrip("#").strip()
            low = stripped.lower()
            if (len(stripped) > 50
                and not stripped.startswith("```")
                and not stripped.startswith("|")
                and not stripped.startswith("-")
                and not stripped.startswith(">")
                and "context file" not in low
                and "read this" not in low
                and "source of truth" not in low
                and "claude" not in low[:20]):
                description = stripped[:200]
                break

        full = "\n".join(row["content"] for row in rows)

        if not stack:
            techs = set()
            tech_keywords = [
                "FastAPI", "Django", "Flask", "Express", "Next.js", "React", "Vue",
                "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite",
                "Celery", "RabbitMQ", "Kafka", "Docker", "Kubernetes",
                "SuperTokens", "Auth0", "JWT", "OAuth",
                "S3", "Cloudflare", "AWS", "GCP", "Azure",
                "SQLAlchemy", "Prisma", "TypeORM", "Drizzle",
                "Tailwind", "shadcn", "Material UI",
            ]
            for kw in tech_keywords:
                if kw.lower() in full.lower():
                    techs.add(kw)
            if techs:
                stack = ", ".join(sorted(techs))

    if not description:
        rows = store._conn.execute(
            """SELECT content FROM chunks
            WHERE file_path LIKE '%README.md'
            ORDER BY line_start ASC LIMIT 1"""
        ).fetchall()
        if rows:
            for para in rows[0]["content"].split("\n\n"):
                stripped = para.strip().lstrip("#").strip()
                if len(stripped) > 30:
                    description = stripped[:200]
                    break

    return description, stack


def _group_by_module(store: RepoStore) -> dict[str, list[dict]]:
    """Group all chunks by their module directory."""
    rows = store._conn.execute(
        """SELECT file_path, symbol_name, symbol_type, chunk_type,
                  summary, language, line_start, line_end, content
        FROM chunks
        WHERE chunk_type IN ('code', 'doc')
        ORDER BY file_path, line_start"""
    ).fetchall()

    modules: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        file_path = row["file_path"]
        module = _path_to_module(file_path)
        modules[module].append(dict(row))

    return dict(modules)


def _path_to_module(file_path: str) -> str:
    """Convert file path to module name.

    /path/to/app/auth/service.py → "auth"
    /path/to/app/brands/rag/chunker.py → "brands/rag"
    /path/to/CLAUDE.md → "_root"
    """
    parts = Path(file_path).parts

    for root_name in ("app", "src", "lib", "packages"):
        if root_name in parts:
            idx = parts.index(root_name)
            remaining = parts[idx + 1:]
            if len(remaining) <= 1:
                return root_name
            module_parts = remaining[:-1]
            if not module_parts:
                return root_name
            return "/".join(module_parts)

    parent = Path(file_path).parent.name
    return parent if parent else "_root"


def _build_module_brain(module_name: str, chunks: list[dict]) -> ModuleBrain:
    """Build a compressed module representation from its chunks."""
    files: set[str] = set()
    symbols: list[str] = []
    model_fields: list[str] = []
    config_values: list[str] = []
    description_candidates: list[str] = []

    for chunk in chunks:
        file_path = chunk["file_path"]
        filename = Path(file_path).name
        files.add(filename)

        sym_name = chunk.get("symbol_name") or ""
        sym_type = chunk.get("symbol_type") or ""
        summary = chunk.get("summary") or ""
        content = chunk.get("content") or ""

        if sym_type == "module" and summary:
            clean = summary.split("|")[-1].strip() if "|" in summary else summary
            if len(clean) > 20:
                description_candidates.append(clean[:100])

        if sym_type in ("function", "method", "class") and sym_name:
            clean_name = sym_name.split(" ")[-1] if " " in sym_name else sym_name
            sig = _extract_signature(clean_name, sym_type, content, summary)
            if sig:
                symbols.append(sig)
        elif sym_type == "module":
            extracted = _extract_symbols_from_content(content, chunk.get("language", ""))
            symbols.extend(extracted)

            fields = _extract_model_fields(content)
            model_fields.extend(fields)

            configs = _extract_config_values(content, chunk.get("language", ""))
            config_values.extend(configs)

    description = ""
    if description_candidates:
        description_candidates.sort(key=len, reverse=True)
        description = description_candidates[0]

    seen: set[str] = set()
    unique_symbols: list[str] = []
    for symbol in symbols:
        key = symbol.split("(")[0].strip()
        if key not in seen:
            seen.add(key)
            unique_symbols.append(symbol)

    return ModuleBrain(
        name=module_name,
        description=description,
        files=sorted(files),
        symbols=unique_symbols,
        model_fields=model_fields[:20],
        config_values=config_values[:10],
    )


def _extract_symbols_from_content(content: str, language: str) -> list[str]:
    """Regex-based symbol extraction with type annotations preserved.

    Extracts: classes with methods, standalone functions with typed signatures.
    """
    symbols: list[str] = []

    if language in ("python", ""):
        class_pattern = re.compile(r"^class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", re.MULTILINE)
        for class_match in class_pattern.finditer(content):
            class_name = class_match.group(1)
            bases = class_match.group(2) or ""
            bases_clean = ", ".join(b.strip().split(".")[-1] for b in bases.split(",") if b.strip())
            rest = content[class_match.start():]
            methods = []
            for method_match in re.finditer(r"^\s+(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*([^:]+))?", rest, re.MULTILINE):
                mname = method_match.group(1)
                if mname.startswith("_"):
                    continue
                args_raw = method_match.group(2)
                ret = (method_match.group(3) or "").strip()
                typed_args = _compress_args(args_raw)
                sig = mname
                if typed_args:
                    sig += f"({typed_args})"
                if ret:
                    sig += f" → {ret}"
                methods.append(sig)
                if len(methods) >= 8 or method_match.start() > 2000:
                    break
            if methods:
                header = f"{class_name}({bases_clean})" if bases_clean else class_name
                symbols.append(f"{header}: {', '.join(methods)}")
            else:
                symbols.append(f"{class_name}({bases_clean})" if bases_clean else class_name)

        func_pattern = re.compile(
            r"^(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*([^:]+))?",
            re.MULTILINE,
        )
        for func_match in func_pattern.finditer(content):
            fname = func_match.group(1)
            if fname.startswith("_"):
                continue
            args_raw = func_match.group(2)
            ret = (func_match.group(3) or "").strip()
            typed_args = _compress_args(args_raw)
            sig = f"{fname}({typed_args})"
            if ret:
                sig += f" → {ret}"
            if not any(fname in symbol for symbol in symbols):
                symbols.append(sig)

    return symbols[:15]


def _compress_args(args_raw: str) -> str:
    """Compress function arguments, keeping type annotations.

    'self, org_id: str, payload: BrandProfileCreate, user_id: str' →
    'org_id: str, payload: BrandProfileCreate, user_id: str'
    """
    parts = []
    for arg in args_raw.split(","):
        arg = arg.strip()
        if not arg or arg in ("self", "cls"):
            continue
        if arg.startswith("*") or arg.startswith("**"):
            continue
        arg = arg.split("=")[0].strip()
        parts.append(arg)
        if len(parts) >= 5:
            break
    return ", ".join(parts)


def _extract_model_fields(content: str) -> list[str]:
    """Extract SQLAlchemy mapped columns, Pydantic fields, and dataclass fields.

    Returns lines like: 'BrandProfile: name(str), voice(Text), colors(ARRAY), profile_embedding(Vector)'
    """
    results: list[str] = []

    class_blocks = re.split(r"^class\s+", content, flags=re.MULTILINE)

    for block in class_blocks[1:]:
        header_match = re.match(r"(\w+)\s*(?:\([^)]*\))?\s*:", block)
        if not header_match:
            continue
        class_name = header_match.group(0).split("(")[0].strip().rstrip(":")

        if class_name.startswith("_") or class_name.startswith("Test"):
            continue

        fields: list[str] = []
        class_lines = block.split("\n")[1:40]

        for line in class_lines:
            stripped = line.strip()

            sa_match = re.match(
                r"(\w+):\s*Mapped\[([^\]]+)\]\s*=\s*mapped_column\((\w+)?",
                stripped,
            )
            if sa_match:
                fname = sa_match.group(1)
                col_type = sa_match.group(3) or sa_match.group(2)
                fields.append(f"{fname}({col_type})")
                continue

            pydantic_match = re.match(
                r"(\w+):\s*([\w\[\], |]+?)(?:\s*=|\s*$)",
                stripped,
            )
            if pydantic_match and not stripped.startswith("def ") and not stripped.startswith("#"):
                fname = pydantic_match.group(1)
                ftype = pydantic_match.group(2).strip()
                if (fname.startswith("_") or fname.startswith("model_")
                        or fname in ("tablename", "table_args")
                        or fname.isupper()):
                    continue
                fields.append(f"{fname}({ftype})")
                continue

            if stripped.startswith("def ") or stripped.startswith("async def "):
                break

        if fields:
            results.append(f"{class_name}: {', '.join(fields[:10])}")

    return results


def _extract_config_values(content: str, language: str) -> list[str]:
    """Extract configuration/settings values from code.

    Looks for: Settings classes, env var reads, constants that affect behavior.
    """
    configs: list[str] = []

    if language in ("python", ""):
        settings_match = re.search(r"class\s+Settings\s*\(", content)
        if settings_match:
            rest = content[settings_match.start():]
            for line in rest.split("\n")[1:50]:
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("class "):
                    break
                cfg_match = re.match(
                    r"(\w+):\s*\w+.*?=\s*(.+)",
                    stripped,
                )
                if cfg_match:
                    name = cfg_match.group(1)
                    value = cfg_match.group(2).strip().rstrip(",")
                    if name.isupper() or not name.startswith("_"):
                        if len(value) > 40:
                            value = value[:40] + "..."
                        configs.append(f"{name}={value}")

        for match in re.finditer(r"^([A-Z][A-Z_0-9]+)\s*[:=]\s*(.+)", content, re.MULTILINE):
            name = match.group(1)
            value = match.group(2).strip().rstrip(",")
            if len(value) > 40:
                value = value[:40] + "..."
            if "import" in value or value.startswith("Type["):
                continue
            configs.append(f"{name}={value}")

    return configs[:10]


def _extract_signature(name: str, sym_type: str, content: str, summary: str) -> str:
    """Extract a compressed function/class signature.

    Goal: "AuthService.login(email, pwd) → LoginResponse" in minimal tokens.
    """
    if sym_type == "class":
        methods = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                mname = stripped.split("(")[0].replace("def ", "").replace("async ", "").strip()
                if not mname.startswith("_"):
                    methods.append(mname)
        if methods:
            return f"{name}({', '.join(methods[:8])})"
        return name

    if sym_type in ("function", "method"):
        for line in content.split("\n"):
            stripped = line.strip()
            if "def " in stripped:
                try:
                    sig_part = stripped.split("def ", 1)[1].rstrip(":")
                    if "(" in sig_part and ")" in sig_part:
                        func_name = sig_part.split("(")[0]
                        args_str = sig_part.split("(", 1)[1].rsplit(")", 1)[0]
                        args = []
                        for arg in args_str.split(","):
                            arg = arg.strip()
                            if arg in ("self", "cls"):
                                continue
                            arg_name = arg.split(":")[0].split("=")[0].strip()
                            if arg_name and arg_name != "*" and not arg_name.startswith("**"):
                                args.append(arg_name)

                        ret = ""
                        if "->" in sig_part:
                            ret = " → " + sig_part.split("->")[-1].strip()

                        return f"{func_name}({', '.join(args[:6])}){ret}"
                except (IndexError, ValueError):
                    pass
                break

    return name


def _build_dependency_graph(store: RepoStore, brain: ProjectBrain) -> None:
    """Build dependency flow chains from the relationships table."""
    rows = store._conn.execute(
        "SELECT source_file, target_module FROM relationships"
    ).fetchall()

    if not rows:
        return

    module_deps: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        source_mod = _path_to_module(row["source_file"])
        target = row["target_module"]
        target_mod = _import_to_module(target)
        if target_mod and target_mod != source_mod:
            module_deps[source_mod].add(target_mod)

    for mod_name, deps in module_deps.items():
        if mod_name in brain.modules:
            brain.modules[mod_name].imports_from = deps
        for dep in deps:
            if dep in brain.modules:
                brain.modules[dep].imported_by.add(mod_name)

    chains: list[str] = []
    visited_chains: set[str] = set()

    for mod_name, deps in sorted(module_deps.items(), key=lambda x: len(x[1]), reverse=True):
        for dep in sorted(deps):
            if dep in module_deps:
                for dep2 in sorted(module_deps[dep]):
                    chain = f"{mod_name} → {dep} → {dep2}"
                    if chain not in visited_chains:
                        visited_chains.add(chain)
                        chains.append(chain)

    for mod_name, deps in sorted(module_deps.items()):
        for dep in sorted(deps):
            simple = f"{mod_name} → {dep}"
            if not any(simple in chain for chain in chains):
                chains.append(simple)

    brain.graph = chains[:25]


def _import_to_module(import_path: str) -> str:
    """Convert an import path to a module name.

    "app.auth.service" → "auth"
    "app.brands.rag.chunker" → "brands/rag"
    "app.core.database" → "core"
    "fastapi" → "" (external, skip)
    """
    parts = import_path.split(".")

    if parts[0] not in ("app", "src", "lib"):
        return ""

    if len(parts) < 2:
        return parts[0]

    module_parts = parts[1:-1]
    if not module_parts:
        return parts[1] if len(parts) > 1 else ""

    return "/".join(module_parts)


def _detect_key_flows(store: RepoStore, brain: ProjectBrain) -> list[str]:
    """Detect entry points and trace execution flows.

    Entry points are:
    - API routes (files with router.py, @router.get/post decorators)
    - Celery tasks (files in tasks/ with @celery.task or celery_app.task)
    - CLI commands (files with @click.command or argparse)
    - Main entry (main.py, __main__.py)

    For each entry point, follow IMPORTS edges to trace the execution path.
    """
    flows: list[str] = []

    rows = store._conn.execute(
        """SELECT file_path, content, symbol_name FROM chunks
        WHERE chunk_type = 'code'
        ORDER BY file_path"""
    ).fetchall()

    entry_points: list[tuple[str, str, str]] = []

    for row in rows:
        file_path = row["file_path"]
        content = row["content"] or ""
        filename = Path(file_path).name

        if filename == "router.py" or "router" in filename:
            for match in re.finditer(r"@router\.(get|post|put|patch|delete)\([\"']([^\"']+)", content):
                method = match.group(1).upper()
                path = match.group(2)
                module = _path_to_module(file_path)
                entry_points.append((file_path, "API", f"{method} /{module}{path}"))

        if "tasks" in file_path.lower() or "celery" in content.lower():
            for match in re.finditer(r"@(?:celery_app\.task|shared_task|app\.task)\b", content):
                rest = content[match.end():]
                func_match = re.search(r"def\s+(\w+)", rest)
                if func_match:
                    entry_points.append((file_path, "TASK", func_match.group(1)))

            for match in re.finditer(r"send_task\([\"']([^\"']+)", content):
                task_name = match.group(1).split(".")[-1]
                entry_points.append((file_path, "TASK", task_name))

        if filename in ("main.py", "__main__.py"):
            entry_points.append((file_path, "MAIN", filename))

    seen_flows: set[str] = set()

    for endpoint_file, endpoint_type, endpoint_name in entry_points[:30]:
        imports = store.get_imports(endpoint_file)
        if not imports:
            continue

        target_modules = []
        for imp in imports:
            mod = _import_to_module(imp)
            if mod and mod in brain.modules:
                target_modules.append(mod)

        if not target_modules:
            continue

        source_mod = _path_to_module(endpoint_file)
        targets = " → ".join(sorted(set(target_modules))[:3])
        flow = f"[{endpoint_type}] {endpoint_name} ({source_mod}) → {targets}"

        if flow not in seen_flows:
            seen_flows.add(flow)
            flows.append(flow)

    type_order = {"API": 0, "TASK": 1, "MAIN": 2}
    flows.sort(key=lambda f: (type_order.get(f.split("]")[0].lstrip("["), 3), f))

    return flows[:15]


def _load_insights(store: RepoStore) -> list[str]:
    """Load saved insights from the store."""
    rows = store._conn.execute(
        """SELECT content, summary FROM chunks
        WHERE chunk_type = 'insight'
        ORDER BY indexed_at DESC
        LIMIT 20"""
    ).fetchall()

    return [row["content"][:200] for row in rows]
