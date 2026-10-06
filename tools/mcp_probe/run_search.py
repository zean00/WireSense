"""MS-15 runner: tool-selection-as-a-tool.

Arms (see docs/PROTOCOL-mcp-selection.md MS-15, registered pre-data):
  search-primary      manifest = mcp_search only; model must search to see tools
  hier-synth-esc/top2 k=5 top-2 shortlist + mcp_search escape line

Search = slot-6 embedding of the query, cosine vs per-tool synth centroids,
top-8 tools returned; history-free single re-plan; one search per case max.
Reuses corpus / frozen eval / pools / C budget / planner params from run 2.
Results go to results_search.jsonl (run-2 files untouched).
"""
import os
import json
import time
import sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("MCP_PROBE_RESULTS", os.path.join(HERE, "results_search.jsonl"))
os.environ.setdefault("MCP_PROBE_K", "5")  # arm B cluster count
sys.path.insert(0, HERE)
import run_eval as re_  # noqa: E402  (module import chdirs to web/ + loads server)

RESULTS = re_.RESULTS
C = json.load(open(re_.BUDGET))["C"] if os.path.exists(re_.BUDGET) else 200

SEARCH_CARD = {"name": "mcp_search", "server": "meta",
               "desc": "Search the connected MCP servers for tools that match "
                       "a need. Use this first to discover which tool fits.",
               "required": ["query"]}
CONTRACT_PRIME = ('Reply with ONLY a JSON object: {"tool": "mcp_search", '
                  '"args": {"query": "<a few keywords>"}} to search for tools, '
                  'or {"tool": null} if this request needs no tool. No other text.')
CONTRACT3 = ('Reply with ONLY a JSON object: {"tool": "<name>", "args": {...}}, '
             'or {"tool": "mcp_search", "args": {"query": "<keywords>"}} to '
             'search for other tools, or {"tool": null} if no tool fits. '
             'No other text.')
CONTRACT_AFTER = ('Reply with ONLY a JSON object: {"tool": "<name>", '
                  '"args": {...}} or {"tool": null} if no tool fits. '
                  'No other text.')
ESCAPE_LINE = ("- mcp_search: search all connected MCP servers for a tool "
               "that fits; args: query")
N_RETURN = 8  # MS-15: top-8 schemas per search


def prime_directive():
    return ("Choose a tool for this request. Available tools:\n"
            + re_.manifest_line(SEARCH_CARD, C) + "\n" + CONTRACT_PRIME)


def esc_directive(short):
    return ("Choose a tool for this request. Available tools:\n"
            + "\n".join(re_.manifest_line(t, C) for t in short)
            + "\n" + ESCAPE_LINE + "\n" + CONTRACT3)


def result_directive(query, found):
    return (f'Choose a tool for this request. Your search for "{query}" '
            "returned:\n"
            + "\n".join(re_.manifest_line(t, C) for t in found)
            + "\n" + CONTRACT_AFTER)


def nn_search(query, svecs, k=N_RETURN):
    v = re_.l2(re_.embed(query))
    return np.argsort(-(svecs @ v))[:k]


def main():
    arms = sys.argv[1:] or ["search-primary", "hier-synth-esc/top2"]
    tools = re_.load_tools()
    tools_by_name = {t["name"]: t for t in tools}
    cases = json.load(open(os.path.join(HERE, "eval_cases.json")))
    if re_.CANARY_N:
        pos = [c for c in cases if c["kind"] == "pos"][:2]
        neg = [c for c in cases if c["kind"] == "neg"][:2]
        trap = [c for c in cases if c["kind"] == "trap"][:2]
        cases = pos + neg + trap
        print(f"CANARY subset: {[c['id'] for c in cases]}")
    print(f"{len(tools)} tools, {len(cases)} cases, arms={arms}, C={C}")

    pools = json.load(open(os.path.join(HERE, "synth_pools.json")))
    svecs = []
    for t in tools:
        us = pools.get(f"{t['server']}__{t['name']}", [])[:8]
        svecs.append(re_.l2(np.stack([re_.l2(re_.embed(u)) for u in us]).mean(0)))
    svecs = np.stack(svecs)
    space = None
    if any(a.startswith("hier-synth") for a in arms):
        cents, assign = re_.kmeans(svecs, k=re_.K)
        space = {"cents": cents, "assign": assign}
        print(f"hier-synth k={re_.K} space built; "
              f"sizes={np.bincount(assign, minlength=re_.K).tolist()}")

    done = set()
    if os.path.exists(RESULTS):
        for line in open(RESULTS):
            try:
                r = json.loads(line)
                done.add((r["arm"], r["case_id"]))
            except (ValueError, KeyError):
                pass
    print(f"{len(done)} prior results, resuming")
    out = open(RESULTS, "a")
    t_start = time.time()

    for arm in arms:
        arm_rec = arm + ("@k5" if arm.startswith("hier-synth") else "")
        n = 0
        for c in cases:
            if (arm_rec, c["id"]) in done:
                continue
            calls, pt_tot, ms_tot, searched = 0, 0, 0, False
            route = None
            if arm == "search-primary":
                d = prime_directive()
            else:
                v = re_.l2(re_.embed(c["utterance"]))
                sims = space["cents"] @ v
                order = np.argsort(-sims)[:2]
                top = set(int(x) for x in order)
                members = [i for i, a in enumerate(space["assign"]) if a in top]
                short = [tools[i] for i in members]
                route = {"clusters": [int(x) for x in order],
                         "shortlist": len(short)}
                d = esc_directive(short)
            r = re_.plan(c["utterance"], d)
            calls += 1
            pt_tot += r.get("prompt_tokens", 0)
            ms_tot += r.get("ms", 0)
            if "error" in r:
                out.write(json.dumps({"arm": arm_rec, "case_id": c["id"],
                                      "kind": c["kind"], "want": c["want"],
                                      "error": r["error"], "calls": calls,
                                      "search": False}) + "\n")
                out.flush()
                continue
            pick = r["plan"]
            if arm != "search-primary" and pick and \
                    pick.get("tool") == "mcp_search":
                searched = True
                q = (pick.get("args") or {}).get("query") or c["utterance"]
                found = [tools[i] for i in nn_search(q, svecs)]
                route = route or {}
                route.update({"query": q,
                              "returned": [t["name"] for t in found]})
                r2 = re_.plan(c["utterance"], result_directive(q, found))
                calls += 1
                pt_tot += r2.get("prompt_tokens", 0)
                ms_tot += r2.get("ms", 0)
                pick = r2["plan"] if "plan" in r2 else None
            rec = {"arm": arm_rec, "case_id": c["id"], "kind": c["kind"],
                   "want": c["want"], "prompt_tokens": pt_tot, "ms": ms_tot,
                   "calls": calls, "search": searched}
            if route:
                rec["route"] = route
            if pick is not None:
                got = pick.get("tool")
                rec["got"] = got
                if arm == "search-primary" and got not in (None, "mcp_search"):
                    rec["ok"] = False  # MS-15: no recovery for blind picks
                else:
                    rec["ok"] = ((got == c["want"])
                                 if c["want"] is not None else (got is None))
                    if rec["ok"] and c["want"] is not None:
                        rec["schema_ok"] = re_.schema_ok(got,
                                                         pick.get("args"),
                                                         tools_by_name)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            n += 1
            if n % 25 == 0:
                print(f"  {arm}: {n} new, {(time.time()-t_start)/60:.1f} min",
                      flush=True)
        print(f"arm {arm} done (+{n})", flush=True)
    re_.summarize()


if __name__ == "__main__":
    main()
