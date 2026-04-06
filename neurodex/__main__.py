"""Allow running as `python -m neurodex.server` for MCP."""

import asyncio
from neurodex.server import main

asyncio.run(main())
