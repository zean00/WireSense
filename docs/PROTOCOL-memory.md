# PROTOCOL: persistent memory (remember / recall / forget)

Registered 2026-10-07 BEFORE implementation. Motivated by the capability
suite review (docs/PROTOCOL-capability-suite.md): notes persist to
memory.json but are never read back into any prompt, and "remember this /
from now on / don't …" has no capture intent anywhere — the model
acknowledges verbally and forgets instantly. Only reminders survive
(usefully: restart re-arming).

## Pre-implementation routing measurement (production geometry, root)

| utterance | route (conf) | correct? |
|---|---|---|
| "Remember that I'm allergic to peanuts" | think 0.800 | no — lands long_think/tool only via subtree, planner never asked |
| "Keep in mind that I prefer short answers" | deep 0.878 | no |
| "From now on, call me boss" | social 0.829 | no |
| "Forget what I said about the garage" | decide 0.798 | no |
| "Do you remember my name?" | direct 0.834 | YES — recall is an llm turn, must NOT be intercepted |
| "Don't forget my face" | vision 0.832 | YES — face-owned (FACE_INTENT_RE), must not be stolen |

Same measured class as the local-page/search/tool/deep intercepts: the
store-instruction vocabulary scatters across non-tool classes. Ship as a
code intercept; no pool surgery (the record-router gate already proved
these frames inseparable in the tool work).

## Registered design (and nothing more)

1. **Intent intercept** `MEMORY_RE` (checked LAST in the intercept chain,
   after tool/deep, and skipped when FACE_INTENT_RE matches): forces
   think.tool. Frames: "remember (this\|that\|:)", "keep in mind",
   "don't forget", "from now on", "forget (this\|that\|about\|what\|everything)".
   Recall questions ("do you remember…") deliberately do NOT match — they
   are llm turns answered with the digest.
2. **Native tools** `remember {text}` and `forget {about}` in
   TOOL_MANIFEST (+ validate_tool + run_tool branches, the note pattern;
   NOT high-risk — no parked confirm, like note). Facts store as
   `{"type":"fact","id","text","created"}` in memory.json. `forget`
   removes fact/note items whose text contains the keywords (reminders
   are scheduled behavior, not knowledge — out of scope; a failed match
   answers "nothing matching to forget").
3. **Recall digest** — the load-bearing half: `memory_digest_line()` =
   one line "(You remember: t1 / t2 …)" from the newest ≤8 fact+note
   items (each text capped 120 chars), composed into the DIRECTIVE of
   local llm answer turns and prepended to the utterance passed to
   deep_turn. Rides the CURRENT turn only: never the byte-stable system
   block, never history (the raw utterance is what history keeps, the
   camera_line precedent), and never the tool planner prompt (MS-20's
   lesson — no answer-side text in plan calls).
4. **Manifest growth / eval currency**: TOOL_MANIFEST gains 2 lines, so
   the frozen MS-16b-2 deployment evals' prompts change. Registered
   regression bar: re-run filesystem/git/time .b9 evals post-change; PASS
   stands only if pos/neg do not drop below the registered bars
   (pos ≥ 0.60, neg ≥ 20/24). Results append here.

## Acceptance bars (all required for ADOPT)

- B1 routing: "Remember that I'm allergic to peanuts", "From now on, call
  me boss", "Forget what I said about the garage" all land think.tool via
  the intercept; "Do you remember my name?" stays direct.
- B2 store→recall e2e: store a fact, then a FRESH session asks the
  question — the answer must contain the fact's keyword (digest-only
  path; no history).
- B3 forget: keyword removal verified in memory.json; recall turn after
  (fresh session) does not surface it as remembered.
- B4 no-regression: capability suite stays 24/24 (plus the 3 new memory
  scenarios); the three deployment-gate evals hold their bars.
- B5 planner pick: "Remember that X" produces a remember/note tool call
  (either stores; the digest includes both types) on ≥ the suite's frames.

Not in scope: user-scoped memory namespaces, automatic extraction of
preferences from ordinary chat, editing notes by voice, reminder
cancellation, memory injected into vision/browser/ir answers, face memory
(governed by PROTOCOL-face-recognition.md).

## Amendment 1 (2026-10-07, before the pivot arm runs): manifest growth REJECTED, intercept-scoped plan directive adopted instead

Arm 1 (TOOL_MANIFEST +2 lines, fresh re-run of the three frozen .b9
evals — the runner's resume cache was archived to results/*.pre-memory.jsonl
first so the run was genuine):

```
filesystem pos 35/64 = 0.5469 (bar 0.60) FAIL   [was 0.6406 registered]
git        pos 45/64 = 0.7031 PASS  neg 21/24
time       pos 24/32 = 0.7500 PASS  neg 21/24
```

Two extra manifest lines perturb planner selection on UNRELATED MCP turns —
the exact currency the deployment gate guards. B4 rejects arm 1; the
remember/forget lines come OUT of TOOL_MANIFEST.

Arm 2 (adopted design): the shared manifest stays byte-identical (the
pre-memory gate results remain authoritative, no prompt drift by
construction). The MEMORY_RE intercept marks the turn
(`spec["_plan_directive"] = "memory"`), and tool_flow plans memory turns
against a dedicated MEMORY_MANIFEST_DIRECTIVE — remember / forget / note
+ the standard null contract — instead of the gate shortlist. Memory
capture never needs MCP tools, and the 3-line manifest is easier for the
4B planner than 30. validate_tool/run_tool branches unchanged. tool_flow
gains an optional spec arg (mcp_smoke's 2-arg calls stay valid).

Bars re-stated for arm 2: B1-B3, B5 unchanged; B4 becomes (a) the three
frozen .b9 gates re-run fresh on the reverted manifest and PASS, (b) the
capability suite (27 scenarios incl. 3 memory) all green.

## RESULTS (2026-10-07): ADOPTED with one retraction and one incident

**B1 routing PASS.** "Remember that I'm allergic to peanuts" lands
think.tool with note="memory intercept"; "Keep in mind…", "From now on…",
"Forget what I said…" all force the tool flow; "Do you remember my name?"
stays direct; "Don't forget my face" stays vision (FACE_INTENT_RE guard).

**B2 store→recall PASS.** Suite remember_store: planner picks `remember`
under the scoped manifest, fact lands in memory.json.
remember_recall_fresh: a brand-new Session asks "What allergies do I
have?" — no tool call, and the answer is "You are allergic to peanuts…"
from the digest alone (no history).

**B3 forget PASS.** "Forget what I said about peanuts" → forget tool,
keyword removal verified in memory.json, confirm reply quotes the removed
item.

**B5 planner pick PASS.** remember/forget picked on all suite memory
frames; facts and notes both surface in the digest.

**B4b suite PASS: 27/27** (24 prior + 3 memory), run 6.

**B4a gates: superseded — two findings.**

1. **RETRACTION (Amendment 1's arm-1 rejection).** Determinism check: two
   fresh gate runs on the identical code state (rerun3, rerun4) return
   byte-identical results (fs 36/64, git 45/64, time 24/32). Pool
   regeneration is deterministic per tool set, so equal tool sets must
   give equal results — which means the fresh runs are simply NOT on the
   same tool set as the registered run: the spawned tools/list no longer
   matches the cached index (hash 473e375b). Arm-1's fs 35/64 vs arm-2's
   36/64 is 1 case of noise on the SAME (changed) ground, and the "manifest
   growth costs 9 points" claim is WITHDRAWN — the registered 0.6406 was
   measured on a different tool set, not a better manifest. Arm 2 stays
   adopted on its structural merit: the shared manifest stays
   byte-identical to the eval-validated state, so deployment-gate currency
   is preserved by construction instead of by constant re-measurement.
2. **INCIDENT (mcp ground flapping; registered in
   PROTOCOL-mcp-selection.md same day).** With unpinned `npx -y` / `uvx`
   resolution the enabled MCP tool set changes across spawns; gate
   verdicts are version-scoped, and on today's fresh ground filesystem
   FAILS the registered bar (pos 0.5625 < 0.60, extra schema failures —
   the newly-resolved server version changed schemas) while git/time
   PASS. The memory feature is exonerated: fs fails with (35-36/64) and
   without the memory changes identically, and git/time are
   byte-identical across arms. Resolution path: pin server versions and
   re-run the three gates on the pinned ground as the new registered
   verdicts. Config edits are the user's call.

Deployment state: production server restarted on the adopted code (memory
intercept + scoped plan directive + recall digest), 27/27 suite green.
