"""wiresense web — browser mic → Phonon ASR → decide readout → llama-server → Kokoro TTS.

One port serves the page and one WS per client. Browser sends 16 kHz
s16le mono PCM in binary frames; server forwards to Phonon's
/v1/audio/stream (protocol validated in entry V: config frame key is
"format"), relays partials, runs the arm-C decide readout per partial
(entry-V adjudicated form: commit-only, no warming), streams the commit
response, then sentence-by-sentence Kokoro TTS sent back as base64 WAV.
Barge-in lite: a new final cancels playback client-side.
"""

import asyncio
import base64
import io
import json
import re
import time
import wave

import numpy as np
import requests
import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from kokoro_onnx import Kokoro

LLAMA = "http://127.0.0.1:8998"
PHONON = "ws://127.0.0.1:8010/v1/audio/stream"
MODELS = "../models"
PORT = 8996
SR = 16000
SYS = "You are wiresense, a concise voice assistant. Answer in one or two short sentences."
EMPTY_THINK = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
SLOT = 5  # dedicated llama slot for the web assistant (0-4 free for other work)

KOKORO = None


def system_block() -> str:
    return f"<|im_start|>system\n{SYS}<|im_end|>\n"


def build_prompt(history, utterance) -> str:
    p = system_block()
    for u, a in history:
        p += f"<|im_start|>user\n{u}<|im_end|>\n<|im_start|>assistant\n{a}<|im_end|>\n"
    p += f"<|im_start|>user\n{utterance}<|im_end|>\n" + EMPTY_THINK
    return p


class DecideReadout:
    """arm-C state head, numpy-only (weights exported to models/arm_C_heads.npz)."""

    def __init__(self):
        z = np.load(f"{MODELS}/arm_C_heads.npz")
        self.fc1_w, self.fc1_b = z["fc1.weight"], z["fc1.bias"]
        self.ln_w, self.ln_b = z["ln.weight"], z["ln.bias"]
        self.fc2_w, self.fc2_b = z["fc2.weight"], z["fc2.bias"]
        self.sigs = np.load(f"{MODELS}/sig_embeddings.npy").astype(np.float32)
        self.sig_names = json.load(open(f"{MODELS}/sig_names.json"))
        self.sigs = self._project(self.sigs)
        self.sigs = self.sigs / np.linalg.norm(self.sigs, axis=-1, keepdims=True)

    def _project(self, x):
        h = x @ self.fc1_w.T + self.fc1_b
        h = 0.5 * h * (1.0 + np.tanh(0.7978845608 * (h + 0.044715 * h ** 3)))
        ln = (h - h.mean(-1, keepdims=True)) / np.sqrt(h.var(-1, keepdims=True) + 1e-5)
        h = ln * self.ln_w + self.ln_b + h
        return h @ self.fc2_w.T + self.fc2_b

    def score(self, prompt_text):
        t0 = time.perf_counter()
        r = requests.post(f"{LLAMA}/v1/embeddings",
                          json={"input": prompt_text, "id_slot": 6}, timeout=30)
        emb = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
        t1 = time.perf_counter()
        v = self._project(emb[None, :])[0]
        v = v / np.linalg.norm(v)
        sims = self.sigs @ v
        top = int(np.argmax(sims))
        return {"top1": self.sig_names[top], "score": round(float(sims[top]), 3),
                "head_ms": round((time.perf_counter() - t1) * 1000, 1),
                "emb_ms": round((t1 - t0) * 1000, 1)}


def wav_bytes(samples, sr=24000) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.history = []
        self.phonon = None
        self.speaking = False   # synthesizing/streaming
        self.playback = False   # browser actually playing audio out loud
        self.gate_until = 0.0   # + tail guard after either ends
        self.gate_on = False

    def gate_active(self) -> bool:
        return (self.speaking or self.playback
                or time.monotonic() < self.gate_until)

    async def set_gate(self, on: bool):
        if on != self.gate_on:
            self.gate_on = on
            await self.send({"type": "mic_state", "listening": not on})

    async def update_gate(self):
        await self.set_gate(self.gate_active())

    async def release_gate_later(self, delay: float):
        await asyncio.sleep(delay)
        await self.update_gate()

    def schedule_gate_release(self, delay: float):
        # gate expiry is a timer, so push the mic_state transition then
        asyncio.create_task(self.release_gate_later(delay))
        self.turn = 0

    async def send(self, obj):
        try:
            await self.ws.send(json.dumps(obj))
        except ConnectionClosed:
            pass

    async def phonon_connect(self):
        for attempt in range(15):  # Phonon serves ONE stream; wait if busy
            try:
                self.phonon = await websockets.connect(PHONON, max_size=2 ** 22,
                                                       open_timeout=10)
                await self.phonon.send(json.dumps({"sample_rate": SR,
                                                   "format": "pcm_s16le"}))
                asyncio.create_task(self.phonon_reader())
                return
            except ConnectionClosed:
                await asyncio.sleep(1)
        raise RuntimeError("phonon busy: another stream holds /v1/audio/stream")

    async def phonon_reader(self):
        try:
            async for raw in self.phonon:
                if isinstance(raw, bytes):
                    continue
                s = raw.strip()
                if s == "done":
                    continue  # endpointing handled on "final"
                m = json.loads(s)
                t = m.get("type")
                if t == "partial":
                    await self.send({"type": "partial", "text": m["text"]})
                    asyncio.create_task(self.decide(m["text"]))
                elif t == "final":
                    await self.send({"type": "final", "text": m["text"]})
                    await self.respond(m["text"])
        except ConnectionClosed:
            self.phonon = None

    async def decide(self, partial):
        try:
            loop = asyncio.get_event_loop()
            ro = await loop.run_in_executor(None, READOUT.score,
                                            build_prompt(self.history, partial))
            await self.send({"type": "decide", **ro})
        except Exception as e:  # readout must never break the voice loop
            await self.send({"type": "decide_error", "message": str(e)[:120]})

    async def respond(self, utterance):
        self.speaking = True
        await self.update_gate()
        await self.send({"type": "cancel_tts"})
        t0 = time.perf_counter()
        first, chunks = None, []
        try:
            with requests.post(f"{LLAMA}/v1/completions", json={
                    "prompt": build_prompt(self.history, utterance),
                    "max_tokens": 96, "temperature": 0, "cache_prompt": True,
                    "id_slot": SLOT, "stream": True}, stream=True, timeout=120) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith(b"data: "):
                        continue
                    payload = line[len(b"data: "):]
                    if payload == b"[DONE]":
                        break
                    delta = json.loads(payload)["choices"][0].get("text", "")
                    if delta and first is None:
                        first = (time.perf_counter() - t0) * 1000
                        await self.send({"type": "response_start",
                                         "ttft_ms": round(first, 1)})
                    if delta:
                        chunks.append(delta)
                        await self.send({"type": "response_delta", "text": delta})
        except Exception as e:
            await self.send({"type": "error", "message": f"llm: {e}"})
            self.speaking = False
            self.gate_until = time.monotonic() + 0.35
            await self.update_gate()
            self.schedule_gate_release(0.35)
            return
        reply = "".join(chunks).strip()
        await self.send({"type": "response_done", "text": reply})
        await self.tts(reply)
        self.history.append((utterance, reply))
        self.speaking = False
        # gate does NOT open here: the browser reports actual playback end
        # (playback_state) — much later than synthesis on long replies.
        # The floor covers the race before the first playing:true arrives.
        self.gate_until = max(self.gate_until, time.monotonic() + 1.0)
        await self.update_gate()
        self.schedule_gate_release(1.0)

    async def tts(self, text):
        sents = [s for s in SENT_SPLIT.split(text) if s.strip()] or [text]
        seq = 0
        for sent in sents:
            t0 = time.perf_counter()
            try:
                loop = asyncio.get_event_loop()
                samples, sr = await loop.run_in_executor(
                    None, lambda s=sent: KOKORO.create(s, voice="af_heart",
                                                       speed=1.0, lang="en-us"))
                audio = await loop.run_in_executor(None, wav_bytes, samples, sr)
            except Exception as e:
                await self.send({"type": "error", "message": f"tts: {e}"})
                break
            await self.send({"type": "tts_chunk", "seq": seq,
                             "ms": round((time.perf_counter() - t0) * 1000, 1),
                             "audio": base64.b64encode(audio).decode()})
            seq += 1
        await self.send({"type": "tts_done"})


async def handler(ws):
    session = Session(ws)
    peer = ws.remote_address[0] if ws.remote_address else "?"
    print(f"[connect] {peer}", flush=True)
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                if session.gate_active():
                    continue  # half-duplex: never feed TTS playback back to ASR
                if session.phonon is None:
                    await session.phonon_connect()
                    print(f"[{peer}] phonon stream open", flush=True)
                await session.phonon.send(msg)
            else:
                m = json.loads(msg)
                if m.get("type") == "reset":
                    session.history.clear()
                    await session.send({"type": "reset_ok"})
                    print(f"[{peer}] history reset", flush=True)
                elif m.get("type") == "playback_state":
                    session.playback = bool(m.get("playing"))
                    if not session.playback:
                        session.gate_until = time.monotonic() + 0.35
                        session.schedule_gate_release(0.35)
                    await session.update_gate()
    finally:
        if session.phonon is not None:
            await session.phonon.close()  # Phonon serves one stream at a time
            print(f"[{peer}] phonon stream closed", flush=True)


async def http_page(connection, request):
    # websockets process_request hook: serve the single page, else upgrade
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    if request.path == "/":
        html = open("index.html", "rb").read()
        hdrs = Headers([("Content-Type", "text/html; charset=utf-8"),
                        ("Content-Length", str(len(html)))])
        return Response(200, "OK", hdrs, html)
    return None


async def main():
    global KOKORO, READOUT
    t0 = time.perf_counter()
    KOKORO = Kokoro(f"{MODELS}/kokoro-v1.0.onnx", f"{MODELS}/voices.npz")
    READOUT = DecideReadout()
    print(f"kokoro + readout loaded in {time.perf_counter() - t0:.1f}s", flush=True)
    async with serve(handler, "0.0.0.0", PORT, process_request=http_page,
                     max_size=2 ** 20, ping_interval=20):
        print(f"wiresense web on 0.0.0.0:{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
