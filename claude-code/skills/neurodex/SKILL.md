---
name: neurodex
description: Complete project memory for AI code assistants. Persistent context across sessions.
---

# NEURODEX

Persistent project memory. Call `neurodex_brain` first on every session.

## Tools

### Context
- `mcp__neurodex__neurodex_brain` - Complete project brain. Call this FIRST.
- `mcp__neurodex__neurodex_status` - Index health and project info
- `mcp__neurodex__neurodex_list_projects` - All indexed repos and workspaces

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
