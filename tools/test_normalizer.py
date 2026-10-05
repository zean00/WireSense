#!/usr/bin/env python
"""G1 of docs/PROTOCOL-asr-normalizer.md: offline units for the transcript
normalizer. Positives cover the measured who/two garble family; negatives
are utterances that must pass through byte-identical."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "web"))
import server  # noqa: E402

failures = []
n_checks = 0


def check(cond, msg):
    global n_checks
    n_checks += 1
    if cond:
        print(f"  PASS {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL {msg}")


def main() -> int:
    norm = server.normalize_transcript

    print("positives — the measured garble family repairs to 'Who am I?'")
    for raw in ("Two am I?", "two am I", "TO AM I?", "Too am i.",
                "to am i?", "2 am I?", "  two   am   i  ", "Two am I!"):
        check(norm(raw) == "Who am I?", f"{raw!r} -> 'Who am I?'")

    print("negatives — real utterances pass through byte-identical")
    for raw in ("It's two a.m.", "It is two am I think", "Two am I late?",
                "wake me at two am", "Set a timer for 10 minutes.",
                "Remind me to call mom in 15 seconds.",
                "What is the capital of France?", "Who am I, do you know?",
                "Who is this?", "I had two apples", "Turn to am I side",
                "From two to am I right", ""):
        check(norm(raw) == raw, f"{raw!r} untouched")

    print("rule table is the registered v1 set")
    check(len(server.ASR_NORMALIZERS) == 1
          and server.ASR_NORMALIZERS[0][1] == "Who am I?",
          "exactly one rule, fixed replacement 'Who am I?'")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURES'} "
          f"({n_checks} checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
