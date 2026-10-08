#!/usr/bin/env python
"""Prefill probe — shipped-stack commit-path latency, three arms.

Protocol: docs/PROTOCOL-prefill.md (pre-registered BEFORE any data; this
runner ships with it, also data-free). Questions, arm definitions and
decision rules live there. Executes against the RUNNING llama-server
:8998 with id_slot=1 (production owns 4/5/6); the web server and config
files are untouched.

Arms: COMMIT (cache_prompt=true, the shipped stream_local call),
NOCACHE (cache_prompt=false, full-prefill reference), WARM (entry-V
replay policy: every frozen partial as a max_tokens=1 completion before
the commit — the mechanism the About page wrongly credits; think-on
only). Grid: 8 frozen entry-V scenarios x 5 reps, both think modes for
COMMIT/NOCACHE. Descriptive: greedy output identity across arms;
readout per partial on the shipped DecideReadout heads (slot 1).

Usage: tools/prefill_probe/run_probe.py [--reps 5] [--out results.json]
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
os.chdir(WEB)  # server.py's MODELS = "../models" assumes cwd=web (prod)
import server as ws  # noqa: E402  (build_prompt / system_block / DecideReadout)

LLAMA = ws.LLAMA
SLOT = 1                      # probe slot; production owns 4/5/6
# frozen artifacts from the rewire repo (github.com/zean00/rewire),
# local checkout: /home/sahal/workspace/refleqwen
SCEN_DIR = ("/home/sahal/workspace/refleqwen/eval/reports/entry_v"
            "/scenarios")
THINK_DIRECTIVE = "Reason step by step, then give a clear, concise answer."
HISTORY = [("what can you do?",
            "Listen, decide, think, speak — timers, notes, tools, "
            "research. Ask away.")]
DISPOSER = ("tell me about the weather in a place no one lives "
            "just to clear the slot")

MODES = {
    "think_off": dict(directive=None, open_think=False, max_tokens=96),
    "think_on": dict(directive=THINK_DIRECTIVE, open_think=True,
                     max_tokens=640),
}


def prompt_for(text, mode):
    m = MODES[mode]
    return ws.build_prompt(HISTORY, text, m["directive"], m["open_think"])


def commit(text, mode, cache=True, timeout=180):
    """One shipped-shaped streaming completion. TTFT = pre-POST ->
    first non-empty content delta (server.py's response_start point)."""
    m = MODES[mode]
    t0 = time.perf_counter()
    ttft = None
    chunks, timings = [], None
    with requests.post(f"{LLAMA}/v1/completions", json={
            "prompt": prompt_for(text, mode), "max_tokens": m["max_tokens"],
            "temperature": 0, "cache_prompt": cache, "id_slot": SLOT,
            "stream": True}, stream=True, timeout=timeout) as r:
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
    t = timings or {}
    return {"ttft_ms": round(ttft, 1), "prompt_n": t.get("prompt_n"),
            "prompt_ms": round(t["prompt_ms"], 1) if t.get("prompt_ms")
                         else None,
            "cache_n": t.get("cache_n"),
            "predicted_n": t.get("predicted_n"),
            "text": "".join(chunks)}


def warm_replay(partial, mode):
    """One partial as a 1-token completion (entry-V warming policy)."""
    requests.post(f"{LLAMA}/v1/completions", json={
        "prompt": prompt_for(partial, mode), "max_tokens": 1,
        "temperature": 0, "cache_prompt": True, "id_slot": SLOT},
        timeout=60).raise_for_status()


def readout_ms(rd, text):
    """Shipped decide() input shape; embeddings on slot 1 (not prod 6)."""
    prompt = ws.build_prompt(HISTORY, text)
    t0 = time.perf_counter()
    r = requests.post(f"{LLAMA}/v1/embeddings",
                      json={"input": prompt, "id_slot": SLOT}, timeout=30)
    t_emb = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    emb = ws.np.asarray(r.json()["data"][0]["embedding"], dtype="float32")
    rd.readout(emb)
    return {"emb_ms": round(t_emb, 1),
            "head_ms": round((time.perf_counter() - t1) * 1000, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", default=os.path.join(HERE, "results.json"))
    args = ap.parse_args()

    scen = [json.load(open(os.path.join(SCEN_DIR, f"scenario_{i:02d}.json")))
            for i in range(8)]
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    res = {"protocol": "docs/PROTOCOL-prefill.md", "git_head": rev.strip(),
           "slot": SLOT, "reps": args.reps,
           "llama": LLAMA, "started": time.strftime("%F %T"),
           "scenarios": [{"i": i, "final": s["final"],
                          "n_partials": s["n_partials"]}
                         for i, s in enumerate(scen)],
           "arms": {}, "readout": []}

    rd = ws.DecideReadout()

    plan = [("COMMIT", "think_off", False), ("COMMIT", "think_on", False),
            ("NOCACHE", "think_off", False), ("NOCACHE", "think_on", False),
            ("WARM", "think_on", False)]
    for arm, mode, _ in plan:
        rows = []
        commit(DISPOSER, mode)          # disposer: first measured commit
        for rep in range(args.reps):    # must not share a prompt prefix
            for i, s in enumerate(scen):
                if arm == "WARM":
                    for p in s["partials"]:
                        warm_replay(p, mode)
                row = commit(s["final"], mode, cache=(arm != "NOCACHE"))
                row.update({"scenario": i, "rep": rep})
                rows.append(row)
            print(f"{arm}/{mode}: {len(rows)}/{len(scen) * args.reps}",
                  flush=True)
        res["arms"].setdefault(arm, {})[mode] = rows

    for i, s in enumerate(scen):
        for j, p in enumerate(s["partials"]):
            ms = readout_ms(rd, p)
            ms.update({"scenario": i, "partial": j})
            res["readout"].append(ms)
        print(f"readout: {i + 1}/8 scenarios", flush=True)

    res["finished"] = time.strftime("%F %T")
    json.dump(res, open(args.out, "w"), indent=1)
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
