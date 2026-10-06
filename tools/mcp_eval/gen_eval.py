#!/usr/bin/env python
"""MS-16b per-server deployment evals: generate + FREEZE, no scoring here.

Usage: gen_eval.py <server> [<server> ...]   (servers must be in SERVERS below)

Per server: connect live, tools/list, one RECIPE_B8 call per tool (seed 2,
modeled on the MS-4 recipe B text, registered 8 direct + 8 indirect), positives
capped at 64 per server by deterministic stride sample, plus the probe's 24
hand negatives copied verbatim. Frozen to evals/<server>.json BEFORE any
scoring; an existing frozen file is never regenerated (house rule) — deletion
requires a protocol amendment naming the reason.
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.abspath(os.path.join(HERE, "..", "..", "web"))
sys.path.insert(0, WEB)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "mcp_probe")))

import requests  # noqa: E402
import mcp_client  # noqa: E402

# MS-4 recipe B, widened to the registered 8+8 (protocol MS-16b)
RECIPE_B8 = ('Write 8 short things a user might say to a voice assistant for '
             'this tool. "directs": plain direct requests for what the tool '
             'does, do not copy the tool name, max 14 words each. '
             '"indirects": goal-oriented phrasings where the tool is implied '
             'but never named, max 16 words each. Reply with ONLY JSON: '
             '{"directs": ["...", ...], "indirects": ["...", ...]}')
# verbatim copy of tools/mcp_probe/gen_eval.py HAND_NEG (frozen list)
HAND_NEG = [
    "What's a deference span",
    "Set a timer for 20 minutes",
    "Remind me about the dentist on Friday",
    "Make a note that the wifi password is on the fridge",
    "How are you doing today",
    "Write a haiku about autumn",
    "What's 17 times 23",
    "Tell me a joke",
    "What's the weather in Jakarta",
    "Good morning",
    "What did you just say",
    "Stop",
    "Who made you",
    "Say that again louder",
    "I love this song",
    "Are you real",
    "Thank you",
    "What's your favorite color",
    "Explain quantum computing simply",
    "Translate hello to French",
    "Spell entrepreneurship",
    "How do you make bread",
    "Open the pod bay doors",
    "What's my name",
]
POS_CAP = 64
SERVERS = {
    "filesystem": {"cmd": ["npx", "-y",
                           "@modelcontextprotocol/server-filesystem", "/tmp"]},
    "time": {"cmd": ["uvx", "mcp-server-time", "--local-timezone",
                     "Asia/Jakarta"]},
    "everything": {"cmd": ["npx", "-y",
                           "@modelcontextprotocol/server-everything"]},
    "git": {"cmd": ["uvx", "mcp-server-git", "--repository",
                    "/home/sahal/workspace/wiresense"]},
}


def parse_json_obj(text):
    import re
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def gen(card, seed=2, max_tokens=560):
    sys.path.insert(0, WEB)
    import server
    prompt = server.build_prompt([], card, RECIPE_B8)
    # MS-16c: the model can emit EOS mid-JSON on a specific card+seed
    # (diagnosed finish=stop with no closing brace) — one retry at seed+1
    for seed_i in (seed, seed + 1):
        r = requests.post(f"{server.LLAMA}/v1/completions", json={
            "prompt": prompt, "max_tokens": max_tokens, "temperature": 0.8,
            "seed": seed_i, "id_slot": server.SLOT}, timeout=120)
        r.raise_for_status()
        j = parse_json_obj(r.json()["choices"][0]["text"])
        if j and isinstance(j.get("directs"), list) \
                and isinstance(j.get("indirects"), list) \
                and j["directs"] and j["indirects"]:
            return j
    return None


def stride_sample(items, cap):
    if len(items) <= cap:
        return items
    n = len(items)
    return [items[round(i * n / cap)] for i in range(cap)]


def main():
    wanted = sys.argv[1:]
    assert wanted, "usage: gen_eval.py <server> ..."
    os.makedirs(os.path.join(HERE, "evals"), exist_ok=True)
    for name in wanted:
        out_path = os.path.join(HERE, "evals", f"{name}.json")
        if os.path.exists(out_path):
            print(f"{name}: frozen eval already exists — leaving untouched")
            continue
        cfg = dict(SERVERS[name])
        srv = mcp_client.MCPServer({"name": name, **cfg})
        srv.start()
        print(f"{name}: {len(srv.tools)} tools live")
        cases, fails = [], []
        for t in srv.tools:
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            req = [p for p in (schema.get("required") or []) if p in props]
            desc = t.get("description") or ""
            card = (f'Tool "{t["name"]}" from server "{name}".\n'
                    f'Description: {desc}\n'
                    f'Required args: {", ".join(req) if req else "none"}')
            try:
                j = gen(card)
            except Exception as e:
                fails.append(f'{t["name"]}: {type(e).__name__}')
                continue
            if not (j and isinstance(j.get("directs"), list)
                    and isinstance(j.get("indirects"), list)):
                fails.append(f"{t['name']}: unparseable generation")
                continue
            pairs = [("direct", d) for d in j["directs"][:8]] + \
                    [("indirect", i) for i in j["indirects"][:8]]
            for flavor, u in pairs:
                cases.append({"id": f"pos_{name}__{t['name']}__{flavor}_"
                                    f"{len([c for c in cases])}",
                              "kind": "pos", "utterance": str(u),
                              "want": t["name"], "flavor": flavor})
        pos = stride_sample([c for c in cases if c["kind"] == "pos"], POS_CAP)
        for i, u in enumerate(HAND_NEG):
            pos.append({"id": f"neg_{i:02d}", "kind": "neg",
                        "utterance": u, "want": None, "flavor": "hand"})
        srv.kill()
        with open(out_path, "w") as f:
            json.dump({"server": name, "generated": datetime.date.today()
                       .isoformat(), "recipe": RECIPE_B8, "seed": 2,
                       "pos_cap": POS_CAP, "cases": pos}, f, indent=1)
        npos = sum(1 for c in pos if c["kind"] == "pos")
        print(f"{name}: FROZEN {npos} pos + {len(pos)-npos} neg -> {out_path}"
              + (f"  (generation failures: {fails})" if fails else ""))


if __name__ == "__main__":
    main()
