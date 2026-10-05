"""Frozen gate for deep-agents stages 1-2 (PROTOCOL-deep-agents.md).

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

Stage 2 extension (registered 2026-10-05 before any stage-2 data): the
DA1/DA2 checks above are untouched; DA4 adds the dual-marker unit
gate; DA5 adds the spoken browser-leg gates (case C read-only, case D
guard-yield park + spoken YES resolution).

Stage 3 extension (registered 2026-10-06 before any stage-3 data):
DA1-DA5 untouched; DA6 adds the manifest dual-exposure unit gate
(decision 23-26: directive names both tools, leg-plan validators with
the browse site-required tightening, CREDS refusal inputs, the four
existing tools' byte-identical replies) plus the in-process delegation
machinery gate (a stub-socket Session runs real tool_flow calls:
ir_research -> ir_event + out-of-turn answer, browse -> browser_event,
decision 27); DA7 adds the spoken gates (case G intercept authority —
research frames pull ir, never the tool class, decision 24; case H
tool-class regression — the timer still routes think.tool and parks on
the high-risk read-back, decision 28).

    .venv/bin/python tools/test_deep_agents.py
"""
import asyncio
import json
import os
import sys
import tempfile
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


# ---------------------------------------------------------------- DA4 unit
def da4():
    print("DA4 unit (offline, stage 2):")
    m = S.IR_TOOL_MARKER
    bm = S.BROWSE_TOOL_MARKER

    # browse marker cut (dual-marker parser, decision 15)
    txt = (f'Check the page.\n{bm} {{"site": "shop.html", "query": null,'
           ' "task": "find the price"}}')
    a, t = S.split_tool_marker(txt)
    check("browse-marker-cut", a == "Check the page.", repr(a))
    j = browser_use._extract_json(t or "")
    check("browse-marker-json",
          isinstance(j, dict) and j.get("site") == "shop.html", repr(j))

    # earliest of both wins
    txt = (f'Go.\n{bm} {{"task": "t", "site": "a.html"}}\n'
           f'and later {m} {{"query": "q", "question": "w"}}')
    a, t = S.split_tool_marker(txt)
    check("browse-earliest", a == "Go." and t is not None
          and t.lstrip().startswith('{"task"'), repr((a, t)))
    txt = (f'Go.\n{m} {{"query": "q", "question": "w"}}\n'
           f'and later {bm} {{"task": "t", "site": "a.html"}}')
    a, t = S.split_tool_marker(txt)
    check("ir-earliest", a == "Go." and t is not None
          and t.lstrip().startswith('{"query"'), repr((a, t)))

    # browse marker without json / prose mention -> no call
    a, t = S.split_tool_marker(f"{bm} not json at all")
    check("browse-nojson", t is None and a.startswith(bm), repr((a, t)))
    a, t = S.split_tool_marker(f"I could {bm} things if needed.")
    check("browse-prose", t is None)

    # browse args validation (stage-2 decision 15)
    check("bargs-no-task",
          S.validate_browse_args({"site": "shop.html"}) is None)
    check("bargs-no-source", S.validate_browse_args({"task": "t"}) is None)
    p = S.validate_browse_args({"site": "shop.html", "query": None,
                                "task": "find the price"})
    check("bargs-site-only",
          p == {"site": "shop.html", "query": None,
                "task": "find the price"}, repr(p))
    p = S.validate_browse_args({"query": "grinder price", "task": "t"})
    check("bargs-query-only",
          p["site"] is None and p["query"] == "grinder price", repr(p))
    check("bargs-both-null",
          S.validate_browse_args({"site": None, "query": None,
                                  "task": "t"}) is None)

    # IR->browse re-scope (stage-2 Rev A): a named local .html is
    # browser intent even when the model picked the research tool
    p = S.validate_leg_args({"query": "aero presco price",
                             "question": "what does it cost"})
    br = S.rescope_ir_to_browse(
        "Think hard about the price of the AeroPress Go, and look at "
        "the local file shop.html before answering.", p)
    check("rescope-named-file",
          br == {"site": "shop.html", "query": None,
                 "task": "what does it cost"}, repr(br))
    check("rescope-archive-stays",
          S.rescope_ir_to_browse(
              "research it in the local archive before answering", p)
          is None)
    p_url = S.validate_leg_args({"url": "shop.html",
                                 "question": "what does it cost"})
    check("rescope-url-stays",
          S.rescope_ir_to_browse(
              "look at the local file shop.html before answering",
              p_url) is None)

    # deep-intake ASR repair (stage-2 Rev C): the corrupted separator
    # form never reaches the deep prompt
    check("asr-repair-separator",
          S.deep_asr_repair("Look at the local file form. HTML to send "
                            "a note before answering.")
          == "Look at the local file form.html to send a note before "
             "answering.",
          repr(S.deep_asr_repair("form. HTML")))
    check("asr-repair-clean-untouched",
          S.deep_asr_repair("Look at the local file form.html before "
                            "answering.")
          == "Look at the local file form.html before answering.",
          repr(S.deep_asr_repair("Look at the local file form.html "
                                 "before answering.")))
    check("asr-repair-nohtml-untouched",
          S.deep_asr_repair("Think hard about our feedback. Look at "
                            "the sky.")
          == "Think hard about our feedback. Look at the sky.")

    # holdback covers the longer marker; directive v2 names both tools
    check("holdback-covers-browse", S.IR_TOOL_HOLD >= len(bm))
    d = S.DEEP_TOOL_DIRECTIVE
    check("directive-v2", m in d and bm in d and '"site"' in d
          and '"task"' in d and '"query"' in d)
    check("cont-v2", "IR_TOOL" in S.IR_TOOL_CONT_DIRECTIVE
          and "BROWSE_TOOL" in S.IR_TOOL_CONT_DIRECTIVE)
    check("budget-note-v2", "BROWSE_TOOL" in S.IR_TOOL_BUDGET_NOTE)
    # Rev D: the task clause keeps the user's action verb (measured:
    # the noun-only example let "send a note" flip to "read the
    # feedback note", and the content-free leg then no-opped into the
    # stuck watch without ever reaching the submit-class click)
    check("directive-verb-clause", "action verb" in d
          and "fill the named form and submit it" in d)


# ------------------------------------------------------------- DA5 spoken
CASE_C = ("Think hard about the price of the AeroPress Go, and look "
          "at the local file shop.html before answering.")
CASE_D = ("Think hard about our feedback. Look at the local file "
          "form.html to send a note before answering.")


async def open_session():
    ws = await H.websockets.connect(H.WS, max_size=2 ** 22)
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
    return ws, runs, rt


async def close_session(ws, rt):
    try:
        await asyncio.wait_for(rt, timeout=2)
    except asyncio.TimeoutError:
        rt.cancel()
    await ws.close()


def latest_trace_matching(site):
    """steps.json of the newest browser trace whose site matches (the
    browser track's observation point; inlined to keep this file's
    imports light)."""
    import glob as _glob
    import os as _os
    root = "/tmp/wiresense_browser"
    files = sorted(_glob.glob(f"{root}/*/steps.json"),
                   key=_os.path.getmtime, reverse=True)
    for f in files[:4]:
        try:
            d = json.load(open(f))
        except (OSError, ValueError):
            continue
        if site in (d.get("site") or ""):
            return d
    return None


async def case_c(ws, runs, failures, name, utt):
    print(f"\n=== {name}: {utt!r}")
    if not await H.case_start(ws, runs, failures, name):
        return
    s = runs.index()
    t0 = time.monotonic()
    await H.speak_turn(ws, runs, utt, since=s, timeout=260)
    await wait_metrics(runs, s)
    since = runs.since(s)
    route = H.route_of(runs, s)
    reply = next((e.get("text") for e in since
                  if e.get("type") == "response_done"), None)
    metrics = next((e for e in since if e.get("type") == "metrics"), None)
    deep = (metrics or {}).get("deep") or {}
    calls = deep.get("tool_calls") or []
    browse = [c for c in calls if c.get("tool") == "browse"]
    steps = sum(c.get("steps", 0) for c in browse)
    confirms = [r for r in H.replies(runs, s) if "confirm" in r.lower()]
    errs = [e for e in since if e.get("type") == "error"]
    total_s = time.monotonic() - t0
    print(f"  route={route} legs={deep.get('legs')} "
          f"total={total_s:.1f}s calls={calls}")
    print(f"  reply: {reply!r}")
    check("C-route-deep", route == "deep", str(route))
    check("C-browse-done",
          len(browse) == 1 and browse[0].get("outcome") == "done",
          str(calls))
    check("C-steps", steps <= 10, str(steps))
    check("C-cites-price", reply is not None and "39.95" in reply,
          repr(reply))
    check("C-no-confirm", not confirms, str(confirms))
    check(f"{name}-no-error", not errs, str(errs))


async def case_d(ws, runs, failures, name, utt):
    print(f"\n=== {name}: {utt!r}")
    if not await H.case_start(ws, runs, failures, name):
        return
    s = runs.index()
    await H.speak_turn(ws, runs, utt, since=s, timeout=260)
    # the handoff: the read-back must arrive out-of-turn after the yield
    rb = []
    end = time.monotonic() + 60
    while time.monotonic() < end:
        rb = [e for e in runs.since(s)
              if e.get("type") == "response_done"
              and "confirm" in (e.get("text") or "").lower()]
        if rb:
            break
        await asyncio.sleep(0.25)
        await ws.send(H.SILENCE)
    await wait_metrics(runs, s, timeout=40)
    since = runs.since(s)
    route = H.route_of(runs, s)
    metrics = next((e for e in since if e.get("type") == "metrics"), None)
    deep = (metrics or {}).get("deep") or {}
    calls = deep.get("tool_calls") or []
    done_all = [e for e in since if e.get("type") == "response_done"]
    partial = done_all[0] if done_all else None
    pre_trace = latest_trace_matching("form.html")
    errs = [e for e in since if e.get("type") == "error"]
    print(f"  route={route} parked={deep.get('parked')} calls={calls}")
    print(f"  partial: {(partial or {}).get('text')!r}")
    print(f"  read-back: {(rb[-1] if rb else {}).get('text')!r}")
    check("D-route-deep", route == "deep", str(route))
    check("D-parked", deep.get("parked") is True and bool(calls)
          and calls[-1].get("outcome") == "parked", str(calls))
    check("D-partial-first",
          partial is not None and bool(rb)
          and partial.get("_arr", 0) < rb[0].get("_arr", 0)
          and "confirm" not in (partial.get("text") or "").lower(),
          repr((partial or {}).get("text")))
    check("D-read-back", bool(rb), "no confirm reply")
    check("D-no-click-before-yes",
          not any(st.get("confirmed")
                  for st in ((pre_trace or {}).get("steps") or [])),
          str((pre_trace or {}).get("steps", [])[-3:]))
    check(f"{name}-no-error", not errs, str(errs))
    if not rb or not await H.wait_listening(ws, runs, s):
        check("D-mic-reopens", False, "mic never reopened for resolution")
        return
    check("D-mic-reopens", True)
    # the spoken resolution drives the EXISTING pending machinery
    s2 = runs.index()
    await H.speak_turn(ws, runs, "Yes please.", since=s2)
    end = time.monotonic() + 60
    ev = None
    while time.monotonic() < end:
        ev = next((e for e in runs.since(s2)
                   if e.get("type") == "browser_event"
                   and e.get("event") != "step"), None)
        if ev:
            break
        await asyncio.sleep(0.1)
        await ws.send(H.SILENCE)
    steps = (ev or {}).get("steps") or []
    conf = [st for st in steps if st.get("confirmed")]
    ans = ((ev or {}).get("answer") or "").lower()
    print(f"  outcome={(ev or {}).get('event')} confirmed={len(conf)} "
          f"answer={(ev or {}).get('answer', '')[:70]!r}")
    check("D-confirmed-click", bool(conf), str(steps[-3:]))
    check("D-answer-spoken",
          ev is not None and ev.get("event") == "done"
          and ("thanks" in ans or "received" in ans), repr(ev))


async def da5():
    print("DA5 spoken e2e (stage 2):")
    ws, runs, rt = await open_session()
    await case_c(ws, runs, FAILS, "DA5c-shop", CASE_C)
    await asyncio.sleep(0.5)
    await case_d(ws, runs, FAILS, "DA5d-form", CASE_D)
    await close_session(ws, rt)
    # registered spoken-variance clause (as DA2): exactly one case
    # failed -> one bounded rerun, final
    c_failed = any(f.startswith("DA5c") or f == "C-route-deep"
                   or f.startswith("C-") for f in FAILS)
    d_failed = any(f.startswith("DA5d") or f.startswith("D-")
                   for f in FAILS)
    if c_failed != d_failed:
        print("\n  [variance rerun: exactly one case failed]")
        drop = (lambda f: f.startswith("DA5c") or f.startswith("C-")) \
            if c_failed else \
            (lambda f: f.startswith("DA5d") or f.startswith("D-"))
        FAILS[:] = [f for f in FAILS if not drop(f)]
        ws, runs, rt = await open_session()
        if c_failed:
            await case_c(ws, runs, FAILS, "DA5c-shop-rerun", CASE_C)
        else:
            await case_d(ws, runs, FAILS, "DA5d-form-rerun", CASE_D)
        await close_session(ws, rt)


# ------------------------------------------- DA6 unit + machinery (stage 3)
CASE_E = ("Research the archive for brew ratios and tell me which "
          "grind setting to use.")
CASE_F = ("Open shop.html and find the price of the AeroPress Go.")


class StubWS:
    """In-process stand-in for the client socket (DA6 machinery): every
    Session.send payload is collected; no spoken turn, no ASR."""

    def __init__(self):
        self.events = []

    async def send(self, d):
        self.events.append(json.loads(d) if isinstance(d, str) else d)


def da6():
    print("DA6 unit (stage 3, offline):")
    d = S.tool_manifest_directive()
    check("D6-directive-names-both",
          "ir_research" in d and "browse" in d
          and all(k in d for k in ("query", "question", "depth",
                                   "site", "task")))
    check("D6-delegate-set",
          S.DELEGATE_TOOLS == {"ir_research", "browse"},
          str(S.DELEGATE_TOOLS))
    # decision 25: ir_research -> validate_leg_args verbatim (list
    # query joined, depth clamped 1-3 default 2)
    p = S.validate_tool("ir_research", {
        "query": ["brew ratio", "coffee grind"],
        "question": "what grind setting to use", "depth": 9})
    check("D6-ir-plan-clamp",
          p == {"query": "brew ratio coffee grind", "url": None,
                "question": "what grind setting to use", "depth": 2},
          repr(p))
    for bad, why in (({"question": "q"}, "no-query"),
                     ({"query": "q"}, "no-question"),
                     ({}, "empty")):
        try:
            S.validate_tool("ir_research", bad)
            ok = False
        except ValueError:
            ok = True
        check(f"D6-ir-invalid:{why}", ok, repr(bad))
    # decision 25: browse -> validate_browse_args + one tightening:
    # a delegated browse NAMES its page (repair still applies)
    p = S.validate_tool("browse", {"site": "shop. HTML",
                                   "task": "find the Apex price"})
    check("D6-browse-repair",
          p == {"site": "shop.html", "query": None,
                "task": "find the Apex price"}, repr(p))
    for bad, why in (({"query": "coffee", "task": "find it"},
                      "query-only-is-ir"),
                     ({"site": "shop.html"}, "no-task"),
                     ({"site": "shop.html", "task": "   "},
                      "blank-task")):
        try:
            S.validate_tool("browse", bad)
            ok = False
        except ValueError:
            ok = True
        check(f"D6-browse-invalid:{why}", ok, repr(bad))
    # decision 26: CREDS refusal inputs — question/task AND raw utterance
    check("D6-creds-task-clause",
          bool(browser_use.CREDS_TASK_RE.search(
              "sign in with my password and submit the form")))
    check("D6-creds-utterance-clause",
          bool(browser_use.CREDS_TASK_RE.search(
              "check my email and log in for me")))
    # decisions 23/27: the four existing tools keep their exact replies
    check("D6-timer-readback",
          S.readback_text("timer", {"seconds": 300, "label": None})
          == "Just to confirm — a timer for 300 seconds?",
          repr(S.readback_text("timer", {"seconds": 300, "label": None})))
    check("D6-reminder-readback",
          S.readback_text("reminder", {"text": "call mom",
                                       "when": "in 10 minutes"})
          == "Just to confirm — a reminder to call mom, in 10 minutes?")
    check("D6-note-shape",
          S.validate_tool("note", {"text": "  buy beans  "})
          == {"text": "buy beans"})
    try:
        S.validate_tool("detect", {"labels": ["person"] * 20})
        ok = False
    except ValueError:
        ok = True
    check("D6-detect-over-cap-raises", ok)
    p = S.validate_tool("detect", {"labels": ["Person", "coffee mug",
                                              "person"]})
    check("D6-detect-shape",
          p == {"labels": ["person", "coffee mug"]}, repr(p))


async def wait_delegated(events, kind, timeout):
    """Bounded wait for the delegated task's terminal event (event ==
    'done'; ir fetches / browser steps fire intermediate events) plus
    the out-of-turn answer from ir_done/browser_done."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ev = [e for e in events
              if e.get("type") == kind and e.get("event") == "done"]
        if ev:
            ans = [e for e in events if e.get("type") == "response_done"]
            return (ans[-1].get("text") if ans else None), ev[-1]
        await asyncio.sleep(0.5)
    return None, None


async def da6_machinery():
    print("DA6 machinery (stage 3, in-process delegation):")
    # the in-process legs must see exactly what the server process sees:
    # server.py's model paths are relative to web/ (MODELS = "../models")
    web_dir = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "..", "web")
    old_cwd = os.getcwd()
    os.chdir(web_dir)
    if S.READOUT is None or S.WALKER is None:
        S.READOUT = S.DecideReadout()
        S.WALKER = S.TreeWalker(S.READOUT._project)
    mem_fd, mem_path = tempfile.mkstemp(suffix=".json")
    os.close(mem_fd)
    old_mem = S.MEMORY_PATH
    S.MEMORY_PATH = mem_path
    try:
        # the four existing tools: run_tool's result strings (the fixed
        # input of the confirm turn) byte-identical, memory isolated
        sess = S.Session(StubWS())
        r = await sess.run_tool("timer", {"seconds": 300, "label": ""})
        check("D6-timer-result",
              r == "timer started for 300 seconds", repr(r))
        r = await sess.run_tool("reminder", {"when": "Friday 9am",
                                             "text": "call mom"})
        check("D6-reminder-result",
              r == "reminder saved: call mom (Friday 9am)", repr(r))
        r = await sess.run_tool("note", {"text": "wifi is falcon nimbus"})
        check("D6-note-result",
              r == "note saved: wifi is falcon nimbus", repr(r))
        mem = json.load(open(mem_path))
        check("D6-memory-isolated",
              [m.get("type") for m in mem] == ["reminder", "note"],
              str(mem)[:120])
        # decision 27: ir_research delegation — the IR track's own
        # intake spawn, out-of-turn completion through ir_done
        s1 = S.Session(StubWS())
        reply, skip = await s1.tool_flow(CASE_E)
        check("D6-ir-intake-turn",
              reply == "On it." and skip is False, repr((reply, skip)))
        ans, ev = await wait_delegated(s1.ws.events, "ir_event", 120)
        tcs = [e for e in s1.ws.events if e.get("type") == "tool_call"]
        check("D6-ir-toolcall",
              len(tcs) == 1 and tcs[0].get("tool") == "ir_research"
              and tcs[0].get("result") == "delegated", str(tcs))
        check("D6-ir-out-of-turn",
              ev is not None and ev.get("event") == "done", str(ev))
        check("D6-ir-answer",
              ans is not None and ("1:2" in ans
                                   or "comandante" in ans.lower()),
              repr(ans))
        check("D6-ir-task-cleared", s1.ir_task is None)
        # decision 27: browse delegation — the browser track's own
        # intake spawn, out-of-turn completion through browser_done
        s2 = S.Session(StubWS())
        reply, skip = await s2.tool_flow(CASE_F)
        check("D6-browse-intake-turn",
              reply == "On it." and skip is False, repr((reply, skip)))
        ans, ev = await wait_delegated(s2.ws.events, "browser_event", 150)
        tcs = [e for e in s2.ws.events if e.get("type") == "tool_call"]
        check("D6-browse-toolcall",
              len(tcs) == 1 and tcs[0].get("tool") == "browse"
              and tcs[0].get("result") == "delegated", str(tcs))
        check("D6-browse-out-of-turn",
              ev is not None and ev.get("event") == "done", str(ev))
        check("D6-browse-answer",
              ans is not None and "39.95" in ans, repr(ans))
        check("D6-browse-task-cleared",
              s2.browser_task is None and not s2.pending)
    finally:
        S.MEMORY_PATH = old_mem
        os.unlink(mem_path)
        os.chdir(old_cwd)


# ------------------------------------------------- DA7 spoken (stage 3)
CASE_G = "Research the archive and tell me which grind setting to use."
CASE_H = "Set a timer for 10 minutes."


async def da7():
    print("DA7 spoken (stage 3, live server):")
    ws, runs, rt = await open_session()
    # (a) intercept authority (decision 24): the voice router never
    # sends a research frame to the manifest — ir owns it
    if not await H.case_start(ws, runs, FAILS, "DA7g-intercept"):
        await close_session(ws, rt)
        return
    s = runs.index()
    await H.speak_turn(ws, runs, CASE_G, since=s, timeout=60)
    end = time.monotonic() + 120
    term = None
    while time.monotonic() < end:
        term = H.ir_terminal(runs, s)
        if term and "response_done" in runs.types_since(s):
            break
        await asyncio.sleep(0.5)
    since = runs.since(s)
    route = H.route_of(runs, s)
    done = [e for e in since if e.get("type") == "response_done"]
    reply = done[-1].get("text") if done else None
    tcs = [e for e in since if e.get("type") == "tool_call"]
    errs = [e for e in since if e.get("type") == "error"]
    print(f"  route={route} ir={term and term.get('event')} "
          f"reply={reply!r}")
    check("G-route-ir", route == "ir", str(route))
    check("G-never-tool-class", not tcs, str(tcs))
    check("G-ir-completed",
          term is not None and term.get("event") == "done", str(term))
    check("G-cites-archive-fact",
          reply is not None and ("1:2" in reply
                                 or "comandante" in reply.lower()),
          repr(reply))
    check("G-no-error", not errs, str(errs))
    # (b) tool-class regression (decision 28): the grown manifest must
    # not disturb the timer flow — think.tool, high-risk park, YES arms
    await asyncio.sleep(0.5)
    if not await H.case_start(ws, runs, FAILS, "DA7h-tool-regression"):
        await close_session(ws, rt)
        return
    s = runs.index()
    await H.speak_turn(ws, runs, CASE_H, since=s, timeout=60)
    since = runs.since(s)
    route = H.route_of(runs, s)
    done = [e for e in since if e.get("type") == "response_done"]
    reply = done[0].get("text") if done else None
    early = [e for e in since if e.get("type") == "tool_event"]
    check("H-route-tool", route == "think.tool", str(route))
    check("H-parked-readback",
          reply is not None and "confirm" in reply.lower()
          and "600" in reply, repr(reply))
    check("H-no-execution-before-yes", not early, str(early))
    s2 = runs.index()
    await H.speak_turn(ws, runs, "Yes.", since=s2, timeout=30)
    await asyncio.sleep(1.0)
    since2 = runs.since(s2)
    armed = [e for e in since2 if e.get("type") == "tool_event"
             and e.get("tool") == "timer"]
    errs2 = [e for e in since2 if e.get("type") == "error"]
    check("H-yes-arms-timer",
          len(armed) == 1 and armed[0].get("seconds") == 600, str(armed))
    check("H-no-error", not errs2, str(errs2))
    await close_session(ws, rt)


def main() -> int:
    da1()
    if FAILS:
        print("\nDA1 failed — DA2 (spoken, slow) skipped; fix first.")
        return 1
    da4()
    if FAILS:
        print("\nDA4 failed — spoken gates skipped; fix first.")
        return 1
    da6()
    if FAILS:
        print("\nDA6 unit failed — machinery and spoken gates skipped;")
        return 1
    asyncio.run(da6_machinery())
    if FAILS:
        print("\nDA6 machinery failed — spoken gates skipped; fix first.")
        return 1
    asyncio.run(da2())
    pre = len(FAILS)
    asyncio.run(da5())
    asyncio.run(da7())
    print("\n" + "=" * 64)
    if FAILS:
        print(f"VERDICT: FAIL ({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"VERDICT: PASS (DA1 all; DA2 A+B; DA4 all; DA5 C+D; "
          f"DA6 unit+machinery; DA7 G+H"
          f"{'' if pre == 0 else '; DA2 needed its rerun'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
