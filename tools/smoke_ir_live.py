"""One-off live smoke (recorded, never gated): a real depth-2 web
search off the replay registry, through the voice pipeline."""
import asyncio
import glob
import json
import os
import sys

sys.path.insert(0, "/home/sahal/workspace/wiresense/tools")
import test_ir as h  # noqa: E402
import websockets  # noqa: E402


async def run():
    async with websockets.connect(h.WS, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"type": "hello", "ctx_rate": 16000}))
        runs = h.Runs()

        async def reader():
            try:
                async for raw in ws:
                    runs.add(json.loads(raw))
            except websockets.ConnectionClosed:
                pass

        rt = asyncio.create_task(reader())
        await asyncio.sleep(0.5)
        if not await h.wait_listening(ws, runs):
            print("mic never opened")
            return
        s = runs.index()
        await h.speak_turn(ws, runs, "Search the web for the population "
                           "of Iceland and tell me the number.",
                           since=s, timeout=20)
        for e in runs.since(s):
            if e.get("type") in ("route", "ir_event"):
                print(e["type"], ":", e.get("mode")
                      or (e.get("event"), e.get("fetches"), e.get("depth")))
        ev = await h.wait_ir_terminal(ws, runs, s, timeout=120)
        print("terminal:", json.dumps(ev)[:220])
        print("reply:", h.replies(runs, s)[-1:])
        newest = max(glob.glob("/tmp/wiresense_ir/*/trace.json"),
                     key=os.path.getmtime)
        t = json.load(open(newest))
        print("trace:", json.dumps({k: t[k] for k in
                                    ("utterance", "depth", "query",
                                     "outcome")})[:260])
        print("fetches:", [(f["kind"], f["url"][:60], f["snapped"])
                           for f in t.get("fetches", [])])
        rt.cancel()

asyncio.run(run())
