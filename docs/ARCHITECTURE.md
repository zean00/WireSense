# Wiresense architecture (2026-10-03)

Design decisions from the planning session, recorded while fresh. Component
facts sourced from the model cards the same day.

## 1. Two-zone context — the core discipline

The LLM context is split by stability:

- **Frozen prefix** — system prompt + conversation history + (later) vision
  context. Serialized byte-identically every turn (chat template stability is
  load-bearing). Lives in the KV cache; never re-prefilled. This is where the
  real savings are: the prefix grows to thousands of tokens.
- **Volatile suffix** — the in-flight utterance. ASR partials arrive every
  ~100–500 ms as whole-hypothesis replacements; each one is re-tokenized,
  longest-common-prefix-trimmed against the cached suffix, and only the delta
  is decoded. The last few tokens are withheld (stable margin) because the
  tail keeps changing.

Consequence: on a 5060 (≈11k tok/s prefill), re-prefilling even a 200-token
utterance is ~20 ms — the suffix machinery's value is NOT raw prefill speed.
It is (a) the decide layer can read context state continuously during speech,
(b) prefix-cache survival across turns, (c) barge-in responsiveness.

Serving note: llama-server's slot prompt cache gives frozen-prefix behavior
for free over HTTP; explicit stable-margin control requires in-process
bindings (llama-cpp-rs / incremental-chat pattern). Prototype over HTTP first.

## 2. Streaming contracts (as validated against the component docs)

Phonon-2 `fermion serve` (protocol confirmed 2026-10-03 against server
v0.2.7 source and a live WS session — see `tools/validate_phonon_ws.py`):

- `GET /v1/audio/stream` — WebSocket (RFC 6455). Client sends one config JSON
  frame `{"sample_rate": 16000, "format": "pcm_f32le"|"pcm_s16le"}` (the key
  is `format`, NOT `encoding` — a wrong key is silently ignored and the
  server reads your bytes as f32), then raw mono PCM binary frames, ends
  with `{"type":"end"}` or a clean close.
- Server → client: `{"type":"partial","text"}` (whole-hypothesis replacement,
  NOT a delta), `{"type":"final","text","segment"}` (per segment, ~0.7 s
  silence), `{"type":"done","text"}` (full transcript).
- Observed live: first partial at **145 ms** (docs say ~350 ms), 13 partials
  during 6.5 s of streamed speech, final immediately at end-of-stream.
- Mid-stream revisions are real and visible: successive partials rewrote
  "conversation" → "context to the" → "contact state" — the LCP between
  partials can shrink mid-utterance, then recover. The suffix must tolerate
  full re-prefill of a short tail.
- Limits: one stream at a time (close 1013), 90 s idle timeout (1001) —
  long listening sessions need restart logic.
- Barge-in = close the socket, stop TTS playback, trim the turn, reopen.
- Fallback engine if Fermion's tooling breaks: base parakeet-tdt-0.6b-v3 via
  NeMo (same tokenizer/output conventions).

Kokoro-82M (ONNX):

- Validated 2026-10-03 (`tools/validate_kokoro.py`): **3.4x realtime on
  CPU** (16 cores), model load 0.5 s — run it as a warm sidecar process.
- Not natively streaming; feed sentence/clause chunks as the LLM's output
  stream grows. First audio ≈ 200–400 ms after the first clause. 24 kHz.
- ONNX export + per-voice .bin files live at
  `onnx-community/Kokoro-82M-v1.0-ONNX` (NOT in the hexgrad repo); pack
  voices into an npz for `kokoro-onnx` (`models/voices.npz` built from
  `af_heart.bin`). espeak-ng needed only for fallback G2P (misaki).

Mapping to the incremental-prefill loop: `partial` → TextChanged (re-tokenize
+ LCP trim + delta decode, margin withheld), `final` → Submit (flush margin,
generate). The decide layer may commit BEFORE `final` (semantic endpointing)
— that is the latency lever.

## 3. Decide layer (adopted from refleqwen entry U)

CLM-style heads on the frozen Qwen3.5-4B NVFP4 encoder (dim-2560
last-token embeddings; heads ≈ 20M params each):

- **Endpointing** — state-head readout over the streaming context:
  "utterance semantically complete?" Replaces silence timeouts; runs every
  partial without touching generation.
- **Routing** — quick-decide vs think-chain (the reflewe proxy chain,
  promoted to a router). Same readout, different action labels.
- **Eager response** — with confidence on the state, start answering before
  the utterance formally ends.

The action head additionally scores candidate actions by state·action dot
product — the capability raw-embedding classifiers don't give you. Entry U's
adjudication (claim AUROC, action-ranking top-1) is the feasibility gate;
retraining on voice-loop labels (endpoint outcomes, interruption events) is
the intended follow-on without touching the frozen encoder.

Readout cost: one embedding pass of the current context (~20–40 ms), batched
as a side call per partial — not per token.

## 4. Latency budget (RTX 5060 8GB, CPU sidecars)

| Stage                          | Budget      |
|--------------------------------|-------------|
| VAD gate (silero, 16k)         | ~ms         |
| Phonon-2 first partial         | ~350 ms     |
| Commit ( Phonon final 0.7 s silence, or decide-layer earlier) | 0–700 ms |
| LLM delta prefill + TTFT       | ~100–200 ms |
| Kokoro first clause            | ~200–400 ms |
| **Voice-to-voice**             | **~600–800 ms**, target < 500 ms via semantic endpointing |

### Live-loop gating (`web/`, validated 2026-10-04)

Half-duplex prototype loop — each mechanism below was added because its
absence produced a live failure (self-hearing, ghost turns):

- **Half-duplex mic gate** — browser reports `playback_state`; the server
  drops mic frames from first synth byte until actual playback end + 0.6 s.
  Gate on synthesis time alone leaks the reply's tail back into the ASR.
- **Silero VAD wake latch** — while idle, mic frames are muted until 3
  consecutive 100 ms frames have p(speech) ≥ 0.5 (600 ms pre-roll flushed on
  wake). Room tone must never reach a streaming ASR with a primed decoder:
  it hallucinates fluent "ghost" utterances and endpoints them. Silero
  rejects what an RMS gate accepts (440 Hz tone: p < 0.01 vs speech 0.94).
- **Per-turn ASR streams** — the Phonon WS opens on wake, closes at turn
  end; state reset kills cross-turn decoder priming (and the 90 s idle
  timeout). True barge-in is deferred until AEC is proven or an echo-aware
  VAD pass exists.

Model: `models/silero_vad.onnx`, sha256 `1a153a22f4509e292a94e67d6f9b85e8
deb25b4988682b7e174c65279d8788e3`, extracted from the official
`silero-vad` 6.2.3 wheel (`silero_vad/data/silero_vad.onnx`); feeding
contract is 512-sample chunks + 64-sample rolling context (see
`web/server.py` SileroVAD — a context-less feed returns ~0.001 for
everything, speech included).

### Mic health + pipeline metrics (`web/`, added 2026-10-04)

Motivated by a live failure that looked like an ASR quality regression:
"Tell me some random thing" → "Owning some burning thing". Diagnosis
(replay of saved mic bytes through Phonon reproduced live behavior;
Kokoro-synth baseline transcribed perfectly): the laptop's internal-mic
analog input carried a **DC offset of 272–624 mV at the ADC**, saturating
the converter — speech rode a railed front end, consonant-band energy
(3 kHz+) collapsed to ~1% of total power, and fricatives vanished before
the ASR saw them. Not fixable in software (survives gain changes,
`alsactl init`, card-profile cycle); codec-level reset (charger unplug /
reboot) or an external mic is the remedy.

The web UI now surfaces this class of fault directly:

- **Mic health panel (client-side):** per-100 ms-frame RMS, peak, DC
  offset, clip % (from the Int16 frames the page already has) + an
  AnalyserNode tap for the 3 kHz+ energy share; 2.5 s rolling window,
  500 ms refresh, verdict badge — ok / warn / **DEGRADED** with reasons
  (DC ≥ 0.10, clip ≥ 3%, rms < 0.008, HF share < 0.5% are the bad
  thresholds; the DC + HF checks encode the fault above).
- **Live server status:** `mic_status` pushes Silero p(speech) every
  500 ms pre-wake (VAD confidence is visible before any wake decision).
- **Per-turn pipeline metrics** (`metrics` message after `tts_done`):
  ASR wake→final + first-partial latency + partial count; decide readout
  last/max; llama.cpp streaming `timings` → TTFT, prefill tok/s, decode
  tok/s; TTS final→first-audio, synthesis total; e2e final→first audio;
  browser-measured playback start → voice→voice estimate. Rendered as a
  metrics card + per-turn chips in the turn log.

## 5. Vision (later)

Two options, in preference order:

1. Separate small VLM (Qwen3-VL-2B / Moondream3) captioning at ~1 fps or on
   motion; injects text context. Keeps the text-only NVFP4 GGUF untouched.
2. Native multimodal (Qwen3.5-VL GGUF + mmproj in the same decoder) — cleaner
   but NVFP4 mmproj support in llama.cpp is unverified.

## 6. Language: English-first (decided 2026-10-03)

Both Phonon-2 and Kokoro-82M are English-only. Extending later means:
streaming ASR for other languages → whisper-class models with a
rolling-window wrapper (worse WER/latency); TTS options for e.g. Indonesian
are license-encumbered (MMS-TTS is CC-BY-NC) or weak (Piper id_ID). Decide
the extension when v1 works; the sidecar boundary is where components swap.

## 7. Build order

1. **Entry V prototype (text-first):** mic → Phonon WS → partials →
   incremental prefill over llama-server → decide readout per partial →
   generate on commit → terminal text. No TTS, no vision.
2. TTS + barge-in loop closure.
3. Vision context injection.
4. In-process Rust port (llama-cpp-rs) only if HTTP latency/caching proves
   limiting.
