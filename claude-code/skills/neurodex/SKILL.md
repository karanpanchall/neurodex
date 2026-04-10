---
name: neurodex
description: Complete project memory for AI code assistants. Persistent context across sessions.
---

# NEURODEX

Persistent project memory. Call `neurodex_brain` first on every session.

## HARD RULE — use viz before Grep on symbols

When you're about to `Grep` for a function, class, method, or variable name,
or `Read` a source file to understand its structure, **call
`mcp__neurodex__neurodex_viz` first**:

- Looking up a symbol? → `mcp__neurodex__neurodex_viz target="SymbolName"`
  (returns definition, callers, importers, references — in one tool call,
  no ANSI, ~30 lines).
- Understanding a file? → `mcp__neurodex__neurodex_viz file="path/to/x.py"`
  (imports + every symbol with signatures + external-caller counts).
- Not sure what's indexed? → `mcp__neurodex__neurodex_viz` with no args
  (project overview).

A `PreToolUse` hook auto-injects viz output when you Grep a known symbol,
so you'll see the graph view either way — but calling viz directly is
cheaper, more accurate, and keeps your investigation on the graph rather
than line-by-line text matching.

## Tools

### Context
- `mcp__neurodex__neurodex_brain` - Complete project brain. Call this FIRST.
- `mcp__neurodex__neurodex_status` - Index health and project info
- `mcp__neurodex__neurodex_list_projects` - All indexed repos and workspaces

### Visualize (see what Claude sees)
- `mcp__neurodex__neurodex_viz` - Render the memory graph as scannable text.
  Three modes in one tool:
  - **No args** → overview: file/symbol/edge counts, edge-kind bar chart,
    top files by symbol density, most-imported internal modules.
  - **`target="SymbolName"`** → symbol focus: definition, callers, importers
    (resolved through containing module), inheritors, text references.
  - **`file="path/to/file.py"`** → file view: imports + every symbol in line
    order with signatures, methods nested, per-symbol external-caller count.
  Calling this tool also updates `~/.config/neurodex/viz-state.json`, which
  the `neurodex statusline` command displays below Claude Code's input box.

### Slash command
- **`/neurodex viz`** → overview
- **`/neurodex viz SymbolName`** → symbol focus
- **`/neurodex viz --file store.py`** → file view

  The slash command expands to a prompt instructing you to call
  `mcp__neurodex__neurodex_viz` with the parsed arguments and paste the
  output in a fenced code block. It also refreshes the status line.

### Search
- `mcp__neurodex__neurodex_search` - Full-text search with synonym expansion
- `mcp__neurodex__neurodex_compact_search` - Metadata-only search (saves tokens)
- `mcp__neurodex__neurodex_symbols` - Find functions/classes by name pattern

### Analysis
- `mcp__neurodex__neurodex_references` - Find ALL references to a symbol
- `mcp__neurodex__neurodex_impact` - Blast-radius: what breaks if you change this?
- `mcp__neurodex__neurodex_cross_impact` - Cross-project: what breaks in other repos?
- `mcp__neurodex__neurodex_trace` - Follow dependency chains

### Memory
- `mcp__neurodex__neurodex_save` - Save a decision or insight for future sessions

### Projects
- `mcp__neurodex__neurodex_workspace_create` - Group repos for cross-repo search
- `mcp__neurodex__neurodex_workspace_add` - Add repo to workspace
- `mcp__neurodex__neurodex_set_context` - Set session search scope

## Session Workflow

```
1. neurodex_brain()                              → Know the entire project
2. neurodex_references("SymbolName")             → Find all usages before changing
3. neurodex_impact("file.py")                    → Check blast radius
4. [make changes]
5. neurodex_save("Renamed X to Y because...")    → Persist for next session
```
