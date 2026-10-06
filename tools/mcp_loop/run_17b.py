#!/usr/bin/env python
"""MS-17b scorer: {tool, continue, done} readout over held-out state texts.

Deployed tree mechanics VERBATIM (registered): norm_router_text -> slot-6
embed -> DecideReadout._project -> norm_rows; per-class centroid = mean of
pool vectors -> norm_rows; classify = cosine argmax. No training, no
gating. Frozen inputs are deterministic: the results file is written
fresh, refuses to overwrite, and a complete file is resumed aggregate-only.
"""
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "..", "mcp_probe")
sys.path.insert(0, PROBE)
os.environ.setdefault("MCP_PROBE_K", "5")
import run_eval as probe  # noqa: E402  (chdirs to web/, imports server)

CONT = os.path.join(HERE, "continuations.json")
STATE = os.path.join(HERE, "state_texts.json")
RESULTS = os.path.join(HERE, "results_17b.jsonl")
SUMMARY = os.path.join(HERE, "summary_17b.json")
CLASSES = ["tool", "continue", "done"]
BAR_OVERALL = 0.75
BAR_TOOL_RECALL = 0.80
BAR_FALSE_TOOL = 0.15


def main():
    head = probe.server.DecideReadout()   # MODELS resolves from web/ cwd

    def vec(text):
        e = probe.embed(text)             # norm_router_text + slot 6
        return probe.l2(head._project(e[None, :])[0])

    conts = json.load(open(CONT))
    state = json.load(open(STATE))

    pools, evals = {c: [] for c in CLASSES}, {c: [] for c in CLASSES}
    for key, us in sorted(conts.items()):          # tool class: RECIPE_C reuse
        half = len(us) // 2
        pools["tool"] += [(u, key) for u in us[:half]]
        evals["tool"] += [(u, key) for u in us[half:]]
    for cls in ("continue", "done"):
        for key, us in sorted(state[cls].items()):
            half = len(us) // 2
            pools[cls] += [(u, key) for u in us[:half]]
            evals[cls] += [(u, key) for u in us[half:]]
    n_eval = sum(len(v) for v in evals.values())
    print("pools/eval per class: " +
          ", ".join(f"{c} {len(pools[c])}/{len(evals[c])}" for c in CLASSES),
          flush=True)

    if os.path.exists(RESULTS):
        rows = [json.loads(line) for line in open(RESULTS)]
        if len(rows) != n_eval:
            sys.exit(f"REFUSING: {RESULTS} has {len(rows)} rows, "
                     f"expected {n_eval}")
        print(f"resuming from {len(rows)} complete rows", flush=True)
    else:
        t0 = time.time()
        cents = {}
        for cls in CLASSES:
            V = np.stack([vec(u) for u, _ in pools[cls]])
            cents[cls] = probe.l2(V.mean(0))
            print(f"  centroid {cls} from {len(pools[cls])} pool texts "
                  f"({time.time() - t0:.0f}s)", flush=True)
        C = np.stack([cents[c] for c in CLASSES])
        out = open(RESULTS, "w")
        rows = []
        for cls in CLASSES:
            for i, (u, key) in enumerate(evals[cls]):
                v = vec(u)
                sims = C @ v
                pred = CLASSES[int(np.argmax(sims))]
                row = {"true": cls, "key": key, "i": i, "pred": pred,
                       "sims": {c: round(float(s), 3)
                                for c, s in zip(CLASSES, sims)}}
                out.write(json.dumps(row) + "\n")
                out.flush()
                rows.append(row)
            print(f"  scored {cls} ({len(evals[cls])})", flush=True)
        out.close()

    conf = {t: {p: 0 for p in CLASSES} for t in CLASSES}
    for r in rows:
        conf[r["true"]][r["pred"]] += 1
    recall = {c: conf[c][c] / max(1, sum(conf[c].values())) for c in CLASSES}
    overall = sum(conf[c][c] for c in CLASSES) / len(rows)
    nt = sum(conf[c]["tool"] for c in ("continue", "done"))
    false_tool = nt / max(1, sum(sum(conf[c].values())
                                 for c in ("continue", "done")))
    summary = {
        "n_eval": len(rows),
        "confusion": conf,
        "recall": {c: round(recall[c], 4) for c in CLASSES},
        "overall_acc": round(overall, 4),
        "false_tool_rate": round(false_tool, 4),
        "bars": {"overall": overall >= BAR_OVERALL,
                 "tool_recall": recall["tool"] >= BAR_TOOL_RECALL,
                 "false_tool": false_tool <= BAR_FALSE_TOOL},
    }
    json.dump(summary, open(SUMMARY, "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
