"""Wiresense — Phonon-2 streaming WebSocket contract validation.

Connects to `phonon-2 serve` at ws://127.0.0.1:8010/v1/audio/stream, streams
a 16 kHz mono PCM wav in ~100 ms chunks at ~4x realtime, and records the
server's message sequence. What we need to see for the incremental-prefill
loop: multiple `partial` messages (whole-hypothesis replacements) arriving
DURING streaming, then `final` on end-of-stream, then `done`.

Usage: .venv/bin/python tools/validate_phonon_ws.py [wav] [ws_url]
"""

import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
WS_URL = sys.argv[2] if len(sys.argv) > 2 else "ws://127.0.0.1:8010/v1/audio/stream"
WAV = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "audio/kokoro_chunk1_16k.wav"

CHUNK_MS = 100          # audio per websocket frame
PACE = 4.0              # stream at 4x realtime


def parse_frame(raw) -> dict:
    if isinstance(raw, str) and raw.startswith("{"):
        return json.loads(raw)
    return {"type": str(raw).strip()}


async def main() -> None:
    with wave.open(str(WAV), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, \
            "expect 16 kHz mono"
        pcm = w.readframes(w.getnframes())

    chunk_bytes = 16000 * CHUNK_MS // 1000 * 2
    sleep_s = CHUNK_MS / 1000 / PACE
    t_start = time.perf_counter()
    msgs = []

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"sample_rate": 16000,
                                  "format": "pcm_s16le"}))
        for off in range(0, len(pcm), chunk_bytes):
            await ws.send(pcm[off:off + chunk_bytes])
            await asyncio.sleep(sleep_s)
            while True:
                try:
                    m = parse_frame(
                        await asyncio.wait_for(ws.recv(), 0.01))
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                except websockets.ConnectionClosed as e:
                    print(f"connection closed: {e}")
                    return
                msgs.append((time.perf_counter() - t_start, m))
        await ws.send(json.dumps({"type": "end"}))
        async for raw in ws:
            m = parse_frame(raw)
            msgs.append((time.perf_counter() - t_start, m))
            if m.get("type") in ("done", "error"):
                break

    partials = [m for _, m in msgs if m.get("type") == "partial"]
    finals = [m for _, m in msgs if m.get("type") == "final"]
    print(f"{len(msgs)} messages: "
          f"{len(partials)} partial, {len(finals)} final, "
          f"{sum(1 for _, m in msgs if m.get('type') == 'error')} error")
    for t, m in msgs:
        text = json.dumps(m.get("text", m.get("message", "")))
        print(f"  +{t * 1000:7.0f} ms  {m.get('type'):8s} {text[:90]}")

    if msgs and msgs[0][1].get("type") == "error":
        print("SERVER REJECTED CONFIG FRAME — adapt fields and retry")
        return
    assert len(partials) >= 2, "expected multiple partials during streaming"
    assert finals, "expected a final message"
    print("PHONON WS VALIDATION PASS "
          f"(first msg +{msgs[0][0] * 1000:.0f} ms)")


if __name__ == "__main__":
    asyncio.run(main())
