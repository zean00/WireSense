#!/usr/bin/env python
"""Prefix-warm probe — case 2 (history/context prefill) at context scale.

Protocol: docs/PROTOCOL-prefix-warm.md (pre-registered BEFORE any data).
Part 1: reuse-threshold sweep (shared-prefix length x populator
max_tokens) — descriptive. Part 2: COLD / FREE / WARM arms across a
context ladder (0..64 history pairs), arms interleaved per (rep,
scenario) to kill GPU warm-up drift. Runs against the RUNNING
llama-server :8998 with id_slot=1.

Usage: tools/prefill_probe/run_prefix_warm.py [--reps 3] [--out results_prefix_warm.json]
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
from history_fixture import PAIRS  # noqa: E402

LLAMA = ws.LLAMA
SLOT = 1
SCEN_DIR = ("/home/sahal/workspace/refleqwen/eval/reports/entry_v"
            "/scenarios")
K_LADDER = [0, 3, 8, 16, 32, 64]          # history PAIRS
L_SWEEP = [24, 32, 40, 48, 56, 64, 72, 80, 96, 128]
FILLER = "one more thing to remember, please"
DISPOSER = ("tell me about a place no one lives, just to clear the slot")
FILLER_TOKS = "word " * 160               # 1 word ~= 1 token

MODE = dict(directive=None, open_think=False, max_tokens=96)


def prompt_for(text, history):
    return ws.build_prompt(history, text, MODE["directive"],
                           MODE["open_think"])


def tokenize(text):
    r = requests.post(f"{LLAMA}/tokenize", json={"content": text},
                      timeout=60)
    return r.json()["tokens"]


def complete(prompt, mt, cache=True, timeout=300, stream=False):
    """One completion; TTFT = pre-POST -> first non-empty content delta
    (streaming only — non-stream returns after full generation)."""
    t0 = time.perf_counter()
    ttft = None
    chunks, timings = [], None
    req = dict(prompt=prompt, max_tokens=mt, temperature=0,
               cache_prompt=cache, id_slot=SLOT)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(
        HERE, "results_prefix_warm.json"))
    args = ap.parse_args()

    scen = [json.load(open(os.path.join(SCEN_DIR, f"scenario_{i:02d}.json")))
            for i in range(8)]
    finals = [s["final"] for s in scen]
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    res = {"protocol": "docs/PROTOCOL-prefix-warm.md",
           "git_head": rev.strip(), "slot": SLOT, "reps": args.reps,
           "llama": LLAMA, "started": time.strftime("%F %T"),
           "k_ladder": K_LADDER, "l_sweep": L_SWEEP,
           "threshold": [], "ladder": {}}

    # ---- part 1: threshold sweep (descriptive) --------------------------
    for mt_pop in (8, 1):
        for L in L_SWEEP:
            for rep in range(args.reps):
                filler = FILLER_TOKS[:L * 5]      # L words ~ L tokens
                A = ws.build_prompt([], filler + "alpha ending here")
                B = ws.build_prompt([], filler + "beta ending here")
                ta, tb = tokenize(A), tokenize(B)
                lcp = 0
                for x, y in zip(ta, tb):
                    if x != y:
                        break
                    lcp += 1
                complete(A, mt_pop)
                r = complete(B, 8)
                r.update({"shared_L": L, "lcp_n": lcp, "mt_pop": mt_pop,
                          "rep": rep})
                res["threshold"].append(r)
            done = res["threshold"][-1]
            print(f"threshold mt={mt_pop} L={L} lcp={done['lcp_n']}: "
                  f"cache_n={done['cache_n']}", flush=True)

    # ---- part 2: context ladder -----------------------------------------
    for k in K_LADDER:
        history = PAIRS[:k]
        rows = []
        for rep in range(args.reps):
            for i, final in enumerate(finals):
                # disposer: kill any matching cache from the previous
                # (rep, scenario) block
                complete(ws.build_prompt([], DISPOSER), 8)
                # COLD: context changed since last turn
                cold = complete(prompt_for(final, history), 96,
                                stream=True)
                cold.update({"arm": "COLD", "scenario": i, "rep": rep})
                # FREE: shipped steady state (same context, last turn
                # was a different utterance)
                complete(prompt_for(FILLER, history), 8)
                free = complete(prompt_for(final, history), 96,
                                stream=True)
                free.update({"arm": "FREE", "scenario": i, "rep": rep})
                # WARM: disposer -> prefix-warm at speech onset -> commit
                complete(ws.build_prompt([], DISPOSER), 8)
                t0 = time.perf_counter()
                warm = complete(prompt_for("", history), 8)
                warm_ms = (time.perf_counter() - t0) * 1000
                w = complete(prompt_for(final, history), 96, stream=True)
                w.update({"arm": "WARM", "scenario": i, "rep": rep,
                          "warm_ms": round(warm_ms, 1),
                          "warm_cache_n": warm["cache_n"]})
                rows += [cold, free, w]
            print(f"ladder k={k}: {len(rows)}/{len(finals) * args.reps * 3}",
                  flush=True)
        res["ladder"][str(k)] = rows

    res["finished"] = time.strftime("%F %T")
    json.dump(res, open(args.out, "w"), indent=1)
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
