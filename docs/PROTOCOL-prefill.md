# Protocol: shipped-stack prefill probe (pre-registered 2026-10-07)

Origin: the About page (docs/index.html, commit 8beb249) credits the third
pipeline stage with "the utterance prefills while you speak … the model has
already read most of it". That describes a mechanism the project already
tested and RETIRED: entry V (rewire, eval/reports/entry_v/, adjudicated
2026-10-03) ran partial-warming vs commit-only on llama-server under a
pre-registered identity gate — warming failed the gate (4/16 identical
outputs) and the timing band (warm/cold prompt_ms ratio 1.0–1.08; 0–4
tokens transferred; ~50 ms per-request overhead dwarfs a 15–27-token
prefill). The shipped server.py header records the verdict: "commit-only,
no warming". The page card (and the ASR card's "the LLM prefill … track[s]
the latest hypothesis") are therefore wrong about the shipped system and
will be corrected regardless of this probe's outcome (decision rule 1).

This protocol pre-registers a fresh measurement on the SHIPPED stack, to
put honest numbers on the corrected card — entry V measured the text-first
prototype (loop.py), not server.py's production commit path.

## Questions

Q1. On the shipped commit path, how long from end-of-prompt (the commit
    instant) to the first generated token — and how much of the prompt is
    served from the slot cache (frozen prefix) vs newly prefilled
    (volatile suffix)?
Q2. Does partial-warming (the page-claimed mechanism, entry-V replay
    policy) beat commit-only on the shipped stack? (Entry V: no; this is
    the re-test on production prompts, which differ from the prototype's.)

## Design (frozen before any data)

- Target: the RUNNING llama-server :8998 (root-owned, untouched), probe
  requests pin `id_slot: 1` (production owns 4=judge, 5=gen, 6=embeddings;
  0–3 are free per server.py:55). The web server on :8996 is not involved;
  no config file is read or written.
- Runner: tools/prefill_probe/run_probe.py (committed with this protocol,
  data-free). Imports web/server.py's `build_prompt`/`system_block`/
  `DecideReadout` in-process (same pattern as tools/capability_suite).
- Utterances: the 8 frozen entry-V scenarios
  (rewire/eval/reports/entry_v/scenarios/scenario_0{0..7}.json) — final
  texts 46–89 chars, 4–10 partials each (n=58 partials). Byte-frozen
  artifacts; reused, not regenerated.
- History: ONE fixed exchange, constant across every arm and rep
  (disclosed: production's rolling last-2 history rotates the prefix
  across turns; holding it constant isolates the utterance effect).
- Modes (the two shipped answer paths, tools/decision_tree.json specs):
  - think-on  = long_think: directive "Reason step by step, then give a
    clear, concise answer.", open_think=True, max_tokens 640.
  - think-off = direct: no directive, EMPTY_THINK tail, max_tokens 96.
- Arms:
  1. COMMIT — `cache_prompt: true`, the shipped stream_local call.
     Both modes, 8 scenarios x 5 reps.
  2. NOCACHE — `cache_prompt: false`, full-prefill reference. Both modes,
     8 x 5.
  3. WARM — the page-claimed mechanism: before each commit, replay ALL
     frozen partials of the scenario as `max_tokens: 1` completions of the
     same prompt builder (partial as user turn, same mode tail), then
     commit. Think-on only, 8 x 5 (entry-V "replay every partial" policy;
     ASR revisions that break token-prefix continuity are the phenomenon
     under test, not a bug to sanitize).
- Arm isolation: arms run sequentially; a disposer commit (different
  utterance, unanalyzed) precedes each arm. Within an arm, scenarios cycle
  0..7 per rep, so consecutive measured commits always carry a DIFFERENT
  user turn — the cache state under test is production's steady state
  (frozen prefix served, volatile suffix prefilled).
- Per-commit record: wall TTFT (perf_counter just before the POST → first
  non-empty content delta, the same measurement point as server.py's
  response_start), full llama-server `timings` dict (prompt_n, prompt_ms,
  predicted_n, cache_n when reported), greedy output text (temperature 0).
- Descriptive D: readout cost per partial on the shipped heads —
  DecideReadout over build_prompt(history, partial) exactly as Session.
  decide() builds it, embeddings pinned to slot 1; n=58.

## Pre-registered decision rules

1. The page correction proceeds regardless of outcome: warming is already
   retired by entry V; this probe only supplies the shipped-stack figures
   that replace the wrong copy.
2. Warming re-open condition (Q2): re-open entry V's retirement ONLY if
   WARM median TTFT beats COMMIT median TTFT by ≥ 50 ms AND WARM median
   prompt_ms ≤ 0.8 x COMMIT median prompt_ms (0.8 = entry V's registered
   "not effective" band edge). Otherwise the retirement is CONFIRMED on
   the shipped stack.
3. Figures that may appear on the About page (all from the COMMIT arm
   unless stated): think-on median TTFT; frozen-prefix share = per-commit
   cache_n/prompt_n median; frozen-prefix saving = NOCACHE−COMMIT median
   TTFT delta; readout median from D. Every number must be traceable to
   tools/prefill_probe/results.json.
4. Greedy output identity across arms is DESCRIPTIVE (entry V attributes
   mid-output near-tie rewrites to fp reduction-order changes under cache
   reuse). A COMMIT-vs-NOCACHE divergence — identical cache semantics on
   the same slot — is an anomaly and gets investigated before any number
   is reported.

## Disclosures

Recorded text replay of frozen ASR scenarios, not live mic; routing,
gen_plan and TTS are outside the measured path (TTS first-chunk cost is
additive and unmeasured here, per entry V). Ambient GPU load from the live
production server is possible and uncontrolled. Probe slot 1 is never used
by production, so measured commits queue behind nothing.

## RESULTS (2026-10-07, tools/prefill_probe/results.json, 120 commits + 58 readouts)

| arm/mode | TTFT ms median (p10–p90) | prompt_n med | prompt_ms med | cache_n |
|---|---|---|---|---|
| COMMIT/think_off | 74.0 (71.0–78.5) | 83 | 69.5 | 0 |
| COMMIT/think_on | 78.5 (77.5–79.5) | 96 | 74.4 | 0 |
| NOCACHE/think_off | 74.7 (72.9–78.1) | 83 | 69.0 | — |
| NOCACHE/think_on | 78.7 (78.1–80.5) | 96 | 74.5 | — |
| WARM/think_on | 67.8 (66.4–70.2) | 96 | 62.1 | 0 |

Q1: commit→first token on the shipped path is ~74–79 ms, and it is ~95%
prompt prefill (TTFT ≈ prompt_ms + ~4 ms): the full ~83–96-token prompt is
evaluated every turn. cache_n = 0 on all 120 cache_prompt=true commits —
the two-zone frozen prefix was served from the slot cache ZERO times.

Q2 (re-open rule): WARM beats COMMIT by 10.6 ms TTFT, prompt_ms ratio 0.86.
Rule requires ≥ 50 ms AND ≤ 0.8 — NOT met. RETIREMENT CONFIRMED on the
shipped stack. Stronger: the delta is confounded and mechanistically empty —
WARM ran last (order confound; controlled repeats below show a ~15 ms pure
warm-up drift with cache_n=0), and WARM's own commits show cache_n=0, so the
partial replays bought ZERO KV reuse: the warming mechanism the page
credited delivered literally nothing on this stack.

Identity: 8/8 exact across COMMIT/NOCACHE/WARM. Consistent with entry V's
mechanism — greedy rewrites appear only when cache composition changes fp
reduction order; here no reuse ever fired, so numerics never differed. The
registered anomaly clause (COMMIT-vs-NOCACHE divergence) did not trigger.

Readout per partial (descriptive D, n=58): embeddings 53.7 ms + head 2.1 ms
= 55.9 ms median, p90 63.4 — the shipped decide layer computes the route on
every partial, so the routing decision is ready before speech ends.

## OBSERVATIONS (outside the registered arms; labeled per protocol; did not
## alter the adjudication above)

Controlled same-slot tests on the live server (pid 336728, flags -c 65536
-np 8 -ctk/ctv q8_0, no --cache-reuse):

1. Slot-cache reuse EXISTS on this build but did not fire for any
   production-shaped request. Identical repeats of an 84- or 924-token
   prompt reused (cache_n 80 / 920, prompt_ms 363→26 ms) — but only when
   the PREVIOUS request had generated ≥4 tokens. Identical repeats after a
   max_tokens=1 request never reused (30–84 tokens bracketed, 17 sizes).
   The probe's shipped pattern — consecutive prompts sharing the system +
   fixed-history prefix (~60–70 tokens), different user turn, realistic
   generation — never reused (cache_n=0 in 120/120).
2. Consequence for the shipped design: server.py's "system+history stays
   warm" / "frozen prefix" comments and entry V's "26 of 44–53 tokens
   served" do NOT hold on the current instance at shipped prompt sizes.
   Every turn fully prefills; the two-zone discipline's value here is
   keeping the prompt SHORT (~83–96 tokens → ~74–79 ms), not cache reuse.
   Why a 1-token generation disables reuse on this build was not
   reverse-engineered — recorded as an open server-behavior question.
3. Warm-up drift: first-after-idle requests cost ~15 ms more regardless of
   cache state (53→41 ms across identical no-reuse repeats), an order
   confound for any sequential-arm timing probe. Entry V primed each arm
   per scenario; this probe used one disposer per arm — insufficient. The
   re-open rule's 50 ms bar absorbs it, but future timing protocols should
   interleave arms or per-arm-primed slots.

## DISCLOSURES

Text replay, not live mic; routing/gen_plan/TTS outside the measured path;
probe ran while the production web server was live (ambient GPU load,
uncontrolled); arms sequential (order confound disclosed in OBSERVATIONS 3,
absorbed by the 50 ms re-open bar); threshold bracket and reuse tests are
post-hoc observations on the live server, labeled, non-adjudicating. The
first probe launch crashed pre-data (cwd-relative MODELS path) — fixed and
committed before any completion fired.
