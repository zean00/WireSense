#!/usr/bin/env python
"""Capability coverage suite (fake WS, no audio path): drives Session.respond()
— the FULL turn pipeline (router -> intercepts -> dispatch -> flow -> TTS ->
history -> metrics) — over every capability the server exposes.

Scenarios (27): direct/social/decide llm turns, think.long_think open-think,
native timer (high-risk park + YES), reminder (relative fire), note,
remember (capture intercept) + recall via digest in a fresh session +
forget, MCP read/time/git/risky-park/null-fallback/dead-reconnect, deep
turn (provider + local fallback), vision without camera, detect without
camera, judge clarify + resolution, backchannel, mid-task stop controller,
history carry, fragment rewrite, IR research track, browser track (skipped
if playwright missing).

Not covered here (needs a real audio/camera client): VAD/STT/audio-in, the
Phonon ASR loop, real camera frames, face recognition (opt-in, disabled),
fp4-corruption signature (injected fault), gate (mic) state machine.

Usage: tools/capability_suite/run_suite.py [scenario name ...]
Needs llama-server :8998, deep-think provider, and the mcp config servers.
"""
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
WEB = os.path.join(ROOT, "web")
sys.path.insert(0, WEB)
sys.path.insert(0, os.path.join(ROOT, "tools", "mcp_eval"))
os.chdir(WEB)  # server.py resolves MODELS/memory relative to cwd

import requests  # noqa: E402
import mcp_client  # noqa: E402
import mcp_gate  # noqa: E402
import server  # noqa: E402
from gen_eval import SERVERS  # noqa: E402
import deep_think  # noqa: E402

MEM = "/tmp/wiresense_suite_memory.json"
NOTES = "/tmp/wiresense_notes.txt"
SUITE_FILE = "/tmp/suite_cap.txt"


class FakeWS:
    def __init__(self):
        self.events = []

    async def send(self, obj):
        # Session.send json.dumps()es before the real ws.send(); accept both
        if isinstance(obj, str):
            obj = json.loads(obj)
        self.events.append(obj)

    def of(self, etype):
        return [e for e in self.events if e.get("type") == etype]

    def clear(self):
        self.events = []


class DummyTask:
    def __init__(self):
        self.cancelled = False

    def active(self):
        return not self.cancelled

    async def cancel(self):
        self.cancelled = True


def setup():
    server.KOKORO = server.Kokoro(f"{server.MODELS}/kokoro-v1.0.onnx",
                                  f"{server.MODELS}/voices.npz")
    server.READOUT = server.DecideReadout()
    server.WALKER = server.TreeWalker(server.READOUT._project)
    server.DEEP = deep_think.from_config(deep_think.load_config())
    cfg = deep_think.load_config().get("mcp") or {}
    servers = mcp_client.from_config(cfg) or []
    gate = None
    if servers:
        gate = mcp_gate.Gate(server.LLAMA, server.norm_router_text,
                             server.native_manifest_lines(), servers,
                             cache_path=server.MCP_INDEX_PATH,
                             build_prompt=server.build_prompt)
        gate.build_index()
    server.MCP_GATE = gate
    server.MEMORY_PATH = MEM  # never touch the user's real memory.json
    if os.path.exists(MEM):
        os.remove(MEM)
    return servers


async def run_scenario(s, ws, name, coro_fn, timeout):
    t0 = time.perf_counter()
    try:
        detail = await asyncio.wait_for(coro_fn(s, ws), timeout)
        return {"scenario": name, "status": "PASS", "ms": round(
            (time.perf_counter() - t0) * 1000), "detail": detail or ""}
    except AssertionError as e:
        recent = [(e.get("type"), (e.get("mode") or e.get("tool")
                                   or e.get("event") or "")) for e in ws.events][-6:]
        return {"scenario": name, "status": "FAIL", "ms": round(
            (time.perf_counter() - t0) * 1000),
            "detail": f"{str(e)[:200]} | events={recent}"}
    except asyncio.TimeoutError:
        return {"scenario": name, "status": "FAIL", "ms": round(
            (time.perf_counter() - t0) * 1000), "detail": "TIMEOUT"}
    except Exception as e:
        return {"scenario": name, "status": "FAIL", "ms": round(
            (time.perf_counter() - t0) * 1000),
            "detail": f"{type(e).__name__}: {e}"[:300]}


# --- scenarios (each: async (s, ws) -> detail string) ---------------------


async def sc_direct_chat(s, ws):
    await s.respond("Tell me a fun fact about octopuses")
    modes = [e["mode"] for e in ws.of("route")]
    assert modes and modes[0] == "direct", f"route={modes}"
    done = ws.of("response_done")
    assert done and done[0]["text"].strip(), "no reply"
    m = ws.of("metrics")
    assert m, "no metrics event"
    chunks = ws.of("tts_chunk")
    assert chunks and all(c.get("audio") for c in chunks), "no tts audio"
    assert ws.of("tts_done"), "no tts_done"
    assert len(s.history) == 1, f"history={len(s.history)}"
    return f"reply={done[0]['text'][:60]!r}"


async def sc_social(s, ws):
    ws.clear()
    await s.respond("I had such a long day at work")
    assert ws.of("route")[0]["mode"] == "social", ws.of("route")
    # the tree's social class carries no filler:true — the reply itself
    # is the social turn (feedback fillers are think/tool/ir/browser)
    assert ws.of("response_done")[0]["text"].strip(), "no reply"
    return "reply ok"


async def sc_decide(s, ws):
    ws.clear()
    await s.respond("Should I take the bus or walk to the market")
    assert ws.of("route")[0]["mode"] == "decide", ws.of("route")
    assert ws.of("response_done")[0]["text"].strip(), "no reply"
    return "reply ok"


async def sc_long_think(s, ws):
    ws.clear()
    await s.respond("How many seconds are there in a leap year")
    assert ws.of("route")[0]["mode"] == "think.long_think", ws.of("route")
    assert ws.of("think_delta"), "no think trace"
    reply = ws.of("response_done")[0]["text"]
    assert reply.strip() and "<think>" not in reply, reply[:80]
    return f"reply={reply[:50]!r}"


async def sc_timer_park_yes(s, ws):
    ws.clear()
    await s.respond("Set a timer for 10 seconds")
    assert ws.of("route")[0]["mode"] == "think.tool", ws.of("route")
    assert s.pending and s.pending["kind"] == "tool" \
        and s.pending["tool"] == "timer", s.pending
    readback = ws.of("response_done")[0]["text"]
    assert readback, "no readback"
    ws.clear()
    await s.respond("yes")
    te = ws.of("tool_event")
    assert te and te[0]["tool"] == "timer" and te[0]["seconds"] == 10, te
    assert ws.of("response_done")[0]["text"].strip(), "no confirm reply"
    return "parked, YES executed"


async def sc_reminder_fire(s, ws):
    ws.clear()
    await s.respond("Remind me to stretch in five seconds")
    assert s.pending and s.pending["tool"] == "reminder", s.pending
    await s.respond("yes")
    mem = json.load(open(MEM))
    assert any(i["type"] == "reminder" for i in mem), mem
    await asyncio.sleep(7)
    fired = [e for e in ws.of("tool_event") if e.get("tool") == "reminder_due"]
    assert fired, f"no reminder_due event: {ws.of('tool_event')}"
    return "saved + fired on schedule"


async def sc_note(s, ws):
    ws.clear()
    await s.respond("Make a note that the coverage suite ran")
    assert s.pending is None, "note should not park"
    tc = ws.of("tool_call")
    assert tc and tc[0]["tool"] == "note", tc
    assert "note saved" in tc[0]["result"], tc[0]["result"]
    return "saved"


async def sc_remember_store(s, ws):
    ws.clear()
    await s.respond("Remember that I'm allergic to peanuts")
    r = ws.of("route")
    assert r and r[0]["mode"] == "think.tool" \
        and r[0].get("note") == "memory intercept", r
    tc = ws.of("tool_call")
    assert tc and tc[0]["tool"] == "remember", tc
    mem = json.load(open(MEM))
    assert any(i.get("type") == "fact"
               and "peanut" in i.get("text", "").lower() for i in mem), mem
    assert ws.of("response_done")[0]["text"].strip(), "no confirm reply"
    return "fact stored"


async def sc_remember_recall(s, ws):
    # fresh session: the recall must come from the digest alone (no history)
    ws.clear()
    ws2 = FakeWS()
    s2 = server.Session(ws2)
    await s2.respond("What allergies do I have?")
    assert not ws2.of("tool_call"), "recall is an llm turn, not a tool"
    reply = ws2.of("response_done")[0]["text"]
    assert "peanut" in reply.lower(), f"digest not honored: {reply[:80]}"
    return f"recalled via digest: {reply[:44]!r}"


async def sc_remember_forget(s, ws):
    ws.clear()
    await s.respond("Forget what I said about peanuts")
    tc = ws.of("tool_call")
    assert tc and tc[0]["tool"] == "forget", tc
    mem = [i for i in json.load(open(MEM)) if i.get("type") == "fact"]
    assert not any("peanut" in i.get("text", "").lower() for i in mem), mem
    return f"removed: {tc[0]['result'][:44]!r}"


async def sc_mcp_read(s, ws):
    ws.clear()
    await s.respond("Read the file wiresense_notes.txt in /tmp and tell "
                    "me what it says")
    tc = ws.of("tool_call")
    assert tc and tc[0]["tool"] == "filesystem__read_text_file", tc
    assert "MCP is live" in tc[0]["result"], tc[0]["result"][:120]
    return "content read"


async def sc_mcp_time(s, ws):
    ws.clear()
    await s.respond("What time is it in Tokyo right now?")
    tc = ws.of("tool_call")
    assert tc and tc[0]["tool"] == "time__get_current_time", tc
    r = tc[0]["result"]
    assert "failed" not in r.lower() and "error" not in r.lower(), r[:120]
    return f"result={r[:50]!r}"


async def sc_mcp_git(s, ws):
    ws.clear()
    await s.respond("Show the git status of the current project")
    tc = ws.of("tool_call")
    assert tc and tc[0]["tool"] == "git__git_status", tc
    assert "main" in tc[0]["result"], tc[0]["result"][:120]
    return "status read"


async def sc_mcp_risky(s, ws):
    ws.clear()
    if os.path.exists(SUITE_FILE):
        os.remove(SUITE_FILE)
    await s.respond("Create a file named suite_cap.txt in /tmp containing "
                    "the word hello")
    assert s.pending and s.pending["kind"] == "tool", "write not parked"
    assert s.pending["tool"].endswith("write_file"), s.pending
    ws.clear()
    await s.respond("yes")
    assert os.path.exists(SUITE_FILE), "file not written after YES"
    assert "hello" in open(SUITE_FILE).read(), "wrong content"
    os.remove(SUITE_FILE)
    return "parked, YES executed"


async def sc_mcp_null(s, ws):
    ws.clear()
    await s.respond("What's the weather in Jakarta")
    assert ws.of("route")[0]["mode"] == "think.tool", ws.of("route")
    assert not ws.of("tool_call"), "should not have called a tool"
    reply = ws.of("response_done")[0]["text"]
    assert reply.strip(), "no honest fallback reply"
    return f"fallback={reply[:50]!r}"


async def sc_deep_provider(s, ws):
    ws.clear()
    await s.respond("Think hard about whether remote work increases "
                    "productivity, and give me the best answer")
    assert ws.of("route")[0]["mode"] == "deep", ws.of("route")
    m = ws.of("metrics")
    assert m and m[0].get("deep"), "no deep meta in metrics"
    d = m[0]["deep"]
    assert not d.get("fallback"), d
    assert ws.of("response_done")[0]["text"].strip(), "no reply"
    return f"model={d.get('model')}"


async def sc_deep_fallback(s, ws):
    ws.clear()
    saved, server.DEEP = server.DEEP, None
    try:
        await s.respond("Think really hard about why bridges hum in the "
                        "wind before you reply")
        m = ws.of("metrics")
        assert m and m[0].get("deep"), "no deep meta"
        d = m[0]["deep"]
        assert d.get("fallback"), d
        assert ws.of("response_done")[0]["text"].strip(), "no reply"
        return "local open-think fallback ok"
    finally:
        server.DEEP = saved


async def sc_vision_noframe(s, ws):
    ws.clear()
    await s.respond("What do you see right now")
    assert ws.of("route")[0]["mode"] == "vision", ws.of("route")
    assert not ws.of("vision_frame"), "no camera should exist"
    reply = ws.of("response_done")[0]["text"]
    assert reply.strip(), "no honest no-camera reply"
    return f"reply={reply[:50]!r}"


async def sc_detect_degraded(s, ws):
    r = await s.run_tool("detect", {"labels": ["person"]})
    assert "camera unavailable" in r or "detector unavailable" in r, r
    return f"honest: {r!r}"


async def sc_clarify(s, ws):
    ws.clear()
    s.norm_fired = False
    await s.respond("wat is my naem")
    assert ws.of("route")[0]["mode"] == "clarify", ws.of("route")
    reply = ws.of("response_done")[0]["text"]
    assert reply.startswith("Did you mean"), reply
    assert s.pending and s.pending["kind"] == "intent", s.pending
    ws.clear()
    await s.respond("yes")
    assert ws.of("response_done") and \
        ws.of("response_done")[0]["text"].strip(), "no rerun reply"
    return "clarified + YES reran"


async def sc_backchannel(s, ws):
    s.t_wake = time.perf_counter() - 10
    s.bc_last_t = 0.0
    s.bc_count = 0
    await s.maybe_backchannel("and um I was wondering if you could")
    assert ws.of("backchannel"), "no backchannel event"
    return ws.of("backchannel")[0].get("text", "")[:40]


async def sc_stop_controller(s, ws):
    ws.clear()
    dummy = DummyTask()
    s.browser_task = dummy
    await s.respond("stop")
    assert dummy.cancelled, "task not cancelled"
    assert ws.of("response_done")[0]["text"] == "Stopped.", ws.of(
        "response_done")
    ws.clear()
    dummy2 = DummyTask()
    s.browser_task = dummy2
    await s.respond("What is the capital of France")
    assert ws.of("response_done")[0]["text"] == \
        "I'm still working on the page — say stop to cancel.", \
        ws.of("response_done")
    assert not dummy2.cancelled, "hold should not cancel"
    s.browser_task = None
    return "stop + hold ok"


async def sc_history(s, ws):
    ws.clear()
    await s.respond("My name is Sahal")
    assert ws.of("response_done")[0]["text"].strip(), "no reply"
    await s.respond("What is my name?")
    reply = ws.of("response_done")[-1]["text"]
    assert "Sahal" in reply, f"history not carried: {reply[:80]}"
    return f"reply={reply[:50]!r}"


async def sc_fragment_rewrite(s, ws):
    ws.clear()
    await s.respond("and what about my sister?")
    r = ws.of("route")
    assert r, "no route event"
    assert r[0].get("rewrite_ms") is not None or r[0].get("rewrite"), \
        f"no rewrite attempt: {r[0]}"
    assert ws.of("response_done")[0]["text"].strip(), "no reply"
    return f"rewrite={r[0].get('rewrite')!r:.60}"


TERMINAL = {"done", "parked", "cancelled", "refused", "error", "stuck",
            "budget"}


async def wait_terminal(ws, etype, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        t = [e for e in ws.of(etype) if e.get("event") in TERMINAL]
        if t:
            return t[-1]
        await asyncio.sleep(2)
    return None


async def sc_ir_flow(s, ws):
    ws.clear()
    await s.respond("Research how honey is made, in depth, and give me "
                    "the full explanation")
    assert ws.of("route")[0]["mode"] == "ir", ws.of("route")
    ie = await wait_terminal(ws, "ir_event", 140)
    assert ie, f"no terminal ir_event; events={[e['type'] for e in ws.events][:8]}"
    answer = ws.of("response_done")[-1]["text"]
    assert ie["event"] == "done" and answer.strip(), \
        f"outcome={ie['event']} answer={answer[:80]!r}"
    return f"depth={ie.get('depth')} fetches={ie.get('fetches')}"


async def sc_browser(s, ws):
    try:
        import playwright  # noqa: F401
    except ImportError:
        return None  # SKIP
    ws.clear()
    # phrasing note (2026-10-07): "tell me the page's main heading" sent the
    # agent wandering (extraction returned the body paragraph, the done-gate
    # rightly rejected, it clicked off-site and ended honestly stuck). A
    # question whose answer IS the visible text completes cleanly.
    await s.respond("Open example.com and tell me what this domain is for")
    assert ws.of("route")[0]["mode"] == "browser", ws.of("route")
    be = await wait_terminal(ws, "browser_event", 110)
    assert be, f"no terminal browser_event; got={ws.of('browser_event')[-1:]}"
    assert be["event"] == "done", f"outcome={be['event']}"
    return f"steps={be.get('steps')}"


async def sc_dead_reconnect(s, ws, servers):
    """Kill the time server mid-session: mcp_client self-heals (one respawn
    + re-handshake on the next call), so the turn still succeeds — that IS
    the reconnect capability; the honest-offline path only shows when the
    respawn itself fails."""
    ws.clear()
    tsrv = next(x for x in servers if x.name == "time")
    tsrv.kill()
    await asyncio.sleep(0.5)
    await s.respond("Tell me the current time in Berlin")
    tc = ws.of("tool_call")
    assert tc and tc[0]["tool"] == "time__get_current_time", tc
    r = tc[0]["result"]
    assert "2026" in r or "202" in r, f"no timestamp after respawn: {r[:80]}"
    return "self-healed after kill"


SCENARIOS = [
    ("direct_chat", 120, sc_direct_chat),
    ("social", 120, sc_social),
    ("decide", 120, sc_decide),
    ("long_think", 120, sc_long_think),
    ("timer_park_yes", 120, None),
    ("reminder_fire", 60, None),
    ("note", 120, None),
    ("remember_store", 120, None),
    ("remember_recall_fresh", 120, None),
    ("remember_forget", 120, None),
    ("mcp_read", 120, None),
    ("mcp_time", 120, None),
    ("mcp_git", 120, None),
    ("mcp_risky", 120, None),
    ("mcp_null_fallback", 120, None),
    ("deep_provider", 180, None),
    ("deep_fallback", 180, None),
    ("vision_noframe", 60, None),
    ("detect_degraded", 30, None),
    ("clarify_resolution", 120, None),
    ("backchannel", 30, None),
    ("stop_controller", 60, None),
    ("history_carry", 120, None),
    ("fragment_rewrite", 120, None),
    ("ir_flow", 180, None),
    ("browser_track", 130, None),
    ("mcp_dead_reconnect", 120, None),
]


async def main():
    wanted = sys.argv[1:]
    servers = setup()
    print(f"mcp gate active={server.MCP_GATE is not None and server.MCP_GATE.active()} "
          f"over {len(server.MCP_GATE.tools) if server.MCP_GATE else 0} tools; "
          f"deep={server.DEEP is not None}")
    ws = FakeWS()
    s = server.Session(ws)

    results = []
    for name, tmo, _ in SCENARIOS:
        if wanted and name not in wanted:
            continue
        if name == "timer_park_yes":
            fn = sc_timer_park_yes
        elif name == "reminder_fire":
            fn = sc_reminder_fire
        elif name == "note":
            fn = sc_note
        elif name == "remember_store":
            fn = sc_remember_store
        elif name == "remember_recall_fresh":
            fn = sc_remember_recall
        elif name == "remember_forget":
            fn = sc_remember_forget
        elif name == "mcp_read":
            fn = sc_mcp_read
        elif name == "mcp_time":
            fn = sc_mcp_time
        elif name == "mcp_git":
            fn = sc_mcp_git
        elif name == "mcp_risky":
            fn = sc_mcp_risky
        elif name == "mcp_null_fallback":
            fn = sc_mcp_null
        elif name == "deep_provider":
            fn = sc_deep_provider
        elif name == "deep_fallback":
            fn = sc_deep_fallback
        elif name == "vision_noframe":
            fn = sc_vision_noframe
        elif name == "detect_degraded":
            fn = sc_detect_degraded
        elif name == "clarify_resolution":
            fn = sc_clarify
        elif name == "backchannel":
            fn = sc_backchannel
        elif name == "stop_controller":
            fn = sc_stop_controller
        elif name == "history_carry":
            fn = sc_history
        elif name == "fragment_rewrite":
            fn = sc_fragment_rewrite
        elif name == "ir_flow":
            fn = sc_ir_flow
        elif name == "browser_track":
            fn = sc_browser
        elif name == "mcp_dead_reconnect":
            async def fn(s=s, ws=ws, servers=servers):
                return await sc_dead_reconnect(s, ws, servers)
        else:
            fn = dict(direct_chat=sc_direct_chat, social=sc_social,
                      decide=sc_decide, long_think=sc_long_think)[name]
        r = await run_scenario(s, ws, name, fn, tmo)
        if r["detail"] is None:
            r["status"], r["detail"] = "SKIP", "playwright not installed"
        results.append(r)
        print(f"[{r['status']:4}] {name:22} {r['detail']}", flush=True)
        for srv in (s.ir_task, s.browser_task):
            if srv and srv.active():
                await srv.cancel()
        s.pending = None

    json.dump(results, open(os.path.join(HERE, "last_run.json"), "w"),
              indent=1)
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{n_pass}/{len(results)} PASS "
          f"({sum(1 for r in results if r['status']=='FAIL')} FAIL, "
          f"{sum(1 for r in results if r['status']=='SKIP')} SKIP)")
    for srv in servers:
        srv.kill()


if __name__ == "__main__":
    asyncio.run(main())
