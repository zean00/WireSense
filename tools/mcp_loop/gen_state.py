#!/usr/bin/env python
"""MS-17b generator: RECIPE_D (continue) + RECIPE_E (done) state texts.

Mirrors the MS-17a mechanics exactly (registered): card/build_prompt/
220 max_tokens/temp 0.8/id_slot 5; continue seed 5, done seed 7, ONE
retry at seed+1; freezes to state_texts.json, refuses overwrite. The
tool class is NOT generated here — MS-17a's frozen continuations.json
is reused verbatim.
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

OUT = os.path.join(HERE, "state_texts.json")
RECIPES = {
    "continue": (
        'Write 8 different short things an assistant might say or think '
        'MID-ANSWER while reasoning from what it already knows, where it '
        'does NOT need any tool, lookup, or outside information. It is '
        'working on this tool\'s topic but should NOT use the tool or '
        'mention it. One or two sentences; never the tool name; max 24 '
        'words each. Reply with ONLY JSON: {"utterances": ["...", ...]}',
        5),
    "done": (
        'Write 8 different short COMPLETE final answers (1-2 sentences) '
        'an assistant might give a user on this tool\'s topic, fully '
        'resolving the question from known information WITHOUT calling '
        'this tool or any other; never mention the tool. Max 30 words '
        'each. Reply with ONLY JSON: {"utterances": ["...", ...]}',
        7),
}


def gen(tool, recipe, seed):
    card = (f'Tool "{tool["name"]}" from server "{tool["server"]}".\n'
            f'Description: {tool["desc"]}\n'
            f'Required args: {", ".join(tool["required"]) if tool["required"] else "none"}')
    prompt = probe.server.build_prompt([], card, recipe)
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
    out = {}
    for cls, (recipe, seed) in RECIPES.items():
        texts, failed, retried = {}, [], 0
        t0 = time.time()
        for i, t in enumerate(tools):
            key = f"{t['server']}__{t['name']}"
            us = gen(t, recipe, seed)
            if us is None:
                retried += 1
                us = gen(t, recipe, seed + 1) or []
                if not us:
                    failed.append(key)
            texts[key] = us
            if (i + 1) % 50 == 0:
                print(f"  {cls} {i + 1}/{len(tools)}, "
                      f"{time.time() - t0:.0f}s", flush=True)
        out[cls] = texts
        print(f"{cls}: {sum(len(v) for v in texts.values())} texts "
              f"({retried} retries, failures: {failed or 'none'})", flush=True)
    json.dump(out, open(OUT, "w"), indent=1)
    print("FROZEN", flush=True)


if __name__ == "__main__":
    main()
