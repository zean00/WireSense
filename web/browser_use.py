"""Voice-driven browser tasks (PROTOCOL-browser-use.md).

Code owns the loop; the models propose. Each step: DOM snapshot (<= 40
visible interactive candidates + text excerpt) -> one slot-5 call picks
ONE action from a mutually exclusive set -> code validates it against
the snapshot, checks the stop gates, executes. Finish requires the step
LLM's `done` AND the goal watcher agreeing; the watcher is the
decide-layer readout pool on slot 6 (frozen GOAL_YES/NO state text,
tools/browser_pools.py), with the slot-4 LLM judge as the registered
G3-failure fallback (protocol decision 7).

Safety (decision 10): credential fields are never typed; submit-class
clicks park the loop for a spoken read-back through the session's
pending state machine (pending kind "browser", YES resumes the click,
NO cancels). Every step lands in the run trace
(/tmp/wiresense_browser/<id>/, the replay-folder pattern) and a
browser_event to the UI. Playwright is imported lazily: the server
boots and routes honestly with browser support missing (decision 14).
"""
import asyncio
import difflib
import hashlib
import json
import os
import re
import time
import uuid
from urllib.parse import quote, urlparse

import numpy as np
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = f"{ROOT}/tools/fixtures_browser"
TRACE_ROOT = "/tmp/wiresense_browser"

LLAMA = "http://127.0.0.1:8998"   # must match web/server.py
SLOT = 5                          # main decode — the only writer
JUDGE_SLOT = 4                    # LLM goal-watch fallback

MAX_STEPS = 10
MAX_SECONDS = 90.0
LLM_TIMEOUT = 8
CAND_CAP = 40
EXCERPT_CHARS = 1200

# submit-class clicks never run without a spoken read-back (decision 10b)
SUBMIT_RE = re.compile(
    r"submit|sign ?in|log ? ?in|place ?order|pay|checkout|\bbuy\b|"
    r"purchase|\border\b|delete|confirm|send\b", re.I)

# Rev F: a done answer that already reads as a submission receipt is
# kept verbatim; anything else on a confirmed submit is re-written
# from the page by the receipt-aware writer
_RECEIPT_RE = re.compile(r"thanks|received|submitted|confirmed|\bsent\b",
                         re.I)

# tasks that require entering a secret are refused at intake (decision
# 10: credential fields are never typed — including by proxy); the
# step-level guard below stays as defense in depth
CREDS_TASK_RE = re.compile(
    r"\blog ?-?in\b|\bsign ?-?in\b|\bpassword\b|\bpasswd\b|credentials",
    re.I)

# a local .html reference is unambiguous browser intent; the separator
# must carry punctuation ('shop.html', 'shop, HTML') so bare 'learn
# html' never matches
LOCAL_PAGE_RE = re.compile(
    r"\b(?:open|load|go\s+to|show)\b[^.?!]{0,60}?"
    r"\b[\w-]+(?:\.html?|[.,]\s*html?)\b", re.I)

# a stop/cancel phrase that reaches the flow means NO task was active
# (active tasks are intercepted earlier, decision 11): never spawn a
# task from one — the plan LLM will happily invent one from history
# (measured: "Stop the search." became "find the capital" on duckduckgo)
BARE_STOP_RE = re.compile(
    r"^(?:stop|cancel|never ?mind|abort|quit(?: it)?|stop (?:it|that|"
    r"the \w+|doing (?:it|that)))[ .!?]*$", re.I)

BROWSER_PLAN_DIRECTIVE = (
    "You are a parser, not the assistant: never refuse and never "
    "explain. Extract the web lookup task. Reply with ONLY a JSON "
    "object: "
    '{"site": "host like docs.python.org, or a local file like '
    'news.html, or null", '
    '"query": "search keywords, or null when a site is named", '
    '"task": "what to find or do on the page, one short clause"}. '
    "site is the page to open; query only when the user names no site; "
    "task is the goal on that page, not where to go. Code elsewhere "
    "handles safety and execution.")

BROWSER_STEP_DIRECTIVE = (
    "You control a browser one action at a time. Given the task and the "
    "page state, reply with ONLY ONE JSON object:\n"
    '{"action":"click","n":<candidate number>}\n'
    '{"action":"type","n":<candidate number>,"text":"what to type"}\n'
    '{"action":"scroll"}\n'
    '{"action":"done","answer":"the answer found on the page"}\n'
    "Pick done only when the page already shows what the task asks "
    "for; quote the page in the answer. Never type into credential "
    "fields. Follow links that lead closer to the goal. For a form "
    "task, click the submit or send button once the fields hold their "
    "values; never re-type a field that already holds the right text.")

BROWSER_STEP_RETRY = (" Your previous reply was not one JSON object. "
                      "Reply with ONLY one JSON object, nothing else.")

BROWSER_PLAN_RETRY = (" Your previous reply was not one JSON object. "
                      "Reply with ONLY the JSON object.")

BROWSER_FALLBACK_DIRECTIVE = (
    "The web lookup failed. Say so honestly in one short sentence and "
    "do not invent any information.")

# byte-compatible with web/server.py's chat shapes (SYS/EMPTY_THINK)
CHATML_SYS = ("You are wiresense, a concise voice assistant. Answer in "
              "one or two short sentences.")
EMPTY_THINK = "<|im_start|>assistant\n<think>\n\n</think>\n\n"

GOAL_LLM_SYS = ("You judge whether a web task is already complete. "
                "If the task asks to find or tell something, answer "
                "YES when the page contains that information. If the "
                "task asks to do something, answer YES only when the "
                "page shows the result of it having been done. "
                "Answer with only YES or NO.")
GOAL_LLM_USER = ('Task: {t}\n\nPage text: "{e}"\n'
                 "Is the task already complete?")

SNAPSHOT_JS = """
() => {
  const sel = 'a[href], button, input, select, textarea, '
    + '[role="button"], [role="link"], [role="tab"]';
  const all = document.querySelectorAll(sel);
  const out = [];
  all.forEach((el) => {
    if (out.length >= %d) return;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    if (el.offsetParent === null
        && getComputedStyle(el).position !== "fixed") return;
    el.setAttribute("data-ws-idx", String(out.length));
    const ac = el.getAttribute("autocomplete") || el.name || "";
    const cred = (el.type === "password"
                  || /pass|pwd|card|cvv|csc|ssn/i.test(ac)) ? 1 : 0;
    const role = el.tagName === "A" ? "link"
      : el.tagName === "BUTTON" ? "button"
      : el.tagName === "INPUT" ? (el.type === "password" ? "password"
                                  : "input")
      : el.tagName.toLowerCase();
    const name = (el.getAttribute("aria-label") || el.innerText
                  || el.placeholder || el.value || el.name
                  || el.title || "").trim().slice(0, 60);
    out.push({role, name, value: (el.value || "").slice(0, 40),
              cred, inview: r.top >= 0 && r.bottom <= innerHeight ? 1 : 0});
  });
  const excerpt = (document.body.innerText || "")
    .replace(/\\s+/g, " ").slice(0, %d);
  return {url: location.href, title: document.title,
          candidates: out, total: all.length, excerpt};
}
""" % (CAND_CAP, EXCERPT_CHARS)


def norm_router_text(t):
    """Must match web/server.py and tools/eval_browser.py."""
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _fixture_match(s):
    """Resolve a bare page name to a committed fixture, tolerating ASR
    homophones ("docs.html" heard as "docks.html") and a dropped
    extension. Deterministic: nearest real fixture file wins; no match
    leaves the name untouched."""
    try:
        files = [f for f in os.listdir(FIXTURE_DIR) if f.endswith(".html")]
    except OSError:
        return s
    stem = re.sub(r"\.html?$", "", s.lower())
    for f in files:
        if f.lower() == s.lower() or f.lower() == f"{stem}.html":
            return f
    near = difflib.get_close_matches(f"{stem}.html", files, n=1, cutoff=0.75)
    if near:
        return near[0]
    return s


def build_url(site, query):
    """Protocol decision 3: explicit site; bare <name>.html resolves to
    the committed fixture dir over file://; otherwise duckduckgo-lite."""
    if site:
        s = str(site).strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
        # "news, HTML" / "news html" planner echo -> bare fixture stem
        m = re.fullmatch(r"([\w-]+),?\s+html?", s, re.I)
        if m:
            s = m.group(1)
        if re.fullmatch(r"[\w-]+(\.html?)?", s):
            return f"file://{FIXTURE_DIR}/{_fixture_match(s)}"
        return "https://" + s
    if query:
        return "https://lite.duckduckgo.com/lite/?q=" + quote(str(query))
    return None


def _clean_think(text):
    """Strip a leaked reasoning block from a plain-text writer call
    (measured: the synth-answer continuation can open <think> and burn
    its whole budget there). Unclosed block: everything is trace."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in text:
        text = text.split("<think>")[0]
    return text.strip()


def _extract_json(text):
    m = re.search(r"\{", text)
    if not m:
        return None
    depth = 0
    for i in range(m.start(), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[m.start():i + 1])
                except Exception:
                    return None
    return None


def _llm(prompt, max_tokens, slot=SLOT, timeout=LLM_TIMEOUT):
    """Raw completion on the serving model (temp 0, warm prefix family).
    Deferred server import avoids the import cycle; LLAMA/SLOT must
    match web/server.py."""
    r = requests.post(f"{LLAMA}/v1/completions", json={
        "prompt": prompt, "max_tokens": max_tokens, "temperature": 0,
        "cache_prompt": True, "id_slot": slot}, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["text"]


def _embed(text):
    r = requests.post(f"{LLAMA}/v1/embeddings",
                      json={"input": norm_router_text(text),
                            "model": "wiresense-llm", "id_slot": 6},
                      timeout=30)
    v = r.json()["data"][0]["embedding"]
    if v is None or any(x is None for x in v):
        raise RuntimeError(f"null embedding (corruption signature)")
    a = np.asarray(v, dtype=np.float32)
    if np.isnan(a).any() or not a.any():
        raise RuntimeError("NaN/zero embedding")
    return a


def _heads():
    global _HEADS
    if _HEADS is None:
        _HEADS = np.load(f"{ROOT}/models/arm_C_heads.npz")
    return _HEADS


def _project(x):
    z = _heads()
    h = x @ z["fc1.weight"].T + z["fc1.bias"]
    h = 0.5 * h * (1.0 + np.tanh(0.7978845608 * (h + 0.044715 * h ** 3)))
    ln = (h - h.mean(-1, keepdims=True)) / np.sqrt(h.var(-1, keepdims=True) + 1e-5)
    h = ln * z["ln.weight"] + z["ln.bias"] + h
    return h @ z["fc2.weight"].T


def _norm(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


_HEADS = None


class PoolScorer:
    """1-NN cosine over a frozen YES/NO text pair of pools (slot-6
    embeddings, head-projected — the same path as routing). One shared
    instance per pool pair; pool embeddings are computed lazily once."""

    def __init__(self, yes_texts, no_texts):
        self.yes_texts, self.no_texts = yes_texts, no_texts
        self._vecs = None

    def _load(self):
        if self._vecs is None:
            yv = _norm(_project(np.stack([_embed(t) for t in self.yes_texts])))
            nv = _norm(_project(np.stack([_embed(t) for t in self.no_texts])))
            self._vecs = (yv, nv)
        return self._vecs

    def score(self, line):
        """Returns (picked_yes, yes_sim, no_sim, ms)."""
        t0 = time.perf_counter()
        yv, nv = self._load()
        v = _norm(_project(_embed(line)[None, :]))
        ys, ns = float((yv @ v.T).max()), float((nv @ v.T).max())
        return (ys > ns, ys, ns, (time.perf_counter() - t0) * 1000)


def goal_line(task, excerpt):
    """Scored-line composition — MUST match tools/eval_browser.py."""
    return f"Task: {task}. Page: {excerpt}"


class ReadoutGoalWatch:
    """The decide-layer watcher (protocol decision 7). Ships iff gate G3
    passes; see also LLMGoalWatch."""

    def __init__(self):
        import sys
        sys.path.insert(0, f"{ROOT}/tools")
        from browser_pools import GOAL_NO, GOAL_YES  # frozen single source
        self.pool = PoolScorer(GOAL_YES, GOAL_NO)

    async def check(self, task, excerpt, filled=None, links=None):
        line = goal_line(task, excerpt)
        loop = asyncio.get_event_loop()
        yes, ys, ns, ms = await loop.run_in_executor(None,
                                                     self.pool.score, line)
        return yes, round(max(ys, ns) - min(ys, ns), 3), round(ms)


class LLMGoalWatch:
    """Registered fallback if G3 fails: slot-4 YES/NO judge."""

    async def check(self, task, excerpt, filled=None, links=None):
        t0 = time.perf_counter()
        loop = asyncio.get_event_loop()

        def call():
            # filled fields ride as their own line: input values never
            # appear in body.innerText, and a judge that cannot see
            # them reads every half-filled form as complete (measured)
            user = GOAL_LLM_USER.format(t=task, e=excerpt[:800])
            if filled:
                user += "\nFilled fields: " + "; ".join(filled)
            if links:
                user += "\nPage links in order: " + "; ".join(links)
            r = requests.post(f"{LLAMA}/v1/chat/completions", json={
                "model": "wiresense-llm", "messages": [
                    {"role": "system", "content": GOAL_LLM_SYS},
                    {"role": "user", "content": user}],
                "max_tokens": 4, "temperature": 0, "cache_prompt": True,
                "id_slot": JUDGE_SLOT,
                "chat_template_kwargs": {"enable_thinking": False}},
                timeout=LLM_TIMEOUT)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

        try:
            verdict = (await loop.run_in_executor(None, call)).strip().upper()
        except Exception:
            return False, 0.0, round((time.perf_counter() - t0) * 1000)
        yes = verdict.startswith("YES")
        return yes, 0.0, round((time.perf_counter() - t0) * 1000)


def make_goal_watch():
    """Registered decision rule (protocol decision 7, gate G3): the
    readout goal head FAILED its frozen eval (9/20, chance-level — page
    excerpts are out-of-distribution for utterance-calibrated pools), so
    the slot-4 LLM judge ships. WIRESENSE_GOAL_WATCH=readout overrides
    for experiments only."""
    impl = os.environ.get("WIRESENSE_GOAL_WATCH", "llm")
    return ReadoutGoalWatch() if impl == "readout" else LLMGoalWatch()


_STOP_SCORER = None


def stop_scorer():
    """Mid-task voice control pool (decision 11; frozen texts in
    tools/browser_pools.py). Lazy: pool embeddings on first use."""
    global _STOP_SCORER
    if _STOP_SCORER is None:
        import sys
        sys.path.insert(0, f"{ROOT}/tools")
        from browser_pools import NOT_STOP, STOP
        _STOP_SCORER = PoolScorer(STOP, NOT_STOP)
    return _STOP_SCORER


class BrowserTask:
    def __init__(self, session, plan, utterance, detached=False):
        self.session = session
        self.site = plan.get("site")
        self.query = plan.get("query")
        self.task = str(plan.get("task") or "")[:200]
        self.utterance = utterance
        # deep-agents tool leg (PROTOCOL-deep-agents.md decision 16):
        # detached legs own their trace and page but never speak, set
        # pending, or enter history — the deep loop reads the outcome
        self.detached = detached
        self.park_text = None  # composed on park (detached yield uses it)
        self.id = uuid.uuid4().hex[:8]
        self.steps = []          # trace records, one per executed step
        self.outcome = None      # done|parked|cancelled|refused|error|stuck|budget
        self.answer = None
        self.page = None
        self.parked = None       # (n, name) submit-class click awaiting YES
        self._handle = None
        self._state = None       # snapshot carried across a park
        self._watch = make_goal_watch()
        self._goal_run = 0       # consecutive watcher-YES states
        self._unchanged = 0      # consecutive no-op steps (stuck watch)
        self._disagree = False   # LLM said done, watcher said no
        self._submit_task = bool(SUBMIT_RE.search(self.task))
        self._clicked = False    # confirmed submit-class click fired
        self._progress = False
        self.t0 = None

    def active(self):
        return self.outcome is None and self.parked is None

    # ---------- plumbing ----------

    async def run(self):
        self.t0 = time.perf_counter()
        url = build_url(self.site, self.query)
        try:
            self.page = await ensure_browser()
            if url is None:
                return await self.finish("error",
                                         "I couldn't work out which page "
                                         "to open — name a site and what "
                                         "to look for.")
            await self.page.goto(url, timeout=15000,
                                 wait_until="domcontentloaded")
        except ImportError:
            return await self.finish(
                "error", "Browser support isn't installed on the server.")
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self.finish(
                "error", "I couldn't open that page — it may be down or "
                         "the address may be wrong.")
        self._handle = asyncio.current_task()
        await self.loop()

    async def snapshot(self):
        try:
            self._state = await self.page.evaluate(SNAPSHOT_JS)
        except Exception:
            self._state = None
        return self._state

    @staticmethod
    def _hash(state):
        # typed values must count as change: input values live only in
        # candidate .value (body.innerText never shows them), and a hash
        # that ignores them reads every fill as no-progress -> stuck
        h = hashlib.sha1()
        h.update(state["url"].encode())
        for c in state["candidates"]:
            h.update(c["name"].encode())
            h.update((c.get("value") or "").encode())
        return h.hexdigest()

    def _state_lines(self, state):
        lines = []
        for i, c in enumerate(state["candidates"]):
            line = f'[{i}] {c["role"]}'
            if c["name"]:
                line += f' "{c["name"]}"'
            if c.get("value"):
                line += f' (filled: "{c["value"]}")'
            if not c["inview"]:
                line += " (below the fold)"
            if c["cred"]:
                line += " — credential field, never fill"
            lines.append(line)
        more = state["total"] - len(state["candidates"])
        if more > 0:
            lines.append(f"(+{more} more candidates — scroll to see them)")
        return "\n".join(lines)

    def _budget_left(self):
        return (time.perf_counter() - self.t0) < MAX_SECONDS

    async def _maybe_progress(self):
        if not self._progress and \
                time.perf_counter() - self.t0 > 12.0:
            self._progress = True
            await self.session.speak_fillers(["Still working on it."])

    def _trace(self, meta=None):
        try:
            d = f"{TRACE_ROOT}/{self.id}"
            os.makedirs(d, exist_ok=True)
            with open(f"{d}/steps.json", "w") as f:
                json.dump({"task": self.task, "site": self.site,
                           "query": self.query, "outcome": self.outcome,
                           "answer": self.answer, "steps": self.steps,
                           "meta": meta or {}}, f, indent=1)
        except Exception:
            pass

    async def _screenshot(self):
        try:
            d = f"{TRACE_ROOT}/{self.id}"
            os.makedirs(d, exist_ok=True)
            await self.page.screenshot(path=f"{d}/final.png")
        except Exception:
            pass

    # ---------- the loop (code owns it; models propose) ----------

    async def loop(self):
        state = self._state or await self.snapshot()
        while self.outcome is None and self._budget_left():
            if state is None:  # page died between steps
                return await self.finish(
                    "error", "The page stopped responding.")
            # goal watch FIRST on the fresh state (decision 7)
            filled = [f'{c.get("name")}="{c.get("value")}"'
                      for c in state["candidates"] if c.get("value")]
            links = [c.get("name") for c in state["candidates"]
                     if c.get("role") in ("link", "button")
                     and c.get("name")][:12]
            try:
                achieved, margin, wms = await self._watch.check(
                    self.task, state["excerpt"], filled=filled,
                    links=links)
            except Exception:
                achieved, margin, wms = False, 0.0, -1
            if self._submit_task and not self._clicked:
                # decision 10b invariant: until the confirmed click has
                # fired, a submit task is never complete — the judge
                # reads a filled form as the result (live run), so the
                # code gate, not the prompt, holds this line
                achieved = False
            self._goal_run = self._goal_run + 1 if achieved else 0
            if self._goal_run >= 2:
                return await self.finish("done",
                                         answer=await self.synth_answer(state))
            # stuck watch (decision 8): forced scroll, then honest stop
            if self._unchanged >= 3:
                return await self.finish(
                    "stuck", "I got stuck on that page — the "
                             "information may not be there.")
            forced_scroll = self._unchanged == 2
            step = None
            if not forced_scroll:
                step = await self.step_llm(state)
                if step is None:
                    self.steps.append({"action": "junk", "ms": None})
                    await self.session.send({
                        "type": "browser_event", "id": self.id,
                        "event": "step", "action": "junk"})
                    if len([s for s in self.steps
                            if s["action"] == "junk"]) >= 2:
                        return await self.finish(
                            "error", "I couldn't complete that — the "
                                     "page didn't cooperate.")
                    state = await self.snapshot()
                    continue
            if step and step.get("action") == "done":
                if achieved:
                    ans = str(step.get("answer") or "")[:300]
                    if (self._submit_task and self._clicked
                            and not _RECEIPT_RE.search(ans)):
                        # stage-2 Rev F (measured canaryD8/D9): on a
                        # confirmed submit the answer is the page's
                        # receipt — the model echoes its own typed
                        # note on vague send tasks; the receipt-aware
                        # writer quotes the page instead
                        receipt = await self.synth_answer(state)
                        if "doesn't seem" not in receipt:
                            ans = receipt
                    return await self.finish("done", answer=ans)
                # LLM done without watcher agreement (decision 7): keep
                # going, and nudge the next step call
                self._disagree = True
                self.steps.append({"action": "done_rejected",
                                   "answer": str(step.get("answer"))[:80]})
                continue
            ok = await self.execute(step or {"action": "scroll"}, state,
                                    forced=forced_scroll)
            if ok == "refused":
                return await self.finish(
                    "refused", "I can't type passwords or card details.")
            if ok == "parked":
                return  # resume lands in confirm_resume()
            if ok == "bad_index":
                self.steps.append({"action": "junk", "ms": None})
                if len([s for s in self.steps
                        if s["action"] == "junk"]) >= 2:
                    return await self.finish(
                        "error", "I couldn't complete that — the page "
                                 "didn't cooperate.")
            pre = self._hash(state)
            new_state = await self.snapshot()
            self._unchanged = self._unchanged + 1 \
                if new_state and self._hash(new_state) == pre else 0
            state = new_state
            await self._maybe_progress()
        if self.outcome is None:
            return await self.finish(
                "budget", "That took too long — I've stopped there.")

    # ---------- model calls ----------

    async def step_llm(self, state, retry=False):
        prompt = (f"Task: {self.task}\nURL: {state['url']}\n\n"
                  f"Candidates:\n{self._state_lines(state)}\n\n"
                  f'Page text: "{state["excerpt"][:800]}"')
        if self.detached:
            # stage-2 Rev A (measured, 5 canaries): the deep model
            # compresses the leg task clause ("the price of the
            # AeroPress Go" -> "find the price") and the step planner
            # then clicks blind — give it the deep utterance itself
            prompt += f"\nUser request: {self.utterance}"
        if self._disagree:
            prompt += ("\nNote: the page does not show the answer yet — "
                       "look for a link that leads closer.")
        if retry:
            prompt += BROWSER_STEP_RETRY
        prompt = ("Directives: " + BROWSER_STEP_DIRECTIVE
                  + "\n\nUser: " + prompt + "\nAssistant: {")
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(
                None, lambda: _llm(prompt, 120))
        except Exception:
            return None
        step = _extract_json("{" + text if not text.lstrip().startswith(
            "{") else text)
        if not isinstance(step, dict):
            return await self.step_llm(state, retry=True) if not retry \
                else None
        a = step.get("action")
        if a == "scroll":
            return {"action": "scroll"}
        if a in ("click", "type"):
            n = step.get("n")
            if not isinstance(n, int) or not (0 <= n < len(
                    state["candidates"])):
                return None
            out = {"action": a, "n": n, "name":
                   state["candidates"][n]["name"]}
            if a == "type":
                txt = str(step.get("text") or "").strip()
                if not txt or len(txt) > 200:
                    return None
                out["text"] = txt
            return out
        if a == "done":
            return step
        return None

    async def synth_answer(self, state):
        """Watcher-only finish: one writer call for the spoken answer,
        grounded in the page text (decision 10d)."""
        # ChatML + empty-think prefill: the plain-text "Assistant: "
        # continuation leaked <think> blocks and burned its budget
        # (measured); this shape answers clean
        if self._submit_task:
            # stage-2 Rev F (measured, canaryD8): the generic prompt
            # answers a send task with the message echo it typed — the
            # page's receipt is the answer the user should hear
            u = (f'Task: {self.task}\n\nPage text: "{state["excerpt"]}"\n\n'
                 "The task was to send or submit something: reply with "
                 "the page's confirmation that it was received, quoted "
                 "from the page. If the page does not confirm it, "
                 "reply exactly: NOT FOUND.")
        else:
            u = (f'Task: {self.task}\n\nPage text: "{state["excerpt"]}"\n\n'
                 "Reply with only the answer to the task, taken from "
                 "the page. If the page does not contain it, reply "
                 "exactly: NOT FOUND.")
        prompt = (f"<|im_start|>system\n{CHATML_SYS}<|im_end|>\n"
                  f"<|im_start|>user\n{u}<|im_end|>\n{EMPTY_THINK}")
        loop = asyncio.get_event_loop()
        try:
            text = (await loop.run_in_executor(
                None, lambda: _llm(prompt, 96))).strip()
        except Exception:
            return "I found the page but couldn't read the answer off it."
        text = _clean_think(text)
        return text[:300] if text and "NOT FOUND" not in text else \
            "The page doesn't seem to contain the answer."

    # ---------- action execution ----------

    async def execute(self, step, state, forced=False):
        a, n = step.get("action"), step.get("n")
        cand = (state["candidates"][n]
                if isinstance(n, int) and 0 <= n < len(state["candidates"])
                else None)
        t0 = time.perf_counter()
        rec = {"action": "scroll(forced)" if forced else a,
               "n": n, "name": step.get("name"), "ms": None}
        try:
            if forced:  # stuck watch (decision 8): one deterministic scroll
                await self.page.mouse.wheel(0, 800)
                await self.page.wait_for_timeout(150)
            elif a == "click":
                if cand is None:
                    rec["ms"] = round((time.perf_counter() - t0) * 1000)
                    self.steps.append(rec)
                    return "bad_index"
                if SUBMIT_RE.search(cand["name"] or ""):
                    rec["action"] = "click(confirm-gated)"
                    rec["ms"] = round((time.perf_counter() - t0) * 1000)
                    self.steps.append(rec)
                    await self.park(n, cand["name"])
                    return "parked"
                await self.page.click(f'[data-ws-idx="{n}"]', timeout=3000)
                try:
                    await self.page.wait_for_load_state(
                        "domcontentloaded", timeout=5000)
                except Exception:
                    pass
            elif a == "type":
                if cand is None:
                    rec["ms"] = round((time.perf_counter() - t0) * 1000)
                    self.steps.append(rec)
                    return "bad_index"
                if cand["cred"]:
                    return "refused"
                await self.page.fill(f'[data-ws-idx="{n}"]', step["text"],
                                     timeout=3000)
            elif a == "scroll":
                await self.page.mouse.wheel(0, 800)
                await self.page.wait_for_timeout(150)
            else:
                return "bad_index"
        except asyncio.CancelledError:
            raise
        except Exception:
            rec["error"] = True
        rec["ms"] = round((time.perf_counter() - t0) * 1000)
        self.steps.append(rec)
        await self.session.send({
            "type": "browser_event", "id": self.id, "event": "step",
            "action": rec["action"], "n": n, "ms": rec["ms"]})
        self._disagree = False
        return "ok"

    # ---------- parking (decision 10b: the M2 machinery, generalized) --

    async def park(self, n, name):
        self.parked = (n, name)
        self.outcome = "parked"
        host = urlparse(self.page.url).hostname or "the local page"
        self.park_text = f'Just to confirm — click "{name}" on {host}?'
        self._state = await self.snapshot()
        self._trace({"parked_click": name})
        if self.detached:
            # guard-yield (PROTOCOL-deep-agents.md decision 17): the deep
            # turn is still holding the mic; respond() speaks this
            # read-back only after the partial answer, then sets pending
            return
        # the read-back is the out-of-turn reply; the session's pending
        # state machine takes over until YES/NO/drop
        await self.session.browser_done(self, text=self.park_text)

    async def confirm_resume(self):
        """YES resolution (respond pending branch): execute the parked
        click, then continue the loop out-of-turn. Always returns the
        resolve turn's spoken reply."""
        n, name = self.parked
        self.parked = None
        try:
            await self.page.click(f'[data-ws-idx="{n}"]', timeout=3000)
            try:
                await self.page.wait_for_load_state(
                    "domcontentloaded", timeout=5000)
            except Exception:
                pass
            self.steps.append({"action": "click", "n": n, "name": name,
                               "confirmed": True})
            self._clicked = True
        except Exception:
            self.outcome = "error"
            self._trace()
            self._release()
            self.session.browser_task = None
            return "The click failed after confirmation."
        self.outcome = None
        self.t0 = time.perf_counter()  # fresh time budget after the park
        self._state = None  # the click may have navigated: re-snapshot
        self._handle = asyncio.create_task(self.loop())
        return "On it."

    def abort(self):
        """NO resolution (or dropped pending): nothing was clicked — the
        park held the loop. Silent; the caller speaks."""
        self.parked = None
        self.outcome = "cancelled"
        self.answer = "Okay, cancelled."
        self._trace()
        self._release()

    async def cancel(self):
        """Stop utterance mid-task: hard-cancel the pending await."""
        self.outcome = "cancelled"
        self.answer = "Stopped."
        if self._handle and not self._handle.done():
            self._handle.cancel()
        self._trace()
        await self.session.send({
            "type": "browser_event", "id": self.id, "event": "cancelled",
            "steps": self.steps, "answer": "Stopped."})
        self._release()
        self.session.browser_task = None

    def _release(self):
        try:
            if self.page:
                asyncio.create_task(self.page.close())
        except Exception:
            pass

    # ---------- completion ----------

    async def finish(self, outcome, answer):
        self.outcome = outcome
        self.answer = answer
        await self._screenshot()
        self._trace()
        self._release()
        if self.detached:
            # deep-agents leg (decision 16): the deep reply is the
            # completion — no spoken answer, no history, no terminal
            # browser_event (the IR-leg precedent)
            return
        await self.session.browser_done(self)


# ---------- browser lifecycle (lazy: server boots without playwright) --

_PW = None
_BROWSER = None


async def ensure_browser():
    global _PW, _BROWSER
    if _BROWSER is None:
        from playwright.async_api import async_playwright
        _PW = await async_playwright().start()
        _BROWSER = await _PW.chromium.launch(
            headless=os.environ.get("WIRESENSE_HEADED") != "1")
    return await _BROWSER.new_page()
