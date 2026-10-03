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
| LLM     | Qwen3.5-4B-Instruct NVFP4 GGUF     | ~2.7GB | Apache-2.0 | served by llama.cpp (`--embedding --pooling last` gives the decide layer its encoder) |
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

## Status

- [x] Architecture + component selection (this repo's docs)
- [ ] Sidecar validation: Kokoro ONNX synthesis, Phonon-2 WS streaming contract
- [ ] Entry V: streaming text loop prototype (ASR partials → incremental prefill → decide readout → generation)
- [ ] TTS + barge-in
- [ ] Vision context

Related: [refleqwen](../refleqwen) — the decide/thesis program and entry U
(the state/action heads this project adopts).
