# PROTOCOL: deep agents — tool legs inside deep think (stage 1)

Registered 2026-10-05, before any gate data. Amends PROTOCOL-deep-think.md
and PROTOCOL-ir.md; does not amend PROTOCOL-browser-use.md in stage 1
(stage 2 does). House rules apply: this file is committed before any
implementation gate data; frozen evals are never edited to pass; any
gate revision is registered here with measured evidence BEFORE the
re-run; commits are local, never pushed.

## Goal

A deep-think turn may call the IR track as a bounded tool mid-think:
the model decides it lacks facts, one to three research legs run
(same IRTask machinery, budgets, fixture replay, traces, UI events),
and the findings feed the final verdict. Stage 1 of the staged plan
agreed 2026-10-05:

1. **stage 1 (this protocol)** — IR-only legs, read-only, no new
   safety surfaces;
2. **stage 2 (registered, not implemented)** — browser legs through
   the same protocol, with submit actions yielding to the spoken
   read-back guard (the parked `confirm_resume` machinery, M2);
3. **stage 3 (registered, not implemented)** — manifest dual-exposure:
   `ir_research` and `browse` join TOOL_MANIFEST so an external agent
   could delegate a bounded job (the agreed rule: "a node earns a tool
   form when another agent could delegate a bounded job to it");
   intake intercepts stay authoritative over manifest routing.

## Motivation (measured facts at registration)

- Deep think is a closed single-pass generation with no tool access.
  A deep question whose answer lives on an archive page or the web
  gets a hallucinated or refused verdict — the architecture gap this
  protocol closes.
- The configured deep provider (Mirai 27B,
  http://100.120.167.9:8995/v1) was unreachable at registration
  (connection refused in 69 ms, measured 2026-10-05), so every deep
  turn currently runs the local slot-5 fallback. The tool loop must
  live on BOTH generation sources and be endpoint-agnostic — never
  tied to a provider's native function-calling.
- The IR track's leg machinery is gate-proven (PROTOCOL-ir.md
  Results: E2 4/4, E3 2/2, E4 2/2, E5 2/2 refusals/structural, E6
  regression): legs reuse IRTask, its code-owned budgets, the replay
  registry, traces, and `ir_event` rather than introducing new
  execution code.
- Routing and intake are untouched: no new intercepts, no tree edit,
  no manifest edit. The dual-exposure rule stands — manifest tools
  are one-call actions; tree classes are modes the system enters. A
  think-time tool is neither: it is a leg the think loop owns.

## Decisions

1. **Scope.** Only turns the tree marks `deep` (think node deep
   leaves) run the loop. Everything else — routing, intake, browser
   track, tool flow, decide — is byte-identical to the shipped
   behavior. `TOOL_MANIFEST` gains nothing in stage 1.

2. **Text tool protocol, endpoint-agnostic.** The deep directive
   (decision 10) tells the model to end a round with exactly:
   `IR_TOOL {json}` on its final line. The server parses the JSON
   after the marker with the house `_extract_json`; no server-side
   stop sequences (they are not portable across the openai-chat /
   openai-responses / anthropic dialects the provider layer speaks).
   Stream holdback: the consumer withholds the last 16 characters of
   answer text until more text arrives or the round ends, so the
   marker (7 chars) can never reach the UI or TTS. A round with no
   valid marker is just the answer.

3. **Tool args schema (validated code-side).**
   `{"query": "...", "question": "...", "depth": 1|2|3 (optional,
   default 2), "url": "..." (optional)}`. Invalid when `question` is
   empty or both `query` and `url` are missing; out-of-range depth
   falls to the default; a list query joins with spaces (measured
   planner variance, PROTOCOL-ir.md run 4). An invalid call is not
   retried: the continuation note (decision 9) says the call could
   not be used, and the think continues honestly.

4. **Budgets are code-owned.** Per deep turn: at most **3 tool legs**
   and **180 s combined leg time**. Each leg inherits the IRTask
   budgets for its depth (PROTOCOL-ir.md decision 2: 20/45/90 s;
   fetch/serp caps) — a leg is a full IRTask, not a bare fetch.
   Continuation rounds do not count as legs. When the budget is
   spent and the model calls again, no leg runs; the continuation
   note (decision 9) says the budget is spent and forbids further
   calls.

5. **Legs are detached IR tasks.** `IRTask(session, plan, utterance,
   detached=True)`: identical constructor rules (the RESEARCH_RE
   depth-3 floor fires on the deep utterance, page-name extraction,
   resolver layers with utterance fallback, replay registry), same
   trace at /tmp/wiresense_ir/<id>/trace.json, same `ir_event` fetch
   events to the UI, same depth-3 progress filler. The only
   difference: `finish()` writes the trace, closes the page, and
   returns — it does NOT call `session.ir_done` (no spoken answer, no
   history entry, no task-clear; the loop owns those). The loop reads
   `task.findings` (preferred) or `task.answer` and `task.outcome`.

6. **Credential refusal inherits at leg intake.** A leg whose
   question (or the deep utterance itself) matches CREDS_TASK_RE is
   refused before any page opens; the continuation note says the
   lookup was refused. No password/sign-in task ever reaches a page,
   same as both tracks (decision 10 of both protocols).

7. **One deep handler for both generation sources.** All `deep`
   turns go through one handler (`deep_turn`), whether or not a
   provider is configured. Round 1 uses the configured provider; on
   ANY provider failure the loop falls back to the local open-think
   source for that and all later rounds (recorded in meta.fallback —
   measured: provider refusal costs 69 ms today). The local source is
   the existing slot-5 open-think streaming behavior, extracted into
   one helper shared with the generic think path (same prompts, same
   think-close forcing, same THINK_NOCLOSE_REPLY contract, same
   events). The handler never falls through to a second, duplicate
   generation: on a hard local failure it emits the `error` event,
   releases the gate, and ends the turn (the generic path's
   exception contract).

8. **Continuation shape (true ReAct continuity).** After a leg, the
   conversation is extended: history + (original utterance, partial
   answer) as an assistant pair, then the findings note as the user
   turn (decision 9), and the model continues the SAME answer. The
   final spoken reply is partial-answer + continuation concatenated
   (streamed seamlessly: response_start fires on the first partial
   delta, deltas continue across rounds). Only this final text enters
   history and TTS.

9. **Continuation notes (registered verbatim; the note rides the user
   turn, the continuation directive rides after it).**
   - findings: `[Research findings for '<query>' (outcome:
     done|budget|error): <findings, else the leg answer>]`
   - refused: `[That lookup was refused: it asks for credentials or
     sign-in, which the assistant never handles. Answer without it.]`
   - invalid: `[Your IR_TOOL line could not be used (missing question
     or keywords) — answer from what you know.]`
   - budget spent: `[Research budget spent — answer with what you
     have now; do not call IR_TOOL again.]`

10. **Prompt directives (registered verbatim).**
    - Round 1 (appended to the deep utterance, both sources, so the
      KV prefix stays warm and the decide readout is unaffected):
      DEEP_TOOL_DIRECTIVE — see the constants block in the
      implementation; it fixes the marker casing, the one-short-
      sentence preamble, the args schema with a depth hint, and the
      "never write IR_TOOL when you already know" clause.
    - Continuations: IR_TOOL_CONT_DIRECTIVE — finish from the
      findings, cite concrete facts, say what is missing instead of
      inventing, call IR_TOOL again only for a specific still-
      lookable fact.

11. **Interruption contract: unchanged in stage 1 (registered
    cost).** A deep turn holds the turn; the mic is gated for its
    whole duration; "stop" is not honored mid-turn — exactly today's
    deep-think behavior. The code-owned budgets bound the worst case
    (3 legs x 90 s depth-3 budget, capped at 180 s combined, plus
    per-round provider timeout 120 s; local rounds are fast). Return
    of interruption is stage 2's guard-yield.

12. **Metrics and traces.** meta gains `legs` (count), `tool_calls`
    (list of {query, depth, outcome, ms}) and flows into the existing
    `deep` metrics block; each leg keeps its own full IRTask trace.
    The gate asserts traces are fixture-only (E5's rule).

13. **Numerics parity.** No serving-numerics change: no tree edit, no
    router refs touched, no plan/digest/pick prompt changed. The deep
    generation prompt changes by one appended directive — generation
    prompts carry no recorded references. No re-record is required.

## Gates (frozen in tools/test_deep_agents.py — a NEW file; existing
## frozen evals are never edited)

- **DA1 unit (offline, no browser, no network):** marker parser —
  clean answer returns (text, None); marker + valid JSON returns
  (partial, args); malformed JSON / missing fields return (text,
  None) without raising; args validation (depth clamps, list query
  joins, missing question invalid); the 16-char holdback never emits
  any marker prefix to the fake consumer; directive strings contain
  the marker literal and the args schema fields.
- **DA2 spoken e2e (real ASR -> route -> deep loop -> legs on the
  frozen replay registry, spoken via TTS->ASR like test_ir.py):**
  - Case A: "Think hard about which espresso grind setting to use,
    and research it in the local archive before answering." — must
    route deep, run at least one leg, stay within budgets (legs <= 3,
    combined leg time <= 180 s), answer citing the fixture's concrete
    recommendation (the exact frozen string is recorded after the
    first measured run and then never changed), and every fetch in
    every leg trace must be snapped (fixture-only, E5's rule).
  - Case B (control): "Think hard about this math problem and work
    out the exact answer." — routes deep, runs ZERO legs, answers
    normally (the directive must not force tools on knowledge
    questions).
  - Spoken-variance clause, same as the IR gate: one bounded re-feed
    per clip; if two consecutive full runs fail on DIFFERENT spoken
    cases, that is input variance — a third run decides and is final.
- **DA3 regression battery (unchanged bars, all must hold):**
  eval-router root >= 56/62, eval-ir E1 >= 13/14 and G1 >= 12/14,
  test-voice 14/14, test-safety 31/31, test-normalizer 22/22,
  test-browser PASS, test-deep-think PASS.
- **VERDICT:** PASS iff DA1 all, DA2 A and B, DA3 all bars; any fail
  is adjudicated here in a Results section — including honest failure.

## Revision log

- 2026-10-05: registered (this entry). No gate data exists yet.

## Results (adjudicated)

_to be filled after the gate run_
