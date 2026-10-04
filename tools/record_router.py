"""Record decision-tree reference embeddings from the serving model.

Refs rule (same as decide sigs): these vectors are only valid for the
serving model + numerics they were recorded from. Re-run after any change
to model, context type, or KV cache type:

    make record-router

Reads tools/decision_tree.json (nodes' pools + feedback + backchannel)
and writes models/router_embeddings.npz with one named array per pool:
<node>.<class>, feedback.<mode>, backchannel. Every embedding is
null/zero-checked (llama serializes NaN as JSON null).
"""
import json
import re
import sys

import numpy as np
import requests

LLAMA = "http://127.0.0.1:8998"
TREE = "tools/decision_tree.json"
OUT = "models/router_embeddings.npz"
SLOT = 6


def norm_router_text(t):
    """Must match web/server.py: refs and live utterances share the ASR
    text style (lowercase, punctuation-free) or the margins collapse."""
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def emb(text):
    r = requests.post(f"{LLAMA}/v1/embeddings",
                      json={"input": norm_router_text(text),
                            "model": "wiresense-llm",
                            "id_slot": SLOT}, timeout=60)
    v = r.json()["data"][0]["embedding"]
    if v is None or any(x is None for x in v):
        raise RuntimeError(f"null embedding for {text!r} (corruption signature)")
    a = np.asarray(v, dtype=np.float32)
    if np.isnan(a).any() or not a.any():
        raise RuntimeError(f"NaN/zero embedding for {text!r}")
    return a


def main():
    cfg = json.load(open(TREE))
    heads = np.load("models/arm_C_heads.npz")

    def project(x):
        h = x @ heads["fc1.weight"].T + heads["fc1.bias"]
        h = 0.5 * h * (1.0 + np.tanh(0.7978845608 * (h + 0.044715 * h ** 3)))
        ln = (h - h.mean(-1, keepdims=True)) / np.sqrt(h.var(-1, keepdims=True) + 1e-5)
        h = ln * heads["ln.weight"] + heads["ln.bias"] + h
        return h @ heads["fc2.weight"].T

    def norm(x):
        return x / np.linalg.norm(x, axis=-1, keepdims=True)

    arrays = {}
    n_total = 0
    for nid, node in cfg["nodes"].items():
        for cls, texts in node["pools"].items():
            vecs = np.stack([emb(t) for t in texts])
            arrays[f"{nid}.{cls}"] = vecs
            n_total += len(texts)
            print(f"{nid}.{cls}: {len(texts)} sigs")
    for mode, texts in cfg["feedback"].items():
        arrays[f"feedback.{mode}"] = np.stack([emb(t) for t in texts])
        n_total += len(texts)
        print(f"feedback.{mode}: {len(texts)} sigs")
    bc = cfg["backchannel"]
    arrays["backchannel"] = np.stack([emb(t) for t in bc])
    n_total += len(bc)
    print(f"backchannel: {len(bc)} sigs")

    np.savez(OUT, **arrays)
    print(f"wrote {OUT} ({n_total} embeddings, {arrays['backchannel'].shape[1]} dims)")

    # self-consistency: every pool sig must sit nearest its own class
    # centroid within its own node
    ok = bad = 0
    z = np.load(OUT)
    for nid, node in cfg["nodes"].items():
        classes = list(node["pools"])
        cents = norm(np.stack([norm(project(z[f"{nid}.{c}"])).mean(0) for c in classes]))
        for ci, c in enumerate(classes):
            for t, v in zip(node["pools"][c], norm(project(z[f"{nid}.{c}"]))):
                pred = classes[int(np.argmax(cents @ v))]
                if pred != c:
                    bad += 1
                    print(f"  CROSS {nid}: [{c}] -> {pred}: {t!r}")
                else:
                    ok += 1
    print(f"self-consistency: {ok}/{ok + bad} sigs nearest own centroid")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
