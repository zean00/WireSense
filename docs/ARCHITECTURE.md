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
