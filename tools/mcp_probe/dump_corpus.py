#!/usr/bin/env python
"""MS-1 corpus dumper: run real public MCP servers, dump tools/list verbatim.

Usage: dump_corpus.py [server ...]   (default: all registered servers)
Output: corpus/<server>.json  =  {command, args, tools: [verbatim tool objects]}
No hand-edited descriptions; whatever tools/list returns is what we keep.
"""
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")
GITHUB_BIN = "/tmp/mcp_probe/github-mcp-server"

SERVERS = [
    {"name": "filesystem",
     "cmd": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
    {"name": "memory",
     "cmd": ["npx", "-y", "@modelcontextprotocol/server-memory"]},
    {"name": "sequential-thinking",
     "cmd": ["npx", "-y", "@modelcontextprotocol/server-sequential-thinking"]},
    {"name": "puppeteer",
     "cmd": ["npx", "-y", "@modelcontextprotocol/server-puppeteer"]},
    {"name": "everything",
     "cmd": ["npx", "-y", "@modelcontextprotocol/server-everything"]},
    {"name": "git",
     "cmd": ["uvx", "mcp-server-git", "--repository",
             "/home/sahal/workspace/wiresense"]},
    {"name": "time",
     "cmd": ["uvx", "mcp-server-time", "--local-timezone", "Asia/Jakarta"]},
    {"name": "fetch",
     "cmd": ["uvx", "mcp-server-fetch"]},
    {"name": "sqlite",
     "cmd": ["uvx", "mcp-server-sqlite", "--db-path", "/tmp/mcp_probe.db"]},
    {"name": "github",
     "cmd": [GITHUB_BIN, "stdio"],
     "env": {"GITHUB_TOKEN": "dummy-token-for-tools-list-only",
             "GITHUB_HOST": "https://github.com"},
     "toolsets": ["repos", "issues", "pull_requests", "code_security",
                  "users", "orgs", "actions", "notifications", "discussions",
                  "gists", "search"]},
]

INIT_REQ = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "mcp-probe", "version": "0.0.1"}}}
INITED_NOTIF = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_REQ = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def read_msg(proc, want_id, deadline):
    """Read newline-delimited JSON-RPC until the response with want_id."""
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed stdout (rc={proc.poll()})")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # non-protocol noise on stdout; spec says none, be safe
        if isinstance(msg, dict) and msg.get("id") == want_id:
            if "error" in msg:
                raise RuntimeError(f"JSON-RPC error: {msg['error']}")
            return msg
    raise RuntimeError("timeout waiting for response")


def dump(server, timeout):
    os.makedirs(CORPUS, exist_ok=True)
    env = dict(os.environ)
    env.update(server.get("env", {}))
    extra_args = []
    if server.get("toolsets"):
        for ts in server["toolsets"]:
            extra_args += ["--toolsets", ts]
    log = open(os.path.join("/tmp", f"mcp_probe_{server['name']}.log"), "w")
    proc = subprocess.Popen(server["cmd"] + extra_args, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=log, text=True,
                            bufsize=1, start_new_session=True, env=env)
    try:
        deadline = time.time() + timeout
        proc.stdin.write(json.dumps(INIT_REQ) + "\n")
        proc.stdin.flush()
        read_msg(proc, 1, deadline)
        proc.stdin.write(json.dumps(INITED_NOTIF) + "\n")
        proc.stdin.write(json.dumps(LIST_REQ) + "\n")
        proc.stdin.flush()
        msg = read_msg(proc, 2, deadline)
        tools = msg["result"].get("tools", [])
        out = {"command": server["cmd"], "args_extra": extra_args,
               "tool_count": len(tools), "tools": tools}
        path = os.path.join(CORPUS, f"{server['name']}.json")
        with open(path, "w") as f:
            json.dump(out, f, indent=2)
        return len(tools), path
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        log.close()


def main():
    wanted = sys.argv[1:] or [s["name"] for s in SERVERS]
    total = 0
    for s in SERVERS:
        if s["name"] not in wanted:
            continue
        t0 = time.time()
        try:
            n, path = dump(s, timeout=240)
            total += n
            print(f"{s['name']:22s} {n:3d} tools  {time.time()-t0:5.1f}s  {path}")
        except Exception as e:
            print(f"{s['name']:22s} FAILED: {e}", file=sys.stderr)
    print(f"TOTAL {total} tools across {len(wanted)} servers")


if __name__ == "__main__":
    main()
