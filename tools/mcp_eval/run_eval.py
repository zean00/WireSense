#!/usr/bin/env python
"""MS-16b per-server deployment eval runner: score frozen evals through the
PRODUCTION selector (mcp_gate.Gate over the combined enabled server set —
same pools, same directive, same planner params), then adjudicate against
the registered bar (pos >= 0.60 AND neg >= 20/24).

Usage: run_eval.py <server> [<server> ...]   (frozen evals must exist)
Results append to results/<server>.jsonl (resume by (server, case_id));
results/<server>.verdict lines are append-only history. Frozen evals are
read-only here.
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.abspath(os.path.join(HERE, "..", "..", "web"))
sys.path.insert(0, WEB)

import mcp_client  # noqa: E402
import mcp_gate  # noqa: E402
from gen_eval import SERVERS  # noqa: E402  (same dir, run as a script)

BAR_POS = 0.60
BAR_NEG = 20  # of 24
SUFFIX = os.environ.get("MCP_EVAL_SUFFIX", "")  # MS-16b-2: "b9" -> .b9.json


def norm_got(got):
    return got.rsplit("__", 1)[-1] if isinstance(got, str) else got


async def main():
    wanted = sys.argv[1:]
    assert wanted, "usage: run_eval.py <server> ..."
    import server
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)

    servers = []
    for name in wanted:
        servers.append(mcp_client.MCPServer({"name": name,
                                             **SERVERS[name]}))
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: [s.start() for s in servers])
    gate = mcp_gate.Gate(server.LLAMA, server.norm_router_text,
                         server.native_manifest_lines(), servers,
                         cache_path=os.path.join(WEB, "mcp_index.json"),
                         build_prompt=server.build_prompt)
    await asyncio.to_thread(gate.build_index)
    print(f"selector: {'gate' if gate.active() else 'flat'} over "
          f"{len(gate.tools)} MCP tools + native")

    for name in wanted:
        evals = json.load(open(os.path.join(HERE, "evals",
                                            f"{name}{SUFFIX}.json")))
        res_path = os.path.join(HERE, "results", f"{name}{SUFFIX}.jsonl")
        done = set()
        if os.path.exists(res_path):
            for line in open(res_path):
                try:
                    r = json.loads(line)
                    done.add(r["case_id"])
                except (ValueError, KeyError):
                    pass
        out = open(res_path, "a")
        todo = [c for c in evals["cases"] if c["id"] not in done]
        print(f"{name}: {len(todo)} cases to run ({len(done)} done)")
        for i, c in enumerate(todo):
            try:
                d = gate.directive_for(c["utterance"])
                loop = asyncio.get_running_loop()
                r = await loop.run_in_executor(None, _plan, server, d,
                                               c["utterance"])
                rec = {"case_id": c["id"], "kind": c["kind"], **r}
                pick = r.get("plan")
                if pick is not None:
                    got = norm_got(pick.get("tool"))
                    rec["got"] = got
                    rec["ok"] = ((got == c["want"]) if c["want"] is not None
                                 else (got is None))
                    if rec["ok"] and c["want"] is not None:
                        e = next((t for t in gate.tools if t["name"] == got),
                                 None)
                        args = pick.get("args") or {}
                        rec["schema_ok"] = bool(e) and all(
                            a in args for a in e["required"])
                out.write(json.dumps(rec) + "\n")
                out.flush()
            except Exception as e:
                out.write(json.dumps({"case_id": c["id"], "kind": c["kind"],
                                      "error": str(e)[:120]}) + "\n")
                out.flush()
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(todo)}", flush=True)
        out.close()
        adjudicate(name, res_path)
    for s in servers:
        s.kill()


def _plan(server, directive, utterance):
    import requests
    p = server.build_prompt([], utterance, directive)
    resp = requests.post(f"{server.LLAMA}/v1/completions", json={
        "prompt": p, "max_tokens": 120, "temperature": 0,
        "cache_prompt": True, "id_slot": server.SLOT}, timeout=300)
    resp.raise_for_status()
    body = resp.json()
    return {"plan": server.parse_tool_json(body["choices"][0]["text"]),
            "prompt_tokens": body["usage"]["prompt_tokens"]}


def adjudicate(name, res_path):
    n = ok = pos_ok = pos_n = neg_ok = neg_n = sch_ok = sch_n = 0
    for line in open(res_path):
        r = json.loads(line)
        if "error" in r:
            continue
        n += 1
        ok += int(bool(r.get("ok")))
        if r["kind"] == "neg":
            neg_n += 1
            neg_ok += int(bool(r.get("ok")))
        else:
            pos_n += 1
            pos_ok += int(bool(r.get("ok")))
            if r.get("ok") and "schema_ok" in r:
                sch_n += 1
                sch_ok += int(r["schema_ok"])
    pa, na = pos_ok / max(pos_n, 1), neg_ok / max(neg_n, 1)
    passed = pa >= BAR_POS and neg_ok >= BAR_NEG
    print(f"\n{name}: n={n} acc={ok/max(n,1):.4f}  pos={pos_ok}/{pos_n} "
          f"({pa:.4f}, bar {BAR_POS})  neg={neg_ok}/{neg_n} "
          f"(bar {BAR_NEG}/24)  schema={sch_ok}/{sch_n}")
    print(f"VERDICT {name}: {'PASS' if passed else 'FAIL'} "
          f"(pos_acc>={BAR_POS} and neg>={BAR_NEG}/24)")
    verdict = {"pos": round(pa, 4), "neg": neg_ok, "neg_n": neg_n,
               "passed": passed}
    with open(os.path.join(HERE, "results", f"{name}{SUFFIX}.verdict"),
              "a") as f:
        f.write(json.dumps(verdict) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
