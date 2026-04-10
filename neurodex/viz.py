"""Terminal visualization of the project memory graph.

Renders compact, color-coded views of what Claude sees in memory.
Designed to be called inside a Claude Code session as a CLI command:

  neurodex viz                  - overview of the memory graph
  neurodex viz <symbol>         - focus on a symbol's neighborhood
  neurodex viz --file <path>    - all symbols in a file with edges

Output is plain text with ANSI colors (auto-stripped when piped).
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import click

from neurodex.store import RepoStore


_STATE_FILE = Path.home() / ".config" / "neurodex" / "viz-state.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_STATE_FILE)


def set_focus(
    repo_id: str,
    repo_name: str,
    repo_root: str | None,
    target: str | None = None,
    file: str | None = None,
) -> None:
    """Write the current viz focus so the status line script can pick it up."""
    _save_state({
        "repo_id": repo_id,
        "repo_name": repo_name,
        "repo_root": repo_root,
        "target": target,
        "file": file,
        "updated_at": time.time(),
    })


def _dim(s: str) -> str: return click.style(s, dim=True)
def _b(s: str) -> str: return click.style(s, bold=True)
def _g(s: str) -> str: return click.style(s, fg="green")
def _c(s: str) -> str: return click.style(s, fg="cyan")
def _y(s: str) -> str: return click.style(s, fg="yellow")
def _m(s: str) -> str: return click.style(s, fg="magenta")
def _r(s: str) -> str: return click.style(s, fg="red")


_KIND_COLOR = {
    "IMPORTS": _c,
    "CALLS": _y,
    "INHERITS": _m,
    "IMPLEMENTS": _m,
    "TESTED_BY": _g,
}


def _short(path: str, root: str | None) -> str:
    """Make a project-relative path, falling back to last 3 segments."""
    if root and path.startswith(root):
        return path[len(root):].lstrip("/")
    parts = Path(path).parts
    return "/".join(parts[-3:]) if len(parts) > 3 else path


def _bar(value: int, max_value: int, width: int = 20) -> str:
    if max_value <= 0:
        return ""
    filled = max(1, round(value / max_value * width)) if value > 0 else 0
    return "█" * filled + "·" * (width - filled)


def _project_top_dirs(store: RepoStore, root: str | None) -> set[str]:
    """Return the top-level directories that hold project source files."""
    tops: set[str] = set()
    for fp in store.get_all_file_paths():
        rel = fp[len(root):].lstrip("/") if (root and fp.startswith(root)) else fp
        parts = rel.split("/")
        if len(parts) > 1:
            tops.add(parts[0])
    return tops


# ─── Overview ────────────────────────────────────────────────────────


def render_overview(store: RepoStore, repo_root: str | None = None) -> str:
    out: list[str] = []

    n_files = store.get_file_count()
    n_chunks = store.get_chunk_count()
    n_nodes = store.get_node_count()
    edge_stats = store.get_edge_stats()
    n_edges = sum(edge_stats.values())

    if store.repo_name.lower() == "neurodex":
        out.append(_b("NEURODEX"))
    else:
        out.append(_b(f"NEURODEX  {store.repo_name}"))
    out.append(_dim(
        f"  {n_files} files  ·  {n_nodes} symbols  ·  {n_edges} edges  ·  {n_chunks} chunks"
    ))
    out.append("")

    # Edge breakdown
    out.append(_b("EDGES"))
    if edge_stats:
        max_count = max(edge_stats.values())
        for kind in sorted(edge_stats, key=lambda k: -edge_stats[k]):
            cnt = edge_stats[kind]
            color = _KIND_COLOR.get(kind, lambda s: s)
            out.append(f"  {color(kind.ljust(10))} {_bar(cnt, max_count, 24)} {cnt}")
    else:
        out.append(_dim("  (no edges indexed)"))
    out.append("")

    # Symbol kinds
    nodes = store.get_all_nodes()
    by_kind: Counter[str] = Counter(n["kind"] for n in nodes)
    by_file: Counter[str] = Counter(n["file_path"] for n in nodes)

    if by_kind:
        out.append(_b("SYMBOL KINDS"))
        max_k = max(by_kind.values())
        for k, c in by_kind.most_common():
            out.append(f"  {k.ljust(12)} {_bar(c, max_k, 20)} {c}")
        out.append("")

    # Top files by symbol density
    out.append(_b("TOP FILES BY SYMBOL DENSITY"))
    top = by_file.most_common(10)
    if top:
        max_c = top[0][1]
        for fp, c in top:
            short = _short(fp, repo_root)
            out.append(f"  {short:<42.42} {_bar(c, max_c, 16)} {c}")
    out.append("")

    # Hub files (most imported)
    importer_count: Counter[str] = Counter()
    rows = store._conn.execute(
        "SELECT source_file, target_symbol FROM edges WHERE kind='IMPORTS'"
    ).fetchall()
    tops = _project_top_dirs(store, repo_root)
    for r in rows:
        target = r["target_symbol"] or ""
        first = target.split(".", 1)[0]
        if first in tops:
            importer_count[target] += 1

    if importer_count:
        out.append(_b("MOST-IMPORTED MODULES (internal)"))
        max_v = max(importer_count.values())
        for mod, c in importer_count.most_common(8):
            out.append(f"  {_c(mod):<30} {_bar(c, max_v, 14)} {c}")
        out.append("")

    # Module dependency arrows
    mod_edges: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        src_path = r["source_file"]
        rel = src_path[len(repo_root):].lstrip("/") if (repo_root and src_path.startswith(repo_root)) else src_path
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        src_mod = parts[0]
        target = (r["target_symbol"] or "").split(".")
        if target and target[0] in tops:
            tgt_mod = target[0]
            if tgt_mod != src_mod:
                mod_edges[src_mod].add(tgt_mod)

    if mod_edges:
        out.append(_b("MODULE → MODULE"))
        for src in sorted(mod_edges):
            targets = sorted(mod_edges[src])
            out.append(f"  {_c(src)} → {', '.join(_y(t) for t in targets)}")
        out.append("")

    # Saved memory (insights)
    insights = store._conn.execute(
        "SELECT content FROM chunks WHERE chunk_type='insight' "
        "ORDER BY indexed_at DESC LIMIT 5"
    ).fetchall()
    if insights:
        out.append(_b("SAVED MEMORY"))
        for r in insights:
            txt = (r["content"] or "").strip().replace("\n", " ")[:100]
            out.append(f"  {_r('!')} {txt}")
        out.append("")

    out.append(_dim(
        "hint:  neurodex viz <symbol>     focus on a node\n"
        "       neurodex viz --file PATH  show a file's symbols and edges"
    ))
    return "\n".join(out)


# ─── Symbol focus ────────────────────────────────────────────────────


def render_symbol(store: RepoStore, name: str, repo_root: str | None = None) -> str:
    exact = store._conn.execute(
        "SELECT * FROM nodes WHERE name=? ORDER BY file_path",
        (name,),
    ).fetchall()
    if exact:
        matches = [dict(r) for r in exact]
    else:
        rows = store._conn.execute(
            "SELECT * FROM nodes WHERE name LIKE ? ORDER BY file_path LIMIT 20",
            (f"%{name}%",),
        ).fetchall()
        matches = [dict(r) for r in rows]

    if not matches:
        return _r(f"No symbol matches '{name}'.") + "\n" + _dim(
            "Try `neurodex viz` for an overview, or `neurodex search \"...\"`."
        )

    primary = matches[0]
    out: list[str] = []

    out.append(_b(f"SYMBOL  {primary['name']}"))
    out.append(_dim(
        f"  {primary['kind']}  in  {_short(primary['file_path'], repo_root)}"
        f":{primary['line_start']}-{primary['line_end']}"
    ))
    sig = (primary.get("signature") or "").strip()
    redundant = (
        not sig
        or sig == primary["name"]
        or (primary["kind"] == "class" and sig in {f"class {primary['name']}", f"class {primary['name']}:"})
    )
    if not redundant:
        out.append(f"  {_c(sig[:140])}")
    if primary.get("parent_name"):
        out.append(_dim(f"  parent: {primary['parent_name']}"))
    out.append("")

    if len(matches) > 1:
        out.append(_dim(f"  {len(matches)} symbols match this name:"))
        for m in matches[:8]:
            marker = "›" if m is primary else " "
            out.append(_dim(
                f"    {marker} {m['kind']:<8} {_short(m['file_path'], repo_root)}"
                f":{m['line_start']}"
            ))
        if len(matches) > 8:
            out.append(_dim(f"    ... and {len(matches) - 8} more"))
        out.append("")

    refs = store.find_all_references(primary["name"])

    # IMPORTS edges target module paths (e.g. neurodex.store), not symbols.
    # For class/function symbols we also look up the containing module so the
    # "imported by" view actually answers "who depends on this code".
    module_importers: list[str] = []
    file_path = primary["file_path"]
    if file_path.endswith(".py"):
        rel = file_path[len(repo_root):].lstrip("/") if (repo_root and file_path.startswith(repo_root)) else file_path
        module_dotted = rel[:-3].replace("/", ".")
        candidates = {module_dotted}
        if "." in module_dotted:
            candidates.add(module_dotted.split(".", 1)[1])  # drop top package
        for cand in candidates:
            rows_imp = store._conn.execute(
                "SELECT DISTINCT source_file FROM edges "
                "WHERE kind='IMPORTS' AND target_symbol=?",
                (cand,),
            ).fetchall()
            for r in rows_imp:
                if r["source_file"] != file_path:
                    module_importers.append(r["source_file"])
        # de-dupe while preserving order
        seen_mi: set[str] = set()
        module_importers = [f for f in module_importers if not (f in seen_mi or seen_mi.add(f))]

    out.append(_b("CALLED BY") + _dim(f"  ({len(refs['called_by'])})"))
    seen: set[tuple] = set()
    shown = 0
    for r in refs["called_by"]:
        key = (r.get("source_file"), r.get("line"), r.get("source_symbol"))
        if key in seen:
            continue
        seen.add(key)
        line = r.get("line") or "?"
        sym = r.get("source_symbol") or ""
        suffix = f"  {_dim('· ' + sym)}" if sym else ""
        out.append(f"  ← {_short(r['source_file'], repo_root)}:{line}{suffix}")
        shown += 1
        if shown >= 15:
            break
    if not refs["called_by"]:
        out.append(_dim("  (none)"))
    elif len(refs["called_by"]) > shown:
        out.append(_dim(f"  ... and {len(refs['called_by']) - shown} more"))
    out.append("")

    out.append(_b("INHERITED BY") + _dim(f"  ({len(refs['inherited_by'])})"))
    if refs["inherited_by"]:
        for r in refs["inherited_by"][:10]:
            sym = r.get("source_symbol") or ""
            suffix = f"  {_dim('· ' + sym)}" if sym else ""
            out.append(f"  ⇡ {_short(r['source_file'], repo_root)}{suffix}")
    else:
        out.append(_dim("  (none)"))
    out.append("")

    importer_files: list[str] = [r["file"] for r in refs["imported_by"]]
    for f in module_importers:
        if f not in importer_files:
            importer_files.append(f)

    out.append(_b("IMPORTED BY") + _dim(f"  ({len(importer_files)})"))
    if importer_files:
        for f in importer_files[:12]:
            out.append(f"  ⇠ {_short(f, repo_root)}")
        if len(importer_files) > 12:
            out.append(_dim(f"  ... and {len(importer_files) - 12} more"))
    else:
        out.append(_dim("  (none)"))
    out.append("")

    known = set(importer_files)
    known.update(r.get("source_file") for r in refs["called_by"])
    known.update(r.get("source_file") for r in refs["inherited_by"])
    extra_refs = [r for r in refs["referenced_in"] if r["file"] not in known]
    if extra_refs:
        out.append(_b("ALSO REFERENCED IN") + _dim(f"  ({len(extra_refs)})"))
        for r in extra_refs[:8]:
            out.append(f"  · {_short(r['file'], repo_root)}:{r.get('lines','')}")
        out.append("")

    # Outgoing calls from this symbol's file (best-effort proxy for callees)
    file_path = primary["file_path"]
    out_edges = store._conn.execute(
        "SELECT DISTINCT kind, target_symbol FROM edges "
        "WHERE source_file=? AND kind IN ('CALLS','INHERITS') ORDER BY kind, target_symbol",
        (file_path,),
    ).fetchall()
    if out_edges:
        out.append(_b("OUTGOING") + _dim(f"  (from {Path(file_path).name})"))
        for r in out_edges[:20]:
            color = _KIND_COLOR.get(r["kind"], lambda s: s)
            out.append(f"  → {color(r['kind']):<10} {r['target_symbol']}")
        if len(out_edges) > 20:
            out.append(_dim(f"  ... and {len(out_edges) - 20} more"))
        out.append("")

    out.append(_dim(
        f"total references: {refs['total_references']}  ·  unique files: {refs['unique_files']}"
    ))
    return "\n".join(out)


# ─── File view ───────────────────────────────────────────────────────


def render_file(store: RepoStore, file_query: str, repo_root: str | None = None) -> str:
    rows = store._conn.execute(
        "SELECT DISTINCT file_path FROM nodes WHERE file_path LIKE ? "
        "ORDER BY length(file_path) LIMIT 5",
        (f"%{file_query}%",),
    ).fetchall()
    if not rows:
        rows = store._conn.execute(
            "SELECT DISTINCT file_path FROM chunks WHERE file_path LIKE ? "
            "ORDER BY length(file_path) LIMIT 5",
            (f"%{file_query}%",),
        ).fetchall()
    if not rows:
        return _r(f"No file matches '{file_query}'.")

    actual = rows[0]["file_path"]
    others = [r["file_path"] for r in rows[1:]]

    nodes = store.get_nodes_in_file(actual)
    imports = store.get_imports(actual)
    out_edges = store.get_edges_from(actual)
    edge_kinds = Counter(e["kind"] for e in out_edges)
    non_import_kinds = {k: v for k, v in edge_kinds.items() if k != "IMPORTS"}

    out: list[str] = []
    out.append(_b(f"FILE  {_short(actual, repo_root)}"))
    summary_parts = [f"{len(nodes)} symbols", f"{len(imports)} imports"]
    if non_import_kinds:
        summary_parts.append(
            ", ".join(f"{v} {k}" for k, v in non_import_kinds.items())
        )
    out.append(_dim("  " + "  ·  ".join(summary_parts)))
    if others:
        out.append(_dim(f"  ({len(others)} other files also matched — showing closest)"))
        for o in others[:3]:
            out.append(_dim(f"    · {_short(o, repo_root)}"))
    out.append("")

    if imports:
        out.append(_b("IMPORTS") + _dim(f"  ({len(imports)})"))
        for imp in imports[:25]:
            out.append(f"  ⇠ {_c(imp)}")
        if len(imports) > 25:
            out.append(_dim(f"  ... and {len(imports) - 25} more"))
        out.append("")

    if not nodes:
        out.append(_dim("(no symbols indexed for this file)"))
        return "\n".join(out)

    # Build external-caller index per symbol name in one pass
    name_to_callers: dict[str, set[str]] = defaultdict(set)
    sym_names = [n["name"] for n in nodes]
    placeholder = ",".join("?" for _ in sym_names)
    if sym_names:
        rows2 = store._conn.execute(
            f"SELECT source_file, target_symbol FROM edges "
            f"WHERE kind='CALLS' AND target_symbol IN ({placeholder})",
            sym_names,
        ).fetchall()
        for r in rows2:
            if r["source_file"] != actual:
                name_to_callers[r["target_symbol"]].add(r["source_file"])

    by_parent: dict[str | None, list[dict]] = defaultdict(list)
    for n in nodes:
        by_parent[n.get("parent_name")].append(n)

    def _is_redundant_sig(sig: str, name: str, kind: str) -> bool:
        if not sig:
            return True
        sig = sig.strip()
        if sig == name:
            return True
        if kind == "class" and sig in {f"class {name}", f"class {name}:"}:
            return True
        return False

    out.append(_b("SYMBOLS"))
    top_level = sorted(by_parent.get(None, []), key=lambda n: n["line_start"])
    for n in top_level:
        callers = name_to_callers.get(n["name"], set())
        cstr = _dim(f"  ← {len(callers)} ext") if callers else ""
        kind = _m(n["kind"])
        line = _dim(f":{n['line_start']}")
        sig = (n.get("signature") or "").strip()
        out.append(f"  {kind} {_b(n['name'])}{line}{cstr}")
        if not _is_redundant_sig(sig, n["name"], n["kind"]):
            out.append(f"      {_dim(sig[:110])}")
        if n["kind"] == "class":
            methods = sorted(by_parent.get(n["name"], []), key=lambda x: x["line_start"])
            for m in methods:
                mc = name_to_callers.get(m["name"], set())
                mcstr = _dim(f"  ← {len(mc)} ext") if mc else ""
                msig = m.get("signature") or m["name"]
                out.append(f"    · {msig[:90]}{mcstr}")

    # Methods whose declared parent isn't in this file
    orphan_parents = set(by_parent.keys()) - {None} - {n["name"] for n in top_level}
    for p in sorted(orphan_parents):
        out.append(f"  {_dim('class')} {_dim(p)} {_dim('(declared elsewhere)')}")
        for m in by_parent[p]:
            mc = name_to_callers.get(m["name"], set())
            mcstr = _dim(f"  ← {len(mc)} ext") if mc else ""
            out.append(f"    · {(m.get('signature') or m['name'])[:90]}{mcstr}")

    return "\n".join(out)


# ─── Status line (compact one-liner for Claude Code statusLine) ──────


def render_statusline(store: RepoStore, repo_root: str | None, focus: dict | None = None) -> str:
    """Render a single-line status summary for Claude Code's statusLine slot.

    Format (ANSI-colored):
        ▲ neurodex · myproject 36f·271s·305e  →  RepoStore ← 10imp · 7call · store.py:53
    """
    n_files = store.get_file_count()
    n_nodes = store.get_node_count()
    edge_stats = store.get_edge_stats()
    n_edges = sum(edge_stats.values())

    # Avoid "neurodex neurodex" when the repo itself is named the same as
    # the product.
    if store.repo_name.lower() == "neurodex":
        head = _b("▲ neurodex") + _dim(f"  {n_files}f·{n_nodes}s·{n_edges}e")
    else:
        head = (
            _dim("▲ neurodex ") + _b(store.repo_name)
            + _dim(f"  {n_files}f·{n_nodes}s·{n_edges}e")
        )

    if not focus:
        return head

    target = focus.get("target")
    file_query = focus.get("file")

    if target:
        row = store._conn.execute(
            "SELECT * FROM nodes WHERE name=? LIMIT 1", (target,)
        ).fetchone()
        if not row:
            row = store._conn.execute(
                "SELECT * FROM nodes WHERE name LIKE ? LIMIT 1", (f"%{target}%",)
            ).fetchone()
        if row:
            node = dict(row)
            refs = store.find_all_references(node["name"])

            # Resolve importers of the containing module too.
            importer_files: set[str] = {r["file"] for r in refs["imported_by"]}
            fp = node["file_path"]
            if fp.endswith(".py"):
                rel = fp[len(repo_root):].lstrip("/") if (repo_root and fp.startswith(repo_root)) else fp
                dotted = rel[:-3].replace("/", ".")
                candidates = {dotted}
                if "." in dotted:
                    candidates.add(dotted.split(".", 1)[1])
                for cand in candidates:
                    rows = store._conn.execute(
                        "SELECT DISTINCT source_file FROM edges "
                        "WHERE kind='IMPORTS' AND target_symbol=?",
                        (cand,),
                    ).fetchall()
                    for r in rows:
                        if r["source_file"] != fp:
                            importer_files.add(r["source_file"])

            callers = len({(r.get("source_file"), r.get("line")) for r in refs["called_by"]})
            imp = len(importer_files)
            loc = f"{Path(fp).name}:{node['line_start']}"
            return (
                head + _dim("  →  ")
                + _b(node["name"])
                + _dim("  ") + _c(f"←{imp}imp")
                + _dim(" · ") + _y(f"{callers}call")
                + _dim(" · " + loc)
            )
        return head + _dim("  →  ") + _r(f"?{target}")

    if file_query:
        rows = store._conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE file_path LIKE ? "
            "ORDER BY length(file_path) LIMIT 1",
            (f"%{file_query}%",),
        ).fetchall()
        if rows:
            fp = rows[0]["file_path"]
            n_syms = len(store.get_nodes_in_file(fp))
            n_imp = len(store.get_imports(fp))
            return (
                head + _dim("  →  ")
                + _b(Path(fp).name)
                + _dim("  ") + _c(f"{n_syms}sym")
                + _dim(" · ") + _y(f"{n_imp}imp")
            )

    return head
