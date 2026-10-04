# Incident 2026-10-04 — GPU FP4 datapath corrupts after reboot

## Symptom

The day before (Oct 3), the pinned serving stack (qwen35-4b NVFP4 on
llama-server, entry-U/V flags) was bit-exact against stored sig embeddings.
After the first reboot (Oct 4 morning), the same binary, model, flags and
driver produce garbage:

- embeddings: all-NaN for most texts, corrupting within one server lifetime
  (first canaries 3/3 NaN on a fresh instance; later every input NaN, down to
  single letters)
- generation: `??????????` regardless of prompt
- CPU-only execution of the same binary + model file is clean (real text,
  cos≈0.99 vs stored vectors — normal cross-backend deviation)

## Eliminated (each with direct evidence)

| Suspect | Test | Result |
|---|---|---|
| Binary build | mainline `4de0926` and tq3 fork `8821` both | NaN on both |
| Driver version | previous boot journal already ran NVRM 595.91.07 | unchanged since Oct 1 |
| Model file | sha256 `91e635cd…cba06` vs entry-U `embed_manifest.json` | matches, intact |
| Flash attention | server with `-fa off` | still NaN |
| CUDA JIT cache | `~/.nv/ComputeCache` moved away, regenerated from scratch | still NaN |
| VRAM integrity | torch fill-verify: zeros/ones 4 GiB, random compare 2×2.8 GiB, addressing pattern | all pass |
| Xid / driver faults | `journalctl -k -b 0` | zero Xid this boot |
| GPU general compute | torch matmuls: fp32, fp16, bf16, int8, fp8-e4m3 `_scaled_mm` | all correct |
| GPU general speed | Qwen3-8B Q4_K_M on GPU: prefill 1854 tok/s, decode 49.3 tok/s, mem clock 12001 MHz | healthy |
| System changes | dpkg log Oct 3–4 | only gstreamer/libxpm updates |

## Implicated

The **FP4 (e2m1) block-scaled tensor-core datapath**:

- llama.cpp NVFP4 inference (ggml kernels) → NaN, both binaries
- `torch._scaled_mm` with `float4_e2m1fn_x2` + e4m3 scales (cuBLASLt): with
  all-ones A/B and all-1.0 scales, alternating 16-column output tiles return
  exact zeros (bimodal 0/256); a correct stack cannot produce that. FP8-e4m3
  scaled_mm on the same card is clean.
- Everything else on the card (memory system, PCIe, standard matmuls, other
  GGUF quants) verifies clean and fast.

## Corroborating context

The same power cycle also left the internal mic input railed (DC offset
worse than before the reboot: ~1070 mV vs 272–624 mV, see ARCHITECTURE.md
mic-health section). Two independent hardware subsystems regressing across
one reboot, with no software delta, points at board-level/power-delivery
degradation (ASUS TUF A14).

## Recommended user actions, in order

1. **Full power drain / EC reset**: shutdown, unplug charger (and any
   USB-C power), hold power button ~30 s, reboot, retest canary. Cheapest
   discriminator and the same remedy the mic fault needs.
2. If NaNs persist after that: **hardware service / RMA** — present the
   evidence table above.
3. Interim until fixed: GPU serving of the NVFP4 model is unusable
   (garbage output). CPU-only is clean but 3.2 tok/s. A Qwen3-8B Q4_K_M
   instance on this GPU is fast and correct (1854/49.3 tok/s), but swapping
   models invalidates the decide-layer embedding pin (heads are fitted on
   qwen35-4b embeddings) — that trade is a separate decision.

## Outcome: escalated to hardware service

The full power drain / EC reset was performed (shutdown, charger unplugged,
power button held 30 s; boot 2026-10-04 10:50). The fault persists with the
identical signature:

- canary on the fresh boot: embeddings NaN from the first request, verdict
  STILL FAULTY (`make canary`)
- the independent cuBLASLt fp4 test (`torch._scaled_mm`, float4_e2m1fn_x2,
  all-ones inputs, all-1.0 e4m3 scales) returns the same bimodal output as
  the first faulty boot: 4096 elements at 256.0, 4096 at 0.0 (alternating
  16-column tiles) where a healthy card returns uniform 256.0

The EC reset was the last remedy addressable from the user side. A fault
that survives it, reproduces identically across three boots, and leaves
every other GPU datapath (memory, PCIe, fp16/bf16/int8/fp8 matmuls, other
GGUF quants) fast and correct is failing silicon in the fp4 block-scaled
tensor-core datapath or its power domain. Together with the mic input
railed at DC (worse after each power event), the evidence points at
board-level degradation → warranty service / RMA.

## For the service ticket (summary)

Laptop: ASUS TUF A14, RTX 5060 Laptop GPU (8 GB). Two hardware symptoms,
both absent before 2026-10-04 and present after that morning's power-on:

1. GPU computes garbage on NVFP4 (fp4 tensor-core) workloads only: all-NaN
   embeddings and '?'-only generation from the first request after boot,
   reproducible across three boots and after a full EC reset. Independent
   reproducer with cuBLASLt via PyTorch returns corrupted output (zeroed
   output tiles) for an all-ones fp4 matmul. All other GPU datapaths
   verified correct and at full speed (VRAM fill-verify clean, fp8/fp16/
   bf16/int8 matmuls exact, Qwen3-8B Q4_K_M at 1854 tok/s prefill /
   49 tok/s decode). No Xid errors, no driver/software changes since the
   last known-good boot (model file sha256-verified, two llama.cpp builds
   affected identically, CUDA JIT cache cleared and regenerated).
2. Internal microphone input railed at DC (~1070 mV offset after the last
   power event, 272–624 mV before), destroying consonants in ASR; survives
   driver-level resets and reboots.

## State at close of incident

Services restored and verified end-to-end (WS replay of entry_v utterance:
ASR final "What's the weather looking like tomorrow morning?", TTS audio,
metrics flow; llm ttft 386 ms, prefill 261 tok/s, decode 69.5 tok/s):
llama-server :8998 (pinned entry-U/V flags, slots 0–7), Phonon :8010, web
:8996. The LLM text output is `????…` until the hardware issue is resolved.
