"""Frozen evals for voice-driven browser use (PROTOCOL-browser-use.md).

Run against the live serving stack AFTER `make record-router` (G1 needs
the new root.browser refs):

    make eval-browser

Gates (bars pre-registered in the protocol):
- G1 ROUTE_EVAL >= 12/14  AND the frozen root eval (eval_router) >= 56/62
  (run separately; this file prints the browser side only).
- G2 STOP: >= 18/20 (stop side and non-stop side scored by 1-NN margin;
  a "stop" prediction counts only when the stop side wins).
- G3 GOAL: >= 18/20 AND median |margin| over correct pairs >= 0.02,
  where margin = sim(best GOAL_YES) - sim(best GOAL_NO). Fail -> the
  slot-4 LLM judge ships instead (protocol decision 7).

GOAL_PAIRS excerpts quote the fixture pages verbatim (tools/
fixtures_browser/); the eval set is frozen — edits are protocol changes.
"""
import json
import re
import sys

import numpy as np
import requests

sys.path.insert(0, "tools")
from browser_pools import GOAL_NO, GOAL_YES, NOT_STOP, STOP  # noqa: E402

LLAMA = "http://127.0.0.1:8998"
TREE = "tools/decision_tree.json"
REFS = "models/router_embeddings.npz"
HEADS = "models/arm_C_heads.npz"

G1_BAR = 12
G2_BAR = 18
G3_BAR = 18
G3_MARGIN = 0.02

# (utterance, expected root class) — disjoint from every pool, including
# the new browser pool. Negatives deliberately cover the classes the
# browser pool could steal from (tool-ish, think-ish, vision, social).
ROUTE_EVAL = [
    ("Open the python docs and find how keyword arguments work.", "browser"),
    ("Look up the Eiffel Tower on Wikipedia and tell me its height.", "browser"),
    ("Go to github and check what's trending.", "browser"),
    ("Search for the population of Osaka on Wikipedia.", "browser"),
    ("Open the local file shop.html and find the price of the grinder.", "browser"),
    ("Check the python docs for the syntax of list comprehensions.", "browser"),
    ("Open the local file news.html and tell me the top headline.", "browser"),
    ("Look up the cast of the movie Alien on IMDb.", "browser"),
    ("What's the capital of France?", "direct"),
    ("Set a timer for 5 minutes.", "think"),
    ("Remind me to call mom at 6.", "think"),
    ("Should I take the bus or walk?", "decide"),
    ("What do you think about my new glasses?", "vision"),
    ("I'm exhausted after work today.", "social"),
]

# (task, page excerpt, label) — excerpts are real fixture text.
GOAL_PAIRS = [
    ("tell me the headline of the second article",
     "Solar panels hit record efficiency Researchers reported 40.1 "
     "percent efficiency in lab tests this week, beating the previous "
     "record by two full points.", "yes"),
    ("tell me the top headline",
     "The Daily Circuit Home Archive Fusion startup claims net gain "
     "again Solar panels hit record efficiency City approves night "
     "market Independent coverage of science and the city.", "yes"),
    ("find the price of the AeroPress Go",
     "AeroPress Go Price: $39.95 Travel press with mug included. In "
     "stock — ships tomorrow.", "yes"),
    ("find the price of the AeroPress Go",
     "Back to results AeroPress Go Price: $39.95", "yes"),
    ("find when backups run",
     "Backup schedule Backups run automatically every Sunday at 02:00. "
     "Incremental snapshots are kept for 30 days; full snapshots for "
     "one year.", "yes"),
    ("find when backups run",
     "BackupGuide documentation Getting started Restores FAQ Backup "
     "schedule Backups run automatically every Sunday at 02:00.", "yes"),
    ("submit the feedback form as Alice",
     "Thanks! Your feedback was received. Name: Alice Message: great "
     "app", "yes"),
    ("what efficiency did the solar panels reach",
     "Researchers reported 40.1 percent efficiency in lab tests this "
     "week.", "yes"),
    ("tell me the headline of the second article",
     "Commercial panels are expected to follow within three years.",
     "yes"),
    ("find the storage quota in the backup guide",
     "Storage limits The default quota is 50 GB per machine.", "yes"),
    ("find the price of the AeroPress Go",
     "BrewMart Search results for grinder — 3 items. Comandante C40 "
     "AeroPress Go Timemore C2 Prices shown on each product page.", "no"),
    ("find the price of the AeroPress Go",
     "Solar panels hit record efficiency Researchers reported 40.1 "
     "percent efficiency.", "no"),
    ("find when backups run",
     "The Daily Circuit Fusion startup claims net gain again Solar "
     "panels hit record efficiency.", "no"),
    ("log in as alice",
     "Portal login Username Password Sign in Unauthorized access is "
     "prohibited.", "no"),
    ("tell me the headline of the second article",
     "BrewMart Search results for grinder — 3 items.", "no"),
    ("submit the feedback form as Alice",
     "Send us feedback Name Email Message Submit feedback", "no"),
    ("find when backups run",
     "Storage limits The default quota is 50 GB per machine.", "no"),
    ("tell me the headline of the second article",
     "Thanks! Your feedback was received.", "no"),
    ("find the price of the AeroPress Go",
     "Portal login Username Password Sign in.", "no"),
    ("submit the feedback form as Alice",
     "Portal login Username Password Sign in Unauthorized access is "
     "prohibited.", "no"),
]


def norm_router_text(t):
    """Must match web/server.py and tools/record_router.py."""
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
    cfg = json.load(open(TREE))
    z = np.load(REFS)
    if "root.browser" not in z:
        print("G1 SKIP: root.browser refs missing (run make record-router)")
        return None
    classes = list(cfg["nodes"]["root"]["pools"])
    cents = norm(np.stack(
        [norm(project(z[f"root.{c}"])).mean(0) for c in classes]))
    ok, misses = 0, []
    for text, want in ROUTE_EVAL:
        v = norm(project(emb(text)[None, :]))[0]
        got = classes[int(np.argmax(cents @ v))]
        if got == want:
            ok += 1
        else:
            misses.append((text, want, got))
    print(f"\nG1 route: {ok}/{len(ROUTE_EVAL)} (bar {G1_BAR})")
    for t, w, g in misses:
        print(f"  MISS want={w} got={g}: {t!r}")
    return ok >= G1_BAR


def _pool_scores(line, yes_texts, no_texts):
    v = norm(project(emb(line)[None, :]))
    yv = norm(project(np.stack([emb(t) for t in yes_texts])))
    nv = norm(project(np.stack([emb(t) for t in no_texts])))
    return float((yv @ v.T).max()), float((nv @ v.T).max())


def gate_stop():
    ok, misses = 0, []
    for text in STOP:
        s, n = _pool_scores(text, STOP, NOT_STOP)
        if s > n:
            ok += 1
        else:
            misses.append((text, "stop", round(s, 3), round(n, 3)))
    for text in NOT_STOP:
        s, n = _pool_scores(text, STOP, NOT_STOP)
        if n >= s:
            ok += 1
        else:
            misses.append((text, "not_stop", round(s, 3), round(n, 3)))
    print(f"\nG2 stop pool: {ok}/{len(STOP) + len(NOT_STOP)} "
          f"(bar {G2_BAR})")
    for t, w, s, n in misses:
        print(f"  MISS want={w} yes={s} no={n}: {t!r}")
    return ok >= G2_BAR


def gate_goal():
    ok, margins, misses = 0, [], []
    for task, excerpt, want in GOAL_PAIRS:
        line = f"Task: {task}. Page: {excerpt}"
        s, n = _pool_scores(line, GOAL_YES, GOAL_NO)
        got = "yes" if s > n else "no"
        if got == want:
            ok += 1
            margins.append(abs(s - n))
        else:
            misses.append((task[:40], want, got, round(s, 3), round(n, 3)))
    med = float(np.median(margins)) if margins else 0.0
    print(f"\nG3 goal watch: {ok}/{len(GOAL_PAIRS)} (bar {G3_BAR}), "
          f"median |margin| {med:.3f} (bar {G3_MARGIN})")
    for t, w, g, s, n in misses:
        print(f"  MISS want={w} got={g} yes={s} no={n}: {t!r}")
    return ok >= G3_BAR and med >= G3_MARGIN


def main():
    results = {}
    results["G1"] = gate_route()
    results["G2"] = gate_stop()
    results["G3"] = gate_goal()
    print()
    for k, v in results.items():
        if v is not None:
            print(f"{k}: {'PASS' if v else 'FAIL'}")
    return 0 if all(v is not False for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
