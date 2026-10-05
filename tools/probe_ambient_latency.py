#!/usr/bin/env python
"""G4(v2) of docs/PROTOCOL-ambient-face.md Rev 1: powered ambient latency
A/B. 6 interleaved pairs of the identical direct utterance, history reset
before each, ambient_state off/on alternating within each round. Gates:
median(on e2e) <= median(off e2e) * 1.15 AND median(on ttft) likewise.

Run with the web server already face-enabled + ambient-enabled and the
store POPULATED (the probe enrolls albert from the frozen portrait at
start and forgets at end — faces.json is file-backed per call, so a
separate process can do this)."""
import asyncio
import base64
import json
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import websockets

import test_voice as tv

ROUNDS = 6
UTTERANCE = "What is the capital of France?"


async def one_turn(ws, silence, ambient_on):
    events = []
    got_metrics = None

    async def reader():
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "need_frame":
                await ws.send(json.dumps(
                    {"type": "camera_frame", "b64": tv.face_einstein_b64()}))
                continue
            events.append(m)
            if m.get("type") == "metrics":
                got_metrics.set()

    await ws.send(json.dumps({"type": "reset"}))
    await ws.send(json.dumps({"type": "camera_state", "on": True}))
    await ws.send(json.dumps({"type": "ambient_state", "on": ambient_on}))
    await asyncio.sleep(0.3)
    got_metrics = asyncio.Event()
    rt = asyncio.create_task(reader())
    await tv.feed(ws, tv.synth(UTTERANCE))
    silence = b"\x00\x00" * tv.FRAME
    for _ in range(200):
        await asyncio.sleep(0.1)
        await ws.send(silence)
        if got_metrics.is_set():
            break
    rt.cancel()
    m = next((e for e in events if e.get("type") == "metrics"), None)
    route = next((e["mode"] for e in events if e.get("type") == "route"), None)
    face = m.get("face") if m else None
    return {"route": route, "e2e": m.get("e2e_ms") if m else None,
            "ttft": (m.get("llm") or {}).get("ttft_ms") if m else None,
            "face_ambient": (face or {}).get("ambient") if face else None}


async def run():
    sys.path.insert(0, os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), "web"))
    import faces  # noqa: E402  (lives in web/, file-backed store per call)
    frame = tv.face_einstein_b64()
    ok, detail = faces.enroll("albert", [frame] * 3)
    assert ok, f"enroll failed: {detail}"
    print(f"store populated ({detail}); {ROUNDS} interleaved off/on pairs of "
          f"{UTTERANCE!r}")
    off, on = [], []
    try:
        async with websockets.connect(tv.WS, max_size=2 ** 22) as ws:
            await ws.send(json.dumps({"type": "hello", "ctx_rate": 16000}))
            await asyncio.sleep(0.4)
            for r in range(ROUNDS):
                for arm, want in (("off", False), ("on", True)):
                    t = await one_turn(ws, None, want)
                    (off if arm == "off" else on).append(t)
                    print(f"round {r + 1} {arm:>3}: route={t['route']} "
                          f"e2e={t['e2e']}ms ttft={t['ttft']}ms "
                          f"face_ambient={t['face_ambient']}")
    finally:
        faces.forget()
        print("store cleaned")

    def med(xs, k):
        vals = [x[k] for x in xs if x[k] is not None]
        return statistics.median(vals) if vals else None

    mo, mn = med(off, "e2e"), med(on, "e2e")
    to, tn = med(off, "ttft"), med(on, "ttft")
    routes_ok = all(x["route"] == "direct" for x in off + on)
    flag_ok = all(x["face_ambient"] for x in on)
    print(f"\nmedians: e2e off={mo}ms on={mn}ms | ttft off={to}ms on={tn}ms")
    failures = []
    if not routes_ok:
        failures.append("routing drift: non-direct turns in the pair")
    if not flag_ok:
        failures.append("ambient-on turns missing face.ambient flag")
    if mo and mn and mn > mo * 1.15:
        failures.append(f"e2e median on {mn} > off {mo} * 1.15")
    if to and tn and tn > to * 1.15:
        failures.append(f"ttft median on {tn} > off {to} * 1.15")
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("G4(v2) PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
