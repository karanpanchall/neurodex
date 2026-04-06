---
name: engram
description: Complete project memory for AI code assistants. Persistent context across sessions.
---

# ENGRAM

Persistent project memory. Call `engram_brain` first on every session.

## Tools

### Context
- `mcp__engram__engram_brain` - Complete project brain. Call this FIRST.
- `mcp__engram__engram_status` - Index health and project info
- `mcp__engram__engram_list_projects` - All indexed repos and workspaces

### Search
- `mcp__engram__engram_search` - Full-text search with synonym expansion
- `mcp__engram__engram_compact_search` - Metadata-only search (saves tokens)
- `mcp__engram__engram_symbols` - Find functions/classes by name pattern

### Analysis
- `mcp__engram__engram_references` - Find ALL references to a symbol
- `mcp__engram__engram_impact` - Blast-radius: what breaks if you change this?
- `mcp__engram__engram_cross_impact` - Cross-project: what breaks in other repos?
- `mcp__engram__engram_trace` - Follow dependency chains

### Memory
- `mcp__engram__engram_save` - Save a decision or insight for future sessions

### Projects
- `mcp__engram__engram_workspace_create` - Group repos for cross-repo search
- `mcp__engram__engram_workspace_add` - Add repo to workspace
- `mcp__engram__engram_set_context` - Set session search scope

## Session Workflow

```
1. engram_brain()                              → Know the entire project
2. engram_references("SymbolName")             → Find all usages before changing
3. engram_impact("file.py")                    → Check blast radius
4. [make changes]
5. engram_save("Renamed X to Y because...")    → Persist for next session
```
