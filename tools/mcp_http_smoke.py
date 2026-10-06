"""entry BD smoke: the remote transport (Streamable HTTP) of web/mcp_client.py.

A hand-rolled stateful MCP server on stdlib ThreadingHTTPServer — no SDK,
nothing beyond 127.0.0.1 — exercising exactly what the client implements:
initialize + Mcp-Session-Id header, tools/list, tools/call answered as JSON
and once as SSE, an isError tool, a mid-session 404 session invalidation
(the client must re-initialize transparently and the retried call succeed),
and DELETE on kill(). MCPServer is driven directly: no llama, no gate, no WS.
"""
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "web")   # mcp_client resolves ../models only in server.py
import mcp_client           # noqa: E402

TOOLS = [
    {"name": "echo", "description": "Return the arguments as text.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "sse_echo", "description": "Same, but the answer arrives as an "
     "SSE stream.", "inputSchema": {"type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"]}},
    {"name": "boom", "description": "Always errors.",
     "inputSchema": {"type": "object", "properties": {}}},
]

state = {"sessions": {}, "inits": 0, "sse_used": False, "delete_seen": False,
         "proto_header_seen": False, "invalidate_once": False}


def result_body(rid, result):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()


def reply(h, status, body=b"", ctype="application/json", extra=None):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    if body:
        h.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def _call(self, req, sid):
        name = req["params"]["name"]
        args = req["params"].get("arguments") or {}
        if name == "boom":
            return {"content": [{"type": "text", "text": "boom says no"}],
                    "isError": True}
        text = json.dumps(args)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = req.get("method", "")
        if method == "initialize":
            state["inits"] += 1
            sid = uuid.uuid4().hex
            state["sessions"][sid] = True
            body = result_body(req["id"], {
                "protocolVersion": mcp_client.HTTP_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "httptest", "version": "0"}})
            reply(self, 200, body,
                  extra={"Mcp-Session-Id": sid})
            return
        if "id" not in req:   # notification (notifications/initialized)
            reply(self, 202)
            return
        sid = self.headers.get("Mcp-Session-Id", "")
        if state["invalidate_once"]:
            state["invalidate_once"] = False
            state["sessions"].pop(sid, None)
            reply(self, 404)
            return
        if sid not in state["sessions"]:
            reply(self, 404)
            return
        state["proto_header_seen"] = bool(self.headers.get(
            "MCP-Protocol-Version"))
        if method == "tools/list":
            reply(self, 200, result_body(req["id"], {"tools": TOOLS}))
            return
        if method == "tools/call":
            res = self._call(req, sid)
            if req["params"]["name"] == "sse_echo":
                state["sse_used"] = True
                payload = json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                      "result": res})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(
                    b"event: message\ndata: " + payload.encode() + b"\n\n")
                self.wfile.flush()
                self.close_connection = True
                return
            reply(self, 200, result_body(req["id"], res))
            return
        reply(self, 200, result_body(req["id"], {}))

    def do_DELETE(self):
        sid = self.headers.get("Mcp-Session-Id", "")
        state["sessions"].pop(sid, None)
        state["delete_seen"] = True
        reply(self, 200)

    def log_message(self, *a):
        pass


def ok(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    return bool(cond)


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    cfg = {"name": "httptest", "url": f"http://127.0.0.1:{port}/mcp",
           "timeout_s": 10}
    good = True
    try:
        s = mcp_client.MCPServer(cfg)
        n = s.start()
        good &= ok(n == 3 and state["inits"] == 1 and s._session_id,
                   f"start: {n} tools, 1 initialize, session id issued")

        out = s.call_tool("echo", {"text": "hola"})
        good &= ok(json.loads(out) == {"text": "hola"},
                   "call echo (JSON response): arguments round-trip")
        good &= ok(state["proto_header_seen"],
                   "MCP-Protocol-Version header sent after initialize")

        out = s.call_tool("sse_echo", {"text": "via sse"})
        good &= ok(json.loads(out) == {"text": "via sse"} and
                   state["sse_used"], "call sse_echo: SSE response parsed")

        try:
            s.call_tool("boom", {})
            good &= ok(False, "isError tool raised")
        except mcp_client.MCPError as e:
            good &= ok("boom says no" in str(e), "isError tool raised MCPError")

        state["invalidate_once"] = True
        out = s.call_tool("echo", {"text": "after re-init"})
        good &= ok(json.loads(out) == {"text": "after re-init"} and
                   state["inits"] == 2,
                   "404 mid-session: transparent re-init + retry")

        s.kill()
        good &= ok(state["delete_seen"] and not s.alive(),
                   "kill(): DELETE seen, server not alive")
        n = s.start()
        good &= ok(n == 3 and state["inits"] == 3,
                   "restart after kill: fresh session")
        s.kill()
    except Exception as e:
        good = False
        print(f"FAIL  exception: {e!r}", flush=True)
    finally:
        httpd.shutdown()
    print("SMOKE PASS" if good else "SMOKE FAIL", flush=True)
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
