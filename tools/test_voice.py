"""E2E voice test: synthesize utterances with local Kokoro, feed them as
16 kHz mic PCM into the wiresense WS, and assert the decision tree routes
each turn to its expected class. Covers all five root classes plus the
long-monologue -> social case from live adjudication.

Run against the live stack (llama-server, Phonon, web):
    make test-voice

Exits 1 on any route miss, empty reply, or server error event. Vision
honesty is printed as a warning rather than asserted (the phrasing is
the LLM's, not a protocol constant).
"""
import asyncio
import json
import os
import sys

import numpy as np
import websockets
from kokoro_onnx import Kokoro

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = "ws://127.0.0.1:8996/ws"
SR_IN, SR_OUT = 24000, 16000
FRAME = 1600  # 100 ms of 16 kHz s16le

# (expected route class, utterance) — monologue is not a class; long
# unstructured speech adjudicated to the social path (19eb109 follow-up)
UTTERANCES = [
    ("direct", "What is the capital of France?"),
    ("think", "If I leave at five pm and the drive takes three hours, when will I arrive?"),
    ("decide", "Should I take the bus or walk to the museum?"),
    ("social", "I had such a long day at work today, honestly I'm exhausted."),
    ("vision", "What do you think about my new glasses?"),
    ("social", "So yesterday I was walking to the store and this guy comes up to me "
               "out of nowhere and starts telling me his whole life story and honestly "
               "I could not get away, it was something else."),
]

HONEST_MARKERS = ("can't", "cannot", "can not", "not able", "no camera",
                  "don't see", "do not see", "unable")

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


async def feed(ws, pcm):
    for i in range(0, len(pcm), FRAME * 2):
        await ws.send(pcm[i:i + FRAME * 2])
        await asyncio.sleep(0.1)


async def run() -> int:
    events = []
    failures = []
    async with websockets.connect(WS, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"type": "hello", "ctx_rate": 16000}))

        async def reader():
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    events.append(m)
            except websockets.ConnectionClosed:
                pass

        rt = asyncio.create_task(reader())
        await asyncio.sleep(0.5)
        silence = b"\x00\x00" * FRAME
        for want, text in UTTERANCES:
            print(f"\n--- expect {want}: {text!r}")
            events.clear()
            await feed(ws, synth(text))
            # keep the line silent but OPEN until Phonon endpoints the turn
            for _ in range(300):  # 30 s
                await asyncio.sleep(0.1)
                await ws.send(silence)
                got = [e["type"] for e in events]
                if "response_done" in got and ("tts_done" in got or "error" in got):
                    break
            reply, route = None, None
            for e in events:
                t = e["type"]
                if t == "backchannel":
                    print(f"  backchannel: {e['text']!r}")
                elif t == "route":
                    route = e["mode"]
                    print(f"  route: {route} (conf {e['conf']})")
                elif t == "feedback":
                    print(f"  feedback: {e['text']!r}")
                elif t == "response_done":
                    reply = e["text"]
                    print(f"  reply: {reply[:110]!r}")
                elif t == "metrics":
                    llm = e.get("llm", {})
                    print(f"  metrics: e2e={e['e2e_ms']}ms ttft={llm.get('ttft_ms')}ms "
                          f"decode={llm.get('decode_tps')}tps")
                elif t == "error":
                    print(f"  ERROR: {e['message']}")
                    failures.append(f"{want}: server error {e['message'][:80]}")
            if route != want:
                failures.append(f"{want}: routed to {route}")
            if not reply:
                failures.append(f"{want}: empty reply (endpointing or LLM failure)")
            elif want == "vision" and not any(m in reply.lower()
                                              for m in HONEST_MARKERS):
                print("  WARN: vision reply not recognizably honest "
                      "(no-frame guard may be off)")
            await asyncio.sleep(1.5)
        rt.cancel()
    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nPASS: {len(UTTERANCES)}/{len(UTTERANCES)} routes correct, all replies non-empty")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
