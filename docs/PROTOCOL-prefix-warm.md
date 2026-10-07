# Protocol: prefix warming at scale (pre-registered 2026-10-07)

Follow-up to PROTOCOL-prefill.md. That probe settled case 1 (ASR-partial
warming: retired twice) and surfaced two open facts: the slot cache serves
NOTHING at shipped prompt sizes (cache_n=0 in 120/120, shared prefix
~60–70 tokens), yet reuse EXISTS on this build (identical 84- and 924-token
prompts reused when the previous request generated ≥4 tokens). The user's
case 2 — prefill conversation history + context at speech onset, append the
utterance at commit — is the registered candidate entry V named for
"long-history scale where prompt_n ≫ per-request overhead".

## Questions

Q1 (descriptive): where does exact-prefix reuse start firing on this build
   as a function of shared-prefix length, and how does the previous
   request's generation length (max_tokens 1 vs 8) affect it?
Q2 (decision): does VAD-open prefix warming cut commit→first-token time,
   and at what context sizes is the saving worth the machinery?

## Design (frozen before any data)

- Target: the RUNNING llama-server :8998, probe requests pin `id_slot: 1`
  (production owns 4/5/6). Web server and configs untouched.
- Runner: tools/prefill_probe/run_prefix_warm.py (committed with this
  protocol, data-free). Imports web/server.py's build_prompt in-process;
  same cwd=web discipline as the prefill probe.
- Context ladder: a frozen 16-exchange fixture (32 turns, tools/
  prefill_probe/history_fixture.py — committed, deterministic). History =
  first k PAIRS, k ∈ {0, 3, 8, 16, 32, 64} → estimated prompt sizes
  ~90 / ~230 / ~480 / ~900 / ~1700 / ~3200 tokens (actual prompt_n logged
  per row and used in adjudication). Mode: think-off (direct spec,
  max_tokens 96) throughout — the mechanics under test are mode-agnostic;
  disclosed that production think-on adds only the directive + header
  tokens.
- Utterances: the 8 frozen entry-V scenario finals; 3 reps each.
- Arms (per size; interleaved per (rep, scenario) so GPU warm-up drift
  cannot order-confound arms — the prefill probe's disclosed weakness):
  - COLD — disposer (small unrelated prompt, mt=8) → commit
    (cache_prompt=true). Models "context changed since the last turn"
    (rewritten memory digest, vision line, new session): LCP = system
    block only.
  - FREE — populator (same history, filler utterance "one more thing to
    remember, please", mt=8) → commit (cache_prompt=true). Models the
    shipped steady state "same large context, different utterance":
    LCP = system + history. This is what cache_prompt=true is FOR — the
    free version of case 2.
  - WARM — disposer → prefix-warm request (prompt = system + history, NO
    utterance, max_tokens 8 — above the 1-token quirk) → commit
    (cache_prompt=true). The user's mechanism: prefill while the user
    speaks, append at commit. warm_ms (the off-critical-path cost, paid
    during speech) recorded per commit.
- Per-commit record: wall TTFT (pre-POST → first non-empty content delta),
  timings (prompt_n, prompt_ms, cache_n, predicted_n), greedy text.
  Identity: output text compared across arms within (size, scenario, rep).

Part 1 (Q1) — threshold sweep, same runner, before part 2: prompt pairs
A_L / B_L sharing exactly L tokens (system + L filler tokens) then
diverging; request A_L (populator) then B_L (probe, mt=8), record
cache_n(B_L). L ∈ {24, 32, 40, 48, 56, 64, 72, 80, 96, 128}; populator
max_tokens ∈ {8, 1} (the quirk arm); 3 reps each. Purely descriptive.

## Pre-registered decision rules (Q2)

- R1 ADOPT: at the two largest ladder sizes, WARM median TTFT ≤ COLD
  median TTFT − 100 ms AND WARM ≤ FREE + 10 ms (warming achieves what the
  free path gives a stable context); AND at the shipped ~90-token size,
  WARM ≤ COLD + 10 ms (no regression where the prefix is small). On R1:
  implement VAD-open prefix warming in server.py behind a config flag,
  run the capability suite, commit.
- R2 REGISTERED CANDIDATE: WARM − COLD ≥ 25 ms only at ≥ ~1000-token
  contexts but R1's clauses otherwise fail → record as the standing plan
  for when prompts grow; no production change; page untouched.
- R3 DEAD: WARM − COLD < 25 ms at every size → case 2 recorded dead on
  this build; page untouched.
- Output identity across arms is reported, not gated: entry V's fp-reorder
  rewrites are expected to reappear wherever reuse actually fires. A
  WARM-vs-COLD divergence rate > 25% gets flagged prominently in the
  adjudication regardless of the latency verdict.

## Disclosures

Recorded text, no audio path; TTS excluded; ambient GPU load from the live
production server possible; think-off only (mechanics mode-agnostic,
disclosed); the fixture dialogue is synthetic-but-realistic and cycles
(2–3×) at the largest sizes — repeated text may tokenize marginally more
efficiently than novel text, which is why adjudication uses actual
prompt_n; probe slot 1 unused by production.
