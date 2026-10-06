#!/usr/bin/env python
"""Live smoke for the MCP integration (no audio path): drives
Session.tool_flow() directly with a fake WS over real MCP servers.

Covers: direct read-tool call, risky-tool park (write heuristic) + manual
YES execution (the exact call respond() makes), null turn, dead-server
reconnect. Uses /tmp cache so the production mcp_index.json is untouched.

Usage: tools/mcp_smoke.py   (from repo root or anywhere; needs llama-server)
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.abspath(os.path.join(HERE, "..", "web"))
sys.path.insert(0, WEB)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "mcp_eval")))
os.chdir(WEB)  # server.py resolves MODELS relative to cwd (runs from web/)

import mcp_client  # noqa: E402
import mcp_gate  # noqa: E402
import server  # noqa: E402
from gen_eval import SERVERS  # noqa: E402

DEMO = ["filesystem", "time", "everything", "git"]


class FakeWS:
    def __init__(self):
        self.events = []

    async def send(self, obj):
        self.events.append(obj)
        print(f"    ws: {json.dumps(obj)[:110]}")


async def main():
    servers = [mcp_client.MCPServer({"name": n, **SERVERS[n]}) for n in DEMO]
    for s in servers:
        s.start()
    gate = mcp_gate.Gate(server.LLAMA, server.norm_router_text,
                         server.native_manifest_lines(), servers,
                         cache_path="/tmp/mcp_index_smoke.json",
                         build_prompt=server.build_prompt)
    gate.build_index()
    server.MCP_GATE = gate
    print(f"gate active={gate.active()} over {len(gate.tools)} MCP tools\n")

    ws = FakeWS()
    s = server.Session(ws)

    print("1. direct read call (time__get_current_time)")
    reply, skip = await s.tool_flow("What is the current time in New York?")
    print(f"    reply: {reply!r}")
    assert reply and "New York" in reply, "no confirmation for time call"

    print("2. risky park (filesystem write) + manual YES execution")
    reply, skip = await s.tool_flow("Create a file named wiresense_smoke.txt "
                                    "in /tmp containing the word hello")
    print(f"    reply: {reply!r}")
    assert s.pending and s.pending["kind"] == "tool", "risky call not parked"
    assert "__" in s.pending["tool"], "pending tool not namespaced"
    result = await s.run_tool(s.pending["tool"], s.pending["args"])
    print(f"    YES -> run_tool: {result!r}")
    assert os.path.exists("/tmp/wiresense_smoke.txt"), "file not written"
    s.pending = None

    print("3. null turn (chat)")
    reply, skip = await s.tool_flow("What's the weather in Jakarta")
    print(f"    reply: {reply!r}")
    assert reply is None, "chat turn should fall through (None)"

    print("4. dead-server reconnect (kill time, call again)")
    tsrv = next(x for x in servers if x.name == "time")
    tsrv.kill()
    reply, skip = await s.tool_flow("What time is it in Tokyo right now")
    print(f"    reply: {reply!r}")
    assert reply, "no reply after reconnect"

    print("\nSMOKE PASS")
    for srv in servers:
        srv.kill()


if __name__ == "__main__":
    asyncio.run(main())
