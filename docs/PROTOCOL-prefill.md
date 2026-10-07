# Protocol: shipped-stack prefill probe (pre-registered 2026-10-07)

Origin: the About page (docs/index.html, commit 8beb249) credits the third
pipeline stage with "the utterance prefills while you speak … the model has
already read most of it". That describes a mechanism the project already
tested and RETIRED: entry V (refleqwen, eval/reports/entry_v/, adjudicated
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
  (refleqwen/eval/reports/entry_v/scenarios/scenario_0{0..7}.json) — final
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
