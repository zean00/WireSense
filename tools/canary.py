#!/usr/bin/env python
"""GPU fp4 health canary against the wiresense llama-server.

Checks the two symptoms of the post-reboot fp4 corruption
(docs/INCIDENT-2026-10-04-GPU.md): all-NaN embeddings and '?'-spam
generation. Exit 0 = healthy, 1 = faulty, 2 = server unreachable.

Usage: canary.py [port]   (default 8998)
"""
import json
import math
import sys
import urllib.request

port = sys.argv[1] if len(sys.argv) > 1 else "8998"
base = f"http://127.0.0.1:{port}"


def post(path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))


try:
    name = json.load(open("/home/sahal/workspace/wiresense/models/sig_names.json"))[0]
    emb = post("/v1/embeddings", {"input": name, "id_slot": 0})["data"][0]["embedding"]
except Exception as e:
    print(f"canary: llama-server on :{port} unreachable ({e})")
    sys.exit(2)

nan = any(v is None or (isinstance(v, float) and math.isnan(v)) for v in emb)
zero = all(isinstance(v, (int, float)) and v == 0 for v in emb) and len(emb) > 0
print("embed canary: " + ("NaN/zero/corrupt" if nan or zero else f"clean ({len(emb)} dims)"))

gen = post("/v1/completions", {"prompt": "The capital of France is",
                               "max_tokens": 12, "temperature": 0, "id_slot": 0})
text = gen["choices"][0]["text"]
bad_gen = text.count("?") >= 8
print("gen canary:   " + ("garbage " + repr(text[:40]) if bad_gen else "clean " + repr(text[:60])))

if nan or zero or bad_gen:
    print("VERDICT: GPU fp4 path STILL FAULTY — see docs/INCIDENT-2026-10-04-GPU.md")
    sys.exit(1)
print("VERDICT: GPU fp4 path healthy")
