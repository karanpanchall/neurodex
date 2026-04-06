"""Allow running as `python -m engram.server` for MCP."""

import asyncio
from engram.server import main

asyncio.run(main())
