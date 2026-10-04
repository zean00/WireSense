"""Deep-think bench: frozen 10-question set, local think vs deep provider.

Protocol: docs/PROTOCOL-deep-think.md (committed BEFORE any data). The
question set below is pre-registered — every answer is programmatically
checkable (no LLM judge); each canonical value is absent from its own
prompt so prompt-echo cannot score. Frozen bar: deep justifies the route
iff deep >= 7/10 AND >= local score. Record TTFT/total ms for both sides.

    .venv/bin/python tools/deep_bench.py            # local side only
    .venv/bin/python tools/deep_bench.py --deep     # + configured provider (web/config.json)

Local side = the serving think path verbatim (build_prompt with open
think, temp 0, max_tokens 640 — the long_think class budget). Deep side =
whatever config names, streamed through the production adapter
(web/deep_think.py). Raw results land in /tmp/deep_bench_results.json;
adjudicated numbers go in the protocol doc, not here.
"""
import asyncio
import json
import re
import sys
import time

import requests

LLAMA = "http://127.0.0.1:8998"
THINK_DIRECTIVE = ("Reason step by step, then give a clear, concise answer.")
MAX_TOKENS = 640  # the long_think class budget

# (question, answer regex on normalized text, note)
QUESTIONS = [
    ("A train leaves at 14:47 and the trip takes 3 hours and 38 minutes. "
     "What time does it arrive? Answer in 24-hour HH:MM form.",
     r"18:25", "clock arithmetic"),
    ("An investment of 2500 dollars grows 8 percent per year, compounded "
     "annually. What is it worth after 3 years, to the nearest dollar?",
     r"3,?149", "compound growth"),
    ("All bloops are razzies. All razzies are lazzies. Are all bloops "
     "definitely lazzies? Answer yes or no.",
     r"\byes\b", "syllogism"),
    ("The sum of two numbers is 25 and their difference is 7. What is the "
     "larger number?",
     r"\b16\b", "algebra"),
    ("How many distinct arrangements are there of the letters in the "
     "word LEVEL?",
     r"\b30\b", "combinatorics"),
    ("Two fair dice are rolled. What is the probability that their sum is "
     "8? Give the answer as a fraction.",
     r"5\s*/\s*36", "probability"),
    ("Three printers print three pages in three minutes. How many minutes "
     "would nine printers take to print nine pages?",
     r"\b(3|three)\s*(minutes|mins)\b", "rate trap"),
    ("What does this Python snippet print?\n"
     "x = [1, 2, 3, 4]\nprint(sum(x[1::2]) * x[0])",
     r"(?<![\d.])6(?![\d.])", "code output"),
    ("What does this Python snippet print?\n"
     "s = 'wiresense'\nprint(s[2:6][::-1])",
     r"eser", "code output"),
    ("An item costs 80 dollars. It is discounted 40 percent, then an "
     "extra 25 percent is taken off the reduced price. What is the final "
     "price in dollars?",
     r"\$?\s*36\b", "stacked discount"),
]


def normalize(t):
    t = t.lower().replace("$", " ").replace(",", "")
    return re.sub(r"\s+", " ", t).strip()


def strip_think(text):
    return text.split("</think>", 1)[1].strip() if "</think>" in text else text


def run_local(q, prompt):
    """The serving think path verbatim: open think, strip the trace,
    force-close on budget exhaustion exactly like respond() does."""
    first_tok = first_ans = None
    t0 = time.perf_counter()

    def gen(p):
        with requests.post(f"{LLAMA}/v1/completions", json={
                "prompt": p, "max_tokens": MAX_TOKENS, "temperature": 0,
                "cache_prompt": True, "id_slot": 0, "stream": True},
                stream=True, timeout=300) as r:
            r.raise_for_status()
            chunks = []
            for line in r.iter_lines():
                if not line or not line.startswith(b"data: "):
                    continue
                payload = line[len(b"data: "):]
                if payload == b"[DONE]":
                    break
                delta = json.loads(payload)["choices"][0].get("text", "")
                if not delta:
                    continue
                if first_tok is None:
                    first_tok = (time.perf_counter() - t0) * 1000
                chunks.append(delta)
                if "</think>" in "".join(chunks) and first_ans is None:
                    first_ans = (time.perf_counter() - t0) * 1000
            return "".join(chunks)

    reply = gen(prompt)
    if "</think>" not in reply:
        # budget ran out mid-reasoning: same rescue as the serving path
        try:
            reply += gen(prompt + reply + "\n\n</think>\n\n")
        except Exception:
            pass
    return ({"text": strip_think(reply),
             "ttft_ms": round(first_tok, 1) if first_tok else None,
             "first_answer_ms": round(first_ans, 1) if first_ans else None,
             "total_ms": round((time.perf_counter() - t0) * 1000)})


async def run_deep(deep, q):
    first_tok = first_ans = None
    t0 = time.perf_counter()
    chunks = []
    async for kind, delta in deep.stream(q, THINK_DIRECTIVE, history=[]):
        if not delta:
            continue
        if first_tok is None:
            first_tok = (time.perf_counter() - t0) * 1000
        if kind == "text":
            chunks.append(delta)
            if first_ans is None:
                first_ans = (time.perf_counter() - t0) * 1000
    return ({"text": "".join(chunks).strip(),
             "ttft_ms": round(first_tok, 1) if first_tok else None,
             "first_answer_ms": round(first_ans, 1) if first_ans else None,
             "total_ms": round((time.perf_counter() - t0) * 1000)})


def score(results):
    rows, n_ok = [], 0
    for (q, rx, note), r in zip(QUESTIONS, results):
        ok = re.search(rx, normalize(r["text"])) is not None
        n_ok += ok
        rows.append({"q": note, "ok": ok, "ms": r})
    return rows, n_ok


def main():
    sys.path.insert(0, "web")
    from server import build_prompt  # the exact serving prompt builder

    want_deep = "--deep" in sys.argv
    deep = None
    if want_deep:
        sys.path.insert(0, "web")
        import deep_think
        cfg = deep_think.load_config()
        deep = deep_think.from_config(cfg)
        if deep is None:
            print("deep side: no enabled provider in web/config.json — "
                  "run local side only, or fix the config")
            return 1

    local_results = []
    print(f"local think (slot 0, {LLAMA}):")
    for q, _, note in QUESTIONS:
        prompt = build_prompt([], q, THINK_DIRECTIVE, open_think=True)
        r = run_local(q, prompt)
        local_results.append(r)
        print(f"  {note}: {r['total_ms']} ms "
              f"(ttft {r['ttft_ms']}, first ans {r['first_answer_ms']})")

    deep_rows, deep_ok, deep_results = None, None, []
    if deep is not None:
        print(f"deep think ({deep.provider} {deep.model} @ {deep.base_url}):")
        for q, _, note in QUESTIONS:
            r = asyncio.run(run_deep(deep, q))
            deep_results.append(r)
            print(f"  {note}: {r['total_ms']} ms "
                  f"(ttft {r['ttft_ms']}, first ans {r['first_answer_ms']})")

    local_rows, local_ok = score(local_results)
    print(f"\nlocal: {local_ok}/10")
    if deep is not None:
        deep_rows, deep_ok = score(deep_results)
        print(f"deep:  {deep_ok}/10   (bar: deep >= 7/10 and >= local)")
    for rows, name in ((local_rows, "local"), (deep_rows, "deep")):
        if rows:
            for row in rows:
                print(f"  [{name}] {'PASS' if row['ok'] else 'FAIL'} "
                      f"{row['q']}")
    json.dump({"local": {"n": local_ok, "runs": local_results},
               "deep": ({"n": deep_ok, "runs": deep_results}
                        if deep is not None else None)},
              open("/tmp/deep_bench_results.json", "w"), indent=1)
    print("raw -> /tmp/deep_bench_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
