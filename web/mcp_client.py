"""MCP stdio client for wiresense: transport + lifecycle.

One MCPServer per configured backend, spawned with its own process group
(killable as a unit, the tools/mcp_probe/dump_corpus.py pattern). The wire
protocol is the probe's: newline-delimited JSON-RPC, initialize (2024-11-05)
-> notifications/initialized -> tools/list, responses id-matched with noise
lines skipped. All blocking calls run under asyncio.to_thread at the call
sites; a wedged server is unblocked by killing its process group (readline
sees EOF, the thread exits).

House rules carried in code: secrets never live in web/config.json — an env
value must be a "$ENV_NAME" reference expanded from the environment (the
api_key_env precedent), and a literal value for a TOKEN/KEY/SECRET/PASSWORD
key is refused at load. A dead server is retried lazily on the next call
(60 s cooldown) and by the supervisor task every 60 s; while down its tools
are absent from the manifest.
"""
import asyncio
import json
import os
import signal
import subprocess
import time

INIT_PROTOCOL = "2024-11-05"
RESPAWN_COOLDOWN_S = 60.0
SECRET_KEY_RE = ("TOKEN", "KEY", "SECRET", "PASSWORD")


class MCPError(RuntimeError):
    pass


def expand_env(env_cfg, name):
    """$ENV_NAME references expand from os.environ; literal secrets refuse."""
    out = {}
    for k, v in (env_cfg or {}).items():
        if isinstance(v, str) and v.startswith("$"):
            out[k] = os.environ.get(v[1:], "")
        elif any(s in k.upper() for s in SECRET_KEY_RE):
            raise ValueError(
                f"mcp/{name}: literal value for {k} in config — use "
                f"\"$ENV_NAME\" (secrets never live in config files)")
        else:
            out[k] = v
    return out


class MCPServer:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.argv = list(cfg["cmd"]) + list(cfg.get("args") or [])
        self.env = expand_env(cfg.get("env"), self.name)
        self.confirm = cfg.get("confirm")  # True/False override; None = heuristic
        self.call_timeout = float(cfg.get("timeout_s", 30))
        self.tools = []        # verbatim tools/list entries
        self.down = True
        self.proc = None
        self._id = 0
        self._last_start = 0.0
        self._log = None

    # --- transport -------------------------------------------------------

    def _read_msg(self, want_id, deadline):
        """Newline-JSON-RPC read until the response with want_id (probe
        pattern: skip noise, raise on JSON-RPC error / closed stdout)."""
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise MCPError(
                    f"{self.name}: server closed stdout (rc={self.proc.poll()})")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                if "error" in msg:
                    raise MCPError(f"{self.name}: {msg['error']}")
                return msg
        raise MCPError(f"{self.name}: timeout waiting for response {want_id}")

    def _rpc(self, method, params, timeout):
        self._id += 1
        rid = self._id
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method,
             "params": params}) + "\n")
        self.proc.stdin.flush()
        return self._read_msg(rid, time.time() + timeout)["result"]

    def _notify(self, method):
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    # --- lifecycle -------------------------------------------------------

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self.proc = None
        self.down = True
        self.tools = []

    def start(self, timeout=90.0):
        """Spawn + handshake + tools/list. Raises MCPError; on any failure
        the process group is reaped so nothing leaks."""
        if self._log is not None:
            self._log.close()
        self._log = open(f"/tmp/wiresense_mcp_{self.name}.log", "w")
        env = dict(os.environ)
        env.update(self.env)
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._log, text=True, bufsize=1,
                start_new_session=True, env=env)
            self._rpc("initialize",
                      {"protocolVersion": INIT_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "wiresense", "version": "0.0.1"}},
                      timeout)
            self._notify("notifications/initialized")
            self.tools = self._rpc("tools/list", {}, timeout).get("tools", [])
            self.down = False
            self._last_start = time.time()
            return len(self.tools)
        except Exception:
            self.kill()
            raise

    def restart(self, timeout=90.0, cooldown=RESPAWN_COOLDOWN_S):
        """One respawn attempt, rate-limited; returns tool count or None."""
        if time.time() - self._last_start < cooldown:
            return None
        self.kill()
        try:
            n = self.start(timeout)
            print(f"[mcp] {self.name}: reconnected, {n} tools", flush=True)
            return n
        except Exception as e:
            print(f"[mcp] {self.name}: reconnect failed: {e}", flush=True)
            return None

    # --- tool calls ------------------------------------------------------

    def call_tool(self, name, args):
        """tools/call -> joined text content. Raises MCPError."""
        result = self._rpc("tools/call",
                           {"name": name, "arguments": args},
                           self.call_timeout)
        if result.get("isError"):
            raise MCPError(f"{self.name}.{name}: "
                           + self._content_text(result) or "tool error")
        return self._content_text(result)

    @staticmethod
    def _content_text(result):
        parts = [c.get("text", "") for c in result.get("content", [])
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(p for p in parts if p)

    async def call_tool_async(self, name, args):
        """to_thread wrapper with a hard asyncio timeout: on timeout the
        process group is killed so the blocked reader thread sees EOF."""
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, self.call_tool, name, args)
        try:
            return await asyncio.wait_for(fut, self.call_timeout + 15)
        except asyncio.TimeoutError:
            self.kill()
            raise MCPError(f"{self.name}.{name}: timed out "
                           f"({self.call_timeout:.0f}s), server killed")

    def ensure_alive(self):
        """Lazy reconnect before a call; None on failure (caller degrades)."""
        if self.alive():
            return True
        return self.restart() is not None


def connect_all(mcp_cfg, timeout=90.0):
    """Connect every enabled server; failures log and leave that server
    down (its tools simply never appear). Returns the MCPServer list."""
    servers = []
    for cfg in (mcp_cfg or {}).get("servers", []):
        if not cfg.get("enabled", True):
            continue
        s = MCPServer(cfg)
        try:
            n = s.start(timeout)
            print(f"[mcp] {s.name}: {n} tools", flush=True)
        except Exception as e:
            print(f"[mcp] {s.name}: FAILED at startup: {e}", flush=True)
        servers.append(s)
    return servers


async def supervisor(servers, interval=60.0):
    """Reconnect down servers forever (startup failures included)."""
    while True:
        await asyncio.sleep(interval)
        for s in servers:
            if s.down:
                await asyncio.to_thread(s.restart)


def from_config(cfg):
    """The main() entry: MCPServer list for a config 'mcp' block, or None
    when the block is absent/disabled — byte-identical behavior either way
    (the face precedent)."""
    if not cfg or not cfg.get("enabled"):
        return None
    return connect_all(cfg)
