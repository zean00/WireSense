"""Transcript-safety units (docs/PROTOCOL-transcript-safety.md, gate G1):
pure-helper checks plus live judge spot-checks against the frozen prompts
on the dedicated slot, and a fail-open proof with the endpoint unreachable.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
import server  # noqa: E402

CHECKS = []


def check(name, got, want):
    ok = got == want
    CHECKS.append(ok)
    print(f"  {'ok' if ok else 'FAIL'}  {name}: {got!r}"
          + ("" if ok else f"  (want {want!r})"))


print("eligibility (protocol decision 2)")
check("short passes", server.judge_eligible("Two am I?", False), True)
check("repaired final skips judge", server.judge_eligible("Who am I?", True),
      False)
check("long utterance never judged",
      server.judge_eligible("Set a timer for 10 minutes", False), False)
check("four words is short", server.judge_eligible("what do you see", False),
      True)
check("five words is long",
      server.judge_eligible("what do you see now ok", False), False)

print("resolution vocabulary (protocol decision 6)")
check("yes", server.classify_resolution("Yes."), "yes")
check("go ahead", server.classify_resolution("go ahead"), "yes")
check("sounds good", server.classify_resolution("Sounds good!"), "yes")
check("no", server.classify_resolution("No"), "no")
check("cancel", server.classify_resolution("Cancel."), "no")
check("that's wrong", server.classify_resolution("that's wrong"), "no")
check("unrelated utterance", server.classify_resolution("what time is it"),
      None)
check("empty", server.classify_resolution(""), None)
check("multi-word not in vocab", server.classify_resolution("yes go ahead"),
      None)

print("verdict parse, fail-open (protocol decision 3)")
check("YES", server.parse_verdict("YES"), True)
check("padded yes", server.parse_verdict(" YES "), True)
check("no.", server.parse_verdict("no."), False)
check("junk", server.parse_verdict("<think>thinking"), None)
check("empty", server.parse_verdict(""), None)

print("read-back templates (protocol decision 9)")
check("timer with label",
      server.readback_text("timer", {"seconds": 600, "label": "10 minutes"}),
      "Just to confirm — a timer for 10 minutes?")
check("timer bare seconds",
      server.readback_text("timer", {"seconds": 90, "label": ""}),
      "Just to confirm — a timer for 90 seconds?")
check("reminder",
      server.readback_text("reminder",
                           {"text": "call mom", "when": "in 15 seconds"}),
      "Just to confirm — a reminder to call mom, in 15 seconds?")
check("non-high-risk has none", server.readback_text("note", {"text": "x"}),
      None)
check("high-risk scope", server.HIGH_RISK_TOOLS == {"timer", "reminder"},
      True)


async def judge(t):
    return await server.Session.judge_transcript(None, t)


async def live():
    print("live judge spot-checks (frozen prompts, slot 4)")
    for t in ("What time is it?", "Who is this?",
              "Set a timer for 10 minutes"):
        sensible, likely, ms = await judge(t)
        check(f"sensible {t!r}", (sensible, likely), (True, None))
        print(f"        {ms:.0f} ms")
    for t in ("Yes, you are two.", "The purple sleeps loudly",
              "Set a timer for purple minutes"):
        sensible, likely, ms = await judge(t)
        check(f"nonsense {t!r}", (sensible, bool(likely)),
              (False, True))
        print(f"        {ms:.0f} ms  likely={likely!r}")

    print("fail-open proof (endpoint unreachable)")
    real, server.LLAMA = server.LLAMA, "http://127.0.0.1:59999"
    try:
        sensible, likely, ms = await judge("What time is it?")
        check("unreachable -> sensible", (sensible, likely), (True, None))
    finally:
        server.LLAMA = real


asyncio.run(live())

n, ok = len(CHECKS), sum(CHECKS)
print(f"\n{'PASS' if ok == n else 'FAIL'}: {ok}/{n}")
sys.exit(0 if ok == n else 1)
