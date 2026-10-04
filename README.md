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
| Vision  | Qwen3.5-4B mmproj-F16 (unsloth GGUF) | 672MB  | Apache-2.0 | same llama-server via `--mmproj`; frame per vision turn, ~1 GiB VRAM |
| Detect  | OmDet-Turbo-Tiny (ONNX, CPU)        | ~780MB | Apache-2.0 | open-vocab detector sidecar :8011 (`web/omdet_sidecar.py`); split enc/dec graphs + numpy top-900 selection |
| Deep    | external provider (config)          | —      | —          | optional big-model think route (`web/deep_think.py`, `web/config.json`); openai-chat / openai-responses / anthropic dialects; off by default |

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

## Web UI (2026-10-04)

Three panels: **left** — debug (mic health, router readouts, per-turn pipeline
metrics, camera preview, raw event log); **center** — stage (route badge,
ChatGPT-style live waveform — teal from the mic analyser, violet from the
assistant's playback through a WebAudio `MediaElementSource` graph — live
partial and reply); **right** — conversation history: user/filler bubbles,
the captured frame for vision turns, the streamed thinking trace, tool calls
with args and results, reminder fires, and a per-turn latency chip. The mic
worklet is served same-origin (`/capture-worklet.js`) — blob: module URLs
fail in some webviews.

## Agentic routing (2026-10-04)

The loop routes every turn through a **decision tree** (`tools/decision_tree.json`,
walked by `TreeWalker` in `web/server.py`): the root picks **direct / think /
decide / social / vision / deep**, and the think subtree splits
**long_think vs tool vs deep**.
Every node is the same arm-C head that powers the decide layer — head-projected
class centroids over the slot-6 embedding of the raw utterance; subtree walks
reuse the root's embedding (numpy only, no second embed). Both refs
(`models/router_embeddings.npz`, keyed `<node>.<class>`, recorded by
`make record-router` from the tree config) and live text pass through
`norm_router_text` — refs must match ASR text style (lowercase,
punctuation-free) or the class margins collapse. Mode directives ride the
current user turn (never the system block), so the KV prefix stays warm
across mode switches.

Held-out eval (`make eval-router`): root **56/62** (frozen-56 subset
**50/56** ≥ 49; the 6 v4 deep held-out **6/6**), think subtree **24/26**
(original **19/20** ≥ 18, deep **5/6** ≥ 5); end-to-end component accuracy
tree **47/56 vs flat 35/56**
(`make tree-probe`). Per the tree-probe experiment: no confidence gating —
cosine conf is miscalibrated as an error signal.

**Context rewrites for fragments**: the router embeds the raw utterance, so a
context-dependent fragment ("What about on Tuesday?") misroutes — raw
fragments scored 3/8 against mechanical golds. When history is non-empty and
the utterance is short (≤4 words) or connective/hint-marked (≤9 words; the
e2e caught a long "So…"-led narrative misfiring a bare lead match, so the
trigger is length-gated), `rewrite_context` first rewrites it into one
self-contained sentence — same slot, warm prefix, 48-token cap, 1.5 s hard
timeout, raw-utterance fallback on any failure — and routes on the rewrite
embedding; fillers pick from it too, and the rewrite shows in the router
card. Frozen eval `make eval-followup`: rewrite **6/8 vs raw 3/8**, trigger
recall 8/8 / precision 5/5, rewrites < 400 ms. Golds are mechanical — the
route of the frozen standalone equivalent — so tree-config gaps stay out of
scope; the two residual misses are such pool-coverage gaps (e.g. "Remind me
to call the dentist on friday morning" standalone routes social), not
rewrite failures.

- **direct** — answer immediately.
- **think.long_think** — context-selected filler ("One second, let me figure
  that out."), then **open-think generation**: the prompt ends at an unclosed
  `<think>` block, the reasoning streams to the UI as `think_delta` (never
  spoken), and only the post-`</think>` answer enters TTS and history. Budget
  640 tokens; if the block hasn't closed by then, the server force-closes it
  (`</think>` appended to the trace) and reads the answer off the warm slot
  cache — a ~4-token prefill.
- **think.tool** — tool filler ("Let me do that for you."), then the tool
  flow: the LLM picks from a V1 manifest (timer / reminder / note / detect) as
  manifest-JSON, args are validated and executed (timer → client `tool_event`
  beep + toast; reminder/note → `web/memory.json`, relative-time reminders
  fire back through `tool_event`), and a confirmation sentence is generated.
  Reminders persist: each gets a uuid and `fired` mark, and a fresh session
  re-arms unfired future ones from `memory.json` on hello (past-due dropped).
  A null plan or two invalid JSONs falls back to an honest brief answer
  (weather → "I don't have access to live weather data"), so tool-request
  misroutes degrade gracefully. E2E: `make test-voice` (11 cases, 11/11).
- **decide** — trade-off directive, commits to a recommendation.
- **social** — warm, brief tone; no filler.
- **vision** — camera request ("What do you think about my new glasses?").
  Act-then-decide: `respond()` asks the client for a frame (`need_frame` →
  canvas grab, ≤448 px JPEG, `camera_frame`), and with one attached the turn
  runs through `/v1/chat/completions` (mtmd images only work on the chat
  endpoint; `chat_template_kwargs: {enable_thinking: false}` pre-closes
  thinking; the Qwen template renders the same ChatML as our raw prompts so
  the KV prefix stays warm). Without a frame — camera off, denied, or
  timeout — an honesty directive keeps the answer honest; never asks twice.
  `--image-max-tokens 1024` bounds image KV (upstream #21750: large images
  crash >4B Qwen3.5 VL). mmproj-F16 (672 MB) + vision activations fit beside
  the 64K footprint (~760 MiB VRAM still free); text numerics verified
  bit-identical (eval-router 52/56 + 20/20 on unchanged refs).
- **detect tool** — fast open-vocab looking, separate from the VLM path.
  `OmDet-Turbo-Tiny` runs as a CPU sidecar (:8011, stdlib HTTP +
  onnxruntime, `web/omdet_sidecar.py`): JPEG frame in → detections (label,
  conf, original-pixel box) out, p50 467 ms / p95 653 ms, 2.08 FPS sustained
  (Ryzen AI 7 350, 8 ORT threads; targets pre-registered in
  `tools/test_omdet.py` — `make test-omdet`). The tool manifest gains
  `detect` (labels array, 1-12); on a hit the server grabs a frame through
  the same `need_frame` → `camera_frame` round trip and speaks a summary
  ("I see: person 89%, clock 79%"). RTX 5060 has no free VRAM and the
  Radeon 860M has no Linux inference stack, so CPU ONNX Runtime is the
  deployment; onnx-vulkan-rs (iGPU) stays a parked future option.
  Numerics: the stock forward's ONNX export diverges (box_pred ~5e-01) —
  bisected to the in-graph top-900 query selection, where ~1e-6 ORT-vs-torch
  score noise re-ranks the selection cutoff and the decoder amplifies the
  flip. The export (`omdet` workspace, `export_onnx_cpu.py`) therefore
  splits at the selection: encoder + decoder graphs (every stage ≤1.4e-04
  vs torch, verified with real image + real text), selection in numpy
  (deterministic, matches torch topk 900/900 on identical scores), and
  end-to-end detection parity vs the reference DetEngine — exact counts and
  every detection matched (IoU ≥ 0.9, |Δconf| ≤ 0.05) on all cases.
  Routing note: detect phrasings currently route **vision** (VLM), so the
  detect tool has no router entry yet — the routing pool top-up
  (`record-router` + frozen eval re-runs) is the registered next step, not
  done silently.
- **deep** — explicit-effort cues and open-ended meta requests ("Think this
  through carefully", "Walk me through why the experiment failed") route to
  the optional external provider instead of local open-think; cue+content
  compounds ("Think hard about this math problem…") stay local — content
  dominates the cue (measured; see **Deep think** below).

## Deep think (2026-10-05)

Optional big-model route for the deep class, fully config-gated in
`web/config.json` (gitignored; `web/config.example.json` documents the
contract). **Off by default**: with no config, deep turns fall through to
the local think path — byte-identical pre-deep behavior.

```json
{"deep_think": {"enabled": true, "provider": "openai-chat",
  "base_url": "http://host:8995/v1", "model": "…",
  "api_key_env": "WIRESENSE_DEEP_KEY", "timeout_s": 120, "max_tokens": 700}}
```

`provider` is one of **openai-chat** (`/chat/completions`; doubles as the
local-provider path — llama.cpp, vLLM, LM Studio, Ollama),
**openai-responses** (`/responses`, system via `instructions`), or
**anthropic** (`/v1/messages`, `x-api-key` + `anthropic-version` header).
All three SSE dialects normalize to a (reasoning, text) stream: reasoning
renders as `think_delta` (UI only, never spoken), text enters TTS and
history exactly like local think. The key is read only from the env var
named by `api_key_env` — never from disk. Any provider failure (connect,
auth, timeout, mid-stream) falls back to the local think path, and the
metrics event carries `deep: {model, ttft_ms, ms, fallback, error}` — a
provider turn is never indistinguishable from a local one.

Adjudicated bench (`tools/deep_bench.py`: frozen 10 numeric questions,
regex-checked, no LLM judge): the pre-registered bar "deep ≥ 7/10 AND
≥ local" **FAILED** — local 10/10 (median 13.8 s) vs deep 9/10 (median
5.0 s via the remote 27B); the miss was a genuine dice-probability error
(5/36 answered 1/6). The `≥ local` clause is a ceiling flaw in this regime
(nothing beats 10/10), documented as protocol Rev 2 — **no quality claim is
made**; the frozen set stays frozen. The one measured deep advantage is
wall-clock: 2.6× faster overall, ~4.1 s to first answer vs the local think
trace. For this question regime: keep it off.
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
