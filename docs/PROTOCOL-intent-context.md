# Protocol: intent-inference input shape — does the root decision layer
# need conversation history? (pre-registered 2026-10-08, no data)

Follow-up to the prefix-warm campaign. Measured facts so far: commit
TTFT ≈ 40 + 0.45 ms/prompt-token; native reuse above ~2100-token
prompts; a 2048-token re-prefill floor; cache-reuse architecturally
unavailable (M-RoPE). Design position under test: the ROOT decision
layer (tree readout) runs on the shipped context-poor input
(last message + digest, ~90–100 tokens, 75 ms commit, ~56 ms readout)
while full history is only pulled behind the decision for task/agentic
turns. The claim to measure: full raw history buys the readout little
or nothing — and where it looks like it should help (anaphora), the
gain and cost must be quantified before any design change.

## Machinery under test (frozen, untouched)

tools/eval_router.py — the registered root/think readout eval:
norm_router_text → /v1/embeddings (id_slot 6, production embedding
slot) → arm-C heads (project) → L2-normalized class centroids
(models/router_embeddings.npz + arm_C_heads.npz), argmax cosine.
Frozen items: ROOT_EVAL 62, THINK_EVAL 26. Registered bars: root
56/62, think 24/26. The file is imported, never modified.

CORRECTION (pre-adjudication, first run): this protocol initially
stated the production readout embeds build_prompt(history, partial).
That is wrong for the ROUTER: shipped live turns embed the BARE
normalized utterance (server.py:1217 and the TreeWalker contract —
whose docstring already records that prompt/history-shaped vectors
"were measured and dropped (domain shift cost two route misses in the
first live trial)"). The build_prompt-based score() exists only in the
streaming-loop decide() path (server.py:1065), not the router.
Consequence for this probe: BARE is not just the harness shape, it IS
the shipped production shape; PROMPT0/HIST arms measure hypothetical
input-shape changes, exactly the registered question. The first run's
Part A already replicated the docstring's two-miss anecdote at scale
(prompt-shaped inputs collapse root to 33–42/62 and think to 10/26);
the correction is committed before any adjudication, with a full
re-run (Part B crashed on a runner bug — history passed as a bare
string instead of a (user, assistant) pair; no results JSON was
written by the crashed run).

## Questions

Q1 (frozen 88): does prepending raw history to the readout input
   change root/think classification accuracy vs the bare-utterance
   harness — and does the two-zone commit wrapper (ChatML, no history)
   itself shift it?
Q2 (new anaphora set): where history plausibly SHOULD help — utterances
   that are genuinely ambiguous bare (deictic "it/that", anaphoric
   commands) — does one turn of history recover correct classification?
Q3 (latency): embedding wall ms and prompt_n per input shape, compared
   against the measured prefill line (≈40 + 0.45 ms/token).

## Design (frozen before any data)

- Runner: tools/eval_router_context.py (committed with this protocol,
  data-free). Scoring functions mirror eval_router.py verbatim;
  centroids/heads loaded from the same frozen files (absolute paths).
- Part A arms, each scored on all 88 frozen items:
  BARE      — the utterance alone (registered harness shape; harness
              validation: must reproduce ~56/62 and ~24/26 within the
              noise band, else the run is invalid).
  PROMPT0   — ws.build_prompt([], utt, None, False): the two-zone
              commit shape with empty history (~60–80 tokens).
  HIST3/8/16— ws.build_prompt(large_fixture.PAIRS[:k], utt, None,
              False) (deterministic fixture, sha256
              b42bde63…; ~170/~360/~700-token prompts).
- Part B — new frozen anaphora set (24 pairs, in the runner,
  pre-registered here): 10 vision-deictic (t1 establishes a shown
  object, t2 a bare-pronoun question; gold vision at root), 8
  tool-anaphoric (t1 tool command, t2 anaphoric follow-up; gold tool
  at think node), 6 self-contained controls (gold direct/decide/
  social). Conditions per pair: BARE (t2 alone) and HIST1
  (build_prompt([t1], t2)). The runner applies eval_router's
  pool-clash guard to every t2 and ABORTS on any clash.
- Latency: per scored call, wall ms and /tokenize prompt_n of the
  exact normalized input; reported as median per arm.
- Server: production :8998 untouched; embeddings ride slot 6 exactly
  like the registered harness; production voice idle.

## Pre-registered decision rules

Noise band: ±2 items on 88 (≈2.3 pts) — differences inside it are
noise, matching the ±1-item margins historical runs treated as noise.

- RD1 CONTEXT-POOR VALIDATED: every Part-A arm within ±2 pts of BARE
  on both nodes → raw history neither helps nor hurts frozen intent;
  the only differentiator is latency → keep the shipped input shape.
- RD2 HISTORY POISONS: any arm ≤ BARE − 3 pts on either node → raw
  history must stay out of the readout (hard production rule).
- RD3 HISTORY HELPS FROZEN: any arm ≥ BARE + 3 pts → unexpected;
  re-run and investigate before any design change.
- RA1 ANAPHORA RECOVERS: Part-B t2 accuracy HIST1 − BARE ≥ +15 pts
  (≥ 3.6 items) AND RD1 holds → context expansion earns a place ONLY
  behind an ambiguity gate: register confidence-gated double-readout
  (bare first; history re-readout only on low margin) as the standing
  candidate; its cost is measured separately before any implementation.
- RA2 ANAPHORA INERT: gain < +10 pts → the root layer is validated
  context-poor even for deictic intent; entity resolution stays
  downstream (LLM at commit sees history + digest).
- Latency is reported, not gated; it feeds the standing latency table.

## Disclosures

Anaphora items are new and hand-written (pre-registered verbatim in the
runner before any scoring; pool-clash guard enforced at run time); the
frozen 88 items are untouched; history fixture is synthetic dialogue
(same sha-pinned file as the prefix-warm probe); slot 6 is shared with
production embeddings — probes run while voice is idle; embeddings
wall-time includes HTTP overhead (~similar to the readout path);
no production config or code change; results append to
tools/results_intent_context.json only.

# RESULTS (adjudicated 2026-10-08)

Data: tools/results_intent_context.json (run 2, after the runner fix;
run 1's Part A was byte-identical where it printed — same arm numbers —
and no JSON was written by the crashed run). Harness validation: BARE
root 56/62, think 24/26 — EXACTLY the registered numbers.

## Part A — raw history poisons the readout (RD2 fires, maximally)

| arm | shape | prompt_n | root | think | emb ms (median) |
|---|---|---|---|---|---|
| BARE | utterance alone (shipped) | 8 | **56/62 = 90.3%** | **24/26 = 92.3%** | 24.6–27.7 |
| PROMPT0 | two-zone shape, no history | 38 | 33/62 = 53.2% | 10/26 = 38.5% | 31.9–33.3 |
| HIST3 | +3 pairs | 152 | 37/62 = 59.7% | 10/26 = 38.5% | ~85 |
| HIST8 | +8 pairs | 312 | 42/62 = 67.7% | 10/26 = 38.5% | ~164 |
| HIST16 | +16 pairs | 592 | 37/62 = 59.7% | 10/26 = 38.5% | ~307 |

Every prompt-shaped arm is ≤ BARE − 23 pts (think: −54 pts, all arms).
PROMPT0 — the two-zone commit shape with ZERO history — already
collapses: the poison is the input distribution (system block + ChatML
+ tail), not history per se. This quantifies, at eval scale, the
TreeWalker docstring's recorded live finding ("prompt/history-shaped
vectors were measured and dropped — domain shift cost two route misses
in the first live trial"). The readout is a bare-utterance classifier;
its reference embeddings live in that distribution only.

## Part B — anaphora does not need history at the readout (RA2 fires,
in the strong direction)

| subset | BARE | HIST1 |
|---|---|---|
| vision-deictic (10) | 7/10 | **0/10** |
| tool-anaphoric (8) | 7/8 | 8/8 |
| controls (6) | 3/6 | 6/6 |
| **aggregate (24)** | **17/24 = 70.8%** | **14/24 = 58.3%** |

HIST1 − BARE = −12.5 pts: one turn of history makes intent inference
WORSE, not better. The vision subset is the mechanism made visible —
every deictic item flips to direct once the t1 context rides along
(the history text swamps the utterance in the shared embedding
space). The bare readout already classifies deictic utterances at
70.8% on their lexical class cues alone ("how much light does it
need" is vision-shaped regardless of what "it" is). The two cells
where HIST1 "improved" (tool +1, controls +3) are inside small-n noise
and swamped by the vision collapse. RA1's precondition (RD1) fails;
the confidence-gated double-readout candidate is DEAD — its re-readout
input would poison, not inform.

## Latency (Q3)

Embedding cost tracks the prefill line: ~25–28 ms at 5–8 tokens,
307 ms at 592 tokens (≈0.49 ms/token + small floor — consistent with
the measured 0.45). History would multiply every readout 6–12× for a
−12.5 to −54 pt accuracy change.

## Verdict

The context-poor root decision layer is now measured fact, not design
taste: the shipped input shape (bare normalized utterance) is the
BEST measured input on both frozen and anaphora sets, by 12.5–54
points, at the lowest latency. History has no role at the routing
layer on this machinery. Entity and anaphora resolution belong
downstream — the LLM at commit, which sees history + digest. If
history-aware routing were ever wanted, the registered path is a NEW
readout (re-record all signature pools over prompt-shaped embeddings,
new protocol, new eval bars), not an input flag — and nothing in this
data suggests it would win.

## Ops incidents (disclosed)

Run 1 crashed in Part B (history passed as a bare string instead of a
(user, assistant) pair) after two path/import bugs pre-launch; no
results JSON was written by any crashed run; run 2 is the archived
dataset. Production :8998 untouched; embeddings rode slot 6 while
voice was idle, same as the registered harness.
