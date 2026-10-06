#!/usr/bin/env python
"""MS-17a generator: RECIPE_C continuation texts, one frozen file.

Mirrors mcp_gate._gen_pool mechanics exactly (registered in MS-17a):
same card, build_prompt, 220 max_tokens, temp 0.8, id_slot 5; seed 3
with ONE retry at seed 4 (MS-16c rule). Refuses to overwrite the frozen
output — frozen evals are never regenerated.
"""
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "..", "mcp_probe")
WEB = os.path.join(HERE, "..", "..", "web")
sys.path.insert(0, PROBE)
sys.path.insert(0, WEB)
import run_eval as probe  # noqa: E402  (chdirs to web/, imports server)
from mcp_gate import parse_json_obj  # noqa: E402

OUT = os.path.join(HERE, "continuations.json")
RECIPE_C = ('Write 8 different short things an assistant might SAY or THINK '
            'mid-answer, right before it should use this tool. One or two '
            'sentences of the assistant\'s own reasoning or speech that leads '
            'naturally into the call; never the tool name; max 24 words each. '
            'Reply with ONLY JSON: {"utterances": ["...", ...]}')


def gen(tool, seed):
    card = (f'Tool "{tool["name"]}" from server "{tool["server"]}".\n'
            f'Description: {tool["desc"]}\n'
            f'Required args: {", ".join(tool["required"]) if tool["required"] else "none"}')
    prompt = probe.server.build_prompt([], card, RECIPE_C)
    r = requests.post(f"{probe.server.LLAMA}/v1/completions", json={
        "prompt": prompt, "max_tokens": 220, "temperature": 0.8,
        "seed": seed, "id_slot": 5}, timeout=120)
    r.raise_for_status()
    j = parse_json_obj(r.json()["choices"][0]["text"])
    if j and isinstance(j.get("utterances"), list) \
            and len(j["utterances"]) >= 5:
        return [str(u).strip() for u in j["utterances"] if str(u).strip()][:8]
    return None


def main():
    if os.path.exists(OUT):
        sys.exit(f"REFUSING: {OUT} exists (frozen before scoring)")
    tools = probe.load_tools()
    print(f"{len(tools)} tools; freezing to {OUT}", flush=True)
    out, failed, retried = {}, [], 0
    t0 = time.time()
    for i, t in enumerate(tools):
        key = f"{t['server']}__{t['name']}"
        us = gen(t, 3)
        if us is None:
            retried += 1
            us = gen(t, 4) or []
            if not us:
                failed.append(key)
        out[key] = us
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(tools)} tools, {time.time() - t0:.0f}s",
                  flush=True)
    n = sum(len(v) for v in out.values())
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"FROZEN: {n} continuations over {len(out)} tools "
          f"({retried} retries, failures: {failed or 'none'})", flush=True)


if __name__ == "__main__":
    main()
