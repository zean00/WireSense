"""Diagnostic arm for PROTOCOL-mcp-selection.md's flat-28 registration:
MCP_FLAT_MAX overridden to 28 at runtime (frozen constant untouched),
then the standard runner over the production server set. Registered
before any data; the pre-committed decision rule lives in the protocol."""
import asyncio
import mcp_gate

mcp_gate.MCP_FLAT_MAX = 28

import run_eval

if __name__ == "__main__":
    asyncio.run(run_eval.main())
