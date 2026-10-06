#!/usr/bin/env python
"""MS-3/MS-4 generator: synth pools (recipe A, seed 1) + eval cases (recipe B,
seed 2), plus the hand-written negatives and traps. Run AFTER corpus freeze,
BEFORE eval freeze. Output: synth_pools.json, eval_cases.json, /tmp/audit.txt.

All scored runs read eval_cases.json read-only; corrections happen by editing
this generator's HAND_* tables (or hand-fixes recorded in the freeze commit)
and regenerating BEFORE the freeze commit.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "..", "web")
sys.path.insert(0, WEB)
sys.path.insert(0, os.path.join(HERE, ".."))
os.chdir(WEB)
import server  # noqa: E402
import requests  # noqa: E402

CORPUS = os.path.join(HERE, "corpus")

RECIPE_A = ('Write 8 different short things a user might SAY to a voice '
            'assistant that should trigger this tool. Casual, varied everyday '
            'phrasing, never use the tool name in the text, max 12 words each. '
            'Reply with ONLY JSON: {"utterances": ["...", ...]}')
RECIPE_B = ('Write two short things a user might say to a voice assistant. '
            '"direct": a plain direct request for what this tool does, do not '
            'copy the tool name, max 14 words. "indirect": a goal-oriented '
            'phrasing where this tool is implied but never named, max 16 '
            'words. Reply with ONLY JSON: {"direct": "...", "indirect": "..."}')

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

HAND_TRAPS = [
    ("What time is it right now", "get_current_time"),
    ("Convert 3pm Jakarta time to New York time", "convert_time"),
    ("Commit all my changes with the message 'fix lint'", "git_commit"),
    ("Save this snippet to a gist so I can share it", "create_gist"),
    ("Fork the microsoft/vscode repository to my account", "fork_repository"),
    ("What's the latest release version of sahals/wiresense", "get_latest_release"),
    ("Merge pull request number 42", "merge_pull_request"),
    ("Take a screenshot of the page at example.com", "puppeteer_screenshot"),
    ("Open a browser to news.ycombinator.com and check the top story", "puppeteer_navigate"),
    ("Show the files in my Documents folder", "list_directory"),
    ("What do you remember about the Hoffmann project", "search_nodes"),
    ("Pull the content of https://example.com and summarize it", "fetch"),
]


def tool_card(name, desc, server_name, req):
    return (f'Tool "{name}" from server "{server_name}".\n'
            f'Description: {desc}\n'
            f'Required args: {", ".join(req) if req else "none"}')


def parse_json_obj(text):
    """Raw JSON extraction — NOT server.parse_tool_json (that one requires a
    "tool" key; generation prompts ask for other shapes)."""
    import re
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def gen_json(card, directive, max_tokens, seed):
    prompt = server.build_prompt([], card, directive)
    r = requests.post(f"{server.LLAMA}/v1/completions", json={
        "prompt": prompt, "max_tokens": max_tokens, "temperature": 0.8,
        "seed": seed, "id_slot": server.SLOT}, timeout=120)
    r.raise_for_status()
    return parse_json_obj(r.json()["choices"][0]["text"])


def main():
    tools = []
    for fn in sorted(os.listdir(CORPUS)):
        d = json.load(open(os.path.join(CORPUS, fn)))
        sname = fn[:-5]
        for t in d["tools"]:
            schema = t.get("inputSchema") or {}
            req = schema.get("required") or []
            props = schema.get("properties") or {}
            req = [p for p in req if p in props]
            tools.append({"name": t["name"], "desc": t.get("description") or "",
                          "server": sname, "required": req})
    print(f"{len(tools)} tools loaded")

    pools, cases = {}, []
    fails = []
    for i, t in enumerate(tools):
        card = tool_card(t["name"], t["desc"], t["server"], t["required"])
        a = gen_json(card, RECIPE_A, 220, seed=1)
        b = gen_json(card, RECIPE_B, 160, seed=2)
        if a and isinstance(a.get("utterances"), list) and len(a["utterances"]) >= 5:
            pools[f'{t["server"]}__{t["name"]}'] = [str(u) for u in a["utterances"]][:8]
        else:
            fails.append(f'pool {t["server"]}__{t["name"]}')
        if b and isinstance(b.get("direct"), str) and isinstance(b.get("indirect"), str):
            base = f'pos_{t["server"]}__{t["name"]}'
            cases.append({"id": base + "__direct", "kind": "pos",
                          "utterance": b["direct"], "want": t["name"],
                          "server": t["server"], "flavor": "direct"})
            cases.append({"id": base + "__indirect", "kind": "pos",
                          "utterance": b["indirect"], "want": t["name"],
                          "server": t["server"], "flavor": "indirect"})
        else:
            fails.append(f'eval {t["server"]}__{t["name"]}')
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(tools)} tools, {len(fails)} fails, "
                  f"{time.time()-T0:.0f}s")

    for i, u in enumerate(HAND_NEG):
        cases.append({"id": f"neg_{i:02d}", "kind": "neg", "utterance": u,
                      "want": None, "server": None, "flavor": "hand"})
    for i, (u, want) in enumerate(HAND_TRAPS):
        cases.append({"id": f"trap_{i:02d}", "kind": "trap", "utterance": u,
                      "want": want, "server": None, "flavor": "hand"})

    with open(os.path.join(HERE, "synth_pools.json"), "w") as f:
        json.dump(pools, f, indent=1)
    with open(os.path.join(HERE, "eval_cases.json"), "w") as f:
        json.dump(cases, f, indent=1)

    with open("/tmp/audit.txt", "w") as f:
        for t in tools:
            key = f'{t["server"]}__{t["name"]}'
            f.write(f"### {key}\n")
            for u in pools.get(key, []):
                f.write(f"  pool: {u}\n")
        for c in cases:
            if c["kind"] in ("pos", "trap"):
                f.write(f'{c["id"]}  [{c.get("flavor","")}] {c["utterance"]}  => {c["want"]}\n')
            else:
                f.write(f'{c["id"]}  [neg] {c["utterance"]}  => null\n')
    print(f"done: {len(pools)} pools, {len(cases)} cases, fails={fails or 'none'}")


T0 = time.time()
if __name__ == "__main__":
    main()
