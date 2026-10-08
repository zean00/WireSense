#!/usr/bin/env python
"""Intent-inference input-shape eval (docs/PROTOCOL-intent-context.md,
pre-registered before any data).

Does the root decision readout need conversation history? Same frozen
scoring machinery as tools/eval_router.py (norm_router_text -> slot-6
embeddings -> arm-C heads -> class centroids, argmax cosine), scored
under input shapes:

  Part A (frozen 88):  BARE / PROMPT0 / HIST3 / HIST8 / HIST16
  Part B (24 new anaphora pairs, frozen here): BARE vs HIST1
  Latency: wall ms + /tokenize prompt_n per scored call.

Usage: tools/eval_router_context.py   (from anywhere; paths absolute)
"""
import json
import os
import sys
import time
from collections import defaultdict

import numpy as np
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "web"), ROOT,
          os.path.join(ROOT, "tools"),
          os.path.join(ROOT, "tools", "prefill_probe")):
    sys.path.insert(0, p)
os.chdir(os.path.join(ROOT, "web"))   # server.py's MODELS=../models
import server as ws  # noqa: E402
os.chdir(ROOT)                        # eval_router constants are ROOT-relative

from eval_router import EVAL, THINK_EVAL, norm_router_text, LLAMA  # noqa
from large_fixture import PAIRS_LARGE as PAIRS  # noqa: E402  sha-pinned

TREE = os.path.join(ROOT, "tools", "decision_tree.json")
REFS = os.path.join(ROOT, "models", "router_embeddings.npz")
HEADS = os.path.join(ROOT, "models", "arm_C_heads.npz")
OUT = os.path.join(ROOT, "tools", "results_intent_context.json")

FIXTURE_SHA = "b42bde63a0108bc0ccbfedfd7f3c2f5312c948d4aa1d05a5d363ad4cb20b84c4"

# ---- Part B: new frozen anaphora set (pre-registered verbatim) --------
# (node, kind, t1 history utterance, t2 scored utterance, gold)
ANAPH = [
    # vision-deictic: t2 is genuinely ambiguous bare; gold vision
    ("root", "vision", "Here's the fiddle leaf I just bought.",
     "how much light does it need?", "vision"),
    ("root", "vision", "I'm holding up my grandmother's ring.",
     "what karat is it?", "vision"),
    ("root", "vision", "This is the circuit I wired yesterday.",
     "do you see any loose connection?", "vision"),
    ("root", "vision", "Look at this rash on my arm.",
     "does it look serious?", "vision"),
    ("root", "vision", "Here's my resume on the screen.",
     "is the layout okay on it?", "vision"),
    ("root", "vision", "I'm showing you my car's engine bay.",
     "what is that leaking?", "vision"),
    ("root", "vision", "This is the painting I made.",
     "which colors did I use most?", "vision"),
    ("root", "vision", "Here's the label on the bottle.",
     "how many milligrams per serving?", "vision"),
    ("root", "vision", "Look at my desk setup.",
     "is the monitor too low?", "vision"),
    ("root", "vision", "The barber gave me a fade.",
     "is the back even?", "vision"),
    # tool-anaphoric: t2 refers to t1's tool object; gold tool
    ("think", "tool", "Set a timer for the rice, twelve minutes.",
     "actually cancel that one.", "tool"),
    ("think", "tool", "Set a timer for twenty minutes.",
     "and another one for ten minutes after it.", "tool"),
    ("think", "tool", "Remind me to call the dentist on friday.",
     "actually make it next wednesday instead.", "tool"),
    ("think", "tool", "Add milk to the shopping list.",
     "and eggs too while you're at it.", "tool"),
    ("think", "tool", "Wake me up at seven.",
     "actually make it seven thirty.", "tool"),
    ("think", "tool", "Play the beatles.",
     "skip this song.", "tool"),
    ("think", "tool", "Turn off the living room lights.",
     "now dim them to half.", "tool"),
    ("think", "tool", "What's the weather tomorrow?",
     "and what about the day after?", "tool"),
    # self-contained controls (root): gold unambiguous bare
    ("root", "control", "My sister just moved to Lisbon.",
     "what's the best time of year to visit there?", "direct"),
    ("root", "control", "I've been baking sourdough all month.",
     "how much salt is in a standard loaf recipe?", "direct"),
    ("root", "control", "We're choosing between two job offers.",
     "should salary or team matter more?", "decide"),
    ("root", "control", "Training for my first ten k.",
     "what pace should long runs be?", "direct"),
    ("root", "control", "I started a new night shift schedule.",
     "honestly I'm exhausted all the time now.", "social"),
    ("root", "control", "Reading a biography of Ada Lovelace.",
     "what did she actually invent or create?", "direct"),
]

_tok_cache = {}


def prompt_for(history, text):
    return ws.build_prompt(history, text, None, False)


def tokens_of(text):
    if text not in _tok_cache:
        r = requests.post(f"{LLAMA}/tokenize", json={"content": text},
                          timeout=60)
        _tok_cache[text] = len(r.json()["tokens"])
    return _tok_cache[text]


def emb_timed(text):
    """Same request as eval_router.emb, with wall-clock latency."""
    t0 = time.perf_counter()
    r = requests.post(f"{LLAMA}/v1/embeddings",
                      json={"input": norm_router_text(text),
                            "model": "wiresense-llm", "id_slot": 6},
                      timeout=60)
    v = r.json()["data"][0]["embedding"]
    dt = (time.perf_counter() - t0) * 1000
    if v is None or any(x is None for x in v):
        raise RuntimeError("null embedding")
    a = np.asarray(v, dtype=np.float32)
    if np.isnan(a).any() or not a.any():
        raise RuntimeError("NaN/zero embedding")
    return a, dt


def main():
    import hashlib
    blob = "\n".join(f"{u}\n{a}" for u, a in PAIRS)
    sha = hashlib.sha256(blob.encode()).hexdigest()
    assert sha == FIXTURE_SHA, f"fixture drift: {sha}"

    tree = json.load(open(TREE))
    z = np.load(REFS)
    h = np.load(HEADS)

    def project(x):
        hh = x @ h["fc1.weight"].T + h["fc1.bias"]
        hh = 0.5 * hh * (1.0 + np.tanh(0.7978845608 * (hh + 0.044715 * hh ** 3)))
        ln = (hh - hh.mean(-1, keepdims=True)) / np.sqrt(hh.var(-1, keepdims=True) + 1e-5)
        hh = ln * h["ln.weight"] + h["ln.bias"] + hh
        return hh @ h["fc2.weight"].T + h["fc2.bias"]

    def norm(x):
        return x / np.linalg.norm(x, axis=-1, keepdims=True)

    def cents_for(node_id):
        classes = list(tree["nodes"][node_id]["classes"])
        cents = norm(np.stack([norm(project(z[f"{node_id}.{c}"])).mean(0)
                               for c in classes]))
        return classes, cents

    # pool-clash guard for the new t2 utterances (eval_router's rule)
    for node_id in ("root", "think"):
        node_texts = {norm_router_text(t)
                      for ts in tree["nodes"][node_id]["pools"].values()
                      for t in ts}
        clash = [t2 for n, _, _, t2, _ in ANAPH
                 if n == node_id and norm_router_text(t2) in node_texts]
        if clash:
            print(f"PROTOCOL VIOLATION — anaphora t2 in {node_id} pools: "
                  f"{clash}")
            return 1

    lat = defaultdict(list)
    out = {"protocol": "docs/PROTOCOL-intent-context.md",
           "fixture_sha256": sha, "part_a": {}, "part_b": {},
           "latency_ms": defaultdict(list), "prompt_n": {}}

    def score(node_id, items, arm, make_text):
        classes, cents = cents_for(node_id)
        idx = {c: i for i, c in enumerate(classes)}
        misses = []
        for gold, utt in items:
            text = make_text(utt)
            v, dt = emb_timed(text)
            pn = tokens_of(norm_router_text(text))
            lat_key = f"{arm}:{node_id}"
            out["latency_ms"][lat_key].append(round(dt, 1))
            out["prompt_n"].setdefault(lat_key, []).append(pn)
            vv = norm(project(v[None, :]))[0]
            pred = classes[int(np.argmax(cents @ vv))]
            if pred != gold:
                misses.append({"gold": gold, "pred": pred, "t2": utt})
        acc = 1 - len(misses) / len(items)
        print(f"{arm:9s} {node_id:5s}: {len(items) - len(misses)}/"
              f"{len(items)} = {acc:.1%}", flush=True)
        return {"acc": acc, "n": len(items), "misses": misses}

    # ---- Part A ----
    arms = [("BARE", lambda u: u),
            ("PROMPT0", lambda u: prompt_for([], u)),
            ("HIST3", lambda u: prompt_for(PAIRS[:3], u)),
            ("HIST8", lambda u: prompt_for(PAIRS[:8], u)),
            ("HIST16", lambda u: prompt_for(PAIRS[:16], u))]
    for arm, mk in arms:
        out["part_a"][arm] = {
            "root": score("root", [(g, u) for u, g in EVAL], arm, mk),
            "think": score("think", [(g, u) for u, g in THINK_EVAL],
                           arm, mk),
        }

    # ---- Part B ----
    for cond, mk in (("BARE", lambda t1, u: u),
                     ("HIST1", lambda t1, u: prompt_for([(t1, "")], u))):
        res = {}
        for kind in ("vision", "tool", "control"):
            node = "think" if kind == "tool" else "root"
            classes, cents = cents_for(node)
            pairs = [(t1, t2, gold) for n, k, t1, t2, gold in ANAPH
                     if k == kind]
            misses = []
            for t1, t2, gold in pairs:
                text = mk(t1, t2)
                v, dt = emb_timed(text)
                pn = tokens_of(norm_router_text(text))
                lat_key = f"ANAPH-{cond}:{kind}"
                out["latency_ms"][lat_key].append(round(dt, 1))
                out["prompt_n"].setdefault(lat_key, []).append(pn)
                vv = norm(project(v[None, :]))[0]
                pred = classes[int(np.argmax(cents @ vv))]
                if pred != gold:
                    misses.append({"t1": t1, "gold": gold, "pred": pred,
                                   "t2": t2})
            res[kind] = {"acc": 1 - len(misses) / len(pairs),
                         "n": len(pairs), "misses": misses}
            print(f"ANAPH-{cond:4s} {kind:7s}: {len(pairs) - len(misses)}/"
                  f"{len(pairs)} = {res[kind]['acc']:.1%}", flush=True)
        out["part_b"][cond] = res

    out["latency_ms"] = {
        k: {"median": float(np.median(v)), "n": len(v)}
        for k, v in out["latency_ms"].items()}
    out["prompt_n"] = {k: int(np.median(v))
                       for k, v in out["prompt_n"].items()}
    out["finished"] = time.strftime("%F %T")
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"wrote {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
