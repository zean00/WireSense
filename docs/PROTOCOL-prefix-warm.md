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

## AMENDMENT — large-context run to the platform ceiling
(pre-registered 2026-10-07, committed before any large-context data)

Motivation: the user asked whether the probe reaches 50K–100K-token
contexts; it reached 734 (INCIDENT 2). This amendment extends the
measured range as far as THIS instance allows.

Platform facts discovered pre-data (server mechanics, not experiment
data): /props reports default_generation_settings.n_ctx = 8192 — with
`-np 8` the 65,536 context is divided across slots, so EVERY slot
(including probe slot 1 and all production slots) caps at 8192 tokens
per request. Consequence, disclosed up front: prompts of 16K–100K
tokens are unreachable on this instance without a restart
(`-np 1 -c 65536` or a larger `-c`, model-permitting) — user-owned.
Beyond the new top rung the curve remains extrapolation; what this
amendment buys is whether ms/token stays linear to ~10× the old max.

Design (frozen):

- Fixture: tools/prefill_probe/large_fixture.py — deterministic
  (index arithmetic, no RNG; 200 dialogue pairs, combo cycle 840 > 200,
  no repeats). sha256 of the fixture text:
  b42bde63a0108bc0ccbfedfd7f3c2f5312c948d4aa1d05a5d363ad4cb20b84c4.
  The runner asserts this hash before firing anything and aborts on
  drift.
- Ladder in token space: targets {1024, 2048, 4096, 7680} prompt tokens
  (largest ≤ 8192 − 128 headroom for the mt=96 generation). Runner
  bisections the pair count per target against /tokenize on the
  longest entry-V final; actual prompt_n recorded per rung and used in
  adjudication. Pre-sized pre-data (no completions fired): rungs land at
  25/51/104/196 pairs → prompt_n ≈ 1012 / 2002 / 4058 / 7651 (finals
  range ±9 tokens).
- Arms, interleaving, records: identical to part 2 — COLD / FREE /
  WARM per (rep, scenario), 3 reps × 8 entry-V finals, think-off,
  disposer/populator/warm mt=8, commits mt=96 streaming (stream flag in
  the body — the incident-1 fix), slot 1. Per-commit: wall TTFT,
  prompt_n, prompt_ms, cache_n, predicted_n, text, warm_ms, warm_cache_n.
- Launch assertions: slot n_ctx == 8192 and every rung's prompt_n ≤
  8064, else abort before writing data.
- Runtime: ~15 min estimated (each arm re-prefills the full history;
  ~5 full prefills per (rep, scenario) at the top rung).

Decision rules (pre-registered):

- D1 DESCRIPTIVE HEADLINE: the COLD TTFT curve across the four rungs;
  marginal ms/token per rung; LSQ fit with the prior data point (734)
  included for reference. Extrapolation beyond 7651 labeled as such.
- D2 RE-OPEN CLAUSE: if ANY FREE or WARM commit shows cache_n > 0 at
  any rung, the rewind-never-fires conclusion does not scale — flag
  prominently and re-adjudicate case 2 under the original R1 rules at
  the largest rung (WARM ≤ COLD − 100 ms AND ≤ FREE + 10 ms at the two
  largest rungs, AND WARM ≤ COLD + 10 ms at the 49-token rung).
- D3 R3 CONFIRMATION: WARM − COLD < 25 ms at every rung → case 2 stays
  dead on this build at scale; expected outcome given part 1 and the
  492/492 cache_n=0 record; no production change; page untouched.
- Output identity across arms reported, not gated; a WARM-vs-COLD
  divergence rate > 25% at any rung is flagged prominently regardless.

Disclosures: think-off only; synthetic generated dialogue (more
repetitive than natural conversation; adjudication uses actual
prompt_n); ambient GPU load from the live production server possible;
no audio/TTS path; the largest rung exercises ~94% of the slot's 8192
context — any server-side context-shift warnings in
/tmp/llama-server.log around that rung would be disclosed if found.

# AMENDMENT RESULTS (adjudicated 2026-10-07)

Data: tools/prefill_probe/results_large_ctx.json (git_head 7c6c4c2,
started 20:56:49, finished 21:09, 3 reps × 8 finals × 3 arms × 4 rungs =
288 commits). Stats: tools/prefill_probe/analyze_large_ctx.py (post-data,
read-only). Launch assertions passed: fixture sha256 matched, slot
n_ctx = 8192, every rung ≤ 8064 prompt tokens (max 7651).

## D2 TRIGGERED — reuse fires at scale

96 rows with cache_n > 0: every FREE and WARM commit at rungs 4096 and
7680. Zero at rungs 1024 and 2048; COLD cache_n = 0 at every rung
(disposer precedes it — nothing shared to serve).

Measured serve amounts (median):
- rung 4096 (full ≈ 4054): FREE served 2001–2010, re-prefilled 2048;
  WARM served 1992, re-prefilled ~2062.
- rung 7680 (full ≈ 7647): FREE served 5594–5603, re-prefilled 2048;
  WARM served 5585, re-prefilled ~2062.

The re-prefill residual is pinned at ~2048 tokens at both rungs — the
`-ub 2048` value in the running server's flags (attribution hypothesis,
labeled; the pinned residual is the measured fact). All reuse data across
the three probes fits one rule: **a divergent continuation is served
max(0, LCP − ~2048) tokens — the trailing ~2048 are always re-prefilled.**
The identical-replay shape (prompt exactly the cached prompt, the
pre-registration probing) is served in full (924-token replay = 26 ms).

This CORRECTS the registered-run verdict's mechanism record: "mid-
sequence rewind never fires on this build" was true only of the tested
sub-2048-LCP regime. It fires once the shared prefix exceeds ~2048
tokens. Probe 1 (120/120), part 1 (60/60), part 2 (432/432), and rungs
1024/2048 were all in the sub-threshold regime; nothing in them is
invalidated, but the mechanism conclusion does not scale — exactly the
contingency D2 was written for.

## D1 — the COLD prefill curve (headline)

| prompt_n | COLD TTFT | FREE TTFT | WARM TTFT | warm_ms |
|---|---|---|---|---|
| 1008 | 440.1 | 444.6 | 444.6 | 549.5 |
| 1998 | 892.5 | 886.2 | 890.7 | 1007.2 |
| 4054 | 1793.5 | 949.9 | 974.7 | 1896.3 |
| 7647 | 3431.1 | 1048.2 | 1082.9 | 3537.7 |

- Marginal COLD cost is LINEAR through 7647 tokens with no quadratic
  onset: 0.457 / 0.438 / 0.456 ms/token between consecutive rungs
  (flash-attn, chunked prefill).
- LSQ over the four rungs plus the registered run's 734-token point:
  TTFT ≈ −8 + 0.449·prompt_n ms (the negative intercept is a fit
  artifact; clamp to the ~40 ms floor). Extrapolation, labeled, beyond
  7651: ~4.4 s @ 16K, ~9 s @ 32K, **~14.5 s @ 50K, ~21.5 s @ 65,536**
  (the server's slot cap makes 100K unreachable without a restart).
- The shipped steady state (FREE) flattens instead: once the history
  prefix crosses ~2048 tokens, per-turn TTFT ≈ 0.95–1.05 s REGARDLESS of
  context size (949.9 @ 4054, 1048.2 @ 7647) — the 2048-token re-prefill
  floor dominates.

## Re-adjudication under the original R1 rules (per D2)

- WARM ≤ COLD − 100 ms at the two largest rungs: PASS (−818.8, −2348.2).
- WARM ≤ FREE + 10 ms at the two largest rungs: **FAIL (+24.8, +34.7)**.
- WARM ≤ COLD + 10 ms at the 49-token rung (registered run): PASS
  (−0.3).

R1 fails on the FREE-parity clause. The registered R2 conditions are met
(WARM − COLD ≥ 25 ms only at ≥~1000-token contexts; an R1 clause
otherwise fails) → **R2 REGISTERED CANDIDATE, no production change, page
untouched** — with the measured reason stated plainly, because it changes
what the "standing plan" is: the FREE clause fails not because warming is
marginal but because **the shipped cache_prompt=true path already
captures the entire benefit natively** — same serve amounts, 25–35 ms
less per turn, and none of the warm request's 0.55–3.5 s of GPU time
during speech. Warming is strictly dominated at every rung. The standing
plan for when prompts grow is therefore: do nothing — the reuse arrives
on its own once the turn-to-turn shared prefix exceeds ~2048 tokens (i.e.
prompts above ~2100 tokens). The registerable follow-up is the 2048-token
re-prefill floor (~0.95 s at any large context): whether a --cache-reuse
restart (user-owned) removes or shrinks it — that would help the FREE
path, not warming.

## Identity

- COLD == FREE: 24/24 at every rung (and 144/144 cells in the registered
  run) — the native reuse path measured output-clean throughout.
- COLD == WARM: 24/24 at 1024/2048/7680; **18/24 at 4096 — the 25%
  divergence flag threshold, flagged**: 6 WARM cells rewrote outputs
  (fp-reorder at the served boundary), some substantively ("cold water"
  → "hot water" in the cold-brew cell). The explicit-warm boundary
  (empty-user-turn tail) is the fragile one; the native path's boundary
  measured clean. A further reason warming stays retired.

## Disclosures

Same as the amendment, plus: the D3 expectation ("stays dead") was
falsified in part — reuse fires at scale — and the adjudication followed
the registered D2 re-open path rather than the expected D3 record. The
ubatch attribution of the 2048-token residual is a labeled hypothesis;
the residual's measured constancy (2048/2062 across 96 commits at two
rungs) is the fact relied on. No server restart, no flag changes;
production untouched throughout.

## AMENDMENT 2 — does --cache-reuse remove the 2048-token floor?
(pre-registered 2026-10-08, committed before any sandbox data)

Question: the standing lever after the large-context run is a
--cache-reuse restart (user-owned). Before recommending a production
restart, measure whether the flag actually delivers on THIS build.
Constraint: the GPU is full (7513/8151 MiB, production), so a second
GPU instance cannot exist; production :8998 (root pid 336728) stays
untouched per standing rule.

Sandbox (pre-data platform facts):

- Binary: /home/sahal/.local/bin/llama-server (the user's own build —
  the production binary; --help confirms the flag exists: "--cache-reuse
  N  min chunk size to attempt reusing from the cache via KV shifting,
  requires prompt caching to be enabled (default: 0)").
- Instance: MY OWN process on 127.0.0.1:8999, CPU-only, run as sahal —
  launched and killed by me; production unharmed regardless of outcome.
- Flags: mirror production where mechanism-relevant — same model file,
  -b 8192 -ub 2048 --embedding --pooling last — minus GPU specifics:
  -ngl 0 (CPU; 16 threads), -c 16384 -np 1 (KV f16; single slot so every
  request lands the same cache, mirroring the probes' id_slot pinning),
  no flash-attn/KV-quant (CUDA-only), no mmproj (text probes only).
  kv cache f16 ≈ 2.4 GB + weights 2.9 GB fits the 10 GB available RAM.
- A/B: identical instance launched TWICE — baseline (no flag), then
  treatment (--cache-reuse 1, the most aggressive chunk size). The only
  delta is the flag. Baseline also validates the harness: it must
  reproduce the known GPU pattern (cache_n = 0 for divergent LCP < 2048,
  ~LCP−2048 served above), on different hardware.
- DISCLOSED LIMIT: CPU sandbox measures MECHANISM — served tokens,
  residual size, output identity — not milliseconds. GPU projections
  use the measured GPU marginal (0.44–0.46 ms/token) applied to the
  measured residual. Absolute TTFTs here are CPU-scaled and
  non-comparable to the 8998 tables.

Design (frozen):

- E1 — sub-2048 regime, both instances (the shipped-size question):
  (a) filler pairs sharing L ∈ {50, 154} words, populator mt ∈ {8, 1}
  (the poison quirk), 2 reps; (b) history-LCP ≈ 1008 tokens (k=25
  large-fixture history): populator (filler utterance, mt=8) → commit
  (final_0, mt=8), 2 reps. Descriptive: does the flag make rewind fire
  where the no-flag build serves nothing?
- E2 — rung-4096 A/B, both instances: history k=104 (prompt ≈ 4054
  tokens, the floor regime), 4 entry-V finals (scenarios 0–3), 1 rep,
  arms COLD/FREE/WARM exactly as the registered large run (disposer →
  commit mt=96 stream; populator mt=8 → commit; disposer → warm
  prompt_for("", history) mt=8 → commit; warm_ms recorded). Per-commit:
  TTFT, prompt_n, prompt_ms, cache_n, predicted_n, text.
- E3 (conditional): rung-7680 (k=196) on the TREATMENT instance only,
  same shape, run iff E2 shows serving (else the floor question is
  already answered negatively). Descriptive replication at scale.
- Known overlap, disclosed: with the flag, "COLD" commits share the
  system-block LCP (~30–45 tokens) with the preceding disposer — any
  treatment COLD serve quantifies it; on 8998 COLD cache_n was 0
  everywhere (comparator).

Pre-registered verdict rules:

- FR1 FLAG DELIVERS: treatment FREE at E2 serves ≥ 3900 of ~4054
  (residual ≤ ~128 tokens) — i.e. the 2048-token floor is gone, not
  just shrunk → the production restart becomes a real registered
  candidate; GPU projection = residual × 0.45 ms/token.
- FR2 PARTIAL: treatment FREE serves strictly more than the no-flag
  rule predicts (LCP − 2048 ≈ 2006) but residual > 128 tokens → record
  the served/residual curve; candidate with quantified benefit.
- FR3 NO DELIVERY: treatment FREE served within ~64 tokens of 2006 →
  flag does not deliver the floor removal on this build; recorded,
  restart not recommended on this evidence.
- Identity: within-instance across-arm cells reported; a treatment
  divergence rate > 25% flagged prominently; cross-instance COLD
  equality descriptive.

Runner: tools/prefill_probe/run_cache_reuse.py (--out per phase);
launch assertions: /health 200, total_slots == 1, n_ctx ≥ 4192, and
every E2 prompt_n ≤ n_ctx − 128.
