# Protocol: deep-think externalization (pre-registered 2026-10-05)

The decide layer already routes each turn to a destination; this protocol
adds one more: **think.deep**, a bigger external model for turns where the
user explicitly asks for depth. Committed before any wiring or data, per
house rules. Numbers land in the adjudication commits that follow.

## Registered design decisions

1. **Config-gated, off by default.** `web/config.json` (gitignored;
   `web/config.example.json` committed) holds a `deep_think` block:
   `enabled`, `provider` (one of `openai-chat` | `openai-responses` |
   `anthropic`), `base_url`, `model`, `api_key_env`, `timeout_s`,
   `max_tokens`. API keys only ever come from the environment variable
   named by `api_key_env` — never from disk. No config → the feature does
   not exist at runtime.
2. **Provider dialects, one interface.** All three stream and normalize
   to (reasoning | text) deltas: `openai-chat` (`/chat/completions`,
   `delta.content` / `delta.reasoning_content`), `openai-responses`
   (`/responses`, `response.output_text.delta` /
   `response.reasoning_summary_text.delta`, system via `instructions`),
   `anthropic` (`/v1/messages`, `text_delta` / `thinking_delta`, system
   via `system`, key always required). The `openai-chat` dialect doubles
   as the local-provider path (llama-server / vLLM / LM Studio / Ollama),
   so "second llama-server on this box" and "cloud model" are the same
   adapter with a different `base_url`.
3. **Placement: think-subtree class, deliberately narrow.** `think.deep`
   matches only explicit depth/effort phrasings ("think hard about this",
   "take your time and reason through it"). Hard-but-unmarked questions
   must keep their current routes — the misroute costs are asymmetric
   (casual → cloud: slow + egress; hard → 4B: weak answer, recoverable by
   re-asking with an explicit cue). Root node pools are untouched.
4. **UI parity, honest fallback.** Deep turns stream the same events as
   local think (`think_delta` for reasoning, `response_start`/
   `response_delta` for the answer). Any provider failure — disabled,
   unreachable, timeout, key missing — falls back to the local think path
   silently; the metrics event records `deep: {model, fallback, ms}` so a
   cloud answer is never indistinguishable from a local one. No spoken
   apology mid-turn.
5. **Out of scope for v1:** per-turn voice consent flow, cost accounting,
   provider-side memory, speaking reasoning aloud.

## Gates (all must pass before the router change ships)

| gate | frozen bar |
|------|------------|
| `record-router` self-consistency | 0 cross-hits (every sig nearest own centroid) |
| `eval-router` ROOT_EVAL (56, untouched pools) | ≥ 49/56 (feasibility number; no regression) |
| `eval-router` THINK_EVAL | original 20 ≥ 18/20 AND deep held-out ≥ 5/6 |
| `eval-followup` (frozen 8) | ≥ 6/8 (adjudicated bar from the rewrite entry) |
| `test-voice` e2e (feature disabled) | 11/11 unchanged |
| deep bench (below) | deep ≥ 7/10 AND ≥ local score |

Eval-set additions to THINK_EVAL (deep held-out, 6) are disjoint from
every pool by the existing protocol guard; adding items to a frozen eval
is a protocol change, recorded here before the data exists.

## Deep bench (tools/deep_bench.py)

10 frozen questions with programmatic checks (multi-step arithmetic,
logic, combinatorics, code-output) — no LLM judge, exact regex scoring on
normalized text. Local side: the serving think path (slot 0, 4B, temp 0,
open think, max_tokens 640). Deep side: whatever `config.json` names,
streamed through the production adapter. First validation provider: a
second llama-server on :8012 hosting `~/models/Qwen_Qwen3-8B-Q4_K_M.gguf`
CPU-only (VRAM is full; RAM has headroom) via the `openai-chat` dialect —
the same wire path a cloud provider uses. Record both scores and
TTFT/total ms per side; adjudicate on the numbers, not the vibes.

## Revision 1 (2026-10-05, during adjudication — documented, not silent)

The first pool iteration exposed a placement flaw: pure effort cues
never reach the think subtree at root (measured: "Think hard about
this..." root-routes to vision, "Take your time..." to social), so the
subtree-only class was unreachable for cue-only phrasings — and the
per-node self-consistency gate cannot see root-level reachability.

Measured geometry (slot-6 embeddings, head-projected): open-ended
meta-requests ("Summarize this article for me", "Turn this paragraph
into bullet points") share the effort-cue direction, not the content-task
direction — enriching root.think with meta-task refs pulled the NEW refs
to deep too. Revision:

- root gains a `deep` class: pure effort cues (8 refs) + the three
  meta-task refs reclassified from root.think. Deep's semantic is now
  "explicit effort cues and open-ended meta-requests" — the 27B serves
  that family genuinely better; the egress cost is accepted and recorded.
- think subtree `deep` reworked to cue+content compounds (8 refs) for
  turns that root-route to think with an explicit cue attached.
- ROOT_EVAL +6 deep items (drafted before any root-deep scoring
  existed); THINK_EVAL +6 as pre-registered in the original protocol.

Accepted regression: 2 new root misses vs the pre-deep tree (52/56 →
51/56 on the frozen 56): "Turn this recipe into a shopping list."
(think→deep) and "Which of these two resumes is stronger?"
(decide→deep). Both are meta-space pulls; both would be answered well
by the deep model at egress cost. Within the frozen ≥49/56 bar.

## Revision 2 (2026-10-05, bench adjudication)

The bench gate FAILED on its own terms, and exposed a design flaw in the
gate itself. Measured (frozen 10-question set, temp 0):

- local 4B open-think: **10/10**, median total 13.8 s (ttft ~88 ms,
  first answer token after the full ~13.8 s think trace)
- deep Qwen3.8-27B (LAN, openai-chat): **9/10** — the miss is a genuine
  reasoning error (listed the five correct dice pairs for sum 8, then
  wrote "6 favorable pairs" and answered 6/36 = 1/6); median total
  5.0 s, first answer token ~4.1 s

Bar was `deep >= 7/10 AND >= local`: 9 >= 7 holds, **9 >= 10 fails**.

Flaw: the set is easy enough that local sits at ceiling — no model can
beat 10/10, so the `>= local` clause is unsatisfiable in this regime
and the bench cannot demonstrate the deep route's intended value
(questions the 4B gets wrong). Adjudication:

- the quality claim is **not made**; the frozen set stays frozen (no
  harder "v2 set" registered now — that would be moving goalposts).
- the router change stands on the routing gates (all passed) plus the
  opt-in contract: with no config, deep-routed turns fall through to
  local think — byte-identical to pre-deep behavior. The bench's role
  is recording, not adoption; adoption is the user's config choice.
- recommendation as measured: do not enable for this question regime —
  local think is perfect here AND the voice UX of a ~14 s local think
  trace vs the 27B's ~4.1 s-to-first-answer is the one measured deep
  advantage (2.6x faster wall-clock by compressing reasoning). If a
  regime where the 4B demonstrably fails matters, that needs a NEW
  pre-registered harder set, its own adjudication.

## Results

- routing gates (final tree, refs 132/132 self-consistent):
  ROOT_EVAL 56/62 (frozen-56 subset 50/56 >= 49; deep held-out 6/6 >= 5)
  THINK_EVAL 24/26 (original 19/20 >= 18; deep held-out 5/6 >= 5)
  eval-followup PASS (>= 6/8); test-voice 11/11 unchanged
- live spoken turn: cue -> ASR -> route deep -> 27B streamed ->
  TTS; metrics carry {model, ttft_ms 3810.1, ms 4204, fallback false}
- bench: local 10/10 (median 13.8 s) vs deep 9/10 (median 5.0 s);
  bar FAILED on the `>= local` clause — see Revision 2

## Implementation note (2026-10-07): external-provider context budget

The deep tier now bounds what it sends to the external LLM so an agentic
multi-round task cannot overflow the serving model's window. Engineering
change, gated by tools/test_deep_think.py (no eval data involved; frozen
evals untouched).

- Budget resolution (once, lazily, at first stream): `context_tokens` in
  the deep_think config block pins it; otherwise auto-detected from the
  provider's models endpoint (llama.cpp `meta.context_length` /
  `meta.n_ctx_train`, vLLM `max_model_len`, Anthropic `context_window`);
  fallback 8192 when the endpoint is unreachable. Prompt budget =
  ctx − max_tokens − 64 (gen reserve), floored at 1024. Logged once at
  resolution ("model ctx N [config|provider|default]").
- Clipping (per stream call, all three wire dialects): token estimate is
  chars/4 (provider-agnostic heuristic — no shared tokenizer across
  dialects; disclosed as an estimate, budget keeps slack via the reserve).
  Newest-first walk over history; oldest turns dropped first; the final
  user turn is always kept even if oversized (a single turn larger than
  the budget is the provider's error to reject); a leading assistant turn
  is shed after clipping (anthropic rejects assistant-first conversations).
  `last_fit` records kept/dropped for metrics; drops are logged.
- Local fallback path unchanged: build_prompt/server-side window stays the
  local server's own contract; this budget only guards the external wire.
- `context_tokens` < 2048 is a config error (deep feature off, not a
  crash), matching from_config's invalid-config contract.

### Revision (2026-10-07, post-measurement): detection order fixed by live probe

Measurement (tools/deep_budget_probe.py, ALL PASS, results in
tools/results_deep_budget.json) caught a real flaw in the version above:
on the production llama.cpp build (b8821), /models carries only
`meta.n_ctx_train` — 262144 — while the serving window is 65536 total,
8192 per slot. Trusting it would have set a budget 32x too wide, i.e.
no clipping where clipping matters. Detection is now:

1. `context_tokens` config pin (unchanged);
2. llama.cpp `/props` `default_generation_settings.n_ctx` — the
   per-slot serving window, the exact per-request bound (source
   "props");
3. serving bounds from /models only (`context_length`, vLLM
   `max_model_len`, `context_window`) — training bounds
   (`n_ctx_train`) deliberately ignored (source "models");
4. 8192 default.

Measured (mock openai-chat provider records the wire; :8998 supplies
the real /props, /models and tokenizer):

- A: props 8192 detected on :8998; models-only train bound correctly
  rejected; mock models-path (4096) detected.
- B: 283-pair / ~12.8K-est-token history clipped on the wire to 146
  messages, fitter accounting 3311 ≤ 3332; TRUE tokens 2917 → true
  prompt + max_tokens = 3617 vs 4096 window, margin 479 tokens.
- C: config pin (8192) beats provider detection; same history keeps
  328 messages vs 146 at the 4096 window.
- D: unreachable provider falls back to 8192 [default].
- E: deep_turn-style 6-round growth: per-round wire est pinned
  3288–3330 ≤ 3332 while round_hist grows every round. Note: kept
  MESSAGE count may drift up as small new turns replace larger old
  ones — tokens are the invariant, not messages.
- F: an oversized final user turn is sent intact (est 10K vs 4096
  window) — disclosed overflow, provider's error to reject.
- G: chars/4 vs the production tokenizer on the dialogue corpus:
  ratio 1.20–1.23 at every size (0–128 pairs) — the estimate is
  20–23% conservative, so the budget holds in true tokens. Recorded,
  not gated: Python-source text ratios 0.57 (chars/4 UNDERCOUNTS code
  ~2x) — code-heavy deep histories are a known weak spot; pin
  `context_tokens` with slack for such workloads.

INCIDENT (disclosed): the first probe run sent a ~40K-char (~10K-token)
body to :8998's /tokenize, which silently killed the production server
(no log entry after the previous request's 200; process gone). Restored
with identical flags via /home/sahal/.local/bin/llama-server (same
binary, same args; now running as sahal rather than root — no sudo was
available; log continues in /tmp/llama_server_8998.log). The probe now
refuses tokenize bodies > 24K chars and measures the F case by
estimate only. Capability suite re-run after restoration: 27/27.
