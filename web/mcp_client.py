"""MCP client for wiresense: transports (stdio + remote HTTP) + lifecycle.

One MCPServer per configured backend. stdio: its own process group
(killable as a unit, the tools/mcp_probe/dump_corpus.py pattern), the
probe's wire protocol — newline-delimited JSON-RPC, initialize (2024-11-05)
-> notifications/initialized -> tools/list, responses id-matched with noise
lines skipped. Remote (config `url`, entry BD): Streamable HTTP, POST one
endpoint, answer is application/json or an SSE stream on the same response,
optional Mcp-Session-Id; auth is NOT implemented (only no-auth servers
connect; a 401/403 fails honestly at initialize). All blocking calls run
under asyncio.to_thread at the call sites; a wedged stdio server is
unblocked by killing its process group (readline sees EOF), a wedged
remote call by dropping the session.

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

import requests

INIT_PROTOCOL = "2024-11-05"    # stdio (probe-verified)
HTTP_PROTOCOL = "2025-03-26"    # streamable HTTP (entry BD)
RESPAWN_COOLDOWN_S = 60.0
SECRET_KEY_RE = ("TOKEN", "KEY", "SECRET", "PASSWORD")


class MCPError(RuntimeError):
    pass


class MCPSessionGone(MCPError):
    """Remote: the server dropped our session (404 on a sessionful POST) —
    one transparent re-initialize + retry is allowed before failing."""


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
        self.url = cfg.get("url")  # set -> remote mode (entry BD)
        self.argv = (None if self.url is not None
                     else list(cfg["cmd"]) + list(cfg.get("args") or []))
        self.env = expand_env(cfg.get("env"), self.name)
        self.confirm = cfg.get("confirm")  # True/False override; None = heuristic
        self.call_timeout = float(cfg.get("timeout_s", 30))
        self.tools = []        # verbatim tools/list entries
        self.down = True
        self.proc = None
        self._open = False     # remote: a handshake completed on this session
        self._session_id = None
        self.protocol = None   # negotiated protocolVersion (remote)
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
        if self.url is not None:
            return self._rpc_remote(rid, method, params, timeout)
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method,
             "params": params}) + "\n")
        self.proc.stdin.flush()
        return self._read_msg(rid, time.time() + timeout)["result"]

    def _notify(self, method):
        if self.url is not None:
            r = self._http_post({"jsonrpc": "2.0", "method": method},
                                self.call_timeout)
            with r:
                if r.status_code not in (200, 202):
                    raise MCPError(
                        f"{self.name}: {method}: HTTP {r.status_code}")
            return
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    # --- remote transport (entry BD) --------------------------------------

    def _http_post(self, payload, timeout):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return requests.post(self.url, json=payload, headers=headers,
                             timeout=timeout, stream=True)

    def _sse_read(self, r, want_id, deadline):
        """Read an SSE stream until the JSON-RPC response with want_id
        (blocks per-read up to the request timeout; non-matching blocks —
        server-initiated messages — are skipped like stdio noise)."""
        data_lines = []
        for raw in r.iter_lines(decode_unicode=True):
            if time.time() > deadline:
                raise MCPError(f"{self.name}: timeout reading SSE response")
            line = (raw or "").strip()
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line and data_lines:   # blank line ends an event block
                try:
                    msg = json.loads("\n".join(data_lines))
                except ValueError:
                    data_lines = []
                    continue
                data_lines = []
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    return msg
        raise MCPError(
            f"{self.name}: SSE stream ended without response {want_id}")

    def _rpc_remote(self, rid, method, params, timeout):
        try:
            r = self._http_post(
                {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": params}, timeout)
        except requests.RequestException as e:
            self._open = False   # next ensure_alive() re-handshakes
            raise MCPError(f"{self.name}: {method}: "
                           f"{e.__class__.__name__}: {e}")
        with r:
            if r.status_code == 404 and self._session_id:
                raise MCPSessionGone(f"{self.name}: session expired (404)")
            if r.status_code != 200:
                raise MCPError(f"{self.name}: {method}: HTTP {r.status_code}")
            ctype = r.headers.get("Content-Type", "")
            if ctype.startswith("text/event-stream"):
                msg = self._sse_read(r, rid, time.time() + timeout)
            else:
                try:
                    msg = r.json()
                except ValueError:
                    raise MCPError(f"{self.name}: {method}: non-JSON body")
        if isinstance(msg, dict) and "error" in msg:
            raise MCPError(f"{self.name}: {msg['error']}")
        return msg["result"]

    def _start_remote(self, timeout):
        """Handshake over HTTP: initialize captures the optional session id
        from the response headers; initialized + tools/list follow."""
        self._session_id = None
        self.protocol = None
        self._id += 1
        rid = self._id
        try:
            r = self._http_post(
                {"jsonrpc": "2.0", "id": rid, "method": "initialize",
                 "params": {"protocolVersion": HTTP_PROTOCOL, "capabilities": {},
                            "clientInfo": {"name": "wiresense",
                                           "version": "0.0.1"}}}, timeout)
        except requests.RequestException as e:
            raise MCPError(f"{self.name}: initialize: "
                           f"{e.__class__.__name__}: {e}")
        with r:
            if r.status_code != 200:
                hint = (" — auth required? (deferred, entry BD)"
                        if r.status_code in (401, 403) else "")
                raise MCPError(f"{self.name}: initialize: "
                               f"HTTP {r.status_code}{hint}")
            sid = r.headers.get("Mcp-Session-Id")
            ctype = r.headers.get("Content-Type", "")
            if ctype.startswith("text/event-stream"):
                msg = self._sse_read(r, rid, time.time() + timeout)
            else:
                try:
                    msg = r.json()
                except ValueError:
                    raise MCPError(f"{self.name}: initialize: non-JSON body")
        if "error" in msg:
            raise MCPError(f"{self.name}: initialize: {msg['error']}")
        self.protocol = msg["result"].get("protocolVersion")
        self._session_id = sid
        self._notify("notifications/initialized")
        # tools/list happens once, in start()'s common path

    # --- lifecycle -------------------------------------------------------

    def alive(self):
        if self.url is not None:
            return self._open
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        if self.url is not None:
            if self._session_id:
                try:   # spec: client SHOULD DELETE to end the session
                    requests.delete(
                        self.url, headers={"Mcp-Session-Id": self._session_id},
                        timeout=5)
                except requests.RequestException:
                    pass
            self._session_id = None
            self._open = False
        elif self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self.proc = None
        self.down = True
        self.tools = []
        self._last_start = 0.0  # a deliberate kill clears the respawn cooldown

    def start(self, timeout=90.0):
        """Connect + handshake + tools/list. Raises MCPError; on any failure
        the transport is torn down so nothing leaks."""
        try:
            if self.url is not None:
                self._start_remote(timeout)
            else:
                if self._log is not None:
                    self._log.close()
                self._log = open(f"/tmp/wiresense_mcp_{self.name}.log", "w")
                env = dict(os.environ)
                env.update(self.env)
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
            self._open = True
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
        """tools/call -> joined text content. Raises MCPError. Remote only:
        one transparent session re-open on a 404 (server restart / session
        expiry), then the honest failure."""
        call = {"name": name, "arguments": args}
        if self.url is not None:
            try:
                result = self._rpc("tools/call", call, self.call_timeout)
            except MCPSessionGone:
                self._session_id = None
                self._open = False
                self.start(self.call_timeout)
                result = self._rpc("tools/call", call, self.call_timeout)
        else:
            result = self._rpc("tools/call", call, self.call_timeout)
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
        transport is torn down — stdio process group killed so the blocked
        reader thread sees EOF, remote session dropped (DELETE + closed)."""
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, self.call_tool, name, args)
        try:
            return await asyncio.wait_for(fut, self.call_timeout + 15)
        except asyncio.TimeoutError:
            how = "session dropped" if self.url is not None else "server killed"
            self.kill()
            raise MCPError(f"{self.name}.{name}: timed out "
                           f"({self.call_timeout:.0f}s), {how}")

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
