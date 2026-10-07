#!/usr/bin/env python
"""Adjudication stats for results_large_ctx.json (post-data, read-only).

D1: COLD TTFT curve across rungs + marginal ms/token + LSQ fit (with the
original 734-token point for reference). D2: any cache_n > 0 anywhere.
D3: WARM-COLD deltas. Plus warm_ms, identity cells.
"""
import json
import os
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
d = json.load(open(os.path.join(HERE, "results_large_ctx.json")))
print(f"slot_n_ctx={d['slot_n_ctx']} fixture_pairs={d['fixture_pairs']} "
      f"sha_ok={d['fixture_sha256'][:12]} started={d['started']}")

# D2 first: any cache reuse anywhere?
cn_nonzero = [(r["arm"], r.get("scenario"), r.get("rep"), t,
               r["cache_n"], r.get("warm_cache_n"))
              for t, blk in d["ladder"].items() for r in blk["rows"]
              if r["cache_n"] not in (0, None) or r.get("warm_cache_n")
              not in (0, None)]
print(f"\nD2 cache_n>0 rows: {len(cn_nonzero)}")
for row in cn_nonzero[:10]:
    print("  ", row)

print("\nD1/D3 per rung:")
xs, ys = [], []   # COLD medians
for t, blk in d["ladder"].items():
    rows = blk["rows"]
    by_arm = defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)
    print(f"-- target={t} k={blk['k_pairs']} pairs "
          f"prompt_n_ref={blk['prompt_n_ref']}")
    meds = {}
    for arm in ("COLD", "FREE", "WARM"):
        rs = by_arm[arm]
        tt = st.median(r["ttft_ms"] for r in rs if r["ttft_ms"] is not None)
        pn = st.median(r["prompt_n"] for r in rs)
        pm = st.median(r["prompt_ms"] for r in rs if r["prompt_ms"])
        none_tt = sum(1 for r in rs if r["ttft_ms"] is None)
        meds[arm] = (tt, pn, pm)
        print(f"  {arm:4s} ttft={tt:8.1f}  prompt_n={pn:5.0f}  "
              f"prompt_ms={pm:8.1f}  ttft_none={none_tt}")
    wr = by_arm["WARM"]
    wm = [r["warm_ms"] for r in wr if r.get("warm_ms") is not None]
    print(f"  warm_ms med={st.median(wm):.1f} range=({min(wm):.1f},"
          f"{max(wm):.1f})  WARM-COLD={meds['WARM'][0]-meds['COLD'][0]:+.1f}"
          f"  WARM-FREE={meds['WARM'][0]-meds['FREE'][0]:+.1f}")
    xs.append(meds["COLD"][1])
    ys.append(meds["COLD"][0])

print("\nCOLD marginal ms/token between consecutive rungs:")
for i in range(1, len(xs)):
    print(f"  {xs[i-1]:.0f}->{xs[i]:.0f}: "
          f"{(ys[i]-ys[i-1])/(xs[i]-xs[i-1]):.3f} ms/token")

# LSQ including the original 734-token point from the registered run
xs_all = xs + [734.0]
ys_all = ys + [331.35]
n = len(xs_all)
mx, my = sum(xs_all) / n, sum(ys_all) / n
slope = sum((x - mx) * (y - my) for x, y in zip(xs_all, ys_all)) / \
    sum((x - mx) ** 2 for x in xs_all)
inter = my - slope * mx
print(f"\nLSQ (4 rungs + 734-pt): ttft = {inter:.1f} + {slope:.4f}*prompt_n")
for tgt in (16384, 32768, 50000, 65536):
    print(f"  extrapolated @{tgt}: {inter + slope * tgt/1000:.1f} s")

print("\nidentity per rung (cells with all 3 arms):")
for t, blk in d["ladder"].items():
    cell = defaultdict(dict)
    for r in blk["rows"]:
        cell[(r["scenario"], r["rep"])][r["arm"]] = r["text"].strip()
    full = [c for c in cell.values() if len(c) == 3]
    eq = lambda a, b: sum(1 for c in full if c[a] == c[b])
    print(f"  target={t}: cells={len(cell)} complete3={len(full)} "
          f"COLD==FREE {eq('COLD','FREE')}/{len(full)}  "
          f"COLD==WARM {eq('COLD','WARM')}/{len(full)}")
