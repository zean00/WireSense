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
import os
import re
import time
import wave
from collections import deque

import numpy as np
import onnxruntime as ort
import requests
import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from kokoro_onnx import Kokoro

LLAMA = "http://127.0.0.1:8998"
PHONON = "ws://127.0.0.1:8010/v1/audio/stream"
MODELS = "../models"
TOOLS = "../tools"
PORT = 8996
MIC_CAPTURE = os.environ.get("WIRESENSE_MIC_CAPTURE", "")  # debug: save mic feed
SR = 16000
GATE_TAIL = 0.6      # s after playback ends before the mic path reopens
VAD_ON = 0.5         # Silero p(speech) threshold
WAKE_FRAMES = 3      # consecutive 100 ms frames >= VAD_ON to open the ASR stream
PREROLL_N = 10        # frames (1 s) buffered while idle, flushed on wake
SYS = "You are wiresense, a concise voice assistant. Answer in one or two short sentences."
EMPTY_THINK = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
SLOT = 5  # dedicated llama slot for the web assistant (0-4 free for other work)

# Agentic decision tree (tools/decision_tree.json): root routes every
# turn to a component (llm paths / tool flow / vision guard); subtrees
# refine think (long_think|tool) and vision. Non-llm-direct turns speak
# a pre-answer filler first; long user speech gets listening cues.
# Mid-speech TTS is safe because the browser mic runs echo cancellation
# (index.html getUserMedia) and a final cancels playback client-side.
# Per the tree-probe experiment: NO confidence gating (cosine conf is
# miscalibrated as an error signal).
BC_FIRST_S = 6.0   # min user speech before the first backchannel
BC_GAP_S = 5.0     # min spacing between backchannels
BC_MAX = 2         # backchannels per turn

KOKORO = None
READOUT = None
WALKER = None


def norm_rows(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def norm_router_text(t: str) -> str:
    """ASR text style for the router: lowercase, punctuation-free. Both
    refs (tools/record_router.py) and live utterances pass through this —
    punctuation asymmetry between ASR transcripts and written pools
    collapsed the class margins in the first live trial."""
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def system_block() -> str:
    # byte-stable across all turns and modes — the frozen prefix the
    # two-zone context discipline depends on
    return f"<|im_start|>system\n{SYS}<|im_end|>\n"


def build_prompt(history, utterance, directive=None) -> str:
    # mode directives ride the CURRENT user turn (never the system block):
    # the KV prefix stays warm across mode switches, and the decide
    # readout's input is identical whether or not a directive is appended
    u = utterance if not directive else f"{utterance}\n({directive})"
    p = system_block()
    for hu, ha in history:
        p += f"<|im_start|>user\n{hu}<|im_end|>\n<|im_start|>assistant\n{ha}<|im_end|>\n"
    p += f"<|im_start|>user\n{u}<|im_end|>\n" + EMPTY_THINK
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

    def embed(self, prompt_text):
        t0 = time.perf_counter()
        r = requests.post(f"{LLAMA}/v1/embeddings",
                          json={"input": prompt_text, "id_slot": 6}, timeout=30)
        emb = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
        return emb, (time.perf_counter() - t0) * 1000

    def readout(self, emb):
        t0 = time.perf_counter()
        v = norm_rows(self._project(emb[None, :]))[0]
        sims = self.sigs @ v
        top = int(np.argmax(sims))
        return {"top1": self.sig_names[top], "score": round(float(sims[top]), 3),
                "head_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def score(self, prompt_text):
        emb, emb_ms = self.embed(prompt_text)
        ro = self.readout(emb)
        ro["emb_ms"] = round(emb_ms, 1)
        return ro, emb


class TreeWalker:
    """Walks tools/decision_tree.json: each node is a CLM readout
    (head-projected class centroids over slot-6 embeddings of RAW text,
    refs keyed <node>.<class> in models/router_embeddings.npz, recorded
    by tools/record_router.py; re-record on any serving-numerics change,
    same rule as decide sigs). Live turns embed the bare utterance —
    prompt/history-shaped vectors were measured and dropped (domain
    shift cost two route misses in the first live trial).

    Feasibility (pre-registered evals): root 49/56 held-out; think
    subtree 18/20 with perfect tool recall; vision subtree 17/20 (probe:
    tools/tree_probe.py). No confidence gating by adjudicated decision.
    """

    def __init__(self, project):
        self._project = project
        self.cfg = json.load(open(f"{TOOLS}/decision_tree.json"))
        z = np.load(f"{MODELS}/router_embeddings.npz")
        self.nodes = {}
        for nid, node in self.cfg["nodes"].items():
            classes = list(node["classes"])
            cents = [norm_rows(self._project(z[f"{nid}.{c}"])).mean(0)
                     for c in classes]
            self.nodes[nid] = (classes, norm_rows(np.stack(cents)))
        self.fb = {mode: (texts, norm_rows(self._project(z[f"feedback.{mode}"])))
                   for mode, texts in self.cfg["feedback"].items()}
        self.bc_texts = self.cfg["backchannel"]
        self.bc_vecs = norm_rows(self._project(z["backchannel"]))

    def route(self, emb, node_id="root"):
        """Return (class, spec, conf) for the node's best class. Spec is
        the class's config entry (action params)."""
        classes, cents = self.nodes[node_id]
        v = norm_rows(self._project(emb[None, :]))[0]
        sims = cents @ v
        i = int(np.argmax(sims))
        cls = classes[i]
        spec = dict(self.cfg["nodes"][node_id]["classes"][cls])
        spec["_class"] = cls
        return cls, spec, round(float(sims[i]), 3)

    def feedback(self, emb, key):
        if key not in self.fb:
            key = "think"
        texts, vecs = self.fb[key]
        v = norm_rows(self._project(emb[None, :]))[0]
        return texts[int(np.argmax(vecs @ v))]

    def backchannel(self, emb):
        v = norm_rows(self._project(emb[None, :]))[0]
        return self.bc_texts[int(np.argmax(self.bc_vecs @ v))]


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


_VAD_SESS = None


def vad_session() -> ort.InferenceSession:
    global _VAD_SESS
    if _VAD_SESS is None:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _VAD_SESS = ort.InferenceSession(f"{MODELS}/silero_vad.onnx",
                                         sess_options=opts)
    return _VAD_SESS


class SileroVAD:
    """Stateful Silero wrapper, contract from the official utils_vad.py:
    each call feeds context(64) + chunk(512) samples at 16 kHz, carries the
    LSTM state, and the context slides to the last 64 samples."""

    CHUNK, CONTEXT = 512, 64

    def __init__(self):
        self.sess = vad_session()
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, self.CONTEXT), dtype=np.float32)
        self.buf = np.zeros(0, dtype=np.float32)

    def probs(self, pcm16: bytes) -> list[float]:
        x = np.frombuffer(pcm16, "<i2").astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, x]) if len(self.buf) else x
        out = []
        while len(self.buf) >= self.CHUNK:
            chunk, self.buf = self.buf[:self.CHUNK], self.buf[self.CHUNK:]
            inp = np.concatenate([self.context, chunk[None, :]], axis=1)
            o = self.sess.run(None, {"input": inp, "state": self.state,
                                     "sr": np.array(16000, dtype=np.int64)})
            out.append(float(o[0][0, 0]))
            self.state = o[1]
            self.context = inp[:, -self.CONTEXT:]
        return out


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.history = []
        self.phonon = None
        self.speaking = False   # synthesizing/streaming
        self.playback = False   # browser actually playing audio out loud
        self.gate_until = 0.0   # + tail guard after either ends
        self.gate_on = False
        self.vad = SileroVAD()
        self.vad_streak = 0
        self.ctx_rate = 0  # browser AudioContext rate; 16k = no resample path
        self.cap = None    # debug wav of exactly what Phonon consumes
        self.preroll = deque(maxlen=PREROLL_N)  # kept while idle, flushed on wake
        # per-turn timing (perf_counter base, all within one event loop)
        self.t_wake = None
        self.n_frames = 0        # 100 ms frames forwarded to Phonon this turn
        self.first_partial_t = None
        self.partial_count = 0
        self.decide_stats = []
        self.t_final = None
        self.tts_first_ms = None
        self.tts_total_ms = 0.0
        self.tts_n = 0
        self.last_status = 0.0
        # agentic routing state (per turn)
        self.route_mode = "direct"
        self.turn_id = 0
        self.bc_count = 0
        self.bc_last_t = 0.0

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
                    self.partial_count += 1
                    if self.first_partial_t is None:
                        self.first_partial_t = time.perf_counter()
                    await self.send({"type": "partial", "text": m["text"]})
                    asyncio.create_task(self.decide(m["text"]))
                elif t == "final":
                    await self.send({"type": "final", "text": m["text"]})
                    await self.respond(m["text"])
                    # turn over: close the ASR stream so idle room tone can
                    # never reach a primed decoder (hallucination source)
                    await self.phonon.close()
                    self.phonon = None
                    self.vad.reset()
                    self.vad_streak = 0
                    self.preroll.clear()
        except ConnectionClosed:
            self.phonon = None

    async def decide(self, partial):
        try:
            loop = asyncio.get_event_loop()
            ro = await loop.run_in_executor(
                None, lambda: READOUT.score(build_prompt(self.history, partial))[0])
            self.decide_stats.append(ro)
            await self.send({"type": "decide", **ro})
            await self.maybe_backchannel(partial)
        except Exception as e:  # readout must never break the voice loop
            await self.send({"type": "decide_error", "message": str(e)[:120]})

    async def maybe_backchannel(self, partial):
        # long user speech: mimic human listening cues. Mid-speech TTS is
        # safe (browser AEC); a final arriving mid-clip cancels playback.
        if self.t_wake is None:
            return
        elapsed = time.perf_counter() - self.t_wake
        if (self.bc_count >= BC_MAX or elapsed < BC_FIRST_S
                or elapsed - self.bc_last_t < BC_GAP_S):
            return
        self.bc_count += 1
        self.bc_last_t = elapsed
        try:
            loop = asyncio.get_event_loop()
            emb, _ = await loop.run_in_executor(None, READOUT.embed,
                                                norm_router_text(partial))
            text = WALKER.backchannel(emb)
        except Exception:
            return
        await self.send({"type": "backchannel", "text": text})
        asyncio.create_task(self.speak_fillers([text]))

    async def speak_fillers(self, lines):
        """TTS a short filler; abandoned the moment a new turn starts."""
        my_turn = self.turn_id
        for line in lines:
            if self.turn_id != my_turn:
                return
            await self.tts(line, guard_turn=my_turn)

    async def respond(self, utterance):
        self.speaking = True
        self.t_final = time.perf_counter()
        self.turn_id += 1
        my_turn = self.turn_id
        await self.update_gate()
        await self.send({"type": "cancel_tts"})
        # route on a fresh raw-utterance embedding (~55 ms on slot 6;
        # refs are raw texts — prompt/history-shaped vectors measured
        # markedly worse). Non-direct turns speak a filler first so the
        # LLM work happens behind speech instead of dead air.
        cls, spec = "direct", {"action": "llm", "max_tokens": 96}
        try:
            loop = asyncio.get_event_loop()
            emb, _ = await loop.run_in_executor(None, READOUT.embed,
                                                norm_router_text(utterance))
            cls, spec, conf = WALKER.route(emb)
            await self.send({"type": "route", "mode": cls, "conf": conf})
            if spec["action"] == "vision_generic":
                # capture hook for the VLM phase: request a frame from the
                # client here, attach it, and take the think path. Until
                # that lands the honesty directive keeps vision turns honest.
                pass
            if spec.get("filler"):
                fb = WALKER.feedback(emb, cls)
                await self.send({"type": "feedback", "text": fb})
                await self.tts(fb, guard_turn=my_turn)
        except Exception:
            cls, spec = "direct", {"action": "llm", "max_tokens": 96}
        self.route_mode = cls
        t0 = time.perf_counter()
        first, chunks, timings = None, [], None
        try:
            with requests.post(f"{LLAMA}/v1/completions", json={
                    "prompt": build_prompt(self.history, utterance,
                                           spec.get("directive")),
                    "max_tokens": spec.get("max_tokens", 96),
                    "temperature": 0, "cache_prompt": True,
                    "id_slot": SLOT, "stream": True}, stream=True, timeout=120) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith(b"data: "):
                        continue
                    payload = line[len(b"data: "):]
                    if payload == b"[DONE]":
                        break
                    j = json.loads(payload)
                    delta = j["choices"][0].get("text", "")
                    if j.get("timings"):
                        timings = j["timings"]  # llama.cpp reports on chunks
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
            self.gate_until = time.monotonic() + GATE_TAIL
            await self.update_gate()
            self.schedule_gate_release(GATE_TAIL)
            return
        reply = "".join(chunks).strip()
        # fp4-fault signature: output is essentially all '?' (see
        # docs/INCIDENT-2026-10-04-GPU.md) — surfaced to the UI, never fatal
        corrupt = len(reply) >= 8 and reply.count("?") >= 0.9 * len(reply)
        await self.send({"type": "response_done", "text": reply})
        await self.tts(reply)
        self.history.append((utterance, reply))
        self.speaking = False
        await self.send_metrics(timings, round(first, 1) if first else None, corrupt)
        # gate does NOT open here: the browser reports actual playback end
        # (playback_state) — much later than synthesis on long replies.
        # The floor covers the race before the first playing:true arrives.
        self.gate_until = max(self.gate_until, time.monotonic() + 1.0)
        await self.update_gate()
        self.schedule_gate_release(1.0)

    async def tts(self, text, guard_turn=None):
        sents = [s for s in SENT_SPLIT.split(text) if s.strip()] or [text]
        seq = 0
        for sent in sents:
            if guard_turn is not None and self.turn_id != guard_turn:
                return  # a new turn started; this filler is stale
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
            ms = (time.perf_counter() - t0) * 1000
            if self.tts_first_ms is None and self.t_final is not None:
                self.tts_first_ms = round((time.perf_counter() - self.t_final)
                                          * 1000, 1)
            self.tts_total_ms += ms
            self.tts_n += 1
            await self.send({"type": "tts_chunk", "seq": seq, "ms": round(ms, 1),
                             "audio": base64.b64encode(audio).decode()})
            seq += 1
        await self.send({"type": "tts_done"})

    async def send_metrics(self, timings, ttft_ms, corrupt=False):
        ds = self.decide_stats
        llm = {}
        if timings:
            pn, pms = timings.get("prompt_n"), timings.get("prompt_ms")
            gn, gms = timings.get("predicted_n"), timings.get("predicted_ms")
            llm = {"prompt_n": pn, "prompt_ms": round(pms, 1) if pms else None,
                   "prefill_tps": round(pn / pms * 1000, 1) if pms and pn else None,
                   "predicted_n": gn,
                   "predicted_ms": round(gms, 1) if gms else None,
                   "decode_tps": round(gn / gms * 1000, 1) if gms and gn else None}
        await self.send({
            "type": "metrics",
            "asr": {
                "wake_to_final_ms": round((self.t_final - self.t_wake) * 1000, 1)
                                    if self.t_wake else None,
                "audio_s": round(self.n_frames * 0.1, 1),
                "partials": self.partial_count,
                "first_partial_ms": round((self.first_partial_t - self.t_wake)
                                          * 1000, 1) if self.first_partial_t
                                          and self.t_wake else None,
            },
            "decide": {
                "n": len(ds),
                "last_ms": round(ds[-1]["head_ms"] + ds[-1]["emb_ms"], 1)
                           if ds else None,
                "max_ms": round(max(d["head_ms"] + d["emb_ms"] for d in ds), 1)
                          if ds else None,
            },
            "llm": {"ttft_ms": ttft_ms, "corrupt": corrupt, **llm},
            "tts": {"first_ms": self.tts_first_ms,
                    "total_ms": round(self.tts_total_ms, 1),
                    "sentences": self.tts_n},
            "e2e_ms": self.tts_first_ms,  # final → first audio out of server
        })
        # reset per-turn accumulators
        self.t_wake = None
        self.n_frames = 0
        self.first_partial_t = None
        self.partial_count = 0
        self.decide_stats = []
        self.tts_first_ms = None
        self.tts_total_ms = 0.0
        self.tts_n = 0
        self.route_mode = "direct"
        self.bc_count = 0
        self.bc_last_t = 0.0


async def handler(ws):
    session = Session(ws)
    peer = ws.remote_address[0] if ws.remote_address else "?"
    print(f"[connect] {peer}", flush=True)
    if MIC_CAPTURE:
        session.cap = wave.open(f"{MIC_CAPTURE}-{peer}.wav", "wb")
        session.cap.setnchannels(1)
        session.cap.setsampwidth(2)
        session.cap.setframerate(SR)
        print(f"[{peer}] capturing mic feed to {MIC_CAPTURE}-{peer}.wav",
              flush=True)
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                if session.gate_active():
                    session.preroll.clear()
                    session.vad_streak = 0
                    continue  # half-duplex: never feed TTS playback back to ASR
                if session.phonon is None:
                    # idle: wake only on sustained SPEECH (Silero VAD latch);
                    # room tone / music / noise never reach the ASR
                    ps = session.vad.probs(msg)
                    session.vad_streak = session.vad_streak + 1 \
                        if ps and all(p >= VAD_ON for p in ps) else 0
                    session.preroll.append(msg)
                    now = time.monotonic()
                    if now - session.last_status > 0.5:  # live VAD readout
                        session.last_status = now
                        await session.send({
                            "type": "mic_status",
                            "vad": round(max(ps), 3) if ps else None,
                            "listening": True})
                    if session.vad_streak < WAKE_FRAMES:
                        continue
                    session.vad_streak = 0
                    await session.phonon_connect()
                    frames = list(session.preroll)
                    for f in frames:  # 1 s pre-roll, INCL. the current frame
                        await session.phonon.send(f)
                    if session.cap:
                        for f in frames:
                            session.cap.writeframes(f)
                    session.preroll.clear()
                    session.t_wake = time.perf_counter()
                    session.n_frames = len(frames)
                    session.first_partial_t = None
                    session.partial_count = 0
                    session.decide_stats = []
                    print(f"[{peer}] wake p_speech_max={max(ps):.2f} "
                          f"ctx={session.ctx_rate}Hz — phonon stream open",
                          flush=True)
                    continue  # current frame already went out with the flush
                await session.phonon.send(msg)
                session.n_frames += 1
                if session.cap:
                    session.cap.writeframes(msg)
            else:
                m = json.loads(msg)
                if m.get("type") == "hello":
                    session.ctx_rate = int(m.get("ctx_rate") or 0)
                    print(f"[{peer}] browser ctx {session.ctx_rate} Hz "
                          "(16000 = no resample path)", flush=True)
                elif m.get("type") == "reset":
                    session.history.clear()
                    await session.send({"type": "reset_ok"})
                    print(f"[{peer}] history reset", flush=True)
                elif m.get("type") == "playback_state":
                    session.playback = bool(m.get("playing"))
                    if not session.playback:
                        session.gate_until = time.monotonic() + GATE_TAIL
                        session.schedule_gate_release(GATE_TAIL)
                    await session.update_gate()
    finally:
        if session.cap:
            session.cap.close()
            session.cap = None
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
    global KOKORO, READOUT, WALKER
    t0 = time.perf_counter()
    KOKORO = Kokoro(f"{MODELS}/kokoro-v1.0.onnx", f"{MODELS}/voices.npz")
    READOUT = DecideReadout()
    WALKER = TreeWalker(READOUT._project)
    print(f"kokoro + readout + decision tree loaded in "
          f"{time.perf_counter() - t0:.1f}s", flush=True)
    async with serve(handler, "0.0.0.0", PORT, process_request=http_page,
                     max_size=2 ** 20, ping_interval=20):
        print(f"wiresense web on 0.0.0.0:{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
