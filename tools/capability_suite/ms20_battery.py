#!/usr/bin/env python
"""MS-20 (docs/PROTOCOL-deep-agents.md): manifest plan history H / L2 / N.

36 plan calls (12 frames x 3 arms), temp 0, production directive_for
shortlists + native lines, fixture history = the capability suite's exact
tool-turn pairs. Aggregate only: per-arm correct-tool / wrong-execution /
null counts + per-frame picks.
"""
import asyncio
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.abspath(os.path.join(HERE, "..", "..", "web"))
sys.path.insert(0, WEB)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "mcp_eval")))
os.chdir(WEB)

import requests  # noqa: E402
import mcp_client  # noqa: E402
import mcp_gate  # noqa: E402
import server  # noqa: E402
import deep_think  # noqa: E402

FRAMES = [  # (utterance, expected tool or None)
    ("Remind me to stretch in five seconds", "reminder"),
    ("Remind me to call mom tomorrow at nine", "reminder"),
    ("Remind me to water the plants in an hour", "reminder"),
    ("Remind me to take out the trash when I get home", "reminder"),
    ("Set a timer for 10 minutes", "timer"),
    ("Start a 30 second timer", "timer"),
    ("Set a countdown for two minutes", "timer"),
    ("Make a note that the meeting moved to Friday", "note"),
    ("Take a note: buy milk tomorrow", "note"),
    ("What time is it in Tokyo?", "time__get_current_time"),
    ("What's the time in Berlin right now?", "time__get_current_time"),
    ("What's the weather in Jakarta?", None),
]

HISTORY = [
    ("Set a timer for 10 seconds", "Just to confirm — a timer for 10 seconds?"),
    ("yes", "The 10-second timer has started."),
    ("Remind me to stretch in five seconds",
     "Just to confirm — a reminder to stretch, in 5 seconds?"),
    ("yes", "Your reminder to stretch in 5 seconds has been saved."),
    ("Make a note that the coverage suite ran",
     "The note that the coverage suite ran has been saved."),
    ("Read the file wiresense_notes.txt in /tmp and tell me what it says",
     "The file confirms that the filesystem server is working end to end."),
    ("What time is it in Tokyo right now?",
     "It is currently Wednesday, October 7, 2026, at 9:09 AM in Tokyo."),
    ("Show the git status of the current project",
     "I couldn't retrieve the git status for the current project."),
    ("Create a file named suite_cap.txt in /tmp containing the word hello",
     "Just to confirm — run filesystem__write_file?"),
    ("yes", "The file /tmp/suite_cap.txt has been created with the word hello."),
]


def expected(plan, want):
    tool = (plan or {}).get("tool")
    if want is None:
        return "correct" if tool is None else "wrong_exec"
    if tool == want:
        return "correct"
    return "wrong_exec" if tool is not None else "null_fall"


async def main():
    server.KOKORO = server.Kokoro(f"{server.MODELS}/kokoro-v1.0.onnx",
                                  f"{server.MODELS}/voices.npz")
    server.READOUT = server.DecideReadout()
    server.WALKER = server.TreeWalker(server.READOUT._project)
    cfg = deep_think.load_config().get("mcp") or {}
    servers = mcp_client.from_config(cfg) or []
    gate = mcp_gate.Gate(server.LLAMA, server.norm_router_text,
                         server.native_manifest_lines(), servers,
                         cache_path=server.MCP_INDEX_PATH,
                         build_prompt=server.build_prompt)
    gate.build_index()
    server.MCP_GATE = gate

    async def gen_plan(utterance, history):
        directive = gate.directive_for(utterance)
        r = await asyncio.get_event_loop().run_in_executor(
            None, lambda: requests.post(
                f"{server.LLAMA}/v1/completions", json={
                    "prompt": server.build_prompt(history, utterance, directive),
                    "max_tokens": 80, "temperature": 0,
                    "cache_prompt": True, "id_slot": server.SLOT}, timeout=60))
        r.raise_for_status()
        return server.parse_tool_json(r.json()["choices"][0]["text"])

    arms = {"H": HISTORY, "L2": HISTORY[-2:], "N": []}
    rows = []
    for arm, hist in arms.items():
        print(f"--- arm {arm} ({len(hist)} pairs)", flush=True)
        for utt, want in FRAMES:
            try:
                plan = await gen_plan(utt, hist)
            except Exception as e:
                plan = {"tool": f"ERR:{e}"}
            tool = (plan or {}).get("tool")
            verdict = expected(plan, want)
            rows.append((arm, utt, want, tool, verdict))
            print(f"  [{verdict:10}] {utt[:44]:44} -> {tool}", flush=True)
    print("\n=== aggregate ===")
    for arm in arms:
        ar = [r for r in rows if r[0] == arm]
        c = sum(1 for r in ar if r[4] == "correct")
        w = sum(1 for r in ar if r[4] == "wrong_exec")
        nf = sum(1 for r in ar if r[4] == "null_fall")
        print(f"{arm}: correct {c}/12, wrong-exec {w}, null-fall {nf}")
    json.dump(rows, open(os.path.join(HERE, "ms20_rows.json"), "w"), indent=1)
    for srv in servers:
        srv.kill()

asyncio.run(main())
