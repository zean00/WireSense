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
import traceback
import uuid
import wave
from collections import deque

import numpy as np
import onnxruntime as ort
import requests
import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from PIL import Image

from kokoro_onnx import Kokoro

import deep_think
import faces
import browser_use
import ir_use

LLAMA = "http://127.0.0.1:8998"
PHONON = "ws://127.0.0.1:8010/v1/audio/stream"
OMDET = "http://127.0.0.1:8011"
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

# --- V1 tool registry (data, not code paths): the LLM picks from this
# manifest; execution is server-side (reminder/note -> memory.json,
# relative-time reminders fire via asyncio) or delegated to the client
# (timer beeps locally via tool_event). ---
TOOL_MANIFEST = {
    "timer": "start a countdown; args: seconds (int 1-86400), label (optional short string)",
    "reminder": "save a reminder; args: when (short phrase, e.g. 'in 10 minutes' or 'Friday 9am'), text (what to remind)",
    "note": "save a note for later; args: text (the note)",
    "detect": "look through the camera and say which listed objects are visible; args: labels (array of 1-12 short object names, e.g. [\"person\", \"coffee mug\"])",
}
MEMORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "memory.json")
TREE_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "tools", "decision_tree.json")
# face recognition (docs/PROTOCOL-face-recognition.md): opt-in, off by
# default; disabled = the tools vanish from the manifest, no face code
# runs, no `face` key in metrics — byte-identical pre-face behavior
FACE_ENABLED = False
FACE_TOOLS = {
    "face_enroll": "learn who a face belongs to; args: name (who is in front of the camera)",
    "face_forget": "delete saved faces; args: name (optional — omit to forget everyone)",
}
# vision-routed face-management phrasings ("remember/forget my face"):
# the router sends face talk to vision (content pull measured 0.795-0.867;
# pool refs cannot beat it at root without breaking direct), so when the
# feature is opted in these are intercepted as the tool flow (Rev 1)
FACE_INTENT_RE = re.compile(
    r"\b(remember|learn|save)\b.{0,40}\bface\b"
    r"|\b(forget|delete|erase)\b.{0,40}\bface\b", re.I)
# ambient face context (docs/PROTOCOL-ambient-face.md): one frame at speech
# onset, recognized while the user is still talking, context line riding
# every non-vision turn; needs BOTH config keys plus the per-session toggle
FACE_AMBIENT = False


def camera_line(names) -> str:
    """The one volatile-suffix context line shared by the vision path and
    ambient turns; names may include the 'an unrecognized person'
    placeholders from faces.recognize."""
    return "(Camera: " + " and ".join(names) + ")\n"


def ambient_active(session_on) -> bool:
    """Ambient runs only when the per-session toggle is on AND both config
    opt-ins are present (server-side clamp, decision 1)."""
    return bool(session_on and FACE_ENABLED and FACE_AMBIENT)


REL_TIME = re.compile(r"\bin (\d+) (second|minute|hour)s?\b", re.I)

# transcript repair (docs/PROTOCOL-asr-normalizer.md): the complete v1
# rule set. Full-utterance matches only, so "It's two a.m." and
# "Two am I late?" can never match; every firing is logged and a rule
# that never fires on real traffic is removed at the next revision.
ASR_NORMALIZERS = [(re.compile(r"^\s*(two|to|too|2)\s+am\s+i\s*[?!.]*\s*$",
                              re.I), "Who am I?")]

# transcript-safety layer (docs/PROTOCOL-transcript-safety.md): M1 semantic
# judge for short rule-missed finals, M2 read-back for high-risk tools.
# Prompts are frozen verbatim from the protocol calibration (36/36 at a
# 115 ms mean on the 36-item corpus).
JUDGE_SLOT = 4  # dedicated: the main decode owns SLOT, embeddings own 6
HIGH_RISK_TOOLS = {"timer", "reminder"}
JUDGE_SYS = ("You judge transcripts from speech recognition. Grammatical "
             "sentences can still be recognition errors. Answer with only "
             "YES or NO.")
JUDGE_USER = ('Examples:\n'
              'Transcript: "The purple sleeps loudly" -> NO\n'
              'Transcript: "What time is it?" -> YES\n'
              'Transcript: "Yes, you are two." -> NO\n'
              'Transcript: "Who is this?" -> YES\n'
              'Transcript: "{t}"\n'
              'Is this something a person would naturally say?')
REPAIR_SYS = "You repair garbled speech-recognition transcripts."
REPAIR_USER = ('Garbled transcript: "{t}"\nWhat did the person most likely '
               'try to say? Answer with only the corrected sentence, '
               'nothing else.')
RESOLVE_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "right",
               "correct", "confirm", "confirmed", "do it", "go ahead",
               "yes please", "that's right", "sounds good", "please do"}
RESOLVE_NO = {"no", "nope", "nah", "cancel", "stop", "wrong", "don't",
              "dont", "no thanks", "that's wrong"}


def normalize_transcript(text: str) -> str:
    for rx, replacement in ASR_NORMALIZERS:
        if rx.match(text):
            fixed = replacement
            if fixed != text:
                print(f"[asr-normalizer] {text!r} -> {fixed!r}", flush=True)
            return fixed
    # Phonon glues digits to the preceding word ("for10 minutes",
    # "in15 seconds") — measured: 'Set a timer for10 minutes.' pulled
    # the root centroid think->browser (0.83 vs 0.82) while the spaced
    # text routes think. Split letter-digit joins (decision 2: the
    # single repair point every turn passes through).
    fixed = re.sub(r"(?<=[a-z])(?=\d)", " ", text, flags=re.I)
    if fixed != text:
        print(f"[asr-normalizer] {text!r} -> {fixed!r}", flush=True)
        return fixed
    return text

TOOL_FALLBACK_DIRECTIVE = ("No available tool fits this request. Answer briefly; "
                           "if you lack the ability (for example live data or "
                           "device control), say so honestly.")
TOOL_RETRY_DIRECTIVE = ('Your last reply was not valid tool JSON. Reply with '
                        'ONLY a JSON object: {"tool": "<name>", "args": {...}} '
                        'or {"tool": null}. No other text.')
THINK_NOCLOSE_REPLY = ("I thought it through, but ran out of room for a "
                       "clean answer.")


def tool_manifest_directive() -> str:
    tools = dict(TOOL_MANIFEST)
    if FACE_ENABLED:
        tools.update(FACE_TOOLS)
    lines = "\n".join(f"- {name}: {desc}" for name, desc in tools.items())
    return ("Choose a tool for this request. Available tools:\n" + lines +
            '\nReply with ONLY a JSON object: {"tool": "<name>", "args": {...}} '
            'or {"tool": null} if no tool fits. No other text.')


def parse_tool_json(text):
    """Extract a tool plan from generation text; None if unparseable."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(j, dict) or "tool" not in j:
        return None
    tool, args = j["tool"], j.get("args")
    if tool is None:
        return {"tool": None, "args": {}}
    if not isinstance(tool, str) or not isinstance(args, dict):
        return None
    return {"tool": tool, "args": args}


def validate_tool(name, args) -> dict:
    """Normalize + bound-check args; raises ValueError on any bad arg."""
    if name == "timer":
        secs = int(args.get("seconds"))
        if not 1 <= secs <= 86400:
            raise ValueError("seconds out of range")
        return {"seconds": secs, "label": str(args.get("label") or "")[:80]}
    if name == "reminder":
        when = str(args.get("when") or "").strip()
        text = str(args.get("text") or "").strip()
        if not when or not text:
            raise ValueError("reminder needs when + text")
        return {"when": when[:120], "text": text[:200]}
    if name == "note":
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("note needs text")
        return {"text": text[:500]}
    if name == "detect":
        raw = args.get("labels")
        if not isinstance(raw, list):
            raise ValueError("detect needs labels (array of object names)")
        labels = [str(x).strip().lower()[:40] for x in raw if str(x).strip()]
        if not 1 <= len(labels) <= 12:
            raise ValueError("detect needs 1-12 labels")
        # dedupe, keep order (the sidecar task prompt is built from these)
        seen = set()
        labels = [x for x in labels if not (x in seen or seen.add(x))]
        return {"labels": labels}
    if name == "face_enroll":
        if not FACE_ENABLED:
            raise ValueError("face recognition is disabled")
        face_name = str(args.get("name") or "").strip()[:40]
        if not face_name:
            raise ValueError("face_enroll needs a name (1-40 chars)")
        return {"name": face_name}
    if name == "face_forget":
        if not FACE_ENABLED:
            raise ValueError("face recognition is disabled")
        return {"name": str(args.get("name") or "").strip()[:40]}
    raise ValueError(f"unknown tool {name}")


def parse_relative_seconds(when: str):
    m = REL_TIME.search(when)
    if not m:
        return None
    return int(m.group(1)) * {"second": 1, "minute": 60, "hour": 3600}[m.group(2)]


def words_count(text) -> int:
    return len(re.findall(r"[a-z0-9']+", text.lower()))


def judge_eligible(text, norm_fired) -> bool:
    """M1 trigger (protocol decision 2): short finals the normalizer did
    not already repair. Longer utterances never see the judge."""
    return (not norm_fired) and words_count(text) <= 4


def classify_resolution(text):
    """Pending-resolution vocabulary (protocol decision 6); exact match
    after lowercase, punctuation-stripping and space-squeezing."""
    k = re.sub(r"[^a-z' ]", " ", text.lower())
    k = re.sub(r"\s+", " ", k).strip()
    if k in RESOLVE_YES:
        return "yes"
    if k in RESOLVE_NO:
        return "no"
    return None


def parse_verdict(text):
    """Judge verdict parse; None means junk and fails open to sensible."""
    v = (text or "").strip().upper().rstrip(".!").strip()
    if v.startswith("YES"):
        return True
    if v.startswith("NO"):
        return False
    return None


def readback_text(tool, args):
    """M2 spoken read-back of the interpreted call (protocol decision 9)."""
    if tool == "timer":
        what = args.get("label") or f"{args.get('seconds')} seconds"
        return f"Just to confirm — a timer for {what}?"
    if tool == "reminder":
        return (f"Just to confirm — a reminder to {args.get('text')}, "
                f"{args.get('when')}?")
    return None


def append_memory(item):
    items = load_memory()
    items.append(item)
    with open(MEMORY_PATH, "w") as f:
        json.dump(items, f, indent=1)


def mark_fired(rid):
    if not rid:
        return
    items = load_memory()
    for it in items:
        if it.get("id") == rid:
            it["fired"] = True
    with open(MEMORY_PATH, "w") as f:
        json.dump(items, f, indent=1)


def load_memory() -> list:
    try:
        with open(MEMORY_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _now(offset_s=0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time()
                                                             + offset_s))

KOKORO = None
READOUT = None
WALKER = None
DEEP = None  # optional big-model think provider (web/config.json), or None


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


def build_prompt(history, utterance, directive=None, open_think=False) -> str:
    # mode directives ride the CURRENT user turn (never the system block):
    # the KV prefix stays warm across mode switches, and the decide
    # readout's input is identical whether or not a directive is appended
    u = utterance if not directive else f"{utterance}\n({directive})"
    p = system_block()
    for hu, ha in history:
        p += f"<|im_start|>user\n{hu}<|im_end|>\n<|im_start|>assistant\n{ha}<|im_end|>\n"
    p += f"<|im_start|>user\n{u}<|im_end|>\n"
    # open_think leaves the block unclosed so the model reasons before
    # answering; everything up to </think> is the (unspoken) trace
    p += ("<|im_start|>assistant\n<think>\n" if open_think else EMPTY_THINK)
    return p


# Context-dependent fragments ("what about on tuesday?") embed far from the
# standalone action sentences the router refs were recorded as; the exploratory
# probe measured 1/6 correct on raw fragments vs 4/6 on self-contained
# rewrites. needs_context gates the ~0.3 s rewrite tax to the cases that need
# it; tools/eval_followup.py is the frozen bar for both trigger and rewrite.
FOLLOWUP_LEAD = re.compile(
    r"^(and|so|also|then|but|or|plus|now|well|ok|okay|actually|instead|"
    r"again|what about|how about|why not)\b", re.I)
FOLLOWUP_HINT = re.compile(
    r"\b(instead|make it|change it|same (?:for|with|thing)|what about|"
    r"how about|that one|this one|the other)\b", re.I)


def needs_context(utterance: str, has_history: bool) -> bool:
    # length-gated: a long connective-led utterance ("So yesterday I was
    # walking to the store…") is self-contained and routes fine raw — the
    # e2e caught its rewrite dragging social -> think.long_think. Only
    # short utterances are context-starved enough to pay the rewrite tax.
    if not has_history:
        return False
    u = utterance.strip()
    n = len(u.split())
    if n <= 4:
        return True
    if n > 9:
        return False
    return bool(FOLLOWUP_LEAD.match(u) or FOLLOWUP_HINT.search(u))


REWRITE_INSTRUCTION = (
    "Rewrite this as one self-contained sentence stating the full request, "
    "using the specifics from the conversation it refers to. If it maps to a "
    "tool command (a timer, a reminder, or a note), phrase it exactly as that "
    "command. Reply with the rewritten sentence only.")
# bare instruction prefill measurably echoes the instruction itself on vague
# fragments ("How about the color?" -> "Please rewrite the user's question…");
# forcing the answer's opening words makes the format structural
REWRITE_LEAD = "Rewritten sentence:"


def rewrite_context(history, utterance, timeout_s=1.5):
    """Self-contained rewrite of a fragment, for routing (and fillers).
    Same prompt family and slot as the main completion (warm prefix),
    tiny budget, hard timeout; any failure or timeout returns None and
    the caller routes on the raw utterance exactly as before."""
    p = build_prompt(history, utterance,
                     directive=REWRITE_INSTRUCTION) + REWRITE_LEAD
    try:
        r = requests.post(f"{LLAMA}/v1/completions", json={
            "prompt": p, "max_tokens": 48, "temperature": 0,
            "cache_prompt": True, "id_slot": SLOT, "stop": ["\n"]},
            timeout=timeout_s)
        lines = r.json()["choices"][0]["text"].strip().splitlines()
        line = lines[0].strip().strip('"') if lines else ""
        if line.startswith(REWRITE_LEAD):
            line = line[len(REWRITE_LEAD):].strip()
        return line[:240] or None
    except Exception:
        return None


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
        self._pending = set()  # background tasks (reminder fires) — kept
                               # referenced or the loop may GC them mid-sleep
        # camera state (vision path): client declares readiness via
        # camera_state, answers need_frame with camera_frame (b64 JPEG)
        self.camera = False
        self.frame_b64 = None
        self.detect_last = None  # last detect tool result (labels/dets/ms)
        self.face_meta = None    # face metrics for the current vision turn
        self.ambient = False     # per-session user toggle (ambient_state)
        self.ambient_meta = None  # ambient probe result for the current turn
        self.pending = None      # transcript-safety pending resolution
        self.norm_fired = False  # normalizer fired on this turn's final
        self._stop_swallow_until = 0.0  # late-final absorber (D11 partial)
        self.browser_task = None  # active BrowserTask (PROTOCOL-browser-use)
        self.ir_task = None       # active IRTask (PROTOCOL-ir.md)
        self._frame_evt = asyncio.Event()

    def active_task(self):
        """The one web task a session may run (either track); IR and
        browser tasks are mutually exclusive by construction."""
        return self.browser_task if self.browser_task else self.ir_task

    async def ambient_probe(self):
        """One frame at speech onset (decision 2): recognition (25-49 ms
        measured) hides behind the utterance; best-effort — any failure
        means no context line, never a broken turn. Decision 4: nothing
        to match against (empty store) means no probe at all — no frame
        is even requested."""
        try:
            if not faces.load_store():
                return
            frame = await self.request_frame()
            if frame is None:
                return
            meta = await asyncio.to_thread(faces.recognize, frame)
            if meta.get("n"):
                self.ambient_meta = meta
        except Exception:
            pass

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
                    # D11 barge-in on the PARTIAL: during an active task
                    # the loop owns the GPU and Phonon decode lags, so a
                    # final-based stop loses the race (measured: finals
                    # land after the task already completed). A bare stop
                    # phrase (>= 2 words, full pool match) cancels now;
                    # the late final is absorbed by the swallow window.
                    p = m["text"].strip()
                    at = self.active_task()
                    if at and at.active() \
                            and len(p.split()) >= 2 \
                            and browser_use.BARE_STOP_RE.match(p):
                        asyncio.create_task(self.partial_stop(p))
                elif t == "final":
                    # single repair point (protocol decision 2): routing,
                    # history, UI and the reply all see the normalized text
                    raw = m["text"]
                    text = normalize_transcript(raw)
                    self.norm_fired = text != raw
                    await self.send({"type": "final", "text": text})
                    # one dead turn must not kill the reader (measured: an
                    # exception here left the stream open and every later
                    # turn reply-less); the failure prints, the session
                    # recovers, the stream still closes
                    try:
                        await self.respond(text)
                    except Exception:
                        traceback.print_exc()
                        # respond() set speaking=True on entry and only
                        # finish_turn clears it — a dead turn would
                        # otherwise hold the half-duplex gate shut
                        # forever (session deaf)
                        self.speaking = False
                        await self.update_gate()
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

    async def respond(self, utterance, _resolved=False):
        # D11 partial-stop absorption: the late final of a clip whose
        # bare stop phrase already cancelled the task must not double-
        # speak ("Nothing is running right now") after the fact
        if time.monotonic() < self._stop_swallow_until \
                and browser_use.BARE_STOP_RE.match(utterance.strip()):
            self._stop_swallow_until = 0.0
            return
        self.speaking = True
        self.t_final = time.perf_counter()
        self.turn_id += 1
        my_turn = self.turn_id
        await self.update_gate()
        await self.send({"type": "cancel_tts"})
        # transcript-safety (protocol decisions 5-6): a pending resolution
        # intercepts the turn before routing. YES on an intent re-runs the
        # FULL pipeline on the proposed sentence; YES on a tool executes
        # the parked call; NO cancels; anything else drops the pending and
        # proceeds as a completely normal turn.
        judge_meta = None
        reply, route_mode = None, None
        pend, self.pending = self.pending, None
        if pend:
            res = classify_resolution(utterance)
            if res == "yes" and pend["kind"] == "intent":
                return await self.respond(pend["likely"], _resolved=True)
            if res == "yes" and pend["kind"] == "tool":
                try:
                    result = await self.run_tool(pend["tool"], pend["args"])
                    reply = await self._confirm_tool(utterance, pend["tool"],
                                                     pend["args"], result)
                except Exception as e:
                    await self.send({"type": "error",
                                     "message": f"tool: {e}"[:120]})
                    reply = "Sorry, the tool failed."
                route_mode = "resolve"
            elif res == "yes" and pend["kind"] == "browser":
                # parked submit-click read-back: YES executes the click
                # and the loop continues out-of-turn (protocol decision 10b)
                reply = await pend["task"].confirm_resume()
                self.browser_task = pend["task"] \
                    if pend["task"].outcome is None else None
                route_mode = "resolve"
            elif res == "no":
                if pend["kind"] == "browser":
                    pend["task"].abort()
                    self.browser_task = None
                reply = ("Okay — say it again." if pend["kind"] == "intent"
                         else "Okay, cancelled.")
                route_mode = "resolve"
            elif pend["kind"] == "browser":
                # dropped pending: the parked click dies quietly (M2
                # drop semantics; the turn proceeds normally)
                pend["task"].abort()
                self.browser_task = None
        # mid-task voice control (protocol decision 11; IR shares the
        # same stop contract — PROTOCOL-ir.md decision 12): while a web
        # task runs, the stop pool decides cancel vs hold; other finals
        # are NOT processed (slot-5 stays owned by the loop — registered
        # v1 UX cost). Canned replies: the M1 judge never sees these.
        at = self.active_task()
        if reply is None and at and at.active():
            if await self.browser_stop_check(utterance):
                await at.cancel()
                reply = "Stopped."
                route_mode = ("browser_ctrl"
                              if at is self.browser_task else "ir_ctrl")
            else:
                reply = ("I'm still working on the page — say stop to "
                         "cancel.")
                route_mode = ("browser_ctrl"
                              if at is self.browser_task else "ir_ctrl")
        # M1 (protocol decisions 2-5): semantic judge on eligible finals —
        # short, not already repaired by the normalizer, not itself a
        # resolution. Fail-open: only a NO verdict with a usable repair
        # diverts the turn.
        if reply is None and not _resolved \
                and judge_eligible(utterance, self.norm_fired):
            sensible, likely, jms = await self.judge_transcript(utterance)
            judge_meta = {"ms": round(jms), "sensible": sensible}
            if not sensible and likely:
                reply = f"Did you mean: {likely}?"
                self.pending = {"kind": "intent", "likely": likely}
                route_mode = "clarify"
        if reply is not None:
            await self.send({"type": "route", "mode": route_mode,
                             "conf": None, "rewrite": None,
                             "rewrite_ms": None})
            self.route_mode = route_mode
            await self.finish_turn(utterance, reply, None, judge_meta, None)
            return
        # route on a fresh raw-utterance embedding (~55 ms on slot 6;
        # refs are raw texts — prompt/history-shaped vectors measured
        # markedly worse). Non-direct turns speak a filler first so the
        # LLM work happens behind speech instead of dead air.
        cls, spec = "direct", {"action": "llm", "max_tokens": 96}
        rewrite, rewrite_ms = None, None
        face_reply = None
        fb = None
        skip_tts = False
        try:
            loop = asyncio.get_event_loop()
            emb, _ = await loop.run_in_executor(None, READOUT.embed,
                                                norm_router_text(utterance))
            # short or connective-led fragment with history behind it:
            # rewrite it into a self-contained sentence and route on that
            # (and pick fillers from it); any rewrite failure keeps the
            # raw-utterance embedding — exact previous behavior
            if needs_context(utterance, bool(self.history)):
                t_rw = time.perf_counter()
                rewrite = await loop.run_in_executor(
                    None, lambda u=utterance: rewrite_context(self.history, u))
                rewrite_ms = round((time.perf_counter() - t_rw) * 1000)
                if rewrite:
                    try:
                        emb, _ = await loop.run_in_executor(
                            None, READOUT.embed, norm_router_text(rewrite))
                    except Exception:
                        rewrite = None
            cls, spec, conf = WALKER.route(emb)
            note = None
            if spec.get("action") != "browser" and \
                    browser_use.LOCAL_PAGE_RE.search(utterance):
                # measured (production geometry, root centroids): 'open
                # shop.html and find the price of the Aero Presco' (ASR
                # garbles the brand) lands think 0.714 vs browser 0.701;
                # routing is punctuation-invariant (norm_router_text),
                # so no text repair can fix it. A local .html reference
                # is unambiguous browser intent — code wins over the
                # centroid, same intercept class as BARE_STOP_RE.
                spec = dict(
                    WALKER.cfg["nodes"]["root"]["classes"]["browser"])
                cls, spec["_class"] = "browser", "browser"
                note = "local-page intercept"
            elif spec.get("action") not in ("browser", "ir") and (
                    ir_use.SEARCH_INTENT_RE.search(utterance)
                    or ir_use.PAGE_QUESTION_RE.search(utterance)):
                # PROTOCOL-ir.md revision log: the fact-lookup and
                # page-question frames are not separable at the root
                # (measured: a pool member scored nearest browser), so
                # they ship as intercepts; the routed ir class covers
                # only research frames
                spec = dict(WALKER.cfg["nodes"]["root"]["classes"]["ir"])
                cls, spec["_class"] = "ir", "ir"
                note = "ir intercept"
            if spec.get("action") == "subtree":
                # the same embedding walks the child node (numpy only);
                # the child's action params win, its feedback key refines
                # the filler (tool work sounds different from thinking)
                sub_cls, sub_spec, sub_conf = WALKER.route(emb, spec["node"])
                await self.send({"type": "route", "mode": f"{cls}.{sub_cls}",
                                 "conf": sub_conf, "rewrite": rewrite,
                                 "rewrite_ms": rewrite_ms, "note": note})
                spec = {**spec, **sub_spec}
                cls = f"{cls}.{sub_cls}"
            else:
                await self.send({"type": "route", "mode": cls, "conf": conf,
                                 "rewrite": rewrite, "rewrite_ms": rewrite_ms,
                                 "note": note})
            if spec.get("action") == "vision":
                # act-then-decide: grab a frame first; with one, the turn
                # becomes a VLM query, without one the honesty directive
                # keeps the answer honest (never asks twice)
                # Face-management talk routes vision at the root (measured
                # content pull 0.795-0.867; refs cannot beat it without
                # breaking direct — protocol Rev 1): when the feature is
                # opted in, intercept it as the tool flow instead of a
                # VLM query. Falls through to the normal vision path on
                # any None.
                if FACE_ENABLED and FACE_INTENT_RE.search(utterance):
                    face_reply = await self.face_intent(utterance)
                if face_reply is None:
                    frame_b64 = await self.request_frame()
                    if frame_b64:
                        # the captured frame is part of the turn's record —
                        # the UI shows it in conversation history
                        await self.send({"type": "vision_frame", "b64": frame_b64})
                    else:
                        spec = dict(spec, directive=spec.get(
                            "fallback_directive", spec.get("directive")))
            if spec.get("filler"):
                fb = WALKER.feedback(emb, spec.get("feedback") or cls)
                await self.send({"type": "feedback", "text": fb})
                await self.tts(fb, guard_turn=my_turn)
        except Exception:
            cls, spec = "direct", {"action": "llm", "max_tokens": 96}
        self.route_mode = cls
        # ambient face context (decision 5): one line riding the volatile
        # suffix on non-vision actions; vision turns recognize fresh off
        # their own frame instead. History keeps the RAW utterance — the
        # line is per-turn context, never persisted into the conversation.
        hist_utterance = utterance
        if (self.ambient_meta and self.ambient_meta.get("names")
                and spec.get("action") != "vision"):
            utterance = camera_line(self.ambient_meta["names"]) + utterance
        t0 = time.perf_counter()
        first, chunks, timings = None, [], None
        reply = None
        if spec.get("action") == "vision" and face_reply is not None:
            reply = face_reply
        if reply is None and spec.get("action") == "vision" and frame_b64:
            reply = await self.vision_reply(utterance, frame_b64, spec)
        if reply is None and spec.get("action") == "tool":
            reply = await self.tool_flow(utterance)
            if reply is None:
                # nothing fit the manifest (or JSON failed twice): honest
                # brief answer instead of pretending
                spec = dict(spec, action="llm", max_tokens=96,
                            directive=TOOL_FALLBACK_DIRECTIVE)
        if reply is None and spec.get("action") == "browser":
            # intake turn: the filler was already spoken above, so the
            # turn ends with it (skip_tts) and the loop runs out-of-turn
            reply, skip_tts = await self.browser_flow(utterance, fb)
        if reply is None and spec.get("action") == "ir":
            reply, skip_tts = await self.ir_flow(utterance, fb)
        deep_meta = None
        if reply is None and spec.get("deep") and DEEP is not None:
            # explicit-depth turn: try the configured big model first;
            # any failure falls through to the local think path below
            reply, deep_meta = await self.deep_reply(utterance, spec)
        if reply is None:
            open_think = bool(spec.get("think"))
            trace_sent = ans_sent = 0
            try:
                with requests.post(f"{LLAMA}/v1/completions", json={
                        "prompt": build_prompt(self.history, utterance,
                                               spec.get("directive"),
                                               open_think=open_think),
                        "max_tokens": spec.get("max_tokens", 96),
                        "temperature": 0, "cache_prompt": True,
                        "id_slot": SLOT, "stream": True}, stream=True,
                        timeout=120) as r:
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
                        if not delta:
                            continue
                        chunks.append(delta)
                        if not open_think:
                            if first is None:
                                first = (time.perf_counter() - t0) * 1000
                                await self.send({"type": "response_start",
                                                 "ttft_ms": round(first, 1)})
                            await self.send({"type": "response_delta",
                                             "text": delta})
                            continue
                        # open think: everything before </think> is the
                        # trace (streamed to the UI, never spoken); deltas
                        # can straddle the tag, so re-split the full text
                        # and hold back a tag-length suffix of the trace
                        full = "".join(chunks)
                        if "</think>" in full:
                            trace, ans = full.split("</think>", 1)
                            show = len(trace)
                        else:
                            trace, ans = full, ""
                            show = max(0, len(trace) - 8)
                        if show > trace_sent:
                            await self.send({"type": "think_delta",
                                             "text": trace[trace_sent:show]})
                            trace_sent = show
                        if ans and first is None:
                            first = (time.perf_counter() - t0) * 1000
                            await self.send({"type": "response_start",
                                             "ttft_ms": round(first, 1)})
                        if len(ans) > ans_sent:
                            await self.send({"type": "response_delta",
                                             "text": ans[ans_sent:]})
                            ans_sent = len(ans)
            except Exception as e:
                await self.send({"type": "error", "message": f"llm: {e}"})
                self.speaking = False
                self.gate_until = time.monotonic() + GATE_TAIL
                await self.update_gate()
                self.schedule_gate_release(GATE_TAIL)
                return
            reply = "".join(chunks).strip()
            if open_think:
                if "</think>" in reply:
                    reply = reply.split("</think>", 1)[1].strip()
                else:
                    # budget ran out mid-reasoning: force the close and read
                    # the answer off the slot cache — same prompt prefix, so
                    # the second call only prefills the ~4 closing tokens
                    forced = None
                    try:
                        loop = asyncio.get_event_loop()
                        r = await loop.run_in_executor(None, lambda rr=reply:
                            requests.post(f"{LLAMA}/v1/completions", json={
                                "prompt": build_prompt(
                                    self.history, utterance,
                                    spec.get("directive"), open_think=True)
                                    + rr + "\n\n</think>\n\n",
                                "max_tokens": 64, "temperature": 0,
                                "cache_prompt": True, "id_slot": SLOT},
                                timeout=60))
                        r.raise_for_status()
                        forced = r.json()["choices"][0]["text"].strip()
                    except Exception:
                        forced = None
                    reply = forced or THINK_NOCLOSE_REPLY
                if first is None and reply:
                    first = (time.perf_counter() - t0) * 1000
                    await self.send({"type": "response_start",
                                     "ttft_ms": round(first, 1)})
                    await self.send({"type": "response_delta", "text": reply})
        # fp4-fault signature check + the shared epilogue live in
        # finish_turn (canned clarify/resolve turns end through it too)
        await self.finish_turn(hist_utterance, reply, timings, judge_meta,
                               round(first, 1) if first else None, deep_meta,
                               skip_tts=skip_tts)

    async def finish_turn(self, hist_utterance, reply, timings, judge_meta,
                          ttft_ms, deep_meta=None, skip_tts=False):
        """Shared turn epilogue: response_done, TTS, history, metrics, and
        the post-turn gate floor (the browser reports actual playback end
        via playback_state; the floor covers the race before the first
        playing:true arrives). skip_tts: the browser intake turn's reply
        is the filler, already spoken when the loop started."""
        # fp4-fault signature: output is essentially all '?' (see
        # docs/INCIDENT-2026-10-04-GPU.md) — surfaced to the UI, never fatal
        corrupt = len(reply) >= 8 and reply.count("?") >= 0.9 * len(reply)
        await self.send({"type": "response_done", "text": reply})
        if not skip_tts:
            await self.tts(reply)
        self.history.append((hist_utterance, reply))
        self.speaking = False
        await self.send_metrics(timings, ttft_ms, corrupt, deep=deep_meta,
                                judge=judge_meta)
        self.gate_until = max(self.gate_until, time.monotonic() + 1.0)
        await self.update_gate()
        self.schedule_gate_release(1.0)

    def _schedule_reminder(self, rid, text, delay_s):
        """Fire a reminder into this session after delay_s; delivered via
        tool_event, then marked fired so a restart never re-fires it."""
        async def fire():
            await asyncio.sleep(delay_s)
            await self.send({"type": "tool_event", "tool": "reminder_due",
                             "text": text})
            mark_fired(rid)
        t = asyncio.create_task(fire())
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    def arm_persisted_reminders(self):
        """Re-arm reminders that outlive a restart (memory.json holds the
        due time). Past-due ones are dropped silently — V1 does not fire
        stale reminders into a fresh session."""
        now = time.time()
        for item in load_memory():
            if item.get("type") != "reminder" or item.get("fired"):
                continue
            due = item.get("due")
            if not due:
                continue
            try:
                ts = time.mktime(time.strptime(due, "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            delay = ts - now
            if delay <= 0:
                continue
            self._schedule_reminder(item.get("id"), item["text"], delay)

    async def request_frame(self, timeout=3.0):
        """Ask the client for a camera frame; returns b64 JPEG or None
        (no camera, denied, or timeout — the honesty fallback covers all
        three, act-then-decide: we never ask the user twice)."""
        if not self.camera:
            return None
        self.frame_b64 = None
        self._frame_evt.clear()
        await self.send({"type": "need_frame"})
        try:
            await asyncio.wait_for(self._frame_evt.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self.frame_b64

    async def vision_reply(self, utterance, frame_b64, spec):
        """VLM turn through /v1/chat/completions (mtmd images only work
        on the chat endpoint, not raw completions). The Qwen template
        renders the same ChatML as our raw prompts, so the KV prefix of
        system+history stays warm. Thinking is disabled per-request.
        Face context (docs/PROTOCOL-face-recognition.md) rides the same
        volatile-suffix text: enabled + at least one enrollment, else
        nothing happens at all."""
        face_line = ""
        if FACE_ENABLED and faces.load_store():
            self.face_meta = await asyncio.to_thread(
                faces.recognize, frame_b64)
            if self.face_meta.get("names"):
                face_line = camera_line(self.face_meta["names"])
        msgs = [{"role": "system", "content": SYS}]
        for hu, ha in self.history:
            msgs += [{"role": "user", "content": hu},
                     {"role": "assistant", "content": ha}]
        msgs.append({"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
            {"type": "text", "text": f"{face_line}{utterance}\n({spec.get('directive')})"}]})

        def gen():
            with requests.post(f"{LLAMA}/v1/chat/completions", json={
                    "model": "wiresense-llm", "messages": msgs,
                    "max_tokens": spec.get("max_tokens", 96),
                    "temperature": 0, "cache_prompt": True,
                    "id_slot": SLOT, "stream": True,
                    "chat_template_kwargs": {"enable_thinking": False}},
                    stream=True, timeout=120) as r:
                r.raise_for_status()
                out = []
                for line in r.iter_lines():
                    if not line or not line.startswith(b"data: "):
                        continue
                    payload = line[len(b"data: "):]
                    if payload == b"[DONE]":
                        break
                    j = json.loads(payload)
                    out.append(j["choices"][0].get("delta", {}).get("content")
                               or "")
                return "".join(out)

        try:
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(None, gen)
            return reply.strip() or None
        except Exception as e:
            await self.send({"type": "error", "message": f"vision: {e}"[:120]})
            return None

    async def deep_reply(self, utterance, spec):
        """Big-model think turn through the configured provider
        (docs/PROTOCOL-deep-think.md). Streams the same events as the
        local think path — reasoning -> think_delta (UI only, never
        spoken), answer -> response_start/response_delta. Returns
        (reply, meta); on ANY failure reply is None and respond() falls
        back to the local think path, with the fallback recorded in the
        metrics event rather than spoken as an apology."""
        t0 = time.perf_counter()
        first = None
        meta = {"model": f"{DEEP.provider}:{DEEP.model}", "fallback": False}
        ans = []
        try:
            async for kind, delta in DEEP.stream(utterance,
                                                 spec.get("directive"),
                                                 self.history, SYS):
                if kind == "reasoning":
                    await self.send({"type": "think_delta", "text": delta})
                    continue
                if first is None:
                    first = (time.perf_counter() - t0) * 1000
                    meta["ttft_ms"] = round(first, 1)
                    await self.send({"type": "response_start",
                                     "ttft_ms": meta["ttft_ms"]})
                ans.append(delta)
                await self.send({"type": "response_delta", "text": delta})
            meta["ms"] = round((time.perf_counter() - t0) * 1000)
            if not "".join(ans).strip():
                raise RuntimeError("empty answer")
        except Exception as e:
            meta["fallback"] = True
            meta["error"] = type(e).__name__
            return None, meta
        return "".join(ans).strip(), meta

    async def tool_flow(self, utterance):
        """Manifest-JSON generation -> validate -> execute -> confirm.
        One retry on invalid JSON. Returns the confirmation reply, or
        None when nothing fit (caller falls back to an honest answer).
        The plan/confirm generations are internal: only the confirmation
        enters history and TTS."""
        plan = None
        for directive in (tool_manifest_directive(), TOOL_RETRY_DIRECTIVE):
            try:
                loop = asyncio.get_event_loop()
                r = await loop.run_in_executor(None, lambda d=directive:
                    requests.post(f"{LLAMA}/v1/completions", json={
                        "prompt": build_prompt(self.history, utterance, d),
                        "max_tokens": 80, "temperature": 0,
                        "cache_prompt": True, "id_slot": SLOT}, timeout=60))
                r.raise_for_status()
                plan = parse_tool_json(r.json()["choices"][0]["text"])
            except Exception:
                return None
            if plan is not None:
                break
        if plan is None or plan["tool"] is None:
            return None
        try:
            args = validate_tool(plan["tool"], plan["args"])
        except (ValueError, TypeError):
            return None
        # M2 (protocol decisions 8-9): high-risk numeric tools never
        # execute on the first utterance — park for a spoken read-back;
        # the YES resolution runs the tool from respond()
        if plan["tool"] in HIGH_RISK_TOOLS:
            self.pending = {"kind": "tool", "tool": plan["tool"],
                            "args": args}
            return readback_text(plan["tool"], args)
        result = await self.run_tool(plan["tool"], args)
        return await self._confirm_tool(utterance, plan["tool"], args, result)

    async def browser_flow(self, utterance, fb_text):
        """Browser intake (PROTOCOL-browser-use.md decision 3): one
        slot-5 JSON call parses the task, then the loop runs as a
        background task. Returns (reply, skip_tts) — the already-spoken
        filler ends the intake turn; parse failure gets an honest
        spoken reply and no task. The plan call is HISTORY-FREE: with
        history in the prompt the model bled earlier tasks into the
        parse (measured: a login task opened form.html; "Stop the
        search." invented "find the capital"). A bare stop/cancel
        phrase here means no task was active (active ones are
        intercepted earlier, decision 11) — honest canned line, never a
        spawned task."""
        if browser_use.BARE_STOP_RE.match(utterance.strip()):
            return ("Nothing is running right now — nothing to stop.",
                    False)
        # Phonon renders "news.html" as "news, HTML" / "form. HTML" and
        # the planner then refuses or emits junk sites (measured);
        # repair to a real filename, and snap near-miss fixture stems to
        # the committed page ("docks.html" -> "docs.html"). "the local
        # file" is also dropped: that phrase deterministically triggers a
        # planner refusal ("I cannot open local files", measured), while
        # the same text as "Open docs.html and ..." parses clean.
        plan_text = re.sub(r"\b([\w-]+)\.?,?\s+html\b,?\s*", r"\1.html ",
                           utterance, flags=re.I)
        plan_text = re.sub(r"\bthe local file\b\s*", "", plan_text,
                           flags=re.I)
        m = re.search(r"\b([\w-]+)\.html\b", plan_text)
        if m:
            fixed = browser_use._fixture_match(m.group(1) + ".html")
            plan_text = plan_text[:m.start()] + fixed + plan_text[m.end():]
        u = f"{plan_text}\n({browser_use.BROWSER_PLAN_DIRECTIVE})"
        prompt0 = (f"<|im_start|>system\n{SYS}<|im_end|>\n"
                   f"<|im_start|>user\n{u}<|im_end|>\n{EMPTY_THINK}")
        plan = None
        for prompt in (prompt0, prompt0 + browser_use.BROWSER_PLAN_RETRY):
            try:
                loop = asyncio.get_event_loop()
                r = await loop.run_in_executor(None, lambda p=prompt:
                    requests.post(f"{LLAMA}/v1/completions", json={
                        "prompt": p, "max_tokens": 90, "temperature": 0,
                        "cache_prompt": True, "id_slot": SLOT}, timeout=60))
                r.raise_for_status()
                plan = browser_use._extract_json(
                    r.json()["choices"][0]["text"])
            except Exception:
                plan = None
            if isinstance(plan, dict) and \
                    str(plan.get("task") or "").strip():
                break
            plan = None
        if plan is None:
            return ("I couldn't work out which page to open — name a "
                    "site and what to look for.", False)
        if browser_use.CREDS_TASK_RE.search(str(plan.get("task") or "")) \
                or browser_use.CREDS_TASK_RE.search(plan_text):
            # decision 10 at intake: login requires typing a secret, so
            # the task is refused before any page opens (the step-level
            # guard measured the model never attempts it — it would
            # otherwise stick typing the username until the stuck watch)
            return ("I can't enter passwords or sign in for you — that "
                    "has to stay in your hands.", False)
        task = browser_use.BrowserTask(self, plan, utterance)
        self.browser_task = task
        task._handle = asyncio.create_task(task.run())
        return (fb_text or "On it.", True)

    async def partial_stop(self, partial_text):
        """D11 barge-in on a bare stop partial (see phonon_reader): the
        cancel fires while the clip is still being spoken; the late
        final lands in the swallow window and says nothing (it was
        already obeyed). Either track's task counts as active."""
        at = self.active_task()
        if not (at and at.active()):
            return  # task finished meanwhile: the final path handles it
        await at.cancel()
        self._stop_swallow_until = time.monotonic() + 5.0
        await self.send({"type": "route",
                         "mode": "browser_ctrl"
                         if at is self.browser_task else "ir_ctrl",
                         "conf": None, "rewrite": None})
        await self.finish_turn(partial_text, "Stopped.", None, None, None)

    async def ir_flow(self, utterance, fb_text):
        """IR intake (PROTOCOL-ir.md decision 12): same shape as
        browser_flow — one history-free slot-5 JSON parse, credential
        refusal at intake, then the read-only loop runs as a background
        task. The .html ASR repairs are shared with browser_flow; the
        fixture snap itself lives in the goto path (fixtures_ir)."""
        if browser_use.BARE_STOP_RE.match(utterance.strip()):
            return ("Nothing is running right now — nothing to stop.",
                    False)
        if self.active_task():
            # one task at a time, shared with the browser track
            return ("One thing at a time — say stop to cancel what I'm "
                    "doing first.", False)
        plan_text = re.sub(r"\b([\w-]+)\.?,?\s+html\b,?\s*", r"\1.html ",
                           utterance, flags=re.I)
        plan_text = re.sub(r"\bthe local file\b\s*", "", plan_text,
                           flags=re.I)
        u = f"{plan_text}\n({ir_use.IR_PLAN_DIRECTIVE})"
        prompt0 = (f"<|im_start|>system\n{SYS}<|im_end|>\n"
                   f"<|im_start|>user\n{u}<|im_end|>\n{EMPTY_THINK}")
        plan = None
        for prompt in (prompt0, prompt0
                       + browser_use.BROWSER_PLAN_RETRY):
            try:
                loop = asyncio.get_event_loop()
                r = await loop.run_in_executor(None, lambda p=prompt:
                    requests.post(f"{LLAMA}/v1/completions", json={
                        "prompt": p, "max_tokens": 110,
                        "temperature": 0, "cache_prompt": True,
                        "id_slot": SLOT}, timeout=60))
                r.raise_for_status()
                plan = ir_use._extract_json(
                    r.json()["choices"][0]["text"])
            except Exception:
                plan = None
            if isinstance(plan, dict) and \
                    str(plan.get("question") or "").strip():
                break
            plan = None
        if plan is None:
            return ("I couldn't work out what to look up — tell me the "
                    "page or the topic and the question.", False)
        if browser_use.CREDS_TASK_RE.search(str(plan.get("question")
                                                or "")) \
                or browser_use.CREDS_TASK_RE.search(plan_text):
            # decision 10, shared with the browser track: a task that
            # requires a secret is refused before any page opens
            return ("I can't enter passwords or sign in for you — that "
                    "has to stay in your hands.", False)
        task = ir_use.IRTask(self, plan, utterance)
        self.ir_task = task
        task._handle = asyncio.create_task(task.run())
        return (fb_text or "On it.", True)

    async def ir_done(self, task):
        """Out-of-turn IR completion: the answer lands in the intake
        turn's reply box via response_done, is spoken, and enters
        history. ir_event mirrors browser_event for the UI."""
        text = task.answer or ""
        await self.send({"type": "ir_event", "id": task.id,
                         "event": task.outcome, "depth": task.depth,
                         "fetches": task.fetches,
                         "queries": task.queries, "answer": text})
        await self.send({"type": "response_done", "text": text})
        await self.tts(text)
        self.history.append((task.utterance, text))
        self.ir_task = None
        self.gate_until = max(self.gate_until, time.monotonic() + 1.0)
        await self.update_gate()
        self.schedule_gate_release(1.0)

    async def browser_stop_check(self, text):
        """Frozen stop pool 1-NN (decision 11); embeds its pools lazily
        on the first mid-task final. Gate G2 failure demotes this to the
        exact resolution vocabulary — recorded in the Results."""
        try:
            loop = asyncio.get_event_loop()
            stop, _ys, _ns, _ms = await loop.run_in_executor(
                None, lambda: browser_use.stop_scorer().score(text))
            return stop
        except Exception:
            return classify_resolution(text) == "no"

    async def browser_done(self, task, text=None):
        """Out-of-turn completion (reminder-fire precedent): the answer
        lands in the intake turn's reply box via response_done, is
        spoken, and enters history. Parked read-backs come through here
        too (text override, browser_task stays for the pending)."""
        text = text or task.answer or ""
        await self.send({"type": "browser_event", "id": task.id,
                         "event": task.outcome, "steps": task.steps,
                         "answer": text})
        await self.send({"type": "response_done", "text": text})
        await self.tts(text)
        self.history.append((task.utterance, text))
        if task.parked:
            # the read-back IS a pending resolution: YES executes the
            # parked click, NO aborts, anything else drops it (M2
            # machinery, generalized — decision 10b)
            self.pending = {"kind": "browser", "task": task}
        else:
            self.browser_task = None
        self.gate_until = max(self.gate_until, time.monotonic() + 1.0)
        await self.update_gate()
        self.schedule_gate_release(1.0)

    async def judge_transcript(self, text):
        """M1 judge (protocol decisions 3-4): YES/NO sensibility on the
        dedicated slot, then the likely sentence only on a NO. Fail-open:
        any exception, junk verdict, or unusable repair reports sensible."""
        t0 = time.perf_counter()

        async def chat(sysmsg, user, mx):
            loop = asyncio.get_event_loop()
            r = await loop.run_in_executor(None, lambda: requests.post(
                f"{LLAMA}/v1/chat/completions", json={
                    "model": "wiresense-llm", "messages": [
                        {"role": "system", "content": sysmsg},
                        {"role": "user", "content": user}],
                    "max_tokens": mx, "temperature": 0,
                    "cache_prompt": True, "id_slot": JUDGE_SLOT,
                    "chat_template_kwargs": {"enable_thinking": False}},
                    timeout=8))
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

        def spent():
            return (time.perf_counter() - t0) * 1000
        try:
            verdict = parse_verdict(await chat(
                JUDGE_SYS, JUDGE_USER.format(t=text), 4))
            if verdict is not False:
                return True, None, spent()
            likely = (await chat(REPAIR_SYS, REPAIR_USER.format(t=text),
                                 24) or "").strip().strip('"').strip()
            if not likely or len(likely) > 80:
                return True, None, spent()
            return False, likely, spent()
        except Exception:
            return True, None, spent()

    async def _confirm_tool(self, utterance, tool, args, result):
        """Record the tool_call event and generate (or fall back to) the
        confirmation sentence. Shared by the manifest flow and the
        vision-branch face gate."""
        # the call itself is part of the turn's record (args + result)
        await self.send({"type": "tool_call", "tool": tool,
                         "args": args, "result": result})
        confirm_d = (f"Tool result: {result}. Confirm to the user in one "
                     "short sentence.")
        try:
            loop = asyncio.get_event_loop()
            r = await loop.run_in_executor(None, lambda: requests.post(
                f"{LLAMA}/v1/completions", json={
                    "prompt": build_prompt(self.history, utterance, confirm_d),
                    "max_tokens": 48, "temperature": 0, "cache_prompt": True,
                    "id_slot": SLOT}, timeout=60))
            r.raise_for_status()
            reply = r.json()["choices"][0]["text"].strip()
        except Exception:
            reply = result  # the tool DID run; speak the plain result
        return reply or result

    async def face_intent(self, utterance):
        """Vision-routed face-management turn (opt-in, FACE_ENABLED only):
        enroll/forget via the same tool plumbing as the manifest flow,
        with the name parsed from the utterance ("learn my face as albert"
        -> albert; unnamed enroll -> 'me'; 'forget albert's face' ->
        albert; bare 'forget my face' -> everything). Returns the
        confirmation reply, or None to fall through to the VLM path."""
        if re.search(r"\b(remember|learn|save)\b", utterance, re.I):
            m = re.search(r"\bas\s+([A-Za-z][A-Za-z]{0,20})\b", utterance)
            tool, args = "face_enroll", {"name": m.group(1) if m else "me"}
        else:
            m = re.search(r"\bforget\s+([a-z]+)\b.{0,10}\bface\b",
                          utterance, re.I)
            stop = {"my", "the", "a", "his", "her", "their", "everything",
                    "all", "it"}
            name = (m.group(1).lower()
                    if m and m.group(1).lower() not in stop else None)
            tool, args = "face_forget", {"name": name or ""}
        try:
            args = validate_tool(tool, args)
        except (ValueError, TypeError):
            return None
        result = await self.run_tool(tool, args)
        return await self._confirm_tool(utterance, tool, args, result)

    async def run_tool(self, name, args):
        """Execute a validated tool; returns a short result note for the
        confirmation turn. Timer execution is delegated to the client
        (tool_event); reminder/note land in memory.json, relative-time
        reminders fire back through tool_event."""
        if name == "timer":
            await self.send({"type": "tool_event", "tool": "timer", **args})
            label = f", label '{args['label']}'" if args["label"] else ""
            return f"timer started for {args['seconds']} seconds{label}"
        if name == "reminder":
            secs = parse_relative_seconds(args["when"])
            rid = uuid.uuid4().hex[:8]
            item = {"type": "reminder", "id": rid, "when": args["when"],
                    "text": args["text"], "created": _now(),
                    "due": _now(secs) if secs else None}
            append_memory(item)
            if secs:
                self._schedule_reminder(rid, item["text"], secs)
            return f"reminder saved: {args['text']} ({args['when']})"
        if name == "note":
            append_memory({"type": "note", "text": args["text"],
                           "created": _now()})
            return f"note saved: {args['text']}"
        if name == "detect":
            return await self.run_detect(args["labels"])
        if name == "face_enroll":
            frames = []
            for _ in range(3):
                frame = await self.request_frame()
                if frame:
                    frames.append(frame)
                await asyncio.sleep(0.35)
            ok, detail = await asyncio.to_thread(
                faces.enroll, args["name"], frames)
            return detail
        if name == "face_forget":
            ok, detail = await asyncio.to_thread(
                faces.forget, args["name"] or None)
            return detail
        raise ValueError(name)  # validate_tool guards every caller

    async def run_detect(self, labels):
        """Grab a frame and ask the omdet sidecar (:8011). Returns a short
        spoken-ready summary; the full detection list lands in the tool_call
        event via DETECT_LAST (per-session, for the UI/debug path)."""
        frame = await self.request_frame()
        if frame is None:
            return "camera unavailable: I can't look right now"

        def run():
            im = Image.open(io.BytesIO(base64.b64decode(frame)))
            w, h = im.size
            r = requests.post(f"{OMDET}/detect", json={
                "frame": frame, "width": w, "height": h,
                "labels": labels}, timeout=20)
            r.raise_for_status()
            return r.json()

        try:
            loop = asyncio.get_event_loop()
            out = await loop.run_in_executor(None, run)
        except Exception as e:
            return f"detector unavailable: {type(e).__name__}"
        dets = out.get("detections", [])
        self.detect_last = {"labels": labels, "detections": dets,
                            "ms": out.get("ms")}
        if not dets:
            return (f"none of those visible (looked for: {', '.join(labels)})")
        parts = [f"{d['label']} {int(round(d['conf'] * 100))}%"
                 for d in dets[:5]]
        more = f" (+{len(dets) - 5} more)" if len(dets) > 5 else ""
        return "I see: " + ", ".join(parts) + more

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

    async def send_metrics(self, timings, ttft_ms, corrupt=False, deep=None,
                           judge=None):
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
        metrics = {
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
        }
        if deep is not None:
            metrics["deep"] = deep  # provider turn (or its honest fallback)
        if judge is not None:
            metrics["judge"] = judge  # M1 ran on this turn (protocol G5)
        if self.face_meta is not None:
            metrics["face"] = self.face_meta  # enabled vision turns only
        elif self.ambient_meta is not None:
            metrics["face"] = {**self.ambient_meta, "ambient": True}
        await self.send(metrics)
        # reset per-turn accumulators (t_final too: a pre-final TTS chunk
        # like a backchannel must not measure against the previous turn)
        self.t_wake = None
        self.t_final = None
        self.n_frames = 0
        self.first_partial_t = None
        self.partial_count = 0
        self.decide_stats = []
        self.tts_first_ms = None
        self.tts_total_ms = 0.0
        self.tts_n = 0
        self.route_mode = "direct"
        self.bc_count = 0
        self.face_meta = None
        self.ambient_meta = None
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
                    # ambient face probe (decision 2): fire at speech onset
                    # so detection hides behind the utterance
                    if ambient_active(session.ambient):
                        asyncio.create_task(session.ambient_probe())
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
                    session.arm_persisted_reminders()
                    # capability advertisement (decision 10): the UI shows
                    # the ambient toggle only when the config allows it
                    await session.send({"type": "capabilities",
                                        "face": FACE_ENABLED,
                                        "ambient": FACE_ENABLED and FACE_AMBIENT})
                    print(f"[{peer}] browser ctx {session.ctx_rate} Hz "
                          "(16000 = no resample path)", flush=True)
                elif m.get("type") == "camera_state":
                    session.camera = bool(m.get("on"))
                    print(f"[{peer}] camera {'on' if session.camera else 'off'}",
                          flush=True)
                elif m.get("type") == "ambient_state":
                    # per-session toggle, clamped to the config opt-ins
                    session.ambient = ambient_active(m.get("on"))
                    print(f"[{peer}] ambient context "
                          f"{'on' if session.ambient else 'off'}", flush=True)
                elif m.get("type") == "camera_frame":
                    b = m.get("b64")
                    if b and len(b) < 2_000_000:  # ~1.5 MB JPEG ceiling
                        session.frame_b64 = b
                        session._frame_evt.set()
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
    # websockets process_request hook: serve the page + mic worklet + tree,
    # else upgrade. The worklet is a same-origin file (blob: module URLs are
    # blocked in some webviews); tree.json feeds the live tree visualization
    # straight from tools/decision_tree.json so the two cannot drift.
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    if request.path == "/":
        html = open("index.html", "rb").read()
        hdrs = Headers([("Content-Type", "text/html; charset=utf-8"),
                        ("Content-Length", str(len(html)))])
        return Response(200, "OK", hdrs, html)
    if request.path == "/capture-worklet.js":
        js = open("capture-worklet.js", "rb").read()
        hdrs = Headers([("Content-Type", "text/javascript; charset=utf-8"),
                        ("Content-Length", str(len(js)))])
        return Response(200, "OK", hdrs, js)
    if request.path == "/tree.json":
        data = open(TREE_JSON, "rb").read()
        hdrs = Headers([("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(data)))])
        return Response(200, "OK", hdrs, data)
    return None


async def main():
    global KOKORO, READOUT, WALKER, DEEP, FACE_ENABLED, FACE_AMBIENT
    t0 = time.perf_counter()
    KOKORO = Kokoro(f"{MODELS}/kokoro-v1.0.onnx", f"{MODELS}/voices.npz")
    READOUT = DecideReadout()
    WALKER = TreeWalker(READOUT._project)
    DEEP = deep_think.from_config(deep_think.load_config())
    if DEEP is not None:
        print(f"deep think: {DEEP.provider} {DEEP.model} @ {DEEP.base_url}",
              flush=True)
    face_cfg = deep_think.load_config().get("face") or {}
    FACE_ENABLED = bool(face_cfg.get("enabled", False))
    FACE_AMBIENT = bool(face_cfg.get("ambient", False))
    if FACE_ENABLED:
        print(f"face recognition: ENABLED "
              f"(ambient {'on' if FACE_AMBIENT else 'off'})", flush=True)
    else:
        print("face recognition: disabled (opt-in, web/config.json 'face')",
              flush=True)
    print(f"kokoro + readout + decision tree loaded in "
          f"{time.perf_counter() - t0:.1f}s", flush=True)
    async with serve(handler, "0.0.0.0", PORT, process_request=http_page,
                     max_size=2 ** 20, ping_interval=20):
        print(f"wiresense web on 0.0.0.0:{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
