"""Held-out eval for the root node of the agentic decision tree
(tools/decision_tree.json).

Protocol: 56 utterances disjoint from the signature pools, embedded via
the production path (norm_router_text -> slot 6 -> head -> centroids),
scored top-1 per class with a confusion matrix. Run against the live
serving model:

    make eval-router

Re-run after any pool edit or re-record. The eval set is pre-registered:
do not add utterances that already appear in the pools, and treat any
change to the eval list itself as a protocol change (note it in git).
"""
import json
import sys

import numpy as np
import requests

LLAMA = "http://127.0.0.1:8998"
TREE = "tools/decision_tree.json"
REFS = "models/router_embeddings.npz"
HEADS = "models/arm_C_heads.npz"

# (utterance, expected class) — disjoint from every pool above.
EVAL = [
    # direct
    ("How cold does it get on the moon at night?", "direct"),
    ("What's the largest planet?", "direct"),
    ("Say goodbye in French.", "direct"),
    ("Who is the president of France?", "direct"),
    ("What does DNA stand for?", "direct"),
    ("How many legs does a spider have?", "direct"),
    ("What's the boiling point of ethanol?", "direct"),
    ("Tell me a fun fact about octopuses.", "direct"),
    ("What language do they speak in Brazil?", "direct"),
    ("How far is the moon from Earth?", "direct"),
    ("What's twelve times twelve?", "direct"),
    ("Is tomato a fruit?", "direct"),
    # think
    ("What's 15 percent tip on an 84 dollar bill?", "think"),
    ("If today is Tuesday, what day is it in 100 days?", "think"),
    ("Plan my week around three meetings.", "think"),
    ("Add a calendar event for lunch tomorrow at noon.", "think"),
    ("How long would it take to read a 400 page book?", "think"),
    ("Turn this recipe into a shopping list.", "think"),
    ("Explain how a bill becomes law, step by step.", "think"),
    ("Write a haiku about autumn.", "think"),
    ("How much would a 30 kilogram suitcase weigh on Mars?", "think"),
    ("Work out the monthly payment on a 20 thousand dollar loan at 6 percent.", "think"),
    ("Draft a cover letter for a barista job.", "think"),
    ("Count the vowels in the word extraordinary.", "think"),
    # decide
    ("Should I upgrade my phone now or wait?", "decide"),
    ("Which should I learn first, guitar or piano?", "decide"),
    ("What do you think, window seat or aisle?", "decide"),
    ("Help me pick a birthday gift for my sister.", "decide"),
    ("Is it better to exercise in the morning or evening?", "decide"),
    ("Choose a name for my goldfish.", "decide"),
    ("Should we order pizza or sushi tonight?", "decide"),
    ("Which of these two resumes is stronger?", "decide"),
    ("Do I sound better in the red shirt or the gray one?", "decide"),
    ("Would you say yes to this deal?", "decide"),
    ("What should I name my bakery?", "decide"),
    ("Is it a good idea to move cities for love?", "decide"),
    # social
    ("My back has been hurting all week and I'm so over it.", "social"),
    ("I got the job! I start on Monday!", "social"),
    ("I feel like nobody listens to me at home.", "social"),
    ("Honestly today was a mess from start to finish.", "social"),
    ("I adopted a cat yesterday and I'm completely in love.", "social"),
    ("The winter months are always hard for me.", "social"),
    ("I finally defended my dissertation yesterday.", "social"),
    ("I think my neighbors hate me now.", "social"),
    ("I'm so proud of my little sister's recital.", "social"),
    ("Some days I just feel invisible at work.", "social"),
    # vision
    ("Do these shoes go with this outfit?", "vision"),
    ("Is my tie crooked?", "vision"),
    ("What is this plant I'm showing you?", "vision"),
    ("How does my new haircut look from the side?", "vision"),
    ("Can you read the label on this bottle?", "vision"),
    ("Does this shirt have a stain on it?", "vision"),
    ("What do you see on my desk right now?", "vision"),
    ("Am I holding the wrench the right way?", "vision"),
    ("Is the picture hanging straight behind me?", "vision"),
    ("What does the error on my screen say?", "vision"),
]


def norm_router_text(t):
    """Must match web/server.py and tools/record_router.py."""
    import re
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def emb(text):
    r = requests.post(f"{LLAMA}/v1/embeddings",
                      json={"input": norm_router_text(text),
                            "model": "wiresense-llm", "id_slot": 6}, timeout=60)
    v = r.json()["data"][0]["embedding"]
    if v is None or any(x is None for x in v):
        raise RuntimeError("null embedding")
    a = np.asarray(v, dtype=np.float32)
    if np.isnan(a).any() or not a.any():
        raise RuntimeError("NaN/zero embedding")
    return a


def main():
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

    root = tree["nodes"]["root"]
    classes = list(root["classes"])
    cents = norm(np.stack([norm(project(z[f"root.{c}"])).mean(0) for c in classes]))

    # protocol guard: no eval utterance may appear in a pool
    pool_texts = {norm_router_text(t)
                  for node in tree["nodes"].values()
                  for ts in node["pools"].values() for t in ts}
    clash = [t for t, _ in EVAL if norm_router_text(t) in pool_texts]
    if clash:
        print(f"PROTOCOL VIOLATION — eval utterances also in pools: {clash}")
        return 1

    idx = {c: i for i, c in enumerate(classes)}
    cm = np.zeros((len(classes), len(classes)), int)
    misses = []
    for text, gold in EVAL:
        v = norm(project(emb(text)[None, :]))[0]
        pred = classes[int(np.argmax(cents @ v))]
        cm[idx[gold], idx[pred]] += 1
        if pred != gold:
            misses.append((gold, pred, text))

    acc = cm.diagonal().sum() / cm.sum()
    print(f"router eval: {int(cm.diagonal().sum())}/{len(EVAL)} = {acc:.0%}")
    print("gold\\pred  " + "  ".join(f"{c:>8}" for c in classes))
    for c, row in zip(classes, cm):
        print(f"{c:>9}  " + "  ".join(f"{n:>8}" for n in row))
    for gold, pred, text in misses:
        print(f"  miss [{gold}]->{pred}: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
