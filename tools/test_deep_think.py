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

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAIL: {FAILS}'}")
sys.exit(1 if FAILS else 0)
