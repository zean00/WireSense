"""Frozen gate for deep-agents stage 1 (PROTOCOL-deep-agents.md).

DA1 unit (offline, no network/browser): the IR_TOOL marker parser, leg
args validation, the stream holdback rule, directive shape, credential
refusal inputs.
DA2 spoken e2e (real ASR -> route -> deep loop -> legs on the frozen
replay registry, harness shared with test_ir.py):
  case A must route deep, fire >= 1 leg, stay fixture-only (every
  fetch event snapped), cite the archive's ratio fact, and hold the
  code-owned budgets (<= 3 legs, <= 180 s combined leg time);
  case B (control) must run ZERO legs and answer the math from knowledge.
DA3 (the regression battery) is run separately — the runner prints the
bars; this file never edits a frozen eval.

    .venv/bin/python tools/test_deep_agents.py
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, "web")
sys.path.insert(0, "tools")

import server as S  # noqa: E402
import browser_use  # noqa: E402
import test_ir as H  # noqa: E402  (harness: synth/feed/speak_turn/Runs)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


# ---------------------------------------------------------------- DA1 unit
def da1():
    print("DA1 unit (offline):")
    m = S.IR_TOOL_MARKER

    # clean answer, no marker
    a, t = S.split_tool_marker("The capital of France is Paris.")
    check("clean-answer", (a, t) == ("The capital of France is Paris.", None))

    # marker + flat args
    txt = f'Check the archive.\n{m} {{"query": "espresso grind settings", "question": "which grind", "depth": 2}}'
    a, t = S.split_tool_marker(txt)
    check("marker-cut", a == "Check the archive.", repr(a))
    j = browser_use._extract_json(t or "")
    check("marker-json", isinstance(j, dict) and j.get("query")
          == "espresso grind settings", repr(j))

    # wrapped form is accepted by the loop's unwrap (decision 3)
    wrapped = {"tool": "ir_research", "args": {"query": "q",
                                               "question": "w"}}
    check("wrapped-unwrap", S.validate_leg_args(wrapped["args"])
          == {"query": "q", "url": None, "question": "w", "depth": 2})

    # malformed / missing json after the marker -> no tool call
    a, t = S.split_tool_marker(f"{m} not json at all")
    check("marker-nojson", t is None and a.startswith(m), repr((a, t)))
    a, t = S.split_tool_marker(f"I could {m} things if needed.")
    check("prose-mention", t is None)

    # args validation (protocol decision 3)
    check("args-no-question",
          S.validate_leg_args({"query": "x"}) is None)
    check("args-no-source",
          S.validate_leg_args({"question": "w"}) is None)
    p = S.validate_leg_args({"query": ["a", "b"], "question": "w",
                             "depth": "3"})
    check("args-list-depth", p == {"query": "a b", "url": None,
                                   "question": "w", "depth": 3}, repr(p))
    p = S.validate_leg_args({"query": "x", "question": "w", "depth": 9})
    check("args-depth-clamp", p["depth"] == 2)
    p = S.validate_leg_args({"url": "moon.html", "question": "w"})
    check("args-url-only", p["url"] == "moon.html" and p["depth"] == 2)

    # credential refusal inputs (decision 6)
    check("creds-question",
          browser_use.CREDS_TASK_RE.search("how do I sign in to my bank")
          is not None)

    # holdback rule: replay the consumer's emission rule over deltas and
    # assert no marker text is ever emitted (decision 2)
    full = ('Let me look.\n' + m +
            ' {"query": "q", "question": "w", "depth": 2}')
    deltas = [full[i:i + 7] for i in range(0, len(full), 7)]
    emitted, buf = "", ""
    for d in deltas:
        buf += d
        pos = buf.find(m)
        limit = pos if pos >= 0 else max(0, len(buf) - S.IR_TOOL_HOLD)
        if limit > len(emitted):
            emitted += buf[len(emitted):limit]
    check("holdback-no-marker", m not in emitted, repr(emitted[-40:]))
    # without a marker the withheld tail is flushed whole
    buf2 = "just a normal answer, nothing withheld"
    emitted2 = buf2[:max(0, len(buf2) - S.IR_TOOL_HOLD)]
    check("holdback-flush", emitted2 + buf2[len(emitted2):] == buf2)

    # directive shape (decision 10)
    d = S.DEEP_TOOL_DIRECTIVE
    check("directive-shape", m in d and '"query"' in d
          and '"question"' in d and '"depth"' in d
          and "<two to five content keywords>" in d)
    check("holdback-covers-marker",
          S.IR_TOOL_HOLD >= len(m))


# ------------------------------------------------------------- DA2 spoken
CASE_A = ("Think hard about which espresso grind setting to use, and "
          "research it in the local archive before answering.")
CASE_B = ("Think hard about this math problem and work out the exact "
          "answer: what is 17 times 23?")


async def wait_metrics(runs, since, timeout=25.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        evs = [e for e in runs.since(since) if e.get("type") == "metrics"]
        if evs:
            return evs[-1]
        await asyncio.sleep(0.25)
    return None


async def deep_case(ws, runs, failures, name, utt):
    print(f"\n=== {name}: {utt!r}")
    if not await H.case_start(ws, runs, failures, name):
        return
    s = runs.index()
    t0 = time.monotonic()
    await H.speak_turn(ws, runs, utt, since=s, timeout=260)
    await wait_metrics(runs, s)
    since = runs.since(s)
    route = H.route_of(runs, s)
    fetches = [e for e in since if e.get("type") == "ir_event"
               and e.get("event") == "fetch"]
    reply = next((e.get("text") for e in since
                  if e.get("type") == "response_done"), None)
    metrics = next((e for e in since if e.get("type") == "metrics"), None)
    deep = (metrics or {}).get("deep") or {}
    calls = deep.get("tool_calls") or []
    errs = [e for e in since if e.get("type") == "error"]
    total_s = time.monotonic() - t0
    print(f"  route={route} fetches={len(fetches)} "
          f"legs={deep.get('legs')} total={total_s:.1f}s")
    for f in fetches:
        print(f"    fetch {str(f.get('url'))[-44:]} "
              f"snapped={f.get('snapped')}")
    print(f"  reply: {reply!r}")

    if name.startswith("DA2a"):
        check("A-route-deep", route == "deep", str(route))
        check("A-leg-fired", len(fetches) >= 1, str(len(fetches)))
        check("A-fixture-only", all(f.get("snapped") for f in fetches))
        check("A-cites-fact", reply is not None and
              ("1:2" in reply or "comandante" in reply.lower()), repr(reply))
        check("A-leg-budget", len(calls) <= S.DEEP_MAX_LEGS, str(calls))
        check("A-time-budget",
              sum(c.get("ms", 0) for c in calls) <= S.DEEP_LEG_TIME * 1000,
              str(calls))
    else:
        check("B-zero-legs", len(fetches) == 0, str(len(fetches)))
        check("B-answers-math", reply is not None and "391" in reply,
              repr(reply))
    check(f"{name}-no-error", not errs, str(errs))


async def da2():
    print("DA2 spoken e2e:")
    async with H.websockets.connect(H.WS, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"type": "hello", "ctx_rate": 16000}))
        runs = H.Runs()

        async def reader():
            try:
                async for raw in ws:
                    runs.add(json.loads(raw))
            except H.websockets.ConnectionClosed:
                pass

        rt = asyncio.create_task(reader())
        await asyncio.sleep(0.5)
        # case B first so a stuck gate from A's long turn can't poison it
        await deep_case(ws, runs, FAILS, "DA2b-math", CASE_B)
        await asyncio.sleep(0.5)
        await deep_case(ws, runs, FAILS, "DA2a-grind", CASE_A)
        try:
            await asyncio.wait_for(rt, timeout=2)
        except asyncio.TimeoutError:
            rt.cancel()
    # registered spoken-variance clause: one bounded rerun of a FAILED
    # case while the other passed (input variance, not code) — the rerun
    # result is final and recorded
    if FAILS:
        a_failed = any(f.startswith("DA2a") for f in FAILS)
        b_failed = any(f.startswith("DA2b") for f in FAILS)
        if a_failed != b_failed:      # exactly one side failed
            print("\n  [variance rerun: exactly one case failed]")
            FAILS[:] = [f for f in FAILS
                        if not (f.startswith("DA2a") if a_failed
                                else f.startswith("DA2b"))]
            async with H.websockets.connect(H.WS, max_size=2 ** 22) as ws:
                await ws.send(json.dumps({"type": "hello",
                                          "ctx_rate": 16000}))
                runs = H.Runs()

                async def reader2():
                    try:
                        async for raw in ws:
                            runs.add(json.loads(raw))
                    except H.websockets.ConnectionClosed:
                        pass

                rt = asyncio.create_task(reader2())
                await asyncio.sleep(0.5)
                if a_failed:
                    await deep_case(ws, runs, FAILS, "DA2a-grind-rerun",
                                    CASE_A)
                else:
                    await deep_case(ws, runs, FAILS, "DA2b-math-rerun",
                                    CASE_B)
                try:
                    await asyncio.wait_for(rt, timeout=2)
                except asyncio.TimeoutError:
                    rt.cancel()


def main() -> int:
    da1()
    if FAILS:
        print("\nDA1 failed — DA2 (spoken, slow) skipped; fix first.")
        return 1
    asyncio.run(da2())
    print("\n" + "=" * 64)
    if FAILS:
        print(f"VERDICT: FAIL ({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("VERDICT: PASS (DA1 all; DA2 A+B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
