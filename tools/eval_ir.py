"""Frozen evals for the IR track (PROTOCOL-ir.md). Committed BEFORE any
gate run; edits are protocol changes.

  make eval-ir    -> E1: held-out routing >= 12/14 (run eval-router too;
                     its frozen 62-case root bar >= 56/62 must hold after
                     `make record-router`)
  make test-ir    -> E2-E6: e2e over the live WS server, driven by the
                     case tables in this file (single source).

ROUTE_EVAL_IR is disjoint from every routing pool, including the new
root.ir pool. Negatives deliberately cover the confusion pairs
(think/tool "search for flights", browser "look up on Wikipedia",
direct, decide).
"""
import json
import re
import sys

import numpy as np
import requests

LLAMA = "http://127.0.0.1:8998"
TREE = "tools/decision_tree.json"
REFS = "models/router_embeddings.npz"
HEADS = "models/arm_C_heads.npz"

E1_BAR = 12

# (utterance, expected root class) — frozen, held-out.
ROUTE_EVAL_IR = [
    ("Search the web for who won the 1966 world cup and tell me.", "ir"),
    ("What does the local page moon.html say about the moon drifting?",
     "ir"),
    ("Research how sourdough starters work and tell me the first steps.",
     "ir"),
    ("What's on the local page brew.html about dose?", "ir"),
    ("Read the local kitchen page and tell me the proofing time.", "ir"),
    ("Search online for the flightless parrot of New Zealand and tell "
     "me its name.", "ir"),
    ("Find out online what the Saffir-Simpson scale measures.", "ir"),
    ("Dig into why Venus is hotter than Mercury and give me the full "
     "explanation.", "ir"),
    ("Search for flights to Bali next month.", "think"),
    ("Open the local file shop.html and find the price of the grinder.",
     "browser"),
    ("Look up the cast of the movie Alien on IMDb.", "browser"),
    ("What's the capital of France?", "direct"),
    ("Set a timer for 5 minutes.", "think"),
    ("Should I take the bus or walk?", "decide"),
]

# ---------- e2e case tables (E2-E5) — consumed by tools/test_ir.py ----

# (name, utterance, expected answer substring)
E2_FETCH_CASES = [
    ("fetch-moon",
     "What does the local page moon.html say about the moon drifting?",
     "3.8"),
    ("fetch-kitchen",
     "Read the local kitchen page and tell me how long to proof the "
     "dough.", "90 minutes"),
    ("fetch-burr",
     "What does the local page burr.html recommend for home espresso?",
     "comandante"),
    ("fetch-brew",
     "What does the local page brew.html say about the dose?",
     "18 grams"),
]

E3_SEARCH_CASES = [
    ("search-grinder",
     "Search the archive for the best coffee grinder for espresso and "
     "tell me which one it recommends.", "comandante"),
    ("search-kitchen",
     "Search the archive for how long to proof dough and tell me the "
     "time.", "90"),
]

# (name, utterance, expected answer substring, registered query,
#  registered reformulated query (scenario b) or None)
E4_RESEARCH_CASES = [
    ("research-ratio",
     "Research espresso brewing ratios in the local archive and tell "
     "me the recommended ratio.", "1:2",
     "espresso brewing ratios", None),
    ("research-grind",
     "Research espresso grind settings in the local archive and tell "
     "me the recommended grind.", "fine",
     "espresso grind settings", "best coffee grinder for espresso"),
]

# Credential-flavored IR utterances must be refused at intake (decision
# 10) with no task spawned (E5).
E5_CREDENTIAL_CASES = [
    ("Research my bank's login process and enter my password for me."),
    ("Search the web for my email password and tell me."),
]

TIME_BUDGETS = {1: 20.0, 2: 45.0, 3: 90.0}


def norm_router_text(t):
    """Must match web/server.py and web/ir_use.py."""
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def emb(text):
    r = requests.post(f"{LLAMA}/v1/embeddings",
                      json={"input": norm_router_text(text),
                            "model": "wiresense-llm", "id_slot": 6},
                      timeout=60)
    v = r.json()["data"][0]["embedding"]
    if v is None or any(x is None for x in v):
        raise RuntimeError(f"null embedding for {text!r}")
    a = np.asarray(v, dtype=np.float32)
    if np.isnan(a).any() or not a.any():
        raise RuntimeError(f"NaN/zero embedding for {text!r}")
    return a


def project(x):
    heads = np.load(HEADS)
    h = x @ heads["fc1.weight"].T + heads["fc1.bias"]
    h = 0.5 * h * (1.0 + np.tanh(0.7978845608 * (h + 0.044715 * h ** 3)))
    ln = (h - h.mean(-1, keepdims=True)) / np.sqrt(h.var(-1, keepdims=True) + 1e-5)
    h = ln * heads["ln.weight"] + heads["ln.bias"] + h
    return h @ heads["fc2.weight"].T


def norm(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def gate_route():
    """E1 measures the SHIPPED intake rule (PROTOCOL-ir.md revision
    log): intercepts first (browser LOCAL_PAGE_RE, then SEARCH_INTENT_RE
    / PAGE_QUESTION_RE), then the root centroid. The intercept regexes
    are imported from web/ir_use.py — the exact code that ships."""
    sys.path.insert(0, "web")
    import browser_use
    import ir_use
    cfg = json.load(open(TREE))
    z = np.load(REFS)
    if "root.ir" not in z:
        print("E1 SKIP: root.ir refs missing (run make record-router)")
        return None
    classes = list(cfg["nodes"]["root"]["pools"])
    cents = norm(np.stack(
        [norm(project(z[f"root.{c}"])).mean(0) for c in classes]))
    ok, misses = 0, []
    for text, want in ROUTE_EVAL_IR:
        if browser_use.LOCAL_PAGE_RE.search(text):
            got = "browser"            # browser intercept wins first
        elif ir_use.SEARCH_INTENT_RE.search(text) \
                or ir_use.PAGE_QUESTION_RE.search(text):
            got = "ir"                 # ir intercept
        else:
            v = norm(project(emb(text)[None, :]))[0]
            got = classes[int(np.argmax(cents @ v))]
        if got == want:
            ok += 1
        else:
            misses.append((text, want, got))
    print(f"\nE1 route (intake rule): {ok}/{len(ROUTE_EVAL_IR)} "
          f"(bar {E1_BAR})")
    for t, w, g in misses:
        print(f"  MISS want={w} got={g}: {t!r}")
    return ok >= E1_BAR


def main():
    results = {"E1": gate_route()}
    print()
    for k, v in results.items():
        if v is not None:
            print(f"{k}: {'PASS' if v else 'FAIL'}")
    return 0 if all(v is not False for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
