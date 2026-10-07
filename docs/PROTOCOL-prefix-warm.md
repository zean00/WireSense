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

# RESULTS (adjudicated 2026-10-07)

Data: tools/prefill_probe/results_prefix_warm.json (re-run, git_head
8d8193a, started 20:25:30 finished 20:32:26, 3 reps × 8 finals × 6 rungs
× 3 arms). Stats: tools/prefill_probe/analyze_prefix_warm.py (post-data,
read-only).

## Execution

1. Registration + runner + fixture committed data-free (1ca8747).
2. First launch: part 1 valid; part 2 every streaming commit recorded
   empty — INCIDENT 1 (complete() omitted `stream` from the request body;
   requests' client-side stream kwarg did not stream). Invalid run
   archived as results_prefix_warm.invalid-streambug.json; fix committed
   (8d8193a) before any re-run.
3. Full re-run on the same server/build (no restart between arms or runs).

## INCIDENT 2 — fixture saturation (disclosed post-data)

The registered ladder intended ~90→~3200 tokens via k∈{0,3,8,16,32,64}
pairs, but the frozen fixture holds 16 pairs: PAIRS[:32] == PAIRS[:64] ==
all 16, so rungs 32/64 duplicate rung 16 (median prompt_n 734.0 at all
three; medians differ ≤0.5 ms — independent replicates of one size, not
new sizes). Largest MEASURED context: 734 prompt tokens, not ~3200.
Effect on the verdict: none — the decision variable (cache_n>0, i.e. the
rewind firing) is a mechanism, not a magnitude, and it was measured
directly at 10 shared-prefix lengths (50–154 tokens) and 6 ladder rungs
(49–734 tokens). The registered R2 branch (a saving only at ≥~1000-token
contexts) is unmeasurable on this fixture; with cache_n=0 at every
measured shape there is no mechanism by which a saving would first appear
above 734 tokens. The ~3200-token figure below is extrapolation, labeled.

## Part 1 (Q1): mid-sequence rewind never fires

cache_n = 0 in 60/60 threshold probes (LCP 50–154 tokens exact via
/tokenize; populator mt=8 and mt=1, 3 reps each). prompt_ms rises
52.5 → 105.0 ms across the sweep — pure prefill cost, zero served. This
replicates the invalid-run's valid part 1 and the prefill probe's
120/120: on this build the slot cache serves exact extension and
whole-prompt truncation (previous request ≥4 generated tokens), but never
a shared prefix with divergent continuation — the exact shape every
case-2 request has (warm/populator prompt = system+history, commit =
system+history+utterance).

## Part 2 (Q2): warming never wins at any size

Median TTFT ms (n=24 per cell):

| k | prompt_n | COLD | FREE | WARM | WARM−COLD | WARM−FREE | warm_ms |
|---|---|---|---|---|---|---|---|
| 0 | 49 | 55.8 | 55.2 | 55.5 | −0.3 | +0.3 | 161.5 |
| 3 | 168 | 109.1 | 109.3 | 110.1 | +1.0 | +0.8 | 212.3 |
| 8 | 388 | 185.8 | 189.0 | 187.8 | +2.1 | −1.2 | 294.1 |
| 16 | 734 | 331.4 | 331.5 | 336.1 | +4.8 | +4.6 | 441.2 |
| 32 (=16) | 734 | 331.4 | 332.2 | 336.5 | +5.1 | +4.3 | 441.9 |
| 64 (=16) | 734 | 331.1 | 332.4 | 336.1 | +5.0 | +3.7 | 442.0 |

- cache_n = 0 in 432/432 ladder commits — including FREE and WARM, whose
  immediately preceding request shared the entire history prefix
  (warm_cache_n=0 too: even the warm request served nothing).
- WARM is never faster than COLD beyond noise (−0.3 ms at k=0) and is
  slightly SLOWER at scale (+4.8/+5.1/+5.0 ms) — the commit re-prefills
  everything regardless, plus cache-state churn.
- WARM ≈ FREE everywhere (−1.2…+4.6 ms): warming achieves nothing the
  free path doesn't already fail to deliver.
- warm_ms (wall cost of the warm request itself, paid during speech):
  161/212/294/441 ms at 49/168/388/734 prompt tokens — feasible against
  multi-second speech, and buys exactly nothing.
- COLD prefill curve (headline): TTFT ≈ 37 + 0.40·prompt_n ms (LSQ over
  the four distinct rungs; server prompt_ms tracks within ~10 ms).
  Extrapolated, unmeasured: ~440 ms @ 1000 tokens, ~1.3 s @ 3200.
- Output identity: 144/144 (k, scenario, rep) cells byte-identical across
  arms — expected, since reuse never fired and the fp-reorder mechanism
  had no opportunity; no divergence flag.

## Verdict: R3 — DEAD

Rule verbatim: "R3 DEAD: WARM − COLD < 25 ms at every size → case 2
recorded dead on this build; page untouched." Measured WARM−COLD spans
−0.3…+5.1 ms at every rung (49–734 tokens); the 25 ms bar is never
approached. R1 fails on its first clause; R2's antecedent (≥25 ms saving
at large contexts) never materializes.

The binding constraint is the server, not economics: at 734 tokens a
working history-prefix serve would save ~285 ms of the 331 ms commit
prefill, so the prize at scale is real — but this llama-server build does
not serve a shared prefix when continuations diverge, at any tested LCP
(50–154) or prompt size (49–734), with populator generation 1 or 8
tokens. Corollary measured fact: FREE ≈ COLD at every rung —
cache_prompt=true's intended steady state (same context, different
utterance) also serves nothing on this build at any size from 49 to 734
tokens. Registered path if prompts ever grow: restart llama-server with
--cache-reuse (or a build whose common-prefix serve fires on divergent
continuation) — user-owned action, the running server is root-owned —
then re-run this protocol before implementing anything.
