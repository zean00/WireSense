#!/usr/bin/env python
"""MS-17a scorer: continuation-keyed vs utterance-keyed gate recall.

Index rebuilt STRICTLY from the committed synth_pools.json via run_eval's
own code path (k=5 asserted); frozen inputs make runs deterministic, so
the results file is written fresh and REFUSES to overwrite.
"""
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "..", "mcp_probe")
sys.path.insert(0, PROBE)
os.environ.setdefault("MCP_PROBE_K", "5")   # registered gate k — before import
import run_eval as probe  # noqa: E402  (chdirs to web/, imports server)

TOPN = 2
CONT = os.path.join(HERE, "continuations.json")
RESULTS = os.path.join(HERE, "results_17a.jsonl")
SUMMARY = os.path.join(HERE, "summary_17a.json")

BAR_PRIMARY = 0.80
BAR_NONINF = -0.05


def main():
    if probe.K != 5:
        sys.exit(f"registered k=5, run_eval loaded k={probe.K}")
    tools = probe.load_tools()
    by_key = {f"{t['server']}__{t['name']}": t["name"] for t in tools}

    pools = json.load(open(os.path.join(PROBE, "synth_pools.json")))
    t0 = time.time()
    svecs = []
    for t in tools:
        us = pools.get(f"{t['server']}__{t['name']}", [])[:8]
        svecs.append(probe.l2(
            np.stack([probe.l2(probe.embed(u)) for u in us]).mean(0)))
    cents, assign = probe.kmeans(svecs, k=5)
    print(f"index rebuilt from committed pools in {time.time() - t0:.0f}s; "
          f"cluster sizes={np.bincount(assign, minlength=5).tolist()}",
          flush=True)
    members_of = {}
    for i, a in enumerate(assign):
        members_of.setdefault(int(a), []).append(tools[i]["name"])

    def hit(text, want):
        v = probe.l2(probe.embed(text))
        sims = cents @ v
        top = [int(x) for x in np.argsort(-sims)[:TOPN]]
        short = sorted({n for c in top for n in members_of[c]})
        return want in short, len(short), top, short

    if os.path.exists(RESULTS):
        sys.exit(f"REFUSING: {RESULTS} exists (frozen inputs -> "
                 f"deterministic; delete by hand to redo)")
    out = open(RESULTS, "w")

    def score(set_name, text, want, key):
        h, n, top, short = hit(text, want)
        out.write(json.dumps({"set": set_name, "key": key, "want": want,
                              "hit": h, "shortlist_n": n,
                              "clusters": top, "members": short}) + "\n")
        out.flush()
        return h, n, short

    cases = [c for c in json.load(open(os.path.join(PROBE,
                                                    "eval_cases.json")))
             if c["kind"] == "pos"]
    conts = json.load(open(CONT))
    print(f"scoring: A={len(cases)} frozen positives, "
          f"B={sum(len(v) for v in conts.values())} continuations", flush=True)

    ra, na, ra_n = [], [], []
    per_tool_a = {}
    t0 = time.time()
    for i, c in enumerate(cases):
        h, n, short = score("A-utterance", c["utterance"], c["want"], c["id"])
        ra.append(h)
        na.append(n)
        per_tool_a.setdefault(c["want"], []).append(h)
        if (i + 1) % 50 == 0:
            print(f"  A {i + 1}/{len(cases)}, {time.time() - t0:.0f}s",
                  flush=True)

    rb, nb = [], []
    per_tool_b = {}
    for key, us in sorted(conts.items()):
        want = by_key.get(key)
        if want is None:
            continue
        for j, u in enumerate(us):
            h, n, short = score("B-continuation", u, want, f"{key}#{j}")
            rb.append(h)
            nb.append(n)
            per_tool_b.setdefault(want, []).append(h)

    recall_a = float(np.mean(ra))
    recall_b = float(np.mean(rb))
    shared = sorted(set(per_tool_a) & set(per_tool_b))
    d_tool = [float(np.mean(per_tool_b[t])) - float(np.mean(per_tool_a[t]))
              for t in shared]
    delta_tool = float(np.mean(d_tool))
    delta_pooled = recall_b - recall_a
    # Jaccard per tool, from the rows (A's shortlist is per-case; keep the
    # last utterance-case shortlist per tool — the paired B shortlist is
    # the same tool's continuation-keyed one)
    rows = [json.loads(line) for line in open(RESULTS)]
    a_short = {}
    for r in rows:
        if r["set"] == "A-utterance":
            a_short[r["want"]] = set(r["members"])
    b_short = {r["want"]: set(r["members"]) for r in rows
               if r["set"] == "B-continuation"}
    for t in shared:
        if t in a_short and t in b_short:
            u = a_short[t] | b_short[t]
            jac.append(len(a_short[t] & b_short[t]) / len(u) if u else 1.0)

    summary = {
        "n_A": len(ra), "n_B": len(rb),
        "recall_A_utterance": round(recall_a, 4),
        "recall_B_continuation": round(recall_b, 4),
        "delta_pooled": round(delta_pooled, 4),
        "delta_per_tool_mean": round(delta_tool, 4),
        "mean_shortlist_A": round(float(np.mean(na)), 1),
        "mean_shortlist_B": round(float(np.mean(nb)), 1),
        "shortlist_jaccard_mean": round(float(np.mean(jac)), 3) if jac else None,
        "bars": {"primary": recall_b >= BAR_PRIMARY,
                 "noninferiority": delta_pooled >= BAR_NONINF},
        "k": 5, "topn": TOPN,
    }
    json.dump(summary, open(SUMMARY, "w"), indent=1)
    print(json.dumps(summary, indent=1))
    out.close()


if __name__ == "__main__":
    main()
