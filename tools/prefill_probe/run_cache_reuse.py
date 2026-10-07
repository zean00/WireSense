#!/usr/bin/env python
"""--cache-reuse A/B sandbox runner (AMENDMENT 2 of
docs/PROTOCOL-prefix-warm.md).

Runs against MY OWN CPU-only llama-server on 127.0.0.1:8999 (never the
production :8998): baseline phase = instance without the flag, treatment
phase = same instance + --cache-reuse 1. E1 sub-2048 threshold probes,
E2 rung-4096 COLD/FREE/WARM. id_slot=0 (np=1 single slot).

Usage: tools/prefill_probe/run_cache_reuse.py --out results_cache_reuse_baseline.json
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

SLOT = 0
E2_TARGET = 4096
E3_TARGET = 7680
GEN_HEADROOM = 128
FILLER = "one more thing to remember, please"
DISPOSER = ("tell me about a place no one lives, just to clear the slot")
FILLER_TOKS = "word " * 160
E1_L = [50, 154]
MODE = dict(directive=None, open_think=False)
SCEN_DIR = ("/home/sahal/workspace/refleqwen/eval/reports/entry_v"
            "/scenarios")


def prompt_for(text, history):
    return ws.build_prompt(history, text, MODE["directive"],
                           MODE["open_think"])


def tokenize(base, text):
    r = requests.post(f"{base}/tokenize", json={"content": text},
                      timeout=120)
    return r.json()["tokens"]


def complete(base, prompt, mt, cache=True, timeout=1800, stream=False):
    t0 = time.perf_counter()
    ttft = None
    chunks, timings = [], None
    req = dict(prompt=prompt, max_tokens=mt, temperature=0,
               cache_prompt=cache, id_slot=SLOT, stream=stream)
    if stream:
        with requests.post(f"{base}/v1/completions", json=req,
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
        j = requests.post(f"{base}/v1/completions", json=req,
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


def history_for(base, target, ref_final):
    lo, hi = 1, len(PAIRS_LARGE)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(tokenize(base, prompt_for(ref_final, PAIRS_LARGE[:mid]))) \
                <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8999")
    ap.add_argument("--out", required=True)
    ap.add_argument("--e3", action="store_true",
                    help="also run the E3 rung-7680 block (treatment only)")
    args = ap.parse_args()
    base = args.base

    # launch assertions
    props = requests.get(f"{base}/props", timeout=30).json()
    n_ctx = props["default_generation_settings"]["n_ctx"]
    assert props["total_slots"] == 1, props["total_slots"]
    assert n_ctx >= E2_TARGET + GEN_HEADROOM + 96, n_ctx

    scen = [json.load(open(os.path.join(SCEN_DIR, f"scenario_{i:02d}.json")))
            for i in range(4)]
    finals = [s["final"] for s in scen]
    ref_final = max(finals, key=len)

    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    res = {"protocol": "docs/PROTOCOL-prefix-warm.md (AMENDMENT 2)",
           "git_head": rev.strip(), "base": base, "slot": SLOT,
           "n_ctx": n_ctx, "fixture_sha256": fixture_sha256(),
           "started": time.strftime("%F %T"),
           "e1": [], "e2": {}, "e3": None}

    # ---- E1: sub-2048 regime -------------------------------------------
    for L in E1_L:
        for mt_pop in (8, 1):
            for rep in range(2):
                filler = FILLER_TOKS[:L * 5]
                A = prompt_for(filler + "alpha ending here", [])
                B = prompt_for(filler + "beta ending here", [])
                ta, tb = tokenize(base, A), tokenize(base, B)
                lcp = 0
                for x, y in zip(ta, tb):
                    if x != y:
                        break
                    lcp += 1
                complete(base, A, mt_pop)
                r = complete(base, B, 8)
                r.update({"kind": "filler", "shared_L": L, "lcp_n": lcp,
                          "mt_pop": mt_pop, "rep": rep})
                res["e1"].append(r)
                print(f"E1 filler L={L} mt_pop={mt_pop} rep={rep} "
                      f"lcp={lcp} cache_n={r['cache_n']}", flush=True)
    k1024 = history_for(base, 1024, ref_final)
    h1024 = PAIRS_LARGE[:k1024]
    for rep in range(2):
        complete(base, prompt_for(FILLER, h1024), 8)
        r = complete(base, prompt_for(finals[0], h1024), 8)
        r.update({"kind": "history", "k_pairs": k1024, "rep": rep})
        res["e1"].append(r)
        print(f"E1 history k={k1024} rep={rep} "
              f"cache_n={r['cache_n']}", flush=True)

    # ---- E2: rung-4096 A/B ----------------------------------------------
    for target, key in ((E2_TARGET, "e2"), (E3_TARGET, "e3")):
        if key == "e3" and not args.e3:
            break
        k = history_for(base, target, ref_final)
        history = PAIRS_LARGE[:k]
        ref_pn = len(tokenize(base, prompt_for(ref_final, history)))
        assert ref_pn <= n_ctx - GEN_HEADROOM
        rows = []
        for i, final in enumerate(finals):
            complete(base, prompt_for(DISPOSER, []), 8)
            cold = complete(base, prompt_for(final, history), 96,
                            stream=True)
            cold.update({"arm": "COLD", "scenario": i, "rep": 0})
            complete(base, prompt_for(FILLER, history), 8)
            free = complete(base, prompt_for(final, history), 96,
                            stream=True)
            free.update({"arm": "FREE", "scenario": i, "rep": 0})
            complete(base, prompt_for([], DISPOSER), 8)
            t0 = time.perf_counter()
            warm = complete(base, prompt_for("", history), 8)
            warm_ms = (time.perf_counter() - t0) * 1000
            w = complete(base, prompt_for(final, history), 96, stream=True)
            w.update({"arm": "WARM", "scenario": i, "rep": 0,
                      "warm_ms": round(warm_ms, 1),
                      "warm_cache_n": warm["cache_n"]})
            rows += [cold, free, w]
            print(f"{key} target={target} k={k} scenario {i}/4 done: "
                  f"COLD cn={cold['cache_n']} FREE cn={free['cache_n']} "
                  f"WARM cn={w['cache_n']}", flush=True)
        res[key] = {"k_pairs": k, "prompt_n_ref": ref_pn, "rows": rows}

    res["finished"] = time.strftime("%F %T")
    json.dump(res, open(args.out, "w"), indent=1)
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
