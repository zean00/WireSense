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
56/62, think 24/26. The file is imported, never modified. Production
readout shape confirmed: DecideReadout.embed(build_prompt(history,
partial)) — the readout input is the full prompt text; shipped history
is just tiny (digest two-zone).

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
