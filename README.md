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
| Face    | YuNet + SFace (OpenCV Zoo, CPU)     | ~39MB  | Apache-2.0 | opt-in face recognition (`web/faces.py`); embeddings only, local `web/faces.json` (gitignored); off by default |
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
card. Frozen eval `make eval-followup`: rewrite **6/8 vs raw 4/8**
(2026-10-05 re-run; raw drifted +1 since adjudication — A/B-verified as
serving drift, recorded in the face protocol Rev 1), trigger recall
8/8 / precision 5/5, rewrites < 400 ms. Golds are mechanical — the
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
- **face tools** — with the feature opted in, "Learn my face as Albert."
  routes **think.tool** (manifest pick); "Forget my face." routes **vision**
  at the root (the "my face" content pull, 0.795-0.867, beats any pool ref
  — measured) and is intercepted inside the vision branch as the tool flow
  (protocol Rev 1); mid-conversation the fragment rewrite re-routes it
  through the manifest path instead. See **Face recognition** below.

## Face recognition (2026-10-05)

Phase-4 vision work, under the standing constraint: **local-only and
opt-in, or not at all** (`docs/PROTOCOL-face-recognition.md`, committed
before any wiring or gate data). YuNet detection + SFace embeddings
(OpenCV Zoo, Apache-2.0, CPU only — the GPU budget stays with the LLM),
~11 ms per frame offline.

**Opt-in contract**: absent or false `face.enabled` in `web/config.json`
means no face code runs at all — the tools vanish from the manifest,
vision turns carry no `face` metrics, and the boot log says disabled;
behavior is byte-identical to pre-face. Enabling adds `face_enroll` /
`face_forget` to the tool manifest ("Learn my face as Albert." /
"Forget my face."). Embeddings (128-d floats) and names live in
`web/faces.json` (gitignored); raw frames are never persisted; nothing
ever leaves the machine. "Forget my face." empties the store.

**Measured geometry** (frozen public-domain portraits,
`tools/face_eval/imgs/`): same-person cosine min **0.881**, different-person
max **0.255** across flip/JPEG/crop/scale/cross-resolution — registered
match threshold **0.50**, leaning toward stranger rejection. Recognition
rides only frames the user's own request captured (vision turns,
enrollment); context rides the volatile suffix as one line —
`(Camera: Albert)` — never the frozen prefix. Out of scope, stated
plainly: liveness/anti-spoofing (a photo of an enrolled person will be
recognized), background processing, emotion/attribute inference.

Gates (all passed, protocol Results): offline `make test-face` (now 472
checks including the ambient units); default-run test-voice 12/12 (face
denied = honest fallback); enabled run exercises gate → enroll →
recognize (`face.ms` 25-49 ms) → overwrite → forget → no trace; all
frozen router bars identical to the v4 adjudication (root 56/62, think
24/26, followup 6/8, self-consistency 135/135).

### Ambient face context (2026-10-05)

`docs/PROTOCOL-ambient-face.md`, committed before any ambient gate data.
The identity-query problem ("Who am I?" routes **direct**, so no frame is
ever grabbed) solved by removing the routing dependence entirely: one
frame is requested the moment speech is detected (Silero VAD wake), the
25-50 ms recognition hides behind the 2-5 s utterance, and the
`(Camera: ...)` line rides **every** non-vision turn as volatile-suffix
context. History stores the raw utterance — the line is never persisted.
The LLM never triggers the lookup; the server runs it unconditionally
when the store is non-empty (empty store = not even a frame requested).

**Triple opt-in**: `face.enabled` AND `face.ambient` in config (both
default false), plus a per-session UI toggle (👁 Ambient, shown only when
the server advertises it via the new `capabilities` event, state clamped
server-side). Measured (protocol Results): identity query answered on
the direct route with the toggle on and honest without it; paired
latency medians e2e 737.6 → 787.4 ms (+6.8%), ttft 59.3 → 64.3 ms
(+8.3%) — the probe is invisible; ambient `face.ms` 13.8-20.2 ms; all
frozen router bars unchanged. G4's first version failed honestly
(single-pair e2e is TTS-noise-dominated; Rev 1 records the diagnosis and
the powered 6-pair amendment before the rerun).

**ASR transcript normalizer** (`docs/PROTOCOL-asr-normalizer.md`): the
e2e run exposed that "Who am I?" deterministically transcribes "Two am
I?" — measured to sit on Phonon's who/two decision boundary (non-monotonic
under leading-silence perturbations; 1 of 5 natural identity questions
affected). Repaired at a single point — the Phonon final, before routing —
by one full-utterance rule (`two|to|too|2 am i` → "Who am I?"), inert
against "It's two a.m." / "Two am I late?", every firing logged, and a
registered deletion criterion: a rule that never fires on real traffic is
removed at the next revision. New rules require a logged real firing —
the one observed garble is testbench speech, so the rule ships
instrumented rather than extended. Hotword biasing was considered and
rejected: the serve path has no knob, and a static "who" prior would
endanger number-heavy utterances (timers).

## Transcript safety (2026-10-05)

Two mechanisms over one pending state machine
(`docs/PROTOCOL-transcript-safety.md`, always-on, no config):

**Clarify-on-uncertainty (M1).** Finals of ≤ 4 words that the normalizer
did not already repair get a semantic judge: a 4-shot YES/NO sensibility
call on a dedicated slot (slot 4, thinking off, 4-shot prompt frozen from
a measured calibration — 1-token logprobs scored 23/30, zero-shot 33/36,
few-shot 36/36 at ~115 ms). A NO verdict triggers a repair call, and the
turn's reply becomes "Did you mean: {likely}?" — routing, TTS and the
main LLM are skipped entirely, which makes clarify turns *faster* than
the turns they replace (762-1208 ms e2e vs 1197-2554 ms). The next
"Yes"/"no" resolves it; a "yes" re-runs the full pipeline on the proposed
sentence. Fail-open everywhere: any exception, junk verdict, or unusable
repair passes the turn through as before — the layer may only remove
harm, never add it. Adjudicated: 0 false clarifications across every
suite run, and one organic catch during latency probing (a truncated
"Time is it?" flagged and clarified in ~800 ms).

**High-risk tool read-back (M2).** `timer` and `reminder` (numeric args,
timed real-world effects — a misheard "15 seconds" used to execute
silently) never run on the first utterance. The turn replies with a
deterministic read-back ("Just to confirm — a reminder to call Mom, in
15 seconds?") and parks the call; "Yes." executes it (tool_call event +
confirmation sentence), "No." cancels with nothing executed, anything
else drops the pending and processes the turn normally. Accepted UX cost:
every timer/reminder is one extra spoken turn.

Known limits (registered): no judge over 4 words; fluent-but-wrong
non-numeric transcripts pass; and after an unresolved read-back, a later
tool-ish utterance can still be planned as a confirmation from history
(state machine correct; planner-context hygiene deferred).

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

## Voice-driven browser use (2026-10-05)

"Open shop.html and find the price of the AeroPress Go" runs a real
headless-Chromium loop — every judgment local, no external APIs
(docs/PROTOCOL-browser-use.md, pre-registered before any gate data).
Code owns the loop; the models propose: each step is a DOM snapshot
(≤ 40 visible interactive candidates + a text excerpt — never
screenshots) and one slot-5 JSON call that picks ONE action
(click / type / scroll / done). Finish needs the step model's `done`
AND the goal watcher agreeing; the decide-layer readout pool failed
its frozen eval at chance (9/20 — page excerpts are out-of-distribution
for utterance-calibrated pools), so the pre-registered fallback ships:
a slot-4 YES/NO judge, 18/20 on the same frozen pairs.

Safety is code, not prompting: credential fields are never typed and
login tasks are refused at intake; submit-class clicks park the loop
for a spoken read-back ("Just to confirm — click "Submit feedback"?") —
YES executes, NO cancels with nothing clicked. Say "stop it" mid-task
and the cancel fires on the ASR *partial* — measured at −580 ms, while
the user is still speaking — because during a task the loop owns the
GPU and final-based stops reliably landed after the task had already
finished. Every step lands in a run trace
(`/tmp/wiresense_browser/<id>/steps.json`), the replay-folder pattern.

Gates (frozen): routing 13/14, stop pool 20/20, six fixture tasks 6/6
across three full e2e runs (≤ 13 s each, filler ≤ 132 ms), safety 3/3,
regressions unchanged (14/14 voice, 31/31 safety, 22/22 normalizer).
Misses and fragilities are adjudicated with their measurements in the
protocol's Results section — including the razor-margin router
phrasings (timer lines at 0.001–0.002) and the deterministic
"AeroPress"→"Aero Presco" garble that a code intercept now rescues,
because routing is punctuation-invariant and no text repair can reach
it.

## Information retrieval track (2026-10-05)

The browser track acts; the IR track reads (docs/PROTOCOL-ir.md,
pre-registered). "Research espresso brewing ratios in the local
archive" runs a read-only loop — goto only, no click/type/fill, no
submit guards needed because nothing is ever submitted. Three depths
in one code-owned budget (20/45/90 s): a named page is fetched and
read (depth 1), a search fans out over the top-3 results into ONE
synthesis (depth 2), a research frame digests page after page —
following deeper links, reformulating when a search comes back empty
— until it can answer or honestly hedges (depth 3).

The split is measured, not aesthetic: fact-lookup frames are not
separable from browser frames in this embedding space (a pool member
scored nearest browser even at 1.0 self-match), so search/page-question
frames ship as two code intercepts above the centroid and the ir class
covers only research/dig frames — nine anchors, recording-clean, with
the root eval held at exactly its pre-IR 56/62 and the browser G1 at
12/14.

Every judgment is a slot-5 LLM call over page text — the readout-pool
lesson applied from day one. Gates run on committed replay fixtures in
the real DuckDuckGo-lite DOM shape (registry-matched, never live:
measured, the real engine serves a bot challenge from this box — the
live smoke ends in the honest spoken "The search engine isn't
returning results right now"). Spoken-input reality drove the last
mile: ASR writes "burr? HTML" and "Doe" for dough, so measured joins
and a measured homophone map live at the task layer; a wrong result
pick self-heals by walking the results queue instead of re-searching.
Final adjudicated gates: intake 13/14, fetch 4/4, search 2/2, research
2/2 (the ratio case lands the full multi-hop chain: wrong pick →
self-heal → deeper link → "1:2 — 18 grams in, 36 grams out"), safety
2/2 with zero structural violations, regressions unchanged.

## Deep agents, stage 1 (2026-10-05)

Deep think can now call the IR track as a tool
(docs/PROTOCOL-deep-agents.md, pre-registered). A deep turn that needs
facts — or names a source ("...research it in the local archive") —
ends its round with a text marker (`IR_TOOL {json}`), the parsed args
run as a detached IR leg (same budgets, replay registry, traces and
`ir_event`s), and the findings ride a continuation note into the same
answer. Code-owned: ≤ 3 legs, ≤ 180 s leg time per turn; endpoint-
agnostic by construction (text protocol + stream holdback, no native
function-calling — the Mirai 27B provider was unreachable the whole
cycle and every gate ran on the local fallback). The tracks' code-
owned credential refusal moved up to the deep turn itself (Rev 1,
gate-caught): a password-flavored deep ask is refused in code before
any generation, like browser and IR intake. Gates: unit 16/16, spoken
e2e green twice (leg fires on the archive case, zero legs on the math
control, every fetch fixture-snapped), full regression battery at its
registered bars (root 56/62 unchanged).

## Deep agents, stage 2 (2026-10-05)

Deep think can now also open pages — and act on them with a spoken
guard (docs/PROTOCOL-deep-agents.md, stage-2 pre-registration +
Revs A-F, every revision registered before any gate data). A deep round
may end with `BROWSE_TOOL {site, query, task}`; a named local file is
unambiguous browser intent even when the model picked the research
marker (measured misfire, code-rescoped). The leg runs as a DETACHED
BrowserTask — the think loop is the planner, step prompts gain the
deep utterance as `User request:` (measured task-clause lossiness) —
and when it reaches a submit-class click it PARKS with nothing
clicked: the deep loop stops generating mid-answer, the turn yields
with the partial reply, then the read-back rides the existing pending
machinery ("Just to confirm — click "Submit feedback"?"). YES
executes; NO cancels with nothing clicked. From the handoff on, the
task speaks exactly like a track task (measured: the detached flag
must clear, or the post-YES click is mute), and a confirmed submit's
spoken answer is the page's receipt, not the model's echo of its own
typed note (both failure modes reproduced offline before the fix).

Gates: unit 16/16 + 22/22 (dual-marker, args repairs, ASR repair,
verb clause), spoken e2e green via the registered variance clause in
both gate runs — case C (read-only: shop chain, cites $39.95) green
on every first attempt; case D's first attempts hit a
serving-numerics bimodality (unquoted JSON in the marker args, 2/5 —
the malformed-marker contract then passes the raw marker as text,
recorded as a known wart for stage 3), reruns fully green 2/2.
Battery all green (root 56/62, voice 14/14, safety 31/31, browser
PASS — its D11 hold-line flake re-manifested once, registered 2/3).
All measurements on the local 4B fallback; the Mirai 27B provider
was unreachable the whole cycle.

## Deep agents, stage 3 (2026-10-06)

The tool manifest now also exposes `ir_research` and `browse` — the
delegation contract ("a node earns a tool form when another agent
could delegate a bounded job to it"). The voice router never sends a
research or page frame to the tool class (16 phrasings probed, zero —
intercepts stay authoritative), so the manifest entries are
agent-facing plumbing gated in-process: a stub-socket Session runs
real `tool_flow` calls, the delegation spawns the track's OWN intake
task (budgets, replay registry, traces, out-of-turn completion and
the submit guard all inherited, not re-implemented), a `tool_call`
event records "delegated", and the intake turn ends with the tracks'
filler. Code-owned rules ride along: the credential refusal (same
line as all three surfaces) and — the battery-caught rule — a
delegate pick must STAND ON ITS OWN WORDS: one history-free re-plan,
accepted only for the same tool. The battery earned its keep on this
one: test_voice's weather case (routes tool, fits no tool, honest
fallback) started delegating via history bleed — with session
history the planner picked ir_research 1/1, history-free it returns
null every time; the old four-tool manifest had immunized the
fallback region by accident because a bled plan still had to fit
timer/reminder/note/detect. Rev G (registered before the fix's data)
restored the fallback; no prompt text changed, the four tools keep
their single history-carrying plan call byte-for-byte.

Gates: unit 17/17 + machinery 15/15 (both delegation legs, the bleed
fallback, and the four existing tools' replies byte-identical),
spoken G (research frames still route ir and cite the archive fact)
+ H (timer still parks on the high-risk read-back; YES arms). Gate
run 1 FAILed on serving variance (DA2-A answered the previous case's
math — the deep prompt is untouched by this stage) and exposed a
stage-1 runner bug: the DA2 variance clause could never fire (name
matcher, dead code, first exercise); fixed as a disclosed amendment,
full rerun final. DA5-D's malformed-marker bimodality hit its first
attempt in all three gate runs (5/5 lifetime in gate runs; reruns
3/3 green). Battery all bars (root 56/62, voice 14/14 post-Rev G,
safety 31/31, normalizer 22/22, ir PASS, browser PASS on its
deciding run — the D11 hold flake re-manifested, registered 2/3).
All measurements on the local 4B fallback; the Mirai 27B provider
was unreachable the whole cycle.

## MCP tool-selection experiment (2026-10-06)

Pre-implementation read for MCP support, fully pre-registered
(`docs/PROTOCOL-mcp-selection.md`, decisions MS-1..MS-14). Corpus: 121 real
tools dumped verbatim from 9 public MCP servers (github-mcp-server 62,
filesystem 14, everything 13, git 12, memory 9, puppeteer 7, time 2, fetch 1,
sequential-thinking 1). Eval: 278 hand-audited frozen cases. All arms on the
production 4B planner, history-free, temp 0; routing via slot-6 embeddings and
deterministic k-means over description / synthetic-utterance centroids.

- Flat 121-tool manifest: 68.4% overall, 3739 prompt tokens, 2205 ms.
- Hierarchical top-2 with k=10 clusters: 56.5% — routing recall is the whole
  loss (hit 71%, planner-given-hit 75-91%: shortlists de-crowd the planner).
- k-sweep found the optimum at k=5: 70.1% (paired tie with flat, p=0.597) at
  57% of flat's tokens and 65% of its latency; every k>=8 loses to flat.
- Scale/composition probe at N=78 (80% one server): flat COLLAPSES to 36.0%
  with 6/24 null discipline; the coarse gate holds 50.5% (p=0.0011).
- Synthetic-utterance pools beat raw-description pools in both topologies.

VERDICT: build MCP selection hierarchically but COARSELY — few domains
(4-8), top-2 union shortlist, `{"tool": null}` escape, LLM does the
near-duplicate disambiguation. Fine unsupervised hierarchies are the failure
mode, and dominant-server flat manifests are the risk the gate removes.

Follow-up (MS-15, same frozen eval): exposing tool search AS a tool was
measured and rejected on both variants. Search-as-primary: 49.3% — the
planner picks fine from returned schemas (90.4% when the target is among the
top-8) but its 2-4-word queries only surface the target 51% vs 87.8% for
embedding the utterance itself; the gate IS the search, keyed on the best
query available. Gate+search-escape: 65.8% < 70.1% plain gate — the trigger
fires on the wrong turns (45x on already-hit shortlists, 15/31 true misses)
and merely offering the option costs ~4 points on turns that never use it.

## Status

- [x] Architecture + component selection (this repo's docs)
- [ ] Sidecar validation: Kokoro ONNX synthesis, Phonon-2 WS streaming contract
- [ ] Entry V: streaming text loop prototype (ASR partials → incremental prefill → decide readout → generation)
- [ ] TTS + barge-in
- [ ] Vision context

Related: [refleqwen](../refleqwen) — the decide/thesis program and entry U
(the state/action heads this project adopts).
