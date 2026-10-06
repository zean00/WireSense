#!/usr/bin/env python
"""MS-5/MS-6 runner: flat vs description-built hierarchy, scored on eval_cases.

Arms: flat | hier-desc/top1 | hier-desc/top2 | hier-synth/top1 | hier-synth/top2.
All arms: production build_prompt + parse_tool_json, temp 0, id_slot 5, max_tokens
120, history-free. Routing: one utterance embedding (norm_router_text, id_slot 6),
cosine vs k=10 k-means centroids (greedy farthest-point init, deterministic;
Lloyd 50 iters, cosine on L2-normalized vectors), shortlist = union of top-N
clusters. Manifest line: "- {name}: {desc[:C]}; args: {required}" with C from
MS-2's measured budget (C_budget.json — runner refuses scored runs without it).

Output: results.jsonl (append, resume-safe), summary.json.
MS-8: infra exceptions retry once per case (logged); serving-variance reruns are
manual, both records kept.
"""
import json
import os
import sys
import time

import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "..", "web")
sys.path.insert(0, WEB)
sys.path.insert(0, os.path.join(HERE, ".."))
os.chdir(WEB)
import server  # noqa: E402

CORPUS = os.path.join(HERE, "corpus")
RESULTS = os.environ.get("MCP_PROBE_RESULTS",
                         os.path.join(HERE, "results.jsonl"))
SUMMARY = os.environ.get("MCP_PROBE_SUMMARY",
                         os.path.join(HERE, "summary.json"))
CANARY_N = int(os.environ.get("MCP_PROBE_CANARY", "0"))  # 0 = all cases
BUDGET = os.path.join(HERE, "C_budget.json")
K = 10
TOPN = {"top1": 1, "top2": 2}
CS = [200, 160, 120, 100, 80]
CEILING = 7168


def embed(text):
    r = requests.post(f"{server.LLAMA}/v1/embeddings", json={
        "input": server.norm_router_text(text), "model": "wiresense-llm",
        "id_slot": 6}, timeout=60)
    r.raise_for_status()
    return np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)


def l2(v):
    n = np.linalg.norm(v)
    return v / (n + 1e-12)


def load_tools():
    tools = []
    for fn in sorted(os.listdir(CORPUS)):
        d = json.load(open(os.path.join(CORPUS, fn)))
        for t in d["tools"]:
            schema = t.get("inputSchema") or {}
            req = [p for p in (schema.get("required") or [])
                   if p in (schema.get("properties") or {})]
            tools.append({"name": t["name"],
                          "desc": t.get("description") or "",
                          "server": fn[:-5], "required": req})
    return tools


def kmeans(vecs, k=K, iters=50):
    """Greedy farthest-point init (deterministic): first centroid = vector
    closest to the global mean, then max-min-distance picks; Lloyd with
    cosine assignment and renormalized means."""
    V = np.stack([l2(v) for v in vecs])
    mean = l2(V.mean(0))
    first = int(np.argmin(V @ mean))
    idx = [first]
    d = 1.0 - V @ V[first]
    for _ in range(k - 1):
        nxt = int(np.argmax(d))
        idx.append(nxt)
        d = np.minimum(d, 1.0 - V @ V[nxt])
    cents = V[idx].copy()
    assign = None
    for _ in range(iters):
        new_assign = np.argmax(V @ cents.T, axis=1)
        if assign is not None and np.array_equal(new_assign, assign):
            break
        assign = new_assign
        for c in range(k):
            m = assign == c
            if m.any():
                cents[c] = l2(V[m].mean(0))
    return cents, assign


def manifest_line(t, C):
    args = ", ".join(t["required"]) if t["required"] else "none"
    return f"- {t['name']}: {t['desc'][:C]}; args: {args}"


def directive_for(tools, C):
    lines = "\n".join(manifest_line(t, C) for t in tools)
    return ("Choose a tool for this request. Available tools:\n" + lines +
            '\nReply with ONLY a JSON object: {"tool": "<name>", "args": {...}} '
            'or {"tool": null} if no tool fits. No other text.')


def measure_budget(tools, probe):
    """MS-2: largest registered C whose flat prompt fits CEILING tokens."""
    for C in CS:
        p = server.build_prompt([], probe, directive_for(tools, C))
        r = requests.post(f"{server.LLAMA}/v1/completions", json={
            "prompt": p, "max_tokens": 1, "temperature": 0,
            "id_slot": server.SLOT}, timeout=300)
        r.raise_for_status()
        pt = r.json()["usage"]["prompt_tokens"]
        print(f"  C={C}: flat prompt_tokens={pt}")
        if pt <= CEILING:
            json.dump({"C": C, "flat_prompt_tokens": pt}, open(BUDGET, "w"))
            return C, pt
    raise RuntimeError("no registered C fits the ceiling")


def plan(utterance, directive):
    t0 = time.time()
    p = server.build_prompt([], utterance, directive)
    for attempt in (1, 2):  # MS-8 infra retry
        try:
            r = requests.post(f"{server.LLAMA}/v1/completions", json={
                "prompt": p, "max_tokens": 120, "temperature": 0,
                "cache_prompt": True, "id_slot": server.SLOT}, timeout=300)
            r.raise_for_status()
            body = r.json()
            break
        except Exception as e:
            if attempt == 2:
                return {"error": str(e)}
            print(f"    infra retry: {e}", flush=True)
    ms = (time.time() - t0) * 1000
    plan = server.parse_tool_json(body["choices"][0]["text"])
    return {"plan": plan, "prompt_tokens": body["usage"]["prompt_tokens"],
            "ms": ms}


def schema_ok(tool, args, tools_by_name):
    t = tools_by_name.get(tool)
    if not t:
        return False
    return all(a in (args or {}) for a in t["required"])


def main():
    arms = sys.argv[1:] or ["flat", "hier-desc/top1", "hier-desc/top2",
                            "hier-synth/top1", "hier-synth/top2"]
    tools = load_tools()
    tools_by_name = {t["name"]: t for t in tools}
    cases = json.load(open(os.path.join(HERE, "eval_cases.json")))
    print(f"{len(tools)} tools, {len(cases)} cases, arms={arms}")

    if os.path.exists(BUDGET):
        C = json.load(open(BUDGET))["C"]
        print(f"MS-2 budget from C_budget.json: C={C}")
    else:
        probe = "Take a screenshot of the page at example.com"
        C, pt = measure_budget(tools, probe)
        print(f"MS-2 measured: C={C} (flat prompt {pt} tokens <= {CEILING})")
    flat_dir = directive_for(tools, C)

    spaces = {}
    if any(a.startswith("hier-desc") for a in arms):
        t0 = time.time()
        dvecs = [l2(embed(f"{t['name']}: {t['desc']}")) for t in tools]
        cents, assign = kmeans(dvecs)
        spaces["hier-desc"] = {"cents": cents, "assign": assign,
                               "vecs": np.stack(dvecs)}
        print(f"hier-desc space built in {time.time()-t0:.0f}s; "
              f"cluster sizes={np.bincount(assign, minlength=K).tolist()}")
    if any(a.startswith("hier-synth") for a in arms):
        t0 = time.time()
        pools = json.load(open(os.path.join(HERE, "synth_pools.json")))
        svecs = []
        for t in tools:
            us = pools.get(f"{t['server']}__{t['name']}", [])[:8]
            svecs.append(l2(np.stack([l2(embed(u)) for u in us]).mean(0)))
        cents, assign = kmeans(svecs)
        spaces["hier-synth"] = {"cents": cents, "assign": assign,
                                "vecs": np.stack(svecs)}
        print(f"hier-synth space built in {time.time()-t0:.0f}s; "
              f"cluster sizes={np.bincount(assign, minlength=K).tolist()}")

    if CANARY_N:
        pos = [c for c in cases if c["kind"] == "pos"][:2]
        neg = [c for c in cases if c["kind"] == "neg"][:2]
        trap = [c for c in cases if c["kind"] == "trap"][:2]
        cases = pos + neg + trap
        print(f"CANARY subset: {[c['id'] for c in cases]}")

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
        if arm == "flat":
            d = flat_dir
            space = None
            n = 0
        else:
            space_name, _, topn_key = arm.partition("/")
            space = spaces[space_name]
            topn = TOPN[topn_key]
        n = 0
        for c in cases:
            if (arm, c["id"]) in done:
                continue
            route = None
            if arm == "flat":
                d = flat_dir
            else:
                v = l2(embed(c["utterance"]))
                sims = space["cents"] @ v
                order = np.argsort(-sims)[:topn]
                members = [i for i in np.argsort(-sims) if space["assign"][i] in set(order)]
                short = [tools[i] for i in members]
                route = {"clusters": [int(x) for x in order],
                         "shortlist": len(short)}
                d = directive_for(short, C)
            r = plan(c["utterance"], d)
            rec = {"arm": arm, "case_id": c["id"], "kind": c["kind"],
                   "want": c["want"], **r, **({"route": route} if route else {})}
            if "plan" in r and r["plan"] is not None:
                got = r["plan"]["tool"]
                rec["got"] = got
                rec["ok"] = (got == c["want"]) if c["want"] is not None else (got is None)
                if rec["ok"] and c["want"] is not None:
                    rec["schema_ok"] = schema_ok(got, r["plan"].get("args"),
                                                 tools_by_name)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            n += 1
            if n % 25 == 0:
                el = time.time() - t_start
                print(f"  {arm}: {n} new cases, {el/60:.1f} min elapsed", flush=True)
        print(f"arm {arm} done (+{n} cases)", flush=True)

    summarize()


def summarize():
    arms = {}
    for line in open(RESULTS):
        r = json.loads(line)
        a = arms.setdefault(r["arm"], {"n": 0, "ok": 0, "pt": [], "ms": [],
                                       "schema": [], "pos": [0, 0], "neg": [0, 0]})
        if "error" in r:
            continue
        a["n"] += 1
        a["ok"] += int(r.get("ok", False))
        a["pt"].append(r.get("prompt_tokens", 0))
        a["ms"].append(r.get("ms", 0))
        if r["kind"] in ("pos", "trap"):
            a["pos"][0] += int(r.get("ok", False))
            a["pos"][1] += 1
            if r.get("ok") and "schema_ok" in r:
                a["schema"].append(int(r["schema_ok"]))
        else:
            a["neg"][0] += int(r.get("ok", False))
            a["neg"][1] += 1
    summary = {}
    for arm, a in arms.items():
        summary[arm] = {
            "n": a["n"], "acc": round(a["ok"] / max(a["n"], 1), 4),
            "pos_acc": round(a["pos"][0] / max(a["pos"][1], 1), 4),
            "neg_acc": round(a["neg"][0] / max(a["neg"][1], 1), 4),
            "schema_valid_rate": round(sum(a["schema"]) / len(a["schema"]), 4) if a["schema"] else None,
            "mean_prompt_tokens": round(sum(a["pt"]) / len(a["pt"]), 1) if a["pt"] else 0,
            "mean_ms": round(sum(a["ms"]) / len(a["ms"]), 1) if a["ms"] else 0}
    json.dump(summary, open(SUMMARY, "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
