"""NEURODEX MCP Server.

Exposes search, memory, and project management tools to Claude Code / Codex.
Starts instantly — no model to load, just open SQLite files.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from neurodex.brain import render_brain_for_repo
from neurodex.chunker import chunk_insight
from neurodex.contracts import analyze_cross_project_impact, render_cross_project_impact
from neurodex.impact import analyze_impact, render_impact
from neurodex.config import EngramConfig, load_config
from neurodex.project import detect_repo
from neurodex.registry import Registry
from neurodex.search import RankedResult, SearchEngine
from neurodex.store import RepoStore
from neurodex.workspace import WorkspaceManager

_session_context: dict = {}


def _get_cwd() -> str:
    """Get current working directory, respecting MCP server cwd."""
    return os.environ.get("NEURODEX_CWD", os.getcwd())


def _result_to_dict(result: RankedResult) -> dict:
    """Convert a search result to a serializable dict."""
    output = {
        "file_path": result.file_path,
        "repo": result.repo_name,
        "symbol": result.symbol_name,
        "type": result.chunk_type,
        "language": result.language,
        "summary": result.summary,
        "score": round(result.final_score, 3),
    }
    if result.line_start:
        output["lines"] = f"{result.line_start}-{result.line_end}"
    if result.content:
        output["content"] = result.content
    if result.more_in_file > 0:
        output["more_in_file"] = result.more_in_file
    return output


def create_server() -> Server:
    config = load_config()
    config.ensure_dirs()
    registry = Registry(config)
    workspace_mgr = WorkspaceManager(registry, config)
    search_engine = SearchEngine(config)
    server = Server("neurodex")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="neurodex_search",
                description=(
                    "Search indexed project files, code symbols, docs, and saved insights. "
                    "Returns relevant code/doc chunks ranked by BM25 relevance. "
                    "Good queries: 'JWT auth middleware flow', 'how does celery retry work', "
                    "'database migration for users table'. "
                    "Bad queries: 'auth', 'db', 'config' (too short — be descriptive)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query — be descriptive, 3+ words is best"},
                        "max_results": {"type": "integer", "default": 5, "description": "Max results to return"},
                        "max_tokens": {"type": "integer", "default": 3000, "description": "Max total tokens in results"},
                        "workspace": {"type": "string", "description": "Workspace name to search across multiple repos"},
                        "repo_id": {"type": "string", "description": "Specific repo ID to search"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="neurodex_compact_search",
                description=(
                    "Search returning metadata only (file path, symbol, line range, summary). "
                    "Use this first, then read specific files with your Read tool. Saves tokens."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 10},
                        "workspace": {"type": "string"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="neurodex_symbols",
                description="List indexed code symbols (functions, classes) matching a pattern. Like 'go to definition' across the whole project.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Symbol name pattern (partial match)"},
                        "workspace": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            ),
            Tool(
                name="neurodex_save",
                description=(
                    "Save a decision, insight, or finding for future sessions. "
                    "Use this to persist important context that should survive across conversations. "
                    "Example: 'Auth uses SuperTokens with JWT. Public API uses API keys. See app/auth/'"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The insight to save"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags for categorization (e.g., ['auth', 'architecture'])",
                        },
                    },
                    "required": ["content"],
                },
            ),
            Tool(
                name="neurodex_trace",
                description=(
                    "Trace import/dependency chain from a file. "
                    "Shows what a file imports and what imports it. "
                    "Use when the user asks 'what happens when X' or 'how does Y flow'."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "File to trace from"},
                        "direction": {
                            "type": "string",
                            "enum": ["imports", "importers", "both"],
                            "default": "both",
                            "description": "Direction: what this file imports, what imports this file, or both",
                        },
                        "depth": {"type": "integer", "default": 2, "description": "How deep to trace"},
                    },
                    "required": ["file_path"],
                },
            ),
            Tool(
                name="neurodex_cross_impact",
                description=(
                    "Cross-project impact: what breaks in OTHER projects when you change a backend endpoint? "
                    "Finds API contracts between backend routes and frontend/mobile consumers. "
                    "Use when changing an API endpoint, renaming a route, or modifying request/response schemas."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Backend file being changed (e.g., router.py)"},
                    },
                    "required": ["file_path"],
                },
            ),
            Tool(
                name="neurodex_references",
                description=(
                    "Find ALL references to a symbol across the entire project. "
                    "Catches imports, calls, inheritance, type annotations, string refs. "
                    "Use when renaming or changing a function/class signature to find every place that breaks."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Symbol name (function, class, variable) to find references for"},
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="neurodex_impact",
                description=(
                    "Blast-radius analysis: what breaks if you change this file? "
                    "Shows affected files, test coverage gaps, and risk score. "
                    "Use BEFORE making changes to understand the impact."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "File you plan to change"},
                        "max_depth": {"type": "integer", "default": 3, "description": "How many hops to trace"},
                    },
                    "required": ["file_path"],
                },
            ),
            Tool(
                name="neurodex_brain",
                description=(
                    "CALL THIS FIRST on session start. Returns complete project brain — "
                    "every module, every function signature, every dependency chain, "
                    "and past session insights. After this call you know the entire project "
                    "without reading any files. Use neurodex_search only for deep dives."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Specific repo ID (optional, auto-detects from cwd)"},
                    },
                },
            ),
            Tool(
                name="neurodex_status",
                description="Get index status: indexed repos, workspaces, current context, health info.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="neurodex_list_projects",
                description="List all indexed repos and workspaces. Use when unsure which project to search.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="neurodex_workspace_create",
                description="Create a workspace linking multiple repos for cross-repo search.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Workspace name (e.g., 'MyApp')"},
                        "repo_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Local paths to repos to include",
                        },
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="neurodex_workspace_add",
                description="Add a repo to an existing workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_name": {"type": "string"},
                        "repo_path": {"type": "string", "description": "Local path to the repo"},
                    },
                    "required": ["workspace_name", "repo_path"],
                },
            ),
            Tool(
                name="neurodex_set_context",
                description="Set which project/workspace to search for this session. Persists across tool calls.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string", "description": "Workspace name"},
                        "repo_id": {"type": "string", "description": "Specific repo ID"},
                    },
                },
            ),
        ]

    def _resolve_repo_ids(
        workspace: str | None = None, repo_id: str | None = None
    ) -> dict:
        """Resolve which repos to search, using session context as fallback."""
        resolved_workspace = workspace or _session_context.get("workspace")
        resolved_repo_id = repo_id or _session_context.get("repo_id")

        return workspace_mgr.resolve_search_targets(
            cwd=_get_cwd(), workspace=resolved_workspace, repo_id=resolved_repo_id
        )

    def _get_repo_names(repo_ids: list[str]) -> dict[str, str]:
        names = {}
        for repo_id in repo_ids:
            repo_record = registry.get_repo(repo_id)
            if repo_record:
                names[repo_id] = repo_record.name
        return names

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "neurodex_search":
                return _handle_search(arguments)
            elif name == "neurodex_compact_search":
                return _handle_compact_search(arguments)
            elif name == "neurodex_symbols":
                return _handle_symbols(arguments)
            elif name == "neurodex_save":
                return _handle_save(arguments)
            elif name == "neurodex_trace":
                return _handle_trace(arguments)
            elif name == "neurodex_cross_impact":
                return _handle_cross_impact(arguments)
            elif name == "neurodex_references":
                return _handle_references(arguments)
            elif name == "neurodex_impact":
                return _handle_impact(arguments)
            elif name == "neurodex_brain":
                return _handle_brain(arguments)
            elif name == "neurodex_status":
                return _handle_status()
            elif name == "neurodex_list_projects":
                return _handle_list_projects()
            elif name == "neurodex_workspace_create":
                return _handle_workspace_create(arguments)
            elif name == "neurodex_workspace_add":
                return _handle_workspace_add(arguments)
            elif name == "neurodex_set_context":
                return _handle_set_context(arguments)
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
        except Exception as exc:
            return [TextContent(
                type="text",
                text=json.dumps({"error": str(exc), "tool": name}, indent=2),
            )]

    def _handle_search(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids(args.get("workspace"), args.get("repo_id"))

        if resolution["status"] != "resolved":
            return [TextContent(type="text", text=json.dumps(resolution, indent=2))]

        repo_ids = resolution["repo_ids"]
        repo_names = _get_repo_names(repo_ids)

        results = search_engine.search(
            query=args["query"],
            repo_ids=repo_ids,
            repo_names=repo_names,
            max_results=args.get("max_results", 5),
            max_tokens=args.get("max_tokens", 3000),
        )

        output = {
            "query": args["query"],
            "results": [_result_to_dict(result) for result in results],
            "total_results": len(results),
            "repos_searched": list(repo_names.values()),
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    def _handle_compact_search(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids(args.get("workspace"))

        if resolution["status"] != "resolved":
            return [TextContent(type="text", text=json.dumps(resolution, indent=2))]

        repo_ids = resolution["repo_ids"]
        repo_names = _get_repo_names(repo_ids)

        results = search_engine.search_compact(
            query=args["query"],
            repo_ids=repo_ids,
            repo_names=repo_names,
            max_results=args.get("max_results", 10),
        )

        output = {
            "query": args["query"],
            "results": [_result_to_dict(result) for result in results],
            "hint": "Use your Read tool to get full content for files you need.",
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    def _handle_symbols(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids(args.get("workspace"))

        if resolution["status"] != "resolved":
            return [TextContent(type="text", text=json.dumps(resolution, indent=2))]

        repo_ids = resolution["repo_ids"]
        repo_names = _get_repo_names(repo_ids)

        results = search_engine.search_symbols(
            pattern=args["pattern"],
            repo_ids=repo_ids,
            repo_names=repo_names,
        )

        output = {
            "pattern": args["pattern"],
            "symbols": [
                {
                    "name": result.symbol_name,
                    "type": result.symbol_type,
                    "file": result.file_path,
                    "lines": f"{result.line_start}-{result.line_end}" if result.line_start else None,
                    "repo": result.repo_name,
                    "language": result.language,
                }
                for result in results
            ],
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    def _handle_save(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids()

        if resolution["status"] != "resolved":
            repos = registry.list_repos()
            if not repos:
                return [TextContent(type="text", text=json.dumps({
                    "error": "No indexed repos. Run `neurodex init` first."
                }))]
            repo_id = repos[0].id
            repo_name = repos[0].name
        else:
            repo_id = resolution["repo_ids"][0]
            repo_record = registry.get_repo(repo_id)
            repo_name = repo_record.name if repo_record else repo_id

        chunk = chunk_insight(
            content=args["content"],
            tags=args.get("tags"),
            repo_id=repo_id,
        )

        store = search_engine.get_or_open_store(repo_id, repo_name)
        store.add_chunks([chunk])

        return [TextContent(type="text", text=json.dumps({
            "saved": True,
            "repo": repo_name,
            "tags": args.get("tags", []),
            "preview": args["content"][:100],
        }, indent=2))]

    def _handle_cross_impact(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids()
        if resolution["status"] != "resolved":
            return [TextContent(type="text", text=json.dumps(resolution, indent=2))]

        repo_id = resolution["repo_ids"][0]
        result = analyze_cross_project_impact(
            changed_file=args["file_path"],
            changed_repo_id=repo_id,
            config=config,
            registry=registry,
        )
        return [TextContent(type="text", text=json.dumps(
            render_cross_project_impact(result), indent=2
        ))]

    def _handle_references(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids()
        if resolution["status"] != "resolved":
            return [TextContent(type="text", text=json.dumps(resolution, indent=2))]

        repo_id = resolution["repo_ids"][0]
        repo_record = registry.get_repo(repo_id)
        repo_name = repo_record.name if repo_record else repo_id
        store = search_engine.get_or_open_store(repo_id, repo_name)

        refs = store.find_all_references(args["symbol"])

        for key in ["defined_in", "imported_by", "called_by", "inherited_by", "referenced_in"]:
            for item in refs.get(key, []):
                for field in ("file", "source_file"):
                    if field in item and "/app/" in item[field]:
                        item[field] = item[field].split("/app/")[-1]

        return [TextContent(type="text", text=json.dumps(refs, indent=2))]

    def _handle_impact(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids()
        if resolution["status"] != "resolved":
            return [TextContent(type="text", text=json.dumps(resolution, indent=2))]

        repo_id = resolution["repo_ids"][0]
        repo_record = registry.get_repo(repo_id)
        repo_name = repo_record.name if repo_record else repo_id
        store = search_engine.get_or_open_store(repo_id, repo_name)

        result = analyze_impact(
            store,
            file_path=args["file_path"],
            max_depth=args.get("max_depth", 3),
        )

        return [TextContent(type="text", text=json.dumps(render_impact(result), indent=2))]

    def _handle_brain(args: dict) -> list[TextContent]:
        repo_id = args.get("repo_id")

        if not repo_id:
            resolution = _resolve_repo_ids()
            if resolution["status"] == "resolved":
                repo_id = resolution["repo_ids"][0]
            elif resolution["status"] == "disambiguation_needed":
                return [TextContent(type="text", text=json.dumps(resolution, indent=2))]
            else:
                repos = registry.list_repos()
                if not repos:
                    return [TextContent(type="text", text="No projects indexed. Run `neurodex init` in a project directory.")]
                return [TextContent(type="text", text=json.dumps({
                    "status": "pick_a_project",
                    "available": [
                        {"id": repo.id, "name": repo.name, "files": repo.file_count, "chunks": repo.chunk_count}
                        for repo in repos
                    ],
                }, indent=2))]

        repo_record = registry.get_repo(repo_id)
        repo_name = repo_record.name if repo_record else repo_id

        brain_text = render_brain_for_repo(repo_id, repo_name, config)
        if not brain_text:
            return [TextContent(type="text", text=f"No index found for repo {repo_id}. Run `neurodex init`.")]

        return [TextContent(type="text", text=brain_text)]

    def _handle_trace(args: dict) -> list[TextContent]:
        resolution = _resolve_repo_ids()

        if resolution["status"] != "resolved":
            return [TextContent(type="text", text=json.dumps(resolution, indent=2))]

        repo_id = resolution["repo_ids"][0]
        repo_record = registry.get_repo(repo_id)
        repo_name = repo_record.name if repo_record else repo_id
        store = search_engine.get_or_open_store(repo_id, repo_name)

        file_path = args["file_path"]
        direction = args.get("direction", "both")
        depth = args.get("depth", 2)

        output: dict = {"file": file_path}

        if direction in ("imports", "both"):
            output["imports"] = store.get_imports(file_path)
            if depth > 1:
                output["dependency_tree"] = store.trace_dependencies(file_path, depth)

        if direction in ("importers", "both"):
            parts = file_path.split("/")
            if "app" in parts:
                idx = parts.index("app")
                module = ".".join(parts[idx:]).removesuffix(".py")
                output["imported_by"] = store.get_importers(module)
            else:
                output["imported_by"] = store.get_importers(
                    file_path.rsplit("/", 1)[-1].removesuffix(".py")
                )

        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    def _handle_status() -> list[TextContent]:
        cwd = _get_cwd()
        context = registry.resolve_context(cwd)
        repos = registry.list_repos()
        workspaces = registry.list_workspaces()

        status = {
            "cwd": cwd,
            "context": context,
            "session_context": _session_context or None,
            "repos_indexed": len(repos),
            "workspaces": len(workspaces),
            "repos": [
                {
                    "id": repo.id,
                    "name": repo.name,
                    "files": repo.file_count,
                    "chunks": repo.chunk_count,
                    "last_indexed": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(repo.last_indexed)
                    ) if repo.last_indexed else "never",
                }
                for repo in repos
            ],
        }
        return [TextContent(type="text", text=json.dumps(status, indent=2))]

    def _handle_list_projects() -> list[TextContent]:
        repos = registry.list_repos()
        workspaces = registry.list_workspaces()

        output = {
            "repos": [
                {
                    "id": repo.id,
                    "name": repo.name,
                    "paths": repo.local_paths,
                    "git_remote": repo.git_remote,
                    "files": repo.file_count,
                    "chunks": repo.chunk_count,
                }
                for repo in repos
            ],
            "workspaces": [
                {
                    "name": workspace_record.name,
                    "repos": workspace_record.repo_ids,
                }
                for workspace_record in workspaces
            ],
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    def _handle_workspace_create(args: dict) -> list[TextContent]:
        workspace_record = workspace_mgr.create_workspace(
            name=args["name"],
            repo_paths=args.get("repo_paths"),
        )
        return [TextContent(type="text", text=json.dumps({
            "created": True,
            "workspace": workspace_record.name,
            "repos": workspace_record.repo_ids,
        }, indent=2))]

    def _handle_workspace_add(args: dict) -> list[TextContent]:
        workspace_record = workspace_mgr.add_to_workspace(
            workspace_name=args["workspace_name"],
            repo_path=args["repo_path"],
        )
        return [TextContent(type="text", text=json.dumps({
            "added": True,
            "workspace": workspace_record.name,
            "repos": workspace_record.repo_ids,
        }, indent=2))]

    def _handle_set_context(args: dict) -> list[TextContent]:
        if args.get("workspace"):
            _session_context["workspace"] = args["workspace"]
            _session_context.pop("repo_id", None)
        elif args.get("repo_id"):
            _session_context["repo_id"] = args["repo_id"]
            _session_context.pop("workspace", None)
        else:
            _session_context.clear()

        return [TextContent(type="text", text=json.dumps({
            "context_set": True,
            "session_context": _session_context,
        }, indent=2))]

    return server


async def main() -> None:
    """Entry point for the MCP server."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
