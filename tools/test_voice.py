"""E2E voice test: synthesize utterances with local Kokoro, feed them as
16 kHz mic PCM into the wiresense WS, and assert the decision tree routes
each turn to its expected class (root.subleaf after the think subtree).
Covers all five root classes, the think subtree split, the V1 tool flow
(timer via tool_event; reminder/note via web/memory.json), and the
no-tool-fits fallback (weather -> honest answer).

Run against the live stack (llama-server, Phonon, web):
    make test-voice

Exits 1 on any route miss, empty reply, failed tool execution, or server
error event. Vision/weather honesty is printed as a warning rather than
asserted (the phrasing is the LLM's, not a protocol constant).
"""
import asyncio
import json
import os
import sys
import time

import numpy as np
import websockets
from kokoro_onnx import Kokoro

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = "ws://127.0.0.1:8996/ws"
SR_IN, SR_OUT = 24000, 16000
FRAME = 1600  # 100 ms of 16 kHz s16le

# (expected route class, utterance, checks) — monologue is not a class;
# long unstructured speech adjudicated to the social path (19eb109).
# checks: timer=<seconds>, reminder=<text fragment> (with due),
# note=<fragment>, honest=True (no-frame vision), frame=True (vision with
# synthetic camera frame; reply must mention the frame content)
UTTERANCES = [
    ("direct", "What is the capital of France?", {}),
    ("think.long_think", "If I leave at five pm and the drive takes three hours, "
                         "when will I arrive?", {}),
    ("decide", "Should I take the bus or walk to the museum?", {}),
    ("social", "I had such a long day at work today, honestly I'm exhausted.", {}),
    ("vision", "What do you think about my new glasses?", {"honest": True}),
    ("social", "So yesterday I was walking to the store and this guy comes up to me "
               "out of nowhere and starts telling me his whole life story and honestly "
               "I could not get away, it was something else.", {}),
    ("think.tool", "Set a timer for 10 minutes.", {"timer": 600}),
    ("think.tool", "Remind me to call mom in 15 seconds.",
     {"reminder": "call mom", "fires": True}),
    ("think.tool", "Make a note that the wifi password is falcon nimbus.",
     {"note": "falcon nimbus"}),
    ("think.tool", "What's the weather in Jakarta?", {"honest": True}),
    ("vision", "What do you see right now?", {"frame": True}),
]

HONEST_MARKERS = ("can't", "cannot", "can not", "not able", "no camera",
                  "don't see", "do not see", "unable", "don't have",
                  "do not have", "no access", "no way to")

kokoro = Kokoro(f"{ROOT}/models/kokoro-v1.0.onnx", f"{ROOT}/models/voices.npz")


def synth(text) -> bytes:
    samples, sr = kokoro.create(text, voice="af_heart", speed=1.0, lang="en-us")
    x = np.asarray(samples, dtype=np.float32)
    assert sr == SR_IN
    n = int(len(x) * SR_OUT / SR_IN)
    x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    # 0.2 s of leading/trailing silence so VAD latches cleanly
    return b"\x00\x00" * (SR_OUT // 5) + pcm + b"\x00\x00" * (SR_OUT // 5)


def memory_items() -> list:
    try:
        with open(f"{ROOT}/web/memory.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def red_circle_b64() -> str:
    """Synthetic camera frame: white background, red circle. Pure-Python
    24-bit BMP (no PIL in the project venv); mtmd's decoder sniffs the
    format from the bytes."""
    import base64
    import struct

    w = h = 224
    row = (w * 3 + 3) & ~3
    px = bytearray()
    for y in range(h - 1, -1, -1):  # BMP rows are bottom-up, BGR
        for x in range(w):
            dx, dy = x - w // 2, y - h // 2
            if dx * dx + dy * dy <= (w // 3) ** 2:
                px += bytes((30, 30, 200))   # red circle (BGR)
            else:
                px += bytes((255, 255, 255))
    px += b"\x00" * (row * h - len(px))
    hdr = b"BM" + struct.pack("<IHHI", 54 + len(px), 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(px),
                      2835, 2835, 0, 0)
    return base64.b64encode(hdr + dib + px).decode()


async def feed(ws, pcm):
    for i in range(0, len(pcm), FRAME * 2):
        await ws.send(pcm[i:i + FRAME * 2])
        await asyncio.sleep(0.1)


async def run() -> int:
    events = []
    all_events = []  # never cleared: catches async fires (reminder_due)
    failures = []
    T0 = time.monotonic()
    want_frame = [False]
    frame_b64 = red_circle_b64()
    async with websockets.connect(WS, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"type": "hello", "ctx_rate": 16000}))

        async def reader():
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "need_frame":
                        if want_frame[0]:
                            await ws.send(json.dumps(
                                {"type": "camera_frame", "b64": frame_b64}))
                        continue
                    m["_arr"] = round((time.monotonic() - T0) * 1000)
                    events.append(m)
                    all_events.append(m)
            except websockets.ConnectionClosed:
                pass

        rt = asyncio.create_task(reader())
        await asyncio.sleep(0.5)
        silence = b"\x00\x00" * FRAME
        for want, text, checks in UTTERANCES:
            t_start = time.monotonic()
            print(f"\n--- expect {want}: {text!r}")
            events.clear()
            want_frame[0] = bool(checks.get("frame"))
            if want_frame[0]:
                await ws.send(json.dumps({"type": "camera_state", "on": True}))
            await feed(ws, synth(text))
            # keep the line silent but OPEN until Phonon endpoints the turn;
            # wait for metrics too or every reading attributes to the
            # previous turn (send_metrics follows tts_done)
            for _ in range(300):  # 30 s
                await asyncio.sleep(0.1)
                await ws.send(silence)
                got = [e["type"] for e in events]
                if ("response_done" in got and "tts_done" in got
                        and ("metrics" in got or "error" in got)):
                    break
            reply, route = None, None
            for e in events:
                t = e["type"]
                at = f"[{e.get('_arr', 0) - round((t_start - T0) * 1000):>6}ms]"
                if t == "backchannel":
                    print(f"  {at} backchannel: {e['text']!r}")
                elif t == "route":
                    route = e["mode"]
                    print(f"  {at} route: {route} (conf {e['conf']})")
                elif t == "feedback":
                    print(f"  {at} feedback: {e['text']!r}")
                elif t == "tool_event":
                    print(f"  {at} tool_event: {e['tool']} "
                          f"{json.dumps(e, default=str)[:90]}")
                elif t == "response_done":
                    reply = e["text"]
                    print(f"  {at} reply: {reply[:110]!r}")
                elif t == "metrics":
                    llm = e.get("llm", {})
                    print(f"  {at} metrics: e2e={e['e2e_ms']}ms "
                          f"ttft={llm.get('ttft_ms')}ms "
                          f"decode={llm.get('decode_tps')}tps")
                elif t == "error":
                    print(f"  {at} ERROR: {e['message']}")
                    failures.append(f"{want}: server error {e['message'][:80]}")
            if route != want:
                failures.append(f"{want}: routed to {route}")
            if not reply:
                failures.append(f"{want}: empty reply (endpointing or LLM failure)")
                continue
            if checks.get("honest") and not any(m in reply.lower()
                                                for m in HONEST_MARKERS):
                print("  WARN: reply not recognizably honest (guard may be off)")
            if checks.get("frame"):
                low = reply.lower()
                if not ("red" in low or "circle" in low):
                    failures.append(f"{want}: frame reply does not mention the "
                                    f"red circle: {reply[:80]!r}")
                if "cannot see" in low or "can't see" in low:
                    failures.append(f"{want}: frame attached but reply claims "
                                    "blindness — frame did not reach the VLM")
            if "timer" in checks:
                fired = [e for e in events if e.get("type") == "tool_event"
                         and e.get("tool") == "timer"
                         and e.get("seconds") == checks["timer"]]
                if not fired:
                    failures.append(f"{want}: no timer tool_event for "
                                    f"{checks['timer']}s")
            if "reminder" in checks:
                hits = [i for i in memory_items()
                        if i.get("type") == "reminder"
                        and checks["reminder"].lower() in i.get("text", "").lower()]
                if not hits:
                    failures.append(f"{want}: reminder missing from memory.json")
                elif not hits[-1].get("due"):
                    failures.append(f"{want}: reminder has no due time")
                elif checks.get("fires"):
                    # give the reminder its 15 s (+ slack) to fire
                    fired = False
                    for _ in range(220):
                        await asyncio.sleep(0.1)
                        fired = any(
                            e.get("type") == "tool_event"
                            and e.get("tool") == "reminder_due"
                            and checks["reminder"].lower()
                            in e.get("text", "").lower()
                            for e in all_events)
                        if fired:
                            break
                    if not fired:
                        failures.append(f"{want}: reminder never fired "
                                        "tool_event reminder_due")
                    else:
                        print("  reminder fired ✓")
            if "note" in checks:
                if not any(i.get("type") == "note"
                           and checks["note"].lower() in i.get("text", "").lower()
                           for i in memory_items()):
                    failures.append(f"{want}: note missing from memory.json")
            await asyncio.sleep(1.5)
        rt.cancel()
    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nPASS: {len(UTTERANCES)}/{len(UTTERANCES)} routes correct, "
          "all replies non-empty, tool checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
