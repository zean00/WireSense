#!/usr/bin/env python
"""Adjudication stats for results_prefix_warm.json (post-data, reads only).

Part 1: cache_n by (mt_pop, L) — did the shared-prefix rewind ever fire?
Part 2: per (k, arm) medians of ttft/prompt_n/prompt_ms, cache_n spread,
WARM-COLD / WARM-FREE deltas, warm_ms, warm_cache_n, output identity.
"""
import json
import os
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
d = json.load(open(os.path.join(HERE, "results_prefix_warm.json")))

print("== part 1: threshold sweep ==")
g = defaultdict(list)
for r in d["threshold"]:
    g[(r["mt_pop"], r["shared_L"])].append(r)
for (mt, L), rows in sorted(g.items()):
    cn = sorted({r["cache_n"] for r in rows})
    lcp = sorted({r["lcp_n"] for r in rows})
    pm = st.median(r["prompt_ms"] for r in rows if r["prompt_ms"])
    print(f"mt_pop={mt} L={L:3d} lcp={lcp} cache_n={cn} "
          f"prompt_ms_med={pm:.1f}")

print("\n== part 2: ladder ==")
for k in d["k_ladder"]:
    rows = d["ladder"][str(k)]
    by_arm = defaultdict(list)
    for r in rows:
        by_arm[r.get("arm", "?")].append(r)
    print(f"\n-- k={k} (n/arm={len(rows) // max(len(by_arm), 1)})")
    meds = {}
    for arm in ("COLD", "FREE", "WARM"):
        rs = by_arm.get(arm, [])
        if not rs:
            continue
        tt = st.median(r["ttft_ms"] for r in rs if r["ttft_ms"] is not None)
        pn = st.median(r["prompt_n"] for r in rs)
        pm = st.median(r["prompt_ms"] for r in rs if r["prompt_ms"])
        cn = sorted({r["cache_n"] for r in rs})
        none_tt = sum(1 for r in rs if r["ttft_ms"] is None)
        meds[arm] = tt
        print(f"  {arm:4s} ttft_med={tt:7.1f}  prompt_n={pn:5.0f}  "
              f"prompt_ms={pm:7.1f}  cache_n={cn}  ttft_none={none_tt}")
    if "COLD" in meds:
        if "WARM" in meds:
            print(f"  WARM-COLD  = {meds['WARM'] - meds['COLD']:+7.1f} ms")
        if "FREE" in meds:
            print(f"  WARM-FREE  = {meds['WARM'] - meds['FREE']:+7.1f} ms")
            print(f"  FREE-COLD  = {meds['FREE'] - meds['COLD']:+7.1f} ms")
    wr = by_arm.get("WARM", [])
    if wr:
        wm = [r["warm_ms"] for r in wr if r.get("warm_ms") is not None]
        wcn = sorted({r.get("warm_cache_n") for r in wr})
        print(f"  warm_ms med={st.median(wm):.1f} "
              f"range=({min(wm):.1f},{max(wm):.1f})  warm_cache_n={wcn}")

print("\n== part 2: output identity per (k, scenario, rep) ==")
for k in d["k_ladder"]:
    rows = d["ladder"][str(k)]
    cell = defaultdict(dict)
    for r in rows:
        arm = r.get("arm")
        if arm:
            cell[(r["scenario"], r["rep"])][arm] = r["text"].strip()
    n = len(cell)
    eq_cf = sum(1 for c in cell.values()
                if len(c) == 3 and c["COLD"] == c["FREE"])
    eq_cw = sum(1 for c in cell.values()
                if len(c) == 3 and c["COLD"] == c["WARM"])
    eq_fw = sum(1 for c in cell.values()
                if len(c) == 3 and c["FREE"] == c["WARM"])
    full = sum(1 for c in cell.values() if len(c) == 3)
    print(f"k={k:2d}: cells={n} complete3={full}  "
          f"COLD==FREE {eq_cf}/{full}  COLD==WARM {eq_cw}/{full}  "
          f"FREE==WARM {eq_fw}/{full}")
