---
description: Visualize the NEURODEX memory graph — overview, symbol focus, or file view
allowed-tools: [mcp__neurodex__neurodex_viz]
argument-hint: "[symbol_name | --file path]"
---

The user ran `/neurodex viz $ARGUMENTS`.

Call the `mcp__neurodex__neurodex_viz` tool and render its output verbatim in your reply. Rules for translating `$ARGUMENTS`:

- Empty → call the tool with **no arguments** (overview mode).
- Starts with `--file ` or `-f ` → strip that flag and pass the rest as `{"file": "..."}`.
- Anything else → treat as a symbol name and pass it as `{"target": "..."}`.

After the tool returns, paste the text into your reply inside a fenced code block so the monospace graph lines up. Do not summarize or restate it — the user wants the raw view.

The same call updates `~/.config/neurodex/viz-state.json`, so the `neurodex statusline` command rendered below the input box will also refresh with the new focus on its next tick. Do not mention this — just let the bar update.
