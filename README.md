# Wiresense

A real-time multimodal assistant: **listen → decide → think → speak**, built
around a small local LLM with a learned decide layer.

## The loop

```
mic 16kHz → VAD → streaming ASR (partials) → incremental LLM prefill
        → decide layer (endpoint / route / respond) → generation (streamed)
        → sentence-chunk TTS → speaker        ↺ barge-in returns to listening
```

## Component choices (validated 2026-10-03)

| Role    | Component                          | Size   | License    | Notes |
|---------|------------------------------------|--------|------------|-------|
| LLM     | Qwen3.5-4B UD-Q4_K_XL GGUF         | ~2.7GB | Apache-2.0 | served by llama.cpp (`--embedding --pooling last` gives the decide layer its encoder); interim engine after the NVFP4 fault (docs/INCIDENT-2026-10-04-GPU.md) |
| Decide  | CLM-style state/action heads       | ~40MB  | project    | from refleqwen entry U; frozen-encoder contrastive heads |
| ASR     | FermionResearch/Phonon-2           | 164MB  | CC-BY-4.0  | streaming WS `partial` = whole-hypothesis replacement; 16kHz mono PCM |
| TTS     | hexgrad/Kokoro-82M (ONNX)          | 326MB  | Apache-2.0 | sentence-chunked streaming, 24kHz |
| Vision  | (later) small VLM captioner or Qwen3.5-VL mmproj | — | — | context injection, not a turn participant |

**English-first** by design; other languages are a later extension (both ASR
and TTS choices above are English-only — revisit then).

## Design principles

1. **GPU for the LLM only.** ASR and TTS run as CPU sidecars; the RTX 5060
   8GB budget belongs to the 4B model (and, later, vision).
2. **Two-zone context.** Frozen prefix (system + history + vision context,
   byte-stable across turns, KV-cached) vs volatile suffix (streaming
   utterance, LCP-trimmed and delta-decoded). Prefix discipline is
   load-bearing; suffix cleverness is a refinement.
3. **The decide layer is learned, not timers.** State-head readouts over the
   frozen encoder decide endpointing, think-vs-quick routing, and eager
   response — replacing silence timeouts.
4. **One execution discipline.** Experiments get pre-registered protocols
   (the refleqwen house style); components get validation harnesses under
   `tools/`.

## Agentic routing (2026-10-04)

The loop routes every turn into **direct / think / decide** (tool use is a
think sub-path) using the same arm-C head that powers the decide layer:
head-projected class centroids over the slot-6 embedding of the raw
utterance. Both refs (`tools/router_pools.json` + `models/router_embeddings.npz`,
recorded by `make record-router`) and live text pass through `norm_router_text`
— refs must match ASR text style (lowercase, punctuation-free) or the class
margins collapse. Measured: 35/36 held-out eval; live e2e 3/4 with the miss
on a social monologue (out of taxonomy, benign).

- **direct** — answer immediately (as before).
- **think / decide** — speak a context-selected filler first ("One second,
  let me figure that out."), then generate with a mode-conditioned system
  suffix and larger `max_tokens`.
- **Backchannels** — during long user speech (>6 s, max 2/turn), a listening
  cue ("Uh-huh.", "And then?") is selected 1-NN from the partial's embedding
  and spoken while the user is still talking; the browser mic runs echo
  cancellation, and a new final cancels playback.

## Status

- [x] Architecture + component selection (this repo's docs)
- [ ] Sidecar validation: Kokoro ONNX synthesis, Phonon-2 WS streaming contract
- [ ] Entry V: streaming text loop prototype (ASR partials → incremental prefill → decide readout → generation)
- [ ] TTS + barge-in
- [ ] Vision context

Related: [refleqwen](../refleqwen) — the decide/thesis program and entry U
(the state/action heads this project adopts).
