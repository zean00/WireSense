#!/usr/bin/env python
"""Large-context amendment runner (AMENDMENT section of
docs/PROTOCOL-prefix-warm.md). Same COLD/FREE/WARM arms and interleaving
as run_prefix_warm.py part 2, but history is sized in TOKEN space from
the deterministic large fixture (large_fixture.py): rungs ~1024 / ~2048 /
~4096 / ~7680 prompt tokens. Platform facts discovered pre-data and
asserted at launch: slot cap default_generation_settings.n_ctx = 8192
(65536 / 8 slots, via /props) — prompts + 96 generated tokens must fit.

Usage: tools/prefill_probe/run_large_ctx.py [--reps 3]
Writes tools/prefill_probe/results_large_ctx.json at the end.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
WEB = os.path.join(ROOT, "web")
sys.path.insert(0, WEB)
sys.path.insert(0, HERE)
os.chdir(WEB)  # server.py's MODELS = "../models" assumes cwd=web (prod)
import server as ws  # noqa: E402
from large_fixture import PAIRS_LARGE, fixture_sha256  # noqa: E402

LLAMA = ws.LLAMA
SLOT = 1
TARGETS = [1024, 2048, 4096, 7680]
FROZEN_SHA256 = "b42bde63a0108bc0ccbfedfd7f3c2f5312c948d4aa1d05a5d363ad4cb20b84c4"
SLOT_N_CTX = 8192          # /props pre-data; asserted at launch
GEN_HEADROOM = 128         # commits are mt=96; require prompt_n <= 8064

FILLER = "one more thing to remember, please"
DISPOSER = ("tell me about a place no one lives, just to clear the slot")
MODE = dict(directive=None, open_think=False, max_tokens=96)
SCEN_DIR = ("/home/sahal/workspace/refleqwen/eval/reports/entry_v"
            "/scenarios")


def prompt_for(text, history):
    return ws.build_prompt(history, text, MODE["directive"],
                           MODE["open_think"])


def tokenize(text):
    r = requests.post(f"{LLAMA}/tokenize", json={"content": text},
                      timeout=120)
    return r.json()["tokens"]


def complete(prompt, mt, cache=True, timeout=600, stream=False):
    """One completion; TTFT = pre-POST -> first non-empty content delta.
    The stream flag goes in the BODY (requests' stream kwarg is
    client-side only — the incident-1 bug)."""
    t0 = time.perf_counter()
    ttft = None
    chunks, timings = [], None
    req = dict(prompt=prompt, max_tokens=mt, temperature=0,
               cache_prompt=cache, id_slot=SLOT, stream=stream)
    if stream:
        with requests.post(f"{LLAMA}/v1/completions", json=req,
                           stream=True, timeout=timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith(b"data: "):
                    continue
                payload = line[len(b"data: "):]
                if payload == b"[DONE]":
                    break
                j = json.loads(payload)
                if j.get("timings"):
                    timings = j["timings"]
                delta = j["choices"][0].get("text", "")
                if not delta:
                    continue
                chunks.append(delta)
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000
    else:
        j = requests.post(f"{LLAMA}/v1/completions", json=req,
                          timeout=timeout).json()
        timings = j.get("timings")
        chunks = [j["choices"][0].get("text", "")]
    t = timings or {}
    return {"ttft_ms": round(ttft, 1) if ttft is not None else None,
            "prompt_n": t.get("prompt_n"),
            "prompt_ms": round(t["prompt_ms"], 1) if t.get("prompt_ms")
                         else None,
            "cache_n": t.get("cache_n"),
            "predicted_n": t.get("predicted_n"),
            "text": "".join(chunks)}


def history_for(target, ref_final):
    """Largest k with prompt_n(prompt_for(ref_final, PAIRS_LARGE[:k]))
    <= target (bisection; monotonic in k). Actual prompt_n recorded."""
    lo, hi = 1, len(PAIRS_LARGE)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(tokenize(prompt_for(ref_final, PAIRS_LARGE[:mid]))) <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(
        HERE, "results_large_ctx.json"))
    args = ap.parse_args()

    sha = fixture_sha256()
    assert sha == FROZEN_SHA256, f"fixture drift: {sha}"
    props = requests.get(f"{LLAMA}/props", timeout=30).json()
    n_ctx = props["default_generation_settings"]["n_ctx"]
    assert n_ctx == SLOT_N_CTX, f"slot ctx changed: {n_ctx}"

    scen = [json.load(open(os.path.join(SCEN_DIR, f"scenario_{i:02d}.json")))
            for i in range(8)]
    finals = [s["final"] for s in scen]
    ref_final = max(finals, key=len)

    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    res = {"protocol": "docs/PROTOCOL-prefix-warm.md (AMENDMENT)",
           "git_head": rev.strip(), "slot": SLOT, "reps": args.reps,
           "llama": LLAMA, "slot_n_ctx": n_ctx, "targets": TARGETS,
           "fixture_sha256": sha, "fixture_pairs": len(PAIRS_LARGE),
           "started": time.strftime("%F %T"), "ladder": {}}

    for target in TARGETS:
        k = history_for(target, ref_final)
        history = PAIRS_LARGE[:k]
        ref_pn = len(tokenize(prompt_for(ref_final, history)))
        assert ref_pn <= SLOT_N_CTX - GEN_HEADROOM, \
            f"rung {target}: prompt_n {ref_pn} exceeds slot budget"
        rows = []
        for rep in range(args.reps):
            for i, final in enumerate(finals):
                complete(ws.build_prompt([], DISPOSER), 8)
                cold = complete(prompt_for(final, history), 96,
                                stream=True)
                cold.update({"arm": "COLD", "scenario": i, "rep": rep})
                complete(prompt_for(FILLER, history), 8)
                free = complete(prompt_for(final, history), 96,
                                stream=True)
                free.update({"arm": "FREE", "scenario": i, "rep": rep})
                complete(ws.build_prompt([], DISPOSER), 8)
                t0 = time.perf_counter()
                warm = complete(prompt_for("", history), 8)
                warm_ms = (time.perf_counter() - t0) * 1000
                w = complete(prompt_for(final, history), 96, stream=True)
                w.update({"arm": "WARM", "scenario": i, "rep": rep,
                          "warm_ms": round(warm_ms, 1),
                          "warm_cache_n": warm["cache_n"]})
                rows += [cold, free, w]
            print(f"large target={target} k={k}: "
                  f"{len(rows)}/{len(finals) * args.reps * 3}", flush=True)
        res["ladder"][str(target)] = {"k_pairs": k, "prompt_n_ref": ref_pn,
                                      "rows": rows}

    res["finished"] = time.strftime("%F %T")
    json.dump(res, open(args.out, "w"), indent=1)
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
