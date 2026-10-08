"""Deep-think providers: three wire dialects, one streaming interface.

Optional and config-gated (docs/PROTOCOL-deep-think.md). Config lives in
web/config.json (gitignored; web/config.example.json is the template);
API keys only ever come from the environment variable named by
api_key_env — never from disk. No config, enabled=false, invalid block,
or a missing key for a dialect that requires one -> from_config()
returns None and server.py never contacts anything.

All dialects stream SSE and normalize to (kind, delta) with kind in
{"reasoning", "text"}:

  openai-chat      POST {base}/chat/completions  — llama-server, vLLM, LM
                   Studio, Ollama, OpenAI and most aggregators; the local
                   big-model path is this dialect with a localhost URL
  openai-responses POST {base}/responses         — OpenAI Responses
                   surface; system prompt rides `instructions`
  anthropic        POST {base}/v1/messages       — system field,
                   x-api-key + anthropic-version headers; key required

stream() raises on any failure; the caller (server) owns the fallback to
the local think path.
"""
import asyncio
import json
import os
import threading
import time

import requests

# keep byte-identical with SYS in server.py (deep turns must sound like
# the same assistant; server passes its SYS explicitly, tools may rely
# on this default)
DEFAULT_SYSTEM = ("You are wiresense, a concise voice assistant. "
                  "Answer in one or two short sentences.")
PROVIDERS = ("openai-chat", "openai-responses", "anthropic")

# input budget when the provider won't report the model's context and
# the config doesn't pin one (conservative; fits the common local
# serving default). Disclosed in the startup log line either way.
DEFAULT_MODEL_CTX = 8192
GEN_RESERVE = 64          # slack over max_tokens for the round's answer
CHARS_PER_TOKEN = 4       # provider-agnostic estimate (no shared tokenizer)


def est_tokens(text):
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def parse_model_ctx(models_json, model):
    """Context window for `model` from a /models payload, or None.
    Only SERVING bounds are trusted: llama.cpp meta.context_length /
    context_length, vLLM max_model_len, OpenAI-style context_window.
    Training bounds (meta.n_ctx_train) are deliberately ignored — a
    262K-trained model can be served at 8192/slot; trusting the train
    number set a budget 32x too wide in measurement (see protocol doc,
    revision 2026-10-07)."""
    for m in (models_json or {}).get("data", []):
        if m.get("id") != model:
            continue
        for path in (("meta", "context_length"), ("context_length",),
                     ("max_model_len",), ("context_window",)):
            v = m
            for k in path:
                v = v.get(k) if isinstance(v, dict) else None
            if isinstance(v, (int, float)) and v >= 2048:
                return int(v)
    return None


def parse_props(props_json):
    """Serving window per request from a llama.cpp /props payload:
    default_generation_settings.n_ctx is the per-slot context — the
    true per-request bound for the chat dialect."""
    n = ((props_json or {}).get("default_generation_settings")
         or {}).get("n_ctx")
    return int(n) if isinstance(n, (int, float)) and n >= 2048 else None


def load_config(path=None):
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config.json")
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def from_config(cfg):
    """DeepThink instance, or None when the feature is off for any
    reason (reason printed once at startup; never raises)."""
    block = (cfg or {}).get("deep_think") or {}
    if not block.get("enabled"):
        return None
    try:
        deep = DeepThink(block)
    except (KeyError, TypeError, ValueError) as e:
        print(f"deep_think: config invalid, feature off: {e}")
        return None
    if not deep.available:
        print(f"deep_think: {deep.provider} requires the API key in env "
              f"{block.get('api_key_env')!r} — feature off")
        return None
    return deep


class DeepThink:
    def __init__(self, cfg):
        if cfg.get("provider") not in PROVIDERS:
            raise ValueError(f"provider must be one of {PROVIDERS}")
        self.provider = cfg["provider"]
        self.base_url = str(cfg["base_url"]).rstrip("/")
        self.model = str(cfg["model"])
        self.timeout_s = float(cfg.get("timeout_s", 120))
        self.max_tokens = int(cfg.get("max_tokens", 700))
        self.cfg = cfg
        self.key = os.environ.get(cfg.get("api_key_env", "")) or None
        ctx = cfg.get("context_tokens")
        if ctx is not None:
            ctx = int(ctx)
            if ctx < 2048:
                raise ValueError("context_tokens must be >= 2048")
        self._ctx_pinned = ctx
        self._budget = None
        self.last_fit = {"kept": 0, "dropped": 0}   # per stream() call

    def input_budget(self):
        """Prompt-token ceiling for one deep request: config pin, else
        the serving window detected from the provider (llama.cpp /props
        per-slot n_ctx, else exact keys from /models — resolved once,
        best effort), else the default — minus the generation reserve.
        Resolved lazily so a slow or absent endpoint never blocks
        startup."""
        if self._budget is None:
            if self._ctx_pinned is not None:
                ctx, src = self._ctx_pinned, "config"
            else:
                ctx, src = self._detect_ctx()
                if ctx is None:
                    ctx, src = DEFAULT_MODEL_CTX, "default"
            budget = max(1024, ctx - self.max_tokens - GEN_RESERVE)
            print(f"deep_think: input budget {budget} tokens "
                  f"(model ctx {ctx} [{src}], gen reserve "
                  f"{self.max_tokens + GEN_RESERVE})")
            self._budget = budget
        return self._budget

    def _detect_ctx(self):
        """-> (ctx, source) from the provider, or (None, None).
        llama.cpp /props first (per-slot serving window; /models on the
        same server only carries the training bound), then the exact
        serving keys in /models."""
        try:
            root = self.base_url[:-3] if self.base_url.endswith("/v1") \
                else self.base_url
            r = requests.get(f"{root}/props", headers=self._headers(),
                             timeout=10)
            n = parse_props(r.json())
            if n is not None:
                return n, "props"
        except Exception:
            pass
        try:
            n = parse_model_ctx(self._get_models(), self.model)
            if n is not None:
                return n, "models"
        except Exception:
            pass
        return None, None

    def _get_models(self):
        url = (f"{self.base_url}/v1/models" if self.provider == "anthropic"
               else f"{self.base_url}/models")
        r = requests.get(url, headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def _fit_messages(self, system, conv):
        """Clip the conversation to the input budget: final user turn
        always kept, history dropped oldest-first, leading assistant
        turns shed (Anthropic rejects assistant-first). Message count
        kept coarse (per-message est + 4-token overhead)."""
        budget = self.input_budget()
        used = est_tokens(system) + 6
        keep, dropped = [], 0
        for i in range(len(conv) - 1, -1, -1):
            role, text = conv[i]
            c = est_tokens(text) + 4
            if i < len(conv) - 1 and used + c > budget:
                dropped += 1
                continue
            if i < len(conv) - 1:
                used += c
            keep.append((role, text))
        keep.reverse()
        while keep and keep[0][0] == "assistant":
            keep.pop(0)
            dropped += 1
        if dropped:
            print(f"deep_think: history clipped, dropped {dropped} "
                  f"turn(s) to fit {budget}-token budget")
        self.last_fit = {"kept": len(keep), "dropped": dropped}
        return keep

    @property
    def available(self):
        return self.key is not None if self.provider == "anthropic" else True

    # --- wire construction -------------------------------------------------
    def _headers(self):
        h = {"content-type": "application/json"}
        if self.key:
            if self.provider == "anthropic":
                h["x-api-key"] = self.key
                h["anthropic-version"] = "2023-06-01"
            else:
                h["authorization"] = f"Bearer {self.key}"
        return h

    def _wire(self, utterance, directive, history, system):
        """-> (url, payload). history is (user, assistant) text pairs,
        clipped to the input budget (oldest dropped first) so an
        agentic round never overflows the serving model's window."""
        user = f"{utterance}\n({directive})" if directive else utterance
        conv = []
        for u, a in history:
            conv.append(("user", u))
            if a:
                conv.append(("assistant", a))
        conv.append(("user", user))
        conv = self._fit_messages(system, conv)
        if self.provider == "openai-chat":
            return (f"{self.base_url}/chat/completions",
                    {"model": self.model,
                     "messages": [{"role": "system", "content": system}] +
                                 [{"role": r, "content": t} for r, t in conv],
                     "max_tokens": self.max_tokens, "temperature": 0,
                     "stream": True})
        if self.provider == "openai-responses":
            return (f"{self.base_url}/responses",
                    {"model": self.model,
                     "input": [{"role": r, "content": [
                         {"type": "input_text" if r == "user"
                          else "output_text", "text": t}]} for r, t in conv],
                     "instructions": system,
                     "max_output_tokens": self.max_tokens,
                     "temperature": 0, "stream": True})
        return (f"{self.base_url}/v1/messages",
                {"model": self.model, "system": system,
                 "messages": [{"role": r, "content": t} for r, t in conv],
                 "max_tokens": self.max_tokens, "temperature": 0,
                 "stream": True})

    # --- SSE extraction ----------------------------------------------------
    def _extract(self, lines):
        """Yield (kind, delta) from SSE data lines; returns on stream end,
        raises on a provider-reported error."""
        for line in lines:
            if isinstance(line, str):
                line = line.encode()
            if not line.startswith(b"data: "):
                continue
            payload = line[len(b"data: "):]
            if payload == b"[DONE]":
                return
            j = json.loads(payload)
            if self.provider == "openai-chat":
                d = (j.get("choices") or [{}])[0].get("delta", {})
                rc = d.get("reasoning_content") or d.get("reasoning")
                if rc:
                    yield "reasoning", rc
                if d.get("content"):
                    yield "text", d["content"]
            elif self.provider == "openai-responses":
                t = j.get("type", "")
                if t == "response.output_text.delta":
                    yield "text", j.get("delta", "")
                elif t == "response.reasoning_summary_text.delta":
                    yield "reasoning", j.get("delta", "")
                elif t in ("response.completed", "response.incomplete",
                           "response.failed"):
                    return
            else:
                t = j.get("type", "")
                if t == "content_block_delta":
                    d = j.get("delta", {})
                    if d.get("type") == "text_delta":
                        yield "text", d.get("text", "")
                    elif d.get("type") == "thinking_delta":
                        yield "reasoning", d.get("thinking", "")
                elif t == "message_stop":
                    return
                elif t == "error":
                    raise RuntimeError(f"provider error: {j}")

    # --- streaming ---------------------------------------------------------
    async def stream(self, utterance, directive=None, history=(),
                     system=None):
        """Async generator of (kind, delta). HTTP + SSE parsing run in a
        worker thread; deltas cross via queue. One overall deadline covers
        the whole stream (ttft + full answer)."""
        system = system or DEFAULT_SYSTEM
        url, payload = self._wire(utterance, directive, history, system)
        loop = asyncio.get_event_loop()
        q = asyncio.Queue()
        done = object()

        def worker():
            try:
                r = requests.post(url, headers=self._headers(),
                                  json=payload, stream=True,
                                  timeout=self.timeout_s + 30)
                r.raise_for_status()
                for item in self._extract(r.iter_lines()):
                    loop.call_soon_threadsafe(q.put_nowait, item)
            except Exception as e:  # surfaced to the awaiting consumer
                loop.call_soon_threadsafe(q.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, done)

        threading.Thread(target=worker, daemon=True).start()
        deadline = time.monotonic() + self.timeout_s
        while True:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError(
                    f"deep provider exceeded {self.timeout_s}s total")
            try:
                item = await asyncio.wait_for(q.get(), timeout=budget)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"deep provider exceeded {self.timeout_s}s total")
            if item is done:
                return
            if isinstance(item, Exception):
                raise item
            yield item
