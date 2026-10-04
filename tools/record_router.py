"""Record router/feedback/backchannel reference embeddings from the serving model.

Refs rule (same as decide sigs): these vectors are only valid for the
serving model + numerics they were recorded from. Re-run after any change
to model, context type, or KV cache type:

    make record-router

Writes models/router_embeddings.npz with one named array per pool. Every
embedding is null/zero-checked (llama serializes NaN as JSON null).
"""
import json
import sys

import numpy as np
import requests

LLAMA = "http://127.0.0.1:8998"
POOLS = "tools/router_pools.json"
OUT = "models/router_embeddings.npz"
SLOT = 6


def norm_router_text(t):
    """Must match web/server.py: refs and live utterances share the ASR
    text style (lowercase, punctuation-free) or the margins collapse."""
    import re
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
    pools = json.load(open(POOLS))
    arrays = {}
    n_total = 0
    for group in ("router", "feedback"):
        for cls, texts in pools[group].items():
            vecs = np.stack([emb(t) for t in texts])
            arrays[f"{group}.{cls}"] = vecs
            n_total += len(texts)
            print(f"{group}.{cls}: {len(texts)} sigs")
    bc = pools["backchannel"]
    arrays["backchannel"] = np.stack([emb(t) for t in bc])
    n_total += len(bc)
    print(f"backchannel: {len(bc)} sigs")

    np.savez(OUT, **arrays)
    print(f"wrote {OUT} ({n_total} embeddings, {arrays['router.direct'].shape[1]} dims)")

    # self-consistency: every sig must sit nearest its own class centroid
    z = np.load(OUT)
    heads = np.load("models/arm_C_heads.npz")

    def project(x):
        h = x @ heads["fc1.weight"].T + heads["fc1.bias"]
        h = 0.5 * h * (1.0 + np.tanh(0.7978845608 * (h + 0.044715 * h ** 3)))
        ln = (h - h.mean(-1, keepdims=True)) / np.sqrt(h.var(-1, keepdims=True) + 1e-5)
        h = ln * heads["ln.weight"] + heads["ln.bias"] + h
        return h @ heads["fc2.weight"].T + heads["fc2.bias"]

    def norm(x):
        return x / np.linalg.norm(x, axis=-1, keepdims=True)

    classes = [k for k in z.files if k.startswith("router.")]
    cents = norm(np.stack([norm(project(z[k])).mean(0) for k in classes]))
    ok = bad = 0
    for ci, k in enumerate(classes):
        for v in norm(project(z[k])):
            ok += int(np.argmax(cents @ v)) == ci
            bad += int(np.argmax(cents @ v)) != ci
    print(f"self-consistency: {ok}/{ok + bad} sigs nearest own centroid")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
