"""Follow-up routing eval: context-dependent fragments vs self-contained rewrites.

PRE-REGISTERED alongside the rewrite implementation (this commit lands
protocol + code together). Disclosed priors: an exploratory probe of six
similar fragments ran against the live router first; and during bring-up
the first table was scored against hand-written golds, two of which
contradicted the tree's own frozen e2e behavior (weather questions route
think.tool + honest fallback per tools/test_voice.py, not long_think).
Golds are therefore defined MECHANICALLY, before any rewrite result is
consulted:

    gold(fragment) := route of its frozen standalone equivalent

That is the design target stated in the proposal — a rewrite succeeds
when the fragment routes exactly like the self-contained sentence a user
would have said with no prior context. Tree-config gaps (a standalone
question that routes "wrong" with no history behind it) are thereby
excluded from this eval by construction; they belong to record_router /
tree-probe territory.

Registered bar (all must hold):
  1. Trigger recall:    needs_context() fires on 8/8 follow-up fragments.
  2. Trigger precision: needs_context() fires on 0/5 controls (self-contained
     or history-less utterances must not pay the rewrite tax). Amended
     during bring-up: the fifth control is the long "So…"-led narrative
     the e2e caught misfiring the original bare lead match — the shipped
     trigger is length-gated (<=4 words, or <=9 with a marker).
  3. Protocol guard: no eval fragment (normalized) may appear verbatim in
     ANY router pool (stricter than eval_router's own-node guard, cheap
     at this size).
  4. Bar: rewrite-routed accuracy vs mechanical gold >= 6/8 AND strictly
     greater than the raw-fragment baseline on the same cases, judged on
     the full production stack (root argmax + think subtree walk, refs
     models/router_embeddings.npz, head models/arm_C_heads.npz).
  5. Latency: every rewrite completes under its 1500 ms hard timeout
     (wall-clock bound asserted at 1600 ms).

The rewrite path under test is the live production code imported from
web/server.py (needs_context / rewrite_context / DecideReadout /
TreeWalker) — the eval cannot drift from what ships.
"""

import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "web"))
import server  # noqa: E402  (production code under test; globals init in main())

TREE = os.path.join(HERE, "..", "tools", "decision_tree.json")

# (history, fragment, standalone equivalent) — frozen. The standalone form
# is what a user with no prior context would say; its route IS the gold.
FOLLOWUP_EVAL = [
    ([("what's the weather in jakarta",
       "jakarta is around 31 degrees and humid today")],
     "What about on Tuesday?",
     "What's the weather in Jakarta on Tuesday?"),
    ([("remember that i parked on level two",
       "noted, you parked on level two")],
     "Actually, make it level three instead.",
     "Remember that i parked on level three."),
    ([("set a timer for ten minutes",
       "timer started, ten minutes")],
     "Can you make it 20 minutes instead?",
     "Set a timer for twenty minutes."),
    ([("how long is the flight from jakarta to bali",
       "roughly one hour and fifty minutes nonstop")],
     "And what about the return flight?",
     "How long is the return flight from bali to jakarta?"),
    ([("what do you think about my new glasses",
       "they suit you, the round frames work well")],
     "How about the color?",
     "What do you think about the color of my new glasses?"),
    ([("remind me to call the dentist tomorrow",
       "reminder saved for tomorrow")],
     "Actually change it to Friday morning.",
     "Remind me to call the dentist on friday morning."),
    ([("how much would a thirty kilogram suitcase weigh on mars",
       "about eleven and a half kilograms, mars gravity is 0.38 g")],
     "What about on the moon?",
     "How much would a thirty kilogram suitcase weigh on the moon?"),
    ([("add oat milk to the shopping list",
       "added oat milk to the list")],
     "And also coffee beans?",
     "Add coffee beans to the shopping list."),
]

# (history, utterance) — must NOT trigger the rewrite. The last control is
# the e2e regression that amended the registered trigger: the long
# "So…"-led narrative fired the bare lead match and its rewrite dragged
# social -> think.long_think; the trigger is now length-gated (<=4 words
# always; <=9 words with a connective/hint marker).
CONTROLS = [
    ([("what's the weather in jakarta",
       "jakarta is around 31 degrees and humid today")],
     "What's the capital of France?"),
    ([("set a timer for ten minutes", "timer started, ten minutes")],
     "Set a timer for five minutes."),
    ([("what do you think about my new glasses",
       "they suit you, the round frames work well")],
     "What do you think about my new scarf?"),
    ([], "What about it?"),
    ([("set a timer for ten minutes", "timer started, ten minutes")],
     "So yesterday I was walking to the store and this guy comes up to me "
     "out of nowhere and starts telling me his whole life story and "
     "honestly I could not get away, it was something else."),
]


def main():
    # server.py paths (MODELS/TOOLS) are relative to web/ — resolve as in
    # production (server runs with cwd=web)
    os.chdir(os.path.join(HERE, "..", "web"))
    server.READOUT = server.DecideReadout()
    server.WALKER = server.TreeWalker(server.READOUT._project)
    tree = json.load(open(TREE))
    failures = []

    # 3. protocol guard: fragments verbatim in any pool
    all_pool = {server.norm_router_text(t)
                for node in tree["nodes"].values()
                for ts in node.get("pools", {}).values()
                for t in ts}
    clash = [f for _, f, _ in FOLLOWUP_EVAL
             if server.norm_router_text(f) in all_pool]
    if clash:
        print(f"PROTOCOL VIOLATION — fragments verbatim in pools: {clash}")
        return 1

    # 2. trigger precision on controls
    for hist, u in CONTROLS:
        if server.needs_context(u, bool(hist)):
            failures.append(f"control triggered rewrite: {u!r}")

    # 1. trigger recall on fragments
    for hist, f, _ in FOLLOWUP_EVAL:
        if not server.needs_context(f, bool(hist)):
            failures.append(f"trigger missed fragment: {f!r}")

    def route_mode(text):
        emb, _ = server.READOUT.embed(server.norm_router_text(text))
        cls, spec, _ = server.WALKER.route(emb)
        if spec.get("action") == "subtree":
            sub_cls, _, _ = server.WALKER.route(emb, spec["node"])
            return f"{cls}.{sub_cls}"
        return cls

    # 4. raw baseline vs rewrite, full production stack; gold is the route
    # of the frozen standalone equivalent (measured, not assumed)
    raw_ok = rw_ok = 0
    print(f"\n{'fragment':40s} {'gold':16s} {'raw':16s} "
          f"{'rewrite route':16s} rewrite")
    for hist, frag, standalone in FOLLOWUP_EVAL:
        gold = route_mode(standalone)
        raw = route_mode(frag)
        t0 = time.perf_counter()
        rw = server.rewrite_context(hist, frag)
        ms = (time.perf_counter() - t0) * 1000
        # 5. hard-timeout bound (rewrite_context returns None on timeout)
        if ms > 1600:
            failures.append(f"rewrite over budget ({ms:.0f} ms): {frag!r}")
        if rw is None:
            failures.append(f"rewrite failed/timed out: {frag!r}")
            rwm, rwp = "—", "—"
        else:
            rwm = route_mode(rw)
            rwp = "PASS" if rwm == gold else "miss"
        rawp = "PASS" if raw == gold else "miss"
        raw_ok += raw == gold
        rw_ok += rwm == gold
        print(f"\n  fragment : {frag!r}\n  standalone: {standalone!r} -> {gold}")
        print(f"  raw      : {raw!r} {rawp}\n  rewrite  : "
              f"{rwm!r} {rwp} — {rw!r} ({ms:.0f} ms)")

    print(f"\nraw baseline:     {raw_ok}/{len(FOLLOWUP_EVAL)}")
    print(f"rewrite routing:  {rw_ok}/{len(FOLLOWUP_EVAL)}")
    print(f"trigger: recall {sum(1 for h, f, _ in FOLLOWUP_EVAL if server.needs_context(f, True))}/8, "
          f"precision {len(CONTROLS) - sum(1 for h, u in CONTROLS if server.needs_context(u, bool(h)))}/{len(CONTROLS)}")

    if rw_ok < 6 or rw_ok <= raw_ok:
        failures.append(f"registered bar not met: rewrite {rw_ok}/8 vs raw {raw_ok}/8 "
                        "(bar: >= 6/8 and strictly > raw)")
    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — registered bar met")
    return 0


if __name__ == "__main__":
    sys.exit(main())
