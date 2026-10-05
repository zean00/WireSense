# Protocol: voice-driven browser use through the decide layer (2026-10-05)

Pre-registered BEFORE any gate data. Commit order: this file + fixtures +
frozen evals first, implementation second, adjudicated results third.
Evals are frozen once committed; any later edit is a protocol change
noted in git.

## Motivation

The assistant answers from model weights and local tools, but cannot act
on live information: "check the docs for X", "find the price of Y" fall
to the think path, which has no page access and will confabulate rather
than say it looked. Browser use closes that with a real lookup loop —
and it is the first capability whose per-step decisions are made the way
the house already makes decisions: the decide layer (slot-6 readout +
tree), not a hosted agent stack.

Design input: two surveyed projects (jev-browser, typesafe-computer-use)
converge on the same shape — a small decision model picks ONE action per
step, code owns the loop, stop gates run before execution, free text
comes only from a separate writer, degradation is visible. We adopt the
pattern, not their paid decision APIs: per user instruction, **no
external API of any kind (TypeSafe/Jev included) — every judgment is the
local stack**.

## Decisions (frozen)

1. **Scope.** The agent acts only inside its own Playwright-managed
   Chromium (headless by default; a `browser.headed` config flag exists
   for later, unregistered). No desktop control, no OS input synthesis —
   this box is Wayland and the user is usually using it. Browser-only
   was the registered v1 cut.
2. **No external services.** Task parse, step choice, writer, goal
   watch, final answer: slot-5 main decode (4B), slot-4 judge, slot-6
   embeddings. Nothing leaves the machine. Playwright ships its own
   browser binary; it talks to pages, not to model APIs.
3. **Intake.** New root class `browser` in tools/decision_tree.json
   (pools committed, refs re-recorded via `make record-router` — refs
   are derived, never committed). The routed turn parses the task with
   one slot-5 JSON call: `{"site": "docs.python.org" | null, "query":
   "…" | null, "task": "…"}`. site -> `https://<site>`; query only ->
   `https://lite.duckduckgo.com/lite/?q=`; neither or invalid JSON
   twice -> honest failure reply, no task. A bare `<name>.html` site
   value resolves to the committed fixture directory over file:// —
   that is how every gate task addresses its page (deterministic, no
   live web in any gate). **Routing boundary:** transactional commands
   ("search for flights", "book", "order") stay with the existing
   tool/think classes — the browser class owns page LOOKUP tasks ("go
   to X and find Y", "look up Z on W", "open the local page and…").
4. **Loop owner is code.** The LLM proposes at most one action per step;
   the loop validates, checks stop gates, executes. The LLM never sees
   a screenshot in v1 — state is the DOM: url, title, ≤ 40 visible
   interactive candidates (role, accessible name, index), and a ~1200
   char text excerpt (goal-watch + answer grounding only).
5. **Mutually exclusive action set** (typesafe-computer-use lesson):
   `{"action":"click","n":i}` | `{"action":"type","n":i,"text":"…"}`
   | `{"action":"scroll"}` | `{"action":"done","answer":"…"}`. Anything
   else is junk; two junk steps in a row end the task honestly.
6. **Writer separation.** The readout never generates; the only
   generators are slot-5 calls (task parse, step choice+writer, answer).
   Each type action is one short write from the step call itself.
7. **Goal watch — registered decision rule.** Finish requires the step
   LLM's `done` AND an independent watcher agreeing. The watcher is the
   decide layer itself: a frozen YES/NO state-description pool scored by
   readout 1-NN cosine on slot 6 (~75 ms, free) — the Noul analog. It
   ships iff its frozen eval passes gate G3; otherwise the LLM judge on
   slot 4 ships instead and the readout head is recorded as NOT adopted.
   Watcher YES twice in a row also finishes (catches a done-happy LLM).
   Stop and goal pools live as committed text in tools/browser_pools.py
   (single source for server and eval; embedded lazily on first browser
   task, no npz refs — routing-class refs keep their npz semantics).
8. **Stuck watch is code, no model.** url + candidate-name hash
   unchanged two consecutive steps -> force one scroll; three -> stop
   and report partials honestly.
9. **Budgets.** 10 steps, 90 s wall, 8 s per LLM call, snapshot cap 40
   candidates (scroll first if more). Exceeding any budget ends the
   task with an honest partial answer or honest failure.
10. **Safety.** (a) Credential fields (type=password, name/autocomplete
    matching password/card/cvv/ssn) are never typed; if reached, the
    task ends honestly ("I can't type passwords."). (b) Submit-class
    clicks (submit/pay/buy/checkout/order/delete/confirm/sign in/place
    order/purchase/send) park the loop and speak a read-back — the M2
    machinery generalized: `pending = {"kind":"browser", …}`, YES
    resumes the click, NO cancels the task, exact resolution vocabulary
    unchanged. (c) Page excerpts are volatile context; they live only
    in the task trace on disk, never in memory.json or long-term state.
    (d) The final answer may only restate what a step's page excerpt
    contained (the step prompt is instructed so; adjudication
    spot-checks answers against traces).
11. **Mid-task voice.** While a task runs, the next final goes to a
    frozen stop-pool (readout 1-NN margin vs a non-stop pool, gate G2):
    stop -> cancel + "Stopped."; anything else gets a canned hold line
    and is not otherwise processed — registered v1 UX cost (no concurrent
    turns during a task; slot-5 stays owned by the loop). G2 failure
    demotes stopping to the exact resolution vocabulary (stop/cancel
    words only).
12. **Turn shape.** Intake turn: route event `browser`, feedback filler
    spoken ("browser" feedback texts), turn ends like a canned turn.
    Completion is out-of-turn (reminder-fire precedent): step events
    `browser_event` to the UI, then response_start/delta/done, TTS,
    history append. One progress line if the task passes 12 s.
13. **Observability.** Every step appends a `browser_event` (action,
    n, ms) and a run trace under /tmp/wiresense_browser/<id>/ (state
    payloads, step JSONs, final screenshot) — the replay folder pattern;
    offline gating uses saved payloads, never the live web.
14. **Failure honesty.** Navigation error, budget stop, junk steps,
    browser-support missing (playwright absent) — all end in a spoken
    honest failure; a browser task never hallucinates success. If
    playwright is missing, the route still matches and fails honest.
15. **Regression.** Adding the root class may steal existing routes:
    the frozen 62-item root eval re-runs unmodified (bar: ≥ 56/62, no
    edited expectations; any new miss is adjudicated in Results). Pool
    refinement BEFORE the gate run is design iteration and allowed;
    after the first recorded gate run, pools are frozen too.

## Gates

- **G1 routing.** New frozen browser eval (14 held-out utterances,
  tools/eval_browser.py) ≥ 12/14; frozen root eval ≥ 56/62 unmodified.
- **G2 stop pool.** Frozen 10-stop/10-non-stop set ≥ 18/20, else the
  exact-vocabulary fallback ships (decision rule, not a choice).
- **G3 goal watch.** Frozen 20 (task, excerpt, label) pairs quoting
  real fixture-page text ≥ 18/20 with YES/NO margin ≥ 0.02, else LLM
  judge ships. Decision recorded in Results either way.
- **G4 e2e tasks.** Six fixture tasks (file:// pages, deterministic —
  no live web in any gate): each must finish with the expected answer
  substring spoken, ≤ 10 steps, ≤ 45 s. Bar ≥ 5/6; misses adjudicated.
- **G5 safety.** Submit read-back + YES completes, NO cancels with no
  navigation, password field never typed (honest end) — 3/3.
- **G6 barge-in.** Stop utterance mid-task cancels ≤ 2 s, the next
  turn routes normally afterwards.
- **G7 latency.** Filler ≤ 1.5 s from final; median step ≤ 1.2 s;
  fixture task median ≤ 30 s.
- **G8 regression.** Default test_voice suite 14/14 unchanged; units
  (test_transcript_safety) 31/31 unchanged.

## Known limits (registered, not fixed in v1)

Single-tab, single task at a time; no downloads, no auth, no iframes
beyond top-frame DOM, no JS-heap introspection; duckduckgo-lite is the
only search entry and live-web behavior is smoke-tested, never gated
(network nondeterminism cannot be a frozen eval — the replay-folder
pattern exists for that reason).
