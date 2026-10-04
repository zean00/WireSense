# Incident 2026-10-04 — GPU FP4 datapath corrupts after reboot

> **Revision (later the same day): the diagnosis below is corrected.** The
> fp4 datapath is NOT dead — the fault is **VRAM-placement-dependent** and
> only the production instruct model's weights hit it. See
> [Revision: placement-dependent VRAM fault](#revision-2026-10-04-later-the-same-day)
> for the differential evidence and the shipped mitigation. The RMA
> recommendation stands; the mechanism in the ticket summary is corrected.

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

## Revision: 2026-10-04 later the same day

**Differential test — two other NVFP4 models, same GPU / boot / binary:**

| Model | Size | Result |
|---|---|---|
| qwen3.5-4b **base** (same arch, different weights) | 2.37 GiB | **clean** — 5/5 embeddings sane, 2/2 generations real text |
| gemma-4-e2b-it (different arch) | 3.13 GiB | **clean** — same |
| qwen35-4b **instruct** (production) | 2.9 GiB | NaN from first request |

If the fp4 datapath were dead, all three would fail. Only the production
model fails → the fault depends on the model file **and where its weights
land in VRAM**.

**Placement experiment (instruct model, single-GPU 8 GB, hog = a verified
0.3 GB torch reservation made before llama-server starts):**

- natural layout (no hog): NaN on every boot, every run — deterministic
- weights shifted ~0.3 GB up (hog first): **canary clean AND full corruption
  stress clean** — 3 passes × 227 sig embeddings + 20 generations = 0 NaN,
  0 garbage — reproduced on two fresh server instances
- production config (`-c 32768 -np 8`) with the same 0.3 GB guard: full
  stress clean, slots 0–7 probed clean, decide slot 6 and conversation
  slot 5 clean
- an apparent failure of a +0.9 GB offset under stress was **invalid**:
  leftover test processes changed the layout (server log showed 3688 MiB
  free vs 6250 in the clean run) — it tested a different, ungated placement

**Interpretation.** Marginal VRAM cells that fail only for specific data
patterns at specific addresses — consistent with the synthetic memtests
passing (zeros/ones/random fills, 4 GiB+) while real kernel workloads hit
the weak cells. The fault survives EC reset and reproduces across boots
whenever the weights land at the natural base address, which makes it a
hardware fault (RMA), but a *localized* one, not a dead datapath.

**The earlier `torch._scaled_mm` "bimodal tile" result is discounted as
corroboration**: with two NVFP4 models running clean through real kernels,
a synthetic all-ones scaled_mm showing alternating zeroed 16-column tiles
is more plausibly a harness/layout artifact of the hand-packed blocked
scale format than card evidence. Do not cite it to the service center.

**Mitigation shipped (in the repo):**

- `tools/vram_guard.py` — reserves 0.3 GB (self-verified) before
  llama-server starts, shifting the weights off the bad placement
- `Makefile start` — launches `vram-guard` first, waits for its ready
  file, then llama-server; `stop`/`status` know about it
- `make canary` + the web UI's "⚠ LLM corrupt" badge remain the detectors
- observed once: a single NaN on the first embedding after load that then
  never recurred (32/32 subsequent probes clean on the same instance) —
  marginal hardware is intermittent; treat any canary failure as real

**Residual risk.** The guard fixes placement relative to the guard block;
if the desktop's baseline VRAM usage changes substantially between boots,
the absolute addresses shift. If `?????` ever returns after a reboot:
rerun `make canary`, and if it fails, sweep the guard size (0.3 → 0.5 →
0.8 GB) — or accept the RMA path.

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

1. GPU computes garbage on NVFP4 (fp4 tensor-core) workloads in a
   **VRAM-placement-dependent** way: the production model's weights produce
   all-NaN embeddings and '?'-only generation when placed at the GPU's
   natural base address (reproducible across three boots and after a full
   EC reset), but run **flawlessly when the allocation base is shifted
   ~0.3 GB** (3 × 227 embedding vectors + 20 generations, zero faults,
   reproduced on fresh server instances). Two *other* NVFP4 models (a
   same-architecture Qwen3.5-4B base and a Gemma-4-E2B) run clean at the
   natural layout on the same boot — so the fp4 datapath itself executes
   correctly; specific weight blocks at specific addresses corrupt.
   Synthetic VRAM tests (4 GiB+ zero/one/random fill-verify) all pass,
   which is expected for pattern-sensitive marginal cells. All other GPU
   datapaths verified correct and at full speed (fp8/fp16/bf16/int8
   matmuls exact, Qwen3-8B Q4_K_M at 1854 tok/s prefill / 49 tok/s
   decode). No Xid errors, no driver/software changes since the last
   known-good boot (model file sha256-verified, two llama.cpp builds
   affected identically, CUDA JIT cache cleared and regenerated).
   Ask the service center to run an extended VRAM stress (e.g.
   memtest_vulkan / video memory stress test with varied patterns), not
   just a standard fill test.
2. Internal microphone input railed at DC (~1070 mV offset after the last
   power event, 272–624 mV before), destroying consonants in ASR; survives
   driver-level resets and reboots.

## State at close of incident

Services restored and verified end-to-end (WS replay of entry_v utterance:
ASR final "What's the weather looking like tomorrow morning?", TTS audio,
metrics flow; llm ttft 386 ms, prefill 261 tok/s, decode 69.5 tok/s):
llama-server :8998 (pinned entry-U/V flags, slots 0–7), Phonon :8010, web
:8996. The LLM text output is `????…` until the hardware issue is resolved.

**Superseded by the revision above**: as of the same day's final state,
the vram-guard mitigation is integrated into `make start` and the full
stack (including the production NVFP4 model on GPU) is verified clean —
full corruption stress 0/681 NaN embeddings, 0/20 garbage generations,
slots 0–7 and the decide layer verified. Text output is real again; the
hardware fault itself remains and is pending warranty service.
