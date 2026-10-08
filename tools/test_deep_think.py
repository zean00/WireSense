"""Offline tests for the deep-think adapters (no network): SSE parsing
per dialect, wire construction, config gating. The bench
(tools/deep_bench.py --deep) covers the live wire path.

    .venv/bin/python tools/test_deep_think.py
"""
import os
import sys

sys.path.insert(0, "web")
import deep_think  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


def lines(*ss):
    return [s.encode() for s in ss]


print("openai-chat:")
chat = deep_think.DeepThink({"provider": "openai-chat",
                             "base_url": "http://x/v1", "model": "m"})
out = list(chat._extract(lines(
    'data: {"choices":[{"delta":{"reasoning_content":"let me"}}]}',
    'data: {"choices":[{"delta":{"content":"Hel"}}]}',
    'data: {"choices":[{"delta":{"content":"lo"}}]}',
    "data: [DONE]")))
check("reasoning+text+dones", out == [("reasoning", "let me"),
                                      ("text", "Hel"), ("text", "lo")], out)
out = list(chat._extract(lines(
    'data: {"choices":[{"delta":{"reasoning":"alt-key"}}]}',
    "data: [DONE]")))
check("alt reasoning key", out == [("reasoning", "alt-key")], out)

print("openai-responses:")
resp = deep_think.DeepThink({"provider": "openai-responses",
                             "base_url": "http://x/v1", "model": "m"})
out = list(resp._extract(lines(
    'data: {"type":"response.reasoning_summary_text.delta","delta":"hmm"}',
    'data: {"type":"response.output_text.delta","delta":"Hi"}',
    'data: {"type":"response.completed"}',
    'data: {"type":"response.output_text.delta","delta":"after"}')))
check("deltas+completed stops", out == [("reasoning", "hmm"),
                                        ("text", "Hi")], out)

print("anthropic:")
os.environ["T_TEST_KEY"] = "k123"  # keys are read at construction time
anth = deep_think.DeepThink({"provider": "anthropic",
                             "base_url": "http://x", "model": "m",
                             "api_key_env": "T_TEST_KEY"})
check("available with key", anth.available)
out = list(anth._extract(lines(
    'data: {"type":"content_block_delta","delta":{"type":"thinking_delta",'
    '"thinking":"ponder"}}',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta",'
    '"text":"Yo"}}',
    'data: {"type":"message_stop"}')))
check("thinking+text+stop", out == [("reasoning", "ponder"),
                                    ("text", "Yo")], out)
err = None
try:
    list(anth._extract(lines('data: {"type":"error","error":{"message":"x"}}')))
except RuntimeError as e:
    err = e
check("provider error raises", err is not None)

print("wire construction:")
hist = [("hi", "hello"), ("how big is the sun", "very")]
url, payload = chat._wire("think hard", "Be careful.", hist, "SYS")
check("chat url", url == "http://x/v1/chat/completions")
check("chat system first",
      payload["messages"][0] == {"role": "system", "content": "SYS"})
check("chat history + directive",
      payload["messages"][1:] == [
          {"role": "user", "content": "hi"},
          {"role": "assistant", "content": "hello"},
          {"role": "user", "content": "how big is the sun"},
          {"role": "assistant", "content": "very"},
          {"role": "user", "content": "think hard\n(Be careful.)"}],
      payload["messages"][-3:])
url, payload = resp._wire("question", None, hist[:1], "SYS")
check("responses instructions", payload.get("instructions") == "SYS")
check("responses input types",
      payload["input"] == [
          {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
          {"role": "assistant",
           "content": [{"type": "output_text", "text": "hello"}]},
          {"role": "user",
           "content": [{"type": "input_text", "text": "question"}]}],
      payload["input"])
url, payload = anth._wire("question", None, [], "SYS")
check("anthropic system field", payload.get("system") == "SYS")
check("anthropic url", url == "http://x/v1/messages")
check("anthropic headers", anth._headers().get("x-api-key") == "k123"
      and anth._headers().get("anthropic-version") == "2023-06-01")
check("no bearer for local", chat._headers() == {"content-type":
                                                 "application/json"})
chat2 = deep_think.DeepThink({"provider": "openai-chat",
                              "base_url": "http://x/v1", "model": "m",
                              "api_key_env": "T_TEST_KEY"})
check("bearer when key set",
      chat2._headers().get("authorization") == "Bearer k123")

print("config gating:")
check("no file -> off", deep_think.from_config(
    deep_think.load_config("/nonexistent/config.json")) is None)
check("enabled=false -> off",
      deep_think.from_config({"deep_think": {"enabled": False}}) is None)
check("bad provider -> off",
      deep_think.from_config({"deep_think": {
          "enabled": True, "provider": "websocket", "base_url": "x",
          "model": "m"}}) is None)
check("anthropic without key -> off",
      deep_think.from_config({"deep_think": {
          "enabled": True, "provider": "anthropic", "base_url": "x",
          "model": "m", "api_key_env": "T_UNSET_KEY"}}) is None)
deep = deep_think.from_config({"deep_think": {
    "enabled": True, "provider": "anthropic", "base_url": "x",
    "model": "m", "api_key_env": "T_TEST_KEY"}})
check("anthropic with key -> on", deep is not None)
check("defaults", deep.timeout_s == 120 and deep.max_tokens == 700)
check("bad context_tokens -> off", deep_think.from_config({"deep_think": {
    "enabled": True, "provider": "openai-chat", "base_url": "x",
    "model": "m", "context_tokens": 100}}) is None)

print("context budget:")
check("est_tokens floors at 1", deep_think.est_tokens("") == 1
      and deep_think.est_tokens("abc") == 1)
check("est_tokens rounds up", deep_think.est_tokens("abcd") == 1
      and deep_think.est_tokens("abcde") == 2)
llama_payload = {"data": [{"id": "m", "meta":
                           {"context_length": 65536, "n_ctx_train": 65536}}]}
check("llama.cpp meta.context_length",
      deep_think.parse_model_ctx(llama_payload, "m") == 65536)
check("llama.cpp meta.n_ctx fallback",
      deep_think.parse_model_ctx({"data": [{"id": "m",
                                            "meta": {"n_ctx": 8192}}]},
                                 "m") == 8192)
check("vllm max_model_len",
      deep_think.parse_model_ctx({"data": [{"id": "m",
                                            "max_model_len": 32768}]},
                                 "m") == 32768)
check("anthropic context_window",
      deep_think.parse_model_ctx({"data": [{"id": "m",
                                            "context_window": 200000}]},
                                 "m") == 200000)
check("model absent -> None",
      deep_think.parse_model_ctx(llama_payload, "other") is None)
check("empty payload -> None", deep_think.parse_model_ctx({}, "m") is None)
check("sub-2048 fields ignored",
      deep_think.parse_model_ctx({"data": [{"id": "m",
                                            "context_window": 1024}]},
                                 "m") is None)

pin = deep_think.DeepThink({"provider": "openai-chat",
                            "base_url": "http://x/v1", "model": "m",
                            "context_tokens": 8192})
check("budget = ctx - max_tokens - reserve",
      pin.input_budget() == 8192 - 700 - 64, pin.input_budget())
check("budget cached", pin.input_budget() == pin._budget)
tight = deep_think.DeepThink({"provider": "openai-chat",
                              "base_url": "http://x/v1", "model": "m",
                              "context_tokens": 2048, "max_tokens": 1984})
check("budget floors at 1024", tight.input_budget() == 1024,
      tight.input_budget())

print("history clipping:")
hist = [(f"question number {i} " + "x" * 60,
         "answer " + "y" * 60) for i in range(30)]
url, payload = pin._wire("final ask", None, hist, "SYS")
check("fits -> all kept", len(payload["messages"]) == 2 * 30 + 2
      and pin.last_fit == {"kept": 61, "dropped": 0}, pin.last_fit)
# budget 1284 tokens; each pair ~ (61..)/4 + 4 + (67)/4 + 4 ~= 36 -> 30
# pairs ~= 1075 + system + final ask fits; shrink the budget hard:
url, payload = tight._wire("final ask", None, hist, "SYS")
n = len(payload["messages"])
check("overflow -> clipped to budget", n < 2 * 30 + 2 and n >= 3
      and n % 2 == 0, n)  # system + even pairs + final user
check("newest pair survives",
      payload["messages"][-3]["content"].startswith("question number 29"),
      payload["messages"][-3]["content"][:30])
check("oldest pair dropped first",
      all("question number 0" != m["content"][:17]
          for m in payload["messages"][1:-1]))
check("final user always last",
      payload["messages"][-1] == {"role": "user", "content": "final ask"})
check("last_fit recorded",
      pin.last_fit["dropped"] == 0
      and tight.last_fit["kept"] == n - 1 and tight.last_fit["dropped"] > 0,
      tight.last_fit)
# leading assistant shed (anthropic rejects assistant-first): a huge final
# assistant fits the budget but squeezes its own user turn out
huge = deep_think.DeepThink({"provider": "anthropic", "base_url": "http://x",
                             "model": "m", "api_key_env": "T_TEST_KEY",
                             "context_tokens": 2048, "max_tokens": 1984})
url, payload = huge._wire("final ask", None,
                          [("q" * 40, "z" * 4000)], "SYS")
check("leading assistant shed",
      payload["messages"] == [{"role": "user", "content": "final ask"}]
      and huge.last_fit["dropped"] >= 1, (payload["messages"],
                                          huge.last_fit))

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAIL: {FAILS}'}")
sys.exit(1 if FAILS else 0)
