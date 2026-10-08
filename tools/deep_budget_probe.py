"""Live verification of the deep-tier input budget
(docs/PROTOCOL-deep-think.md, implementation note 2026-10-07).

A mock openai-chat provider (127.0.0.1:8013, same /v1 shape as
production) records the payloads that actually go over the wire.
Measured against the real llama.cpp server on :8998 for (a) /models
auto-detection and (b) chars/4 estimate accuracy under the production
tokenizer (an estimate that UNDERCOUNTS would defeat the budget).

Checks:
  A  provider /models auto-detect (real 8998 + mock) drives the budget
  B  wire clipping under a tiny window (oldest dropped, final turn kept,
     payload fits, round-trip answer intact)
  C  config pin beats provider detection
  D  unreachable provider falls back to the 8192 default
  E  deep_turn-style round growth: every round's wire payload fits
  F  oversized final user turn is kept (provider's error to reject)
  G  estimate accuracy + true-token fit margin on the measured corpus

    .venv/bin/python tools/deep_budget_probe.py
"""
import asyncio
import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

ROOT = "/home/sahal/workspace/wiresense"
sys.path.insert(0, f"{ROOT}/web")
sys.path.insert(0, f"{ROOT}/tools/prefill_probe")
import deep_think  # noqa: E402
from large_fixture import PAIRS_LARGE  # noqa: E402

MOCK = {"ctx": 4096, "payloads": []}
SYS = "SYS"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, json.dumps(
                {"object": "list", "data": [
                    {"id": "mock-27b", "meta":
                     {"context_length": MOCK["ctx"]}}]}))
        else:
            self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n))
        if self.path == "/v1/chat/completions":
            MOCK["payloads"].append(body)
            print(f"  [mock] POST cl={n} msgs={len(body['messages'])}")
            self._send(200, "".join([
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":'
                '"Budget verified."}}]}\n\n',
                "data: [DONE]\n\n"]), "text/event-stream")
        else:
            self._send(404, "{}")


srv = ThreadingHTTPServer(("127.0.0.1", 8013), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

FAILS = []
RESULTS = {"date": "2026-10-07", "mock": "127.0.0.1:8013/v1",
           "llama": "127.0.0.1:8998", "checks": {}}


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name}"
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)
    RESULTS["checks"][name] = bool(cond)


def collect(dt, utterance, history, system=SYS):
    async def run():
        return [item async for item in
                dt.stream(utterance, None, history, system)]
    return asyncio.run(run())


def mock_dt(**kw):
    cfg = {"provider": "openai-chat", "base_url": "http://127.0.0.1:8013/v1",
           "model": "mock-27b", "max_tokens": 700, "timeout_s": 30}
    cfg.update(kw)
    return deep_think.DeepThink(cfg)


def fit_est(payload):
    """Same accounting as _fit_messages over a captured wire payload."""
    ms = payload["messages"]
    used = deep_think.est_tokens(ms[0]["content"]) + 6
    for m in ms[1:-1]:
        used += deep_think.est_tokens(m["content"]) + 4
    return used


def hist_until(target_tokens):
    """(user, assistant) pairs from PAIRS_LARGE until est >= target."""
    hist, i = [], 0
    est = 0
    while est < target_tokens:
        u, a = PAIRS_LARGE[i % len(PAIRS_LARGE)]
        hist.append((f"[{i}] {u}", f"[{i}] {a}"))
        est += deep_think.est_tokens(u) + deep_think.est_tokens(a) + 8
        i += 1
    return hist


def true_tokens(text):
    # SAFETY: an oversized /tokenize body (>~10K tokens) silently killed
    # the :8998 server once (incident in the doc); every call is clamped
    # far below the 8192-token slot bound.
    if len(text) > 24000:
        raise ValueError(f"tokenize refused: {len(text)} chars too large")
    r = requests.post("http://127.0.0.1:8998/tokenize",
                      json={"content": text}, timeout=30)
    r.raise_for_status()
    return len(r.json()["tokens"])


print("A: provider auto-detect (props serving window vs models)")
LLAMA = "http://127.0.0.1:8998"
try:
    props_raw = requests.get(f"{LLAMA}/props", timeout=10).json()
    models_raw = requests.get(f"{LLAMA}/models", timeout=10).json()
    mid = models_raw["data"][0]["id"]
    ctx_props = deep_think.parse_props(props_raw)
    ctx_models = deep_think.parse_model_ctx(models_raw, mid)
    print(f"  8998 model {mid!r}: props serving ctx {ctx_props}, "
          f"models meta would give {ctx_models} (n_ctx_train)")
    check("A1 props serving window detected", ctx_props == 8192,
          str(ctx_props))
    check("A2 models-only train bound rejected", ctx_models is None,
          str(ctx_models))
    dt = deep_think.DeepThink({"provider": "openai-chat",
                               "base_url": LLAMA, "model": mid})
    b = dt.input_budget()
    check("A3 budget = props ctx - max_tokens - 64",
          b == ctx_props - 700 - 64, str(b))
    RESULTS["llama_model"] = mid
    RESULTS["llama_ctx_props"] = ctx_props
    RESULTS["llama_ctx_train_rejected"] = ctx_models
    RESULTS["llama_budget"] = b
except Exception as e:
    check("A real llama.cpp reachable", False, repr(e))

MOCK["ctx"] = 4096
dt = mock_dt()
check("A4 mock ctx via models path (no props handler)",
      dt.input_budget() == 4096 - 700 - 64, str(dt.input_budget()))

print("B: wire clipping under a tiny window (budget 3332)")
big = hist_until(12000)
print(f"  history: {len(big)} pairs, est "
      f"{sum(deep_think.est_tokens(u) + deep_think.est_tokens(a) + 8 for u, a in big)} tok")
MOCK["payloads"].clear()
bdt = mock_dt()
out = collect(bdt, "final ask", big)
payload = MOCK["payloads"][0]
ms = payload["messages"]
n_hist = len(big)
check("B1 final user turn last, intact",
      ms[-1] == {"role": "user", "content": "final ask"})
check("B2 system first", ms[0]["role"] == "system"
      and ms[0]["content"] == SYS)
check("B3 newest pair survives",
      ms[-3]["content"].startswith(f"[{n_hist - 1}]"), ms[-3]["content"][:12])
check("B4 oldest dropped", len(ms) < 2 * n_hist + 2
      and not any(m["content"].startswith("[0] ") for m in ms[1:-1]))
check("B5 payload fits fitter accounting", fit_est(payload) <= 3332,
      f"{fit_est(payload)}")
true_b = true_tokens("\n".join(m["content"] for m in ms))
margin_b = 4096 - true_b - 700
check("B6 true tokens + max_tokens fit window", margin_b > 0,
      f"true {true_b}, margin {margin_b}")
check("B7 answer round-trips",
      out == [("text", "Budget verified.")], out)
RESULTS["B"] = {"hist_pairs": n_hist, "wire_msgs": len(ms),
                "fit_est": fit_est(payload), "true_tokens": true_b,
                "margin_vs_window": margin_b,
                "kept": bdt.last_fit}
print(f"  wire: {len(ms)} msgs, fit_est {fit_est(payload)}, "
      f"true {true_b} tok, margin {margin_b} tok")

print("C: config pin beats provider detection")
MOCK["payloads"].clear()
pin = mock_dt(context_tokens=8192)
check("C1 pin wins", pin.input_budget() == 8192 - 700 - 64,
      str(pin.input_budget()))
collect(pin, "final ask", big)
p_c = MOCK["payloads"][0]
ms_c = p_c["messages"]
check("C2 larger budget keeps more turns",
      len(ms_c) > len(ms), f"{len(ms_c)} vs {len(ms)}")
check("C3 pinned payload fits its budget", fit_est(p_c) <= 7428,
      f"{fit_est(p_c)}")
RESULTS["C"] = {"msgs": len(ms_c), "fit_est": fit_est(p_c)}

print("D: unreachable provider -> 8192 default")
dead = deep_think.DeepThink({"provider": "openai-chat",
                             "base_url": "http://127.0.0.1:9", "model": "m"})
check("D1 default budget", dead.input_budget() == 8192 - 700 - 64,
      str(dead.input_budget()))

print("E: deep_turn-style round growth (6 rounds, growing round_hist)")
MOCK["payloads"].clear()
rdt = mock_dt()
preload = hist_until(4000)  # over budget: clipping engages from round 0
round_hist = list(preload)
rounds = []
for r in range(6):
    MOCK["payloads"].clear()
    collect(rdt, f"round ask {r}", round_hist)
    p = MOCK["payloads"][0]
    rounds.append({"round": r, "hist_pairs": len(round_hist),
                   "wire_msgs": len(p["messages"]),
                   "fit_est": fit_est(p),
                   "kept": rdt.last_fit["kept"],
                   "dropped": rdt.last_fit["dropped"]})
    check(f"E{r + 1} round {r} fits", fit_est(p) <= 3332, str(fit_est(p)))
    check(f"E{r + 1}b round {r} final ask last",
          p["messages"][-1]["content"] == f"round ask {r}")
    round_hist = round_hist + [(f"round ask {r}", "Budget verified.")]
fit_seq = [r["fit_est"] for r in rounds]
# tokens are the invariant, not message count: each round may keep more,
# smaller messages while the est stays pinned under budget
check("E7 payload est bounded across rounds",
      all(f <= 3332 for f in fit_seq), str(fit_seq))
RESULTS["E"] = rounds
print("  " + " ".join(f"r{r['round']}:{r['fit_est']}est/{r['dropped']}drop"
                      for r in rounds))

print("F: oversized final user turn is kept (provider's error to reject)")
MOCK["payloads"].clear()
huge_user = "y" * 40000
collect(mock_dt(), huge_user, [("q", "a")])
p = MOCK["payloads"][0]
check("F1 oversized final turn sent intact",
      p["messages"][-1]["content"] == huge_user)
# no /tokenize here: the oversized body is what killed :8998 once
est_f = deep_think.est_tokens(huge_user)
print(f"  est {est_f} tok vs window 4096 — overflow by "
      f"{est_f + 700 - 4096}, disclosed behavior (no tokenize of an "
      f"oversized body, see incident note)")
RESULTS["F"] = {"est_tokens": est_f}

print("G: chars/4 estimate vs production tokenizer (:8998)")
ratios = []
gsizes = [0, 4, 16, 64, 128]  # 128 pairs ~= 23K chars, under the clamp
for n in gsizes:
    hist = ([("seed", "seed")] if n == 0 else
            [PAIRS_LARGE[i % len(PAIRS_LARGE)]
             for i in range(n)])
    text = SYS + " final ask " + " ".join(u + " " + a for u, a in hist)
    est = deep_think.est_tokens(text)
    true = true_tokens(text)
    ratios.append(round(est / true, 3))
    print(f"  {n:>5} pairs: est {est:>6} true {true:>6} "
          f"ratio {est / true:.3f}")
code_text = ("def f(x):\n    return {'a': [i*2 for i in range(x)], "
             "\"b\": x if x > 0 else -x}\n" * 20)
code_ratio = round(deep_think.est_tokens(code_text)
                   / true_tokens(code_text), 3)
print(f"  python code sample ratio {code_ratio} (recorded, not gated — "
      f"domain corpus is voice dialogue)")
min_dialogue = min(ratios)
check("G1 estimate conservative on dialogue corpus", min_dialogue >= 1.0,
      f"min ratio {min_dialogue}")
check("G2 measured corpus fits with reserve",
      margin_b > 0)
RESULTS["G"] = {"ratios_by_pairs": dict(zip(gsizes, ratios)),
                "code_ratio": code_ratio}

srv.shutdown()
with open(f"{ROOT}/tools/results_deep_budget.json", "w") as f:
    json.dump(RESULTS, f, indent=1)
print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAIL: {FAILS}'}"
      "  (results -> tools/results_deep_budget.json)")
sys.exit(1 if FAILS else 0)
