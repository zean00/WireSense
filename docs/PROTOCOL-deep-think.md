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

## Results

(filled at adjudication; none exist yet)
