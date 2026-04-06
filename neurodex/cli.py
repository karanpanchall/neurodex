"""NEURODEX CLI.

Commands:
  neurodex init       — Index current project (shows progress)
  neurodex status     — Show index health
  neurodex reindex    — Force full re-index
  neurodex search     — Test search from terminal
  neurodex workspace  — Manage workspaces
  neurodex install    — Add MCP server to Claude Code settings
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

import click

from neurodex.brain import render_brain_for_repo
from neurodex.chunker import chunk_insight
from neurodex.config import load_config
from neurodex.indexer import IndexProgress, Indexer
from neurodex.project import detect_repo
from neurodex.reconciler import Reconciler
from neurodex.registry import Registry
from neurodex.search import SearchEngine
from neurodex.store import RepoStore
from neurodex.workspace import WorkspaceManager


@click.group()
@click.version_option(package_name="neurodex")
def main():
    """NEURODEX — Local memory for AI code assistants."""
    pass


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def init(path: str):
    """Index the current project directory."""
    config = load_config()
    config.ensure_dirs()
    root = Path(path).resolve()

    identity = detect_repo(root)
    click.echo(f"Project: {identity.name}")
    click.echo(f"  ID: {identity.repo_id}")
    if identity.git_remote:
        click.echo(f"  Remote: {identity.git_remote}")
    click.echo(f"  Path: {identity.local_path}")
    click.echo()

    registry = Registry(config)
    registry.upsert_repo(
        repo_id=identity.repo_id,
        name=identity.name,
        git_remote=identity.git_remote,
        local_path=str(identity.local_path),
    )

    suggestions = registry.suggest_workspace(identity.repo_id)
    if suggestions:
        click.echo("Related projects found nearby:")
        for suggestion in suggestions:
            click.echo(f"  - {suggestion.name} ({', '.join(suggestion.local_paths)})")
        click.echo("  Tip: `neurodex workspace create MyApp` to group them\n")

    db_path = config.repo_db_path(identity.repo_id)
    store = RepoStore(db_path, identity.repo_id, identity.name)
    indexer = Indexer(store, config)

    start = time.time()
    click.echo("Indexing...")

    def on_progress(progress: IndexProgress):
        if progress.total_files > 0:
            pct = int(progress.indexed_files / progress.total_files * 100)
            click.echo(
                f"\r  [{pct:3d}%] {progress.indexed_files}/{progress.total_files} files, "
                f"{progress.chunks_created} chunks",
                nl=False,
            )

    result = indexer.index_directory(root, progress_callback=on_progress)
    click.echo()

    elapsed = time.time() - start

    registry.update_repo_stats(
        identity.repo_id,
        chunk_count=store.get_chunk_count(),
        file_count=store.get_file_count(),
    )

    click.echo(f"\nDone in {elapsed:.1f}s:")
    click.echo(f"  Files indexed: {result.indexed_files}")
    click.echo(f"  Files skipped: {result.skipped_files}")
    click.echo(f"  Chunks created: {result.chunks_created}")
    if result.errors:
        click.echo(f"  Errors: {len(result.errors)}")
        for error in result.errors[:5]:
            click.echo(f"    - {error}")

    store.close()
    registry.close()


@main.command()
def status():
    """Show index health and project info."""
    config = load_config()
    registry = Registry(config)
    repos = registry.list_repos()
    workspaces = registry.list_workspaces()

    if not repos:
        click.echo("No projects indexed. Run `neurodex init` in a project directory.")
        registry.close()
        return

    click.echo(f"NEURODEX Status")
    click.echo(f"{'=' * 50}")
    click.echo(f"Data: {config.data_dir}")
    click.echo(f"Projects: {len(repos)}  |  Workspaces: {len(workspaces)}")
    click.echo()

    for repo in repos:
        last = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(repo.last_indexed))
            if repo.last_indexed
            else "never"
        )
        click.echo(f"  {repo.name} [{repo.id}]")
        click.echo(f"    Files: {repo.file_count}  |  Chunks: {repo.chunk_count}  |  Last: {last}")
        for repo_path in repo.local_paths:
            click.echo(f"    Path: {repo_path}")
        if repo.git_remote:
            click.echo(f"    Remote: {repo.git_remote}")
        click.echo()

    if workspaces:
        click.echo("Workspaces:")
        for workspace_record in workspaces:
            repo_names = []
            for repo_id in workspace_record.repo_ids:
                repo_record = registry.get_repo(repo_id)
                repo_names.append(repo_record.name if repo_record else repo_id)
            click.echo(f"  {workspace_record.name}: {', '.join(repo_names)}")

    registry.close()


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def brain(path: str):
    """Generate and display the project brain."""
    config = load_config()
    root = Path(path).resolve()
    identity = detect_repo(root)

    registry = Registry(config)
    repo = registry.get_repo(identity.repo_id)

    if not repo:
        click.echo(f"Project '{identity.name}' not indexed. Run `neurodex init` first.")
        registry.close()
        return

    brain_text = render_brain_for_repo(identity.repo_id, identity.name, config)
    if brain_text:
        click.echo(brain_text)
        token_est = len(brain_text) // 4
        click.echo(f"\n--- {token_est:,} tokens estimated ---")
    else:
        click.echo("Failed to generate brain.")

    registry.close()


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def reindex(path: str):
    """Force full re-index of a project."""
    config = load_config()
    root = Path(path).resolve()
    identity = detect_repo(root)

    registry = Registry(config)
    db_path = config.repo_db_path(identity.repo_id)

    if not db_path.exists():
        click.echo(f"Project '{identity.name}' is not indexed. Run `neurodex init` first.")
        registry.close()
        return

    store = RepoStore(db_path, identity.repo_id, identity.name)
    indexer = Indexer(store, config)
    reconciler = Reconciler(store, indexer, registry, config)

    click.echo(f"Reconciling {identity.name}...")
    result = reconciler.reconcile(root)

    click.echo(f"Done in {result.elapsed_seconds:.1f}s:")
    click.echo(f"  Checked: {result.files_checked}")
    click.echo(f"  Added: {result.files_added}")
    click.echo(f"  Updated: {result.files_updated}")
    click.echo(f"  Removed: {result.files_removed}")
    click.echo(f"  Total chunks: {result.chunks_after}")

    store.close()
    registry.close()


@main.command()
@click.argument("query")
@click.option("--workspace", "-w", help="Workspace to search")
@click.option("--max-results", "-n", default=5, help="Max results")
def search(query: str, workspace: str | None, max_results: int):
    """Test search from the terminal."""
    config = load_config()
    registry = Registry(config)
    workspace_mgr = WorkspaceManager(registry, config)
    search_engine = SearchEngine(config)

    cwd = str(Path.cwd().resolve())
    resolution = workspace_mgr.resolve_search_targets(cwd, workspace=workspace)

    if resolution["status"] != "resolved":
        click.echo(json.dumps(resolution, indent=2))
        registry.close()
        return

    repo_ids = resolution["repo_ids"]
    repo_names = {}
    for repo_id in repo_ids:
        repo_record = registry.get_repo(repo_id)
        if repo_record:
            repo_names[repo_id] = repo_record.name

    results = search_engine.search(
        query=query,
        repo_ids=repo_ids,
        repo_names=repo_names,
        max_results=max_results,
    )

    if not results:
        click.echo("No results found.")
    else:
        for index, result in enumerate(results, 1):
            click.echo(f"\n--- Result {index} (score: {result.final_score:.3f}) ---")
            click.echo(f"File: {result.file_path}")
            if result.symbol_name:
                click.echo(f"Symbol: {result.symbol_name} ({result.symbol_type})")
            if result.line_start:
                click.echo(f"Lines: {result.line_start}-{result.line_end}")
            click.echo(f"Repo: {result.repo_name}")
            if result.content:
                preview = result.content[:300]
                if len(result.content) > 300:
                    preview += "..."
                click.echo(f"\n{preview}")

    search_engine.close_all()
    registry.close()


@main.group()
def workspace():
    """Manage workspaces (groups of related repos)."""
    pass


@workspace.command("create")
@click.argument("name")
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
def workspace_create(name: str, paths: tuple[str, ...]):
    """Create a workspace grouping multiple repos."""
    config = load_config()
    registry = Registry(config)
    mgr = WorkspaceManager(registry, config)

    workspace_record = mgr.create_workspace(name, list(paths) if paths else None)
    click.echo(f"Workspace '{workspace_record.name}' created with {len(workspace_record.repo_ids)} repos.")
    if workspace_record.repo_ids:
        for repo_id in workspace_record.repo_ids:
            repo_record = registry.get_repo(repo_id)
            click.echo(f"  - {repo_record.name if repo_record else repo_id}")

    registry.close()


@workspace.command("add")
@click.argument("workspace_name")
@click.argument("repo_path", type=click.Path(exists=True))
def workspace_add(workspace_name: str, repo_path: str):
    """Add a repo to a workspace."""
    config = load_config()
    registry = Registry(config)
    mgr = WorkspaceManager(registry, config)

    workspace_record = mgr.add_to_workspace(workspace_name, repo_path)
    click.echo(f"Added to workspace '{workspace_record.name}'. Now has {len(workspace_record.repo_ids)} repos.")

    registry.close()


@workspace.command("list")
def workspace_list():
    """List all workspaces."""
    config = load_config()
    registry = Registry(config)
    workspaces = registry.list_workspaces()

    if not workspaces:
        click.echo("No workspaces. Create one with `neurodex workspace create MyApp /path/to/repo1 /path/to/repo2`")
    else:
        for workspace_record in workspaces:
            click.echo(f"\n{workspace_record.name}:")
            for repo_id in workspace_record.repo_ids:
                repo_record = registry.get_repo(repo_id)
                if repo_record:
                    click.echo(f"  - {repo_record.name} ({', '.join(repo_record.local_paths)})")

    registry.close()


@main.command("auto-save")
@click.option("--history-file", type=click.Path(exists=True), help="Path to Claude Code history file")
def auto_save(history_file: str | None):
    """Extract insights from Claude Code session history and save to index.

    Parses assistant messages from history, extracts key decisions and
    architectural insights, and saves them as searchable chunks.
    """
    config = load_config()
    registry = Registry(config)

    if not history_file:
        candidates = [
            Path.home() / ".claude" / "history.jsonl",
            Path.home() / ".claude" / "conversations",
        ]
        for candidate in candidates:
            if candidate.exists():
                history_file = str(candidate)
                break

    if not history_file or not Path(history_file).exists():
        click.echo("No history file found. Pass --history-file or check ~/.claude/")
        registry.close()
        return

    identity = detect_repo(Path.cwd())
    repo = registry.get_repo(identity.repo_id)
    if not repo:
        click.echo(f"Project '{identity.name}' not indexed. Run `neurodex init` first.")
        registry.close()
        return

    db_path = config.repo_db_path(identity.repo_id)
    store = RepoStore(db_path, identity.repo_id, identity.name)

    insights = _extract_insights_from_history(history_file, identity.name)

    if not insights:
        click.echo("No new insights found in history.")
        store.close()
        registry.close()
        return

    chunks = []
    for insight in insights:
        chunks.append(chunk_insight(
            content=insight["content"],
            tags=insight.get("tags", []),
            repo_id=identity.repo_id,
        ))

    store.add_chunks(chunks)
    click.echo(f"Saved {len(chunks)} insights from session history.")

    registry.update_repo_stats(
        identity.repo_id,
        chunk_count=store.get_chunk_count(),
        file_count=store.get_file_count(),
    )

    store.close()
    registry.close()


def _extract_insights_from_history(history_file: str, project_name: str) -> list[dict]:
    """Parse Claude Code history and extract architectural decisions/insights.

    Filters for:
    - Messages mentioning architecture, decisions, or important patterns
    - Messages longer than 200 chars (filters noise)
    - Skips tool calls, file reads, error messages
    """
    history_path = Path(history_file)
    insights: list[dict] = []
    seen_hashes: set[str] = set()

    insight_patterns = [
        r"(?:decided|decision|chose|choosing|picked|went with|architecture|pattern)",
        r"(?:important|critical|note that|keep in mind|remember|caveat|gotcha|quirk)",
        r"(?:the reason|because|tradeoff|trade-off|constraint|requirement)",
        r"(?:bug|issue|problem|fix|workaround|solution|resolved)",
        r"(?:should always|should never|must|don't|avoid|prefer|instead of)",
    ]
    combined_pattern = "|".join(insight_patterns)

    try:
        if history_path.suffix == ".jsonl":
            with open(history_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    _process_history_entry(entry, project_name, combined_pattern,
                                          insights, seen_hashes)
        elif history_path.is_dir():
            for conv_file in sorted(history_path.glob("*.jsonl"))[-5:]:
                with open(conv_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        _process_history_entry(entry, project_name, combined_pattern,
                                              insights, seen_hashes)
    except (OSError, json.JSONDecodeError):
        pass

    return insights


def _process_history_entry(
    entry: dict,
    project_name: str,
    pattern: str,
    insights: list[dict],
    seen_hashes: set[str],
) -> None:
    """Process a single history entry and extract insights."""
    role = entry.get("role", "")
    if role != "assistant":
        return

    content = ""
    if isinstance(entry.get("content"), str):
        content = entry["content"]
    elif isinstance(entry.get("content"), list):
        for block in entry["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                content += block.get("text", "") + "\n"

    if not content or len(content) < 200:
        return

    if content.count("```") > 4:
        return

    if not re.search(pattern, content, re.IGNORECASE):
        return

    content_hash = hashlib.md5(content[:500].encode()).hexdigest()[:12]
    if content_hash in seen_hashes:
        return
    seen_hashes.add(content_hash)

    paragraphs = content.split("\n\n")
    best_paragraph = ""
    for paragraph in paragraphs:
        if re.search(pattern, paragraph, re.IGNORECASE) and len(paragraph) > 100:
            best_paragraph = paragraph
            break

    if not best_paragraph:
        best_paragraph = content[:500]

    tags = []
    tag_patterns = {
        "architecture": r"architect|pattern|design|structure|layer",
        "auth": r"auth|login|token|jwt|oauth|session",
        "database": r"database|sql|query|migration|model|orm",
        "api": r"endpoint|route|api|request|response",
        "performance": r"performance|optimi|cache|slow|fast|n\+1",
        "security": r"security|vulnerab|inject|xss|csrf|sanitiz",
        "deployment": r"deploy|docker|ci|cd|pipeline|release",
        "bug": r"bug|fix|issue|error|crash|broken",
    }
    for tag, tag_pat in tag_patterns.items():
        if re.search(tag_pat, best_paragraph, re.IGNORECASE):
            tags.append(tag)

    insights.append({
        "content": best_paragraph.strip()[:1000],
        "tags": tags[:5],
    })


@main.command()
def install():
    """Add NEURODEX MCP server to Claude Code settings and CLAUDE.md instructions."""
    config = load_config()
    settings_path = Path.home() / ".claude" / "settings.json"

    if settings_path.exists():
        with open(settings_path) as f:
            settings = json.load(f)
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    if "mcpServers" not in settings:
        settings["mcpServers"] = {}

    settings["mcpServers"]["neurodex"] = {
        "command": sys.executable,
        "args": ["-m", "neurodex.server"],
    }

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    click.echo(f"Added MCP server to {settings_path}")

    _install_skill_symlink()
    _inject_claude_md_instructions()

    click.echo("\nRestart Claude Code to activate.")


def _install_skill_symlink():
    """Symlink SKILL.md into both Claude Code and Codex skills directories."""
    skill_source = Path(__file__).parent.parent / "claude-code" / "skills" / "neurodex" / "SKILL.md"

    if not skill_source.exists():
        try:
            import importlib.resources
            package_dir = Path(importlib.resources.files("neurodex")).parent
            skill_source = package_dir / "claude-code" / "skills" / "neurodex" / "SKILL.md"
        except Exception:
            pass

    if not skill_source.exists():
        click.echo("SKILL.md not found in package, skipping skill symlink.")
        return

    resolved_source = skill_source.resolve()

    targets = [
        Path.home() / ".claude" / "skills" / "neurodex" / "SKILL.md",
        Path.home() / ".codex" / "skills" / "neurodex" / "SKILL.md",
    ]

    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(resolved_source)
        platform = "Claude Code" if ".claude" in str(target) else "Codex"
        click.echo(f"  {platform}: {target} → {resolved_source}")


def _inject_claude_md_instructions():
    """Add NEURODEX usage instructions to project's CLAUDE.md for LLM auto-discovery."""
    claude_md = Path.cwd() / "CLAUDE.md"
    neurodex_block = """
## Project Memory (NEURODEX)

This project is indexed by NEURODEX for persistent memory across sessions.

**On session start:** Call `neurodex_status()` to check index health and current context.
**To search:** Use `neurodex_search("descriptive query 3+ words")` before reading files.
**To save insights:** Call `neurodex_save(content="...", tags=["..."])` when you make important decisions.
**Cross-repo search:** Use `neurodex_search(query="...", workspace="WorkspaceName")` for multi-repo projects.
**Trace dependencies:** Use `neurodex_trace(file_path="...")` to understand import chains.
"""

    if claude_md.exists():
        existing = claude_md.read_text()
        if "NEURODEX" in existing:
            click.echo("CLAUDE.md already has NEURODEX instructions.")
            return
        with open(claude_md, "a") as f:
            f.write("\n" + neurodex_block)
        click.echo(f"Added NEURODEX instructions to {claude_md}")
    else:
        click.echo("No CLAUDE.md found. Create one or run `neurodex install` from project root.")


if __name__ == "__main__":
    main()
