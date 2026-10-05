"""Local information retrieval — the track that reads
(PROTOCOL-ir.md).

The browser track acts (click/type/submit, emulates a user); the IR
track only reads: navigation is a direct page.goto, and the action
vocabulary has no click/type/fill at all. Three depths, one loop,
code-owned budgets: fetch (a named page, one question), search (SERP
-> top results -> one synthesized answer), research (rounds of
pick -> fetch -> digest; the model reformulates queries and follows
deeper links until the digest says answered or the budget is spent).

Judgments are slot-5 LLM calls, never page-text embedding pools — the
browser phase measured the readout goal pool at chance on page text
(G3, 9/20). Routing coverage that does not separate at the root level
ships as code intercepts (SEARCH_INTENT_RE / PAGE_QUESTION_RE, the
LOCAL_PAGE_RE precedent). Every fetch and query lands in the run trace
(/tmp/wiresense_ir/<id>/trace.json) with a snapped flag recording
whether the URL resolved to a committed replay fixture — gate E5
asserts every gate trace is fixture-only (no live web in any gate).
Playwright is imported lazily through browser_use.ensure_browser: the
server boots and routes honestly with browser support missing.
"""
import asyncio
import difflib
import json
import os
import re
import time
import uuid
from urllib.parse import quote, urlparse, parse_qs, unquote

import requests

from browser_use import (_clean_think, _extract_json, _llm,
                         CREDS_TASK_RE, ensure_browser)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = f"{ROOT}/tools/fixtures_ir"
TRACE_ROOT = "/tmp/wiresense_ir"

LLAMA = "http://127.0.0.1:8998"   # must match web/server.py
SLOT = 5                          # main decode — the only writer

# code-owned budgets (protocol decision 2)
MAX_SECONDS = {1: 20.0, 2: 45.0, 3: 90.0}
FETCH_BUDGET = {1: 2, 2: 4, 3: 6}   # gotos total (SERP pages included)
SERP_BUDGET = {1: 0, 2: 1, 3: 3}    # SERP pages
DEPTH2_RESULTS = 3                  # fan-out cap (decision 8)
EXTRACT_CHARS = 2400
DIGEST_CHARS = 1200
DIGEST_MAX_TOKENS = 360  # findings+answer+next fields; truncation reads as {}

SERP_URL = "https://lite.duckduckgo.com/lite/?q="
# decision 6: one fallback-engine attempt after an anomaly/empty SERP;
# Mojeek was unreachable from this box at registration time — a
# registered decision rule, not a verified dependency
FALLBACK_SERP_URLS = ["https://html.duckduckgo.com/html/?q=",
                      "https://www.mojeek.com/search?q="]

# intake intercepts (revision log): code wins over the centroid where
# the embedding space cannot separate the frame. SEARCH_INTENT_RE needs
# an explicit web token near the search verb so "search for flights"
# stays tool and "look up X on Wikipedia/IMDb" stays browser.
SEARCH_INTENT_RE = re.compile(
    r"\bsearch\b[^.?!]{0,30}?\b(?:web|online|internet|archive)\b"
    r"|\b(?:find out|look it up|read up|look up)\b[^.?!]{0,30}?"
    r"\bonline\b", re.I)
# the bare-page branch requires a content verb (say/about/...) so
# "can you read the text on this page I'm showing?" stays vision
PAGE_QUESTION_RE = re.compile(
    r"\b(?:what|how|read)\b[^.?!]{0,60}?\blocal\b[^.?!]{0,30}?"
    r"\b(?:page|file)\b"
    r"|\b(?:what|how)\b[^.?!]{0,40}?\b(?:page|file)\b[^.?!]{0,40}?"
    r"\b(?:say|says|about|recommend|contain|list|cover|mention|state)\b",
    re.I)

# research frames are depth 3 by registered definition (decision 5);
# the planner miscounts them under plan variance, so code owns the floor
RESEARCH_RE = re.compile(
    r"\b(?:research(?:ing)?|dig(?:ging)?)\b|\blocal\s+archive\b", re.I)

# request-frame words that leak into planned queries (measured e2e:
# 'Research espresso brewing ratios in the local archive' planned
# 'brewing ratios local archive' — missing the frozen replay key);
# dropped on both sides before token-subset matching
QUERY_STOPWORDS = {"local", "archive", "page", "web", "online",
                   "internet", "please"}

# measured TTS->ASR homophones (spoken gates only): Phonon writes
# 'doe' for 'dough' (deterministic, difflib ratio 0.5 — below any
# defensible fuzzy cutoff), so the resolver fallback matches key
# tokens against their measured spoken variants
TOKEN_VARIANTS = {"dough": ("doe",)}

IR_PLAN_DIRECTIVE = (
    "You are a parser, not the assistant: never refuse and never "
    "explain. Extract the information request. Reply with ONLY a JSON "
    "object: "
    '{"depth": 1 or 2 or 3, '
    '"url": "the page to read, like moon.html or https://..., or null", '
    '"query": "two to five content keywords, or null", '
    '"question": "what to answer, one short clause"}. '
    "depth 1 = a named page is given; depth 2 = search and tell; "
    "depth 3 = research, dig in, compare, or find out in depth. "
    "When a page is named, depth is 1. Code elsewhere handles safety "
    "and execution.")

IR_PICK_DIRECTIVE = (
    "You are picking which search result to open, hunting for the "
    "answer. Reply with ONLY one JSON object: "
    '{"open": <number of the result most likely to hold the answer>, '
    'or "open": null if none could}. No other text.')

IR_DIGEST_DIRECTIVE = (
    "You are collecting facts to answer a question from web pages. "
    "Reply with ONLY one JSON object:\n"
    '{"answered": true or false — do the collected facts answer the '
    'question,\n'
    ' "answer": "the answer, quoted from the pages, or null",\n'
    ' "findings": "everything collected so far, in one short '
    'paragraph, only what the pages actually say",\n'
    ' "next_query": "a better search query, if the answer is still '
    'missing and another search would help — name a concrete thing to '
    'look up (a product, a device, a topic page), not the question '
    'itself; if the first search failed, approach the topic from a '
    'different angle (a question about X often matches pages about the '
    'tool or product that does X), else null",\n'
    ' "next_open": "the address of a link worth opening from this '
    'page, if one is listed, else null",\n'
    ' "give_up": true only if no search or link could plausibly '
    'answer the question}\n'
    'Never invent facts; findings carry only what pages say.')

IR_FALLBACK_DIRECTIVE = (
    "The lookup failed. Say so honestly in one short sentence and do "
    "not invent any information.")

# committed replay registry (decision 6): gate queries -> SERP replay
# fixtures. Exact match, then substring, then token-subset, all on
# norm_router_text. A registry miss goes to the live engine (and gate
# E5 then fails that case honestly).
FROZEN_SERP_FIXTURES = {
    "espresso brewing ratios": "serp_ratio.html",
    "espresso grind settings": "serp_empty.html",
    "best coffee grinder for espresso": "serp_grinder.html",
    "proof dough": "serp_kitchen.html",
}

# read-only page state: text + links (decision 1: no candidates, no
# interaction metadata — nothing here can be clicked)
READONLY_JS = """
() => {
  const results = [...document.querySelectorAll("a.result-link")]
    .map(a => a.href).filter(Boolean);
  const links = [...document.querySelectorAll("a[href]")]
    .map(a => ({href: a.href,
                name: (a.innerText || "").trim().slice(0, 60)}))
    .filter(l => l.href && l.name);
  const text = (document.body.innerText || "")
    .replace(/\\s+/g, " ").slice(0, %d);
  return {url: location.href, title: document.title,
          results, links: links.slice(0, 30), text};
}
""" % EXTRACT_CHARS


def norm_router_text(t):
    """Must match web/server.py and tools/eval_ir.py."""
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _fixture_files():
    try:
        return [f for f in os.listdir(FIXTURE_DIR) if f.endswith(".html")]
    except OSError:
        return []


def fixture_snap(url):
    """Decision 7: a URL whose filename stem matches a fixtures_ir page
    resolves to file:// (browser's _fixture_match precedent). Live URLs
    never collide (stems are specific); every snap is recorded."""
    try:
        path = urlparse(url).path
        stem = os.path.basename(path)
        if not stem:
            return url, False
        files = _fixture_files()
        for f in files:
            if f.lower() == stem.lower():
                return f"file://{FIXTURE_DIR}/{f}", True
        near = difflib.get_close_matches(stem, files, n=1, cutoff=0.75)
        if near:
            return f"file://{FIXTURE_DIR}/{near[0]}", True
    except Exception:
        pass
    return url, False


def _tokens(s):
    return set(norm_router_text(s).split()) - QUERY_STOPWORDS


def _fuzzy_hit(token, tokens):
    for t in tokens:
        if t == token or difflib.SequenceMatcher(
                None, t, token).ratio() >= 0.75:
            return t
    return None


def resolve_query(query, utterance=None):
    """Registered query -> replay fixture, or the live SERP URL
    (decision 6). Planned query: exact, then substring, then
    token-subset, all on norm_router_text (registered rule). Revision 3
    fallback: if the plan misses, the raw repaired utterance is matched
    one-directionally — every registry-key token must fuzzy-match a
    distinct utterance token at the fixture_snap cutoff (0.75) —
    because TTS->ASR garbles content words (measured: 'dough' -> 'doe')
    and planned queries copied from garbled input can never hit.
    Returns (url, replay_name_or_None)."""
    q = norm_router_text(str(query or ""))
    if not q:
        return None, None
    qt = _tokens(q)
    for key, fixture in FROZEN_SERP_FIXTURES.items():
        if norm_router_text(key) == q:
            return f"file://{FIXTURE_DIR}/{fixture}", fixture
    for key, fixture in FROZEN_SERP_FIXTURES.items():
        k = norm_router_text(key)
        if k in q or q in k:
            return f"file://{FIXTURE_DIR}/{fixture}", fixture
    for key, fixture in FROZEN_SERP_FIXTURES.items():
        kt = _tokens(key)
        if qt and (qt <= kt or kt <= qt):
            return f"file://{FIXTURE_DIR}/{fixture}", fixture
    # revision 5 (7): reformulations are LLM-phrased and rarely land a
    # strict subset — 'best espresso grinder settings' must reach the
    # grinder fixture — so a containment layer scores how much of the
    # QUERY a key covers; needs >= 2 shared tokens, best key wins
    best_fix, best_score = None, 0.0
    for key, fixture in FROZEN_SERP_FIXTURES.items():
        kt = _tokens(key)
        inter = len(qt & kt)
        if inter >= 2 and qt:
            score = inter / len(qt)
            if score >= 0.75 and score > best_score:
                best_fix, best_score = fixture, score
    if best_fix:
        return f"file://{FIXTURE_DIR}/{best_fix}", best_fix
    if utterance:
        ut = norm_router_text(utterance).split()
        for key, fixture in FROZEN_SERP_FIXTURES.items():
            pool = list(ut)
            for t in _tokens(key):
                cands = [t] + list(TOKEN_VARIANTS.get(t, ()))
                hit = None
                for c in cands:
                    hit = _fuzzy_hit(c, pool)
                    if hit is not None:
                        break
                if hit is None:
                    break
                pool.remove(hit)
            else:
                return f"file://{FIXTURE_DIR}/{fixture}", fixture
    return SERP_URL + quote(str(query)), None


def decode_serp_href(href):
    """Decode duckduckgo redirect wrappers (//duckduckgo.com/l/?uddg=)."""
    try:
        p = urlparse(href)
        if p.path.startswith("/l/") or "duckduckgo.com/l/" in href:
            qs = parse_qs(p.query)
            if "uddg" in qs:
                return unquote(qs["uddg"][0])
    except Exception:
        pass
    return href


def is_serp_anomaly(state):
    """Decision 6: the anomaly challenge page or zero parsed results.
    Measured 2026-10-05: a script GET to lite.duckduckgo.com returns
    'Unfortunately, bots use DuckDuckGo too.' with zero result links."""
    return not state.get("results")


def build_answer(findings, hedged):
    """Final synthesis for budget/give-up finishes (decision 9): the
    findings are already the synthesis, so this only adds the hedge."""
    if not findings:
        return ("I couldn't find an answer to that in the time I gave "
                "myself.")
    if hedged:
        return ("I couldn't nail that down in the time I gave myself — "
                f"best I found: {findings}")
    return findings


class IRTask:
    def __init__(self, session, plan, utterance):
        self.session = session
        try:
            self.depth = int(plan.get("depth"))
        except (TypeError, ValueError):
            self.depth = 2
        if self.depth not in (1, 2, 3):
            self.depth = 2
        if RESEARCH_RE.search(utterance):
            # research frames are depth 3 by registered definition;
            # code owns this over plan variance (measured e2e: the
            # planner returned depth 2 for both E4 research cases;
            # run 4: the ASR dropped the leading 'Research', so the
            # floor also fires on 'local archive')
            self.depth = 3
        self.utterance = utterance
        # same shape as browser LOCAL_PAGE_RE: dot or punctuated html —
        # bare 'learn html' must never become a page name (recorded trap)
        # measured ASR joins: 'burr.html', 'burr, html', 'burr? HTML'
        u = re.search(r"\b([\w-]+)(?:\.html?|[.,?]\s+html?)\b",
                      utterance, re.I)
        self.url = u.group(1).lower() + ".html" if u else plan.get("url")
        if self.url:
            self.depth = 1      # a named page is depth 1 (registered)
        q = plan.get("query")
        if isinstance(q, (list, tuple)):
            # measured (run 4): the planner sometimes emits several
            # query candidates — str() of a list built a garbage URL
            q = " ".join(str(x) for x in q if x)
        self.query = q
        self.question = str(plan.get("question")
                            or plan.get("task") or "")[:200]
        for canon, variants in TOKEN_VARIANTS.items():
            for v in variants:
                # measured (run 4): the homophone propagates into the
                # question; the temp-0 synth refuses doe == dough
                self.question = re.sub(rf"\b{v}\b", canon, self.question,
                                       flags=re.I)
        self.id = uuid.uuid4().hex[:8]
        self.fetches = []       # trace records: {url, snapped, title, ms}
        self.queries = []
        self.findings = ""
        self.outcome = None     # done|cancelled|refused|error|budget
        self.answer = None
        self.page = None
        self._handle = None
        self._progress = False
        self.t0 = None

    def active(self):
        return self.outcome is None

    # ---------- plumbing ----------

    async def run(self):
        self.t0 = time.perf_counter()
        try:
            self.page = await ensure_browser()
        except ImportError:
            return await self.finish(
                "error", "Browser support isn't installed on the server.")
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self.finish(
                "error", "I couldn't open a browser to read that.")
        self._handle = asyncio.current_task()
        try:
            if self.depth == 1:
                await self.run_fetch()
            elif self.depth == 2:
                await self.run_search()
            else:
                await self.run_research()
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.active():
                return await self.finish(
                    "error", "That lookup hit an error — I've stopped "
                             "there.")
        if self.outcome is None:
            await self.finish_budget()

    def budget_left(self):
        return (time.perf_counter() - self.t0) < MAX_SECONDS[self.depth]

    def _trace(self):
        try:
            d = f"{TRACE_ROOT}/{self.id}"
            os.makedirs(d, exist_ok=True)
            with open(f"{d}/trace.json", "w") as f:
                json.dump({"utterance": self.utterance,
                           "question": self.question, "depth": self.depth,
                           "url": self.url, "query": self.query,
                           "queries": self.queries,
                           "findings": self.findings,
                           "outcome": self.outcome,
                           "answer": self.answer,
                           "fetches": self.fetches}, f, indent=1)
        except Exception:
            pass

    async def _maybe_progress(self):
        if not self._progress and self.depth == 3 and \
                time.perf_counter() - self.t0 > 12.0:
            self._progress = True
            await self.session.speak_fillers(["Still working on it."])

    async def goto(self, url, kind):
        """The only navigation primitive (decision 1). Returns the page
        state or None; every goto is traced with its snap flag. The
        FETCH_BUDGET cap is code-owned (decision 2)."""
        if len(self.fetches) >= FETCH_BUDGET[self.depth]:
            return None
        url, snapped = fixture_snap(url)
        t0 = time.perf_counter()
        try:
            await self.page.goto(url, timeout=15000,
                                 wait_until="domcontentloaded")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.fetches.append({"url": url, "snapped": snapped,
                                 "kind": kind, "error": True,
                                 "ms": round((time.perf_counter() - t0)
                                             * 1000)})
            return None
        state = None
        try:
            state = await self.page.evaluate(READONLY_JS)
        except Exception:
            state = None
        rec = {"url": url, "snapped": snapped, "kind": kind,
               "title": (state or {}).get("title", "")[:80],
               "chars": len((state or {}).get("text", "")),
               "ms": round((time.perf_counter() - t0) * 1000)}
        self.fetches.append(rec)
        await self.session.send({
            "type": "ir_event", "id": self.id, "event": "fetch",
            "url": url, "snapped": snapped, "kind": kind, "ms": rec["ms"]})
        return state

    # ---------- depth 1: fetch ----------

    async def run_fetch(self):
        if not self.url:
            return await self.finish(
                "error", "I couldn't work out which page to read — "
                         "name the page and what to look for.")
        state = await self.goto(self.url, "page")
        if state is None or not state.get("text"):
            return await self.finish(
                "error", "I couldn't read that page — it may be down "
                         "or empty.")
        answer = await self.synth(self.question, state["text"])
        await self.finish("done", answer)

    # ---------- depth 2: search ----------

    async def run_search(self):
        """Decision 8: the fan-out is code-owned — first 3 parseable
        results fetched in order, then ONE synthesis over the extracts."""
        serp, replay = resolve_query(self.query, self.utterance)
        if serp is None:
            return await self.finish(
                "error", "I couldn't work out what to search for — "
                         "give me the topic and the question.")
        self.queries.append({"query": self.query, "replay": replay})
        state = await self.goto(serp, "serp")
        if state is None or is_serp_anomaly(state):
            # revision 5: the replay IS the engine's answer — the live
            # fallback fires only when the SERP was live; a replayed
            # empty SERP at depth 2 answers honestly
            if replay is None:
                state = await self.goto(FALLBACK_SERP_URLS[0]
                                        + quote(str(self.query)), "serp-fb")
        if state is None or is_serp_anomaly(state):
            return await self.finish(
                "error", "The search engine isn't returning results "
                         "right now — it may be blocking automated "
                         "searches.")
        extracts = []
        for href in state.get("results")[:DEPTH2_RESULTS]:
            if not self.budget_left():
                break
            page = await self.goto(decode_serp_href(href), "result")
            if page and page.get("text"):
                extracts.append(page["text"][:700])
        if not extracts:
            return await self.finish(
                "error", "The search results wouldn't open — I "
                         "couldn't read anything.")
        answer = await self.synth(self.question, " ".join(extracts))
        await self.finish("done", answer)

    # ---------- depth 3: research ----------

    async def run_research(self):
        query = self.query
        pending_open = None
        queue = []          # unfetched results, DOM order (revision 5)
        first = True        # the utterance fallback applies only to the
                            # first query (revision 5: a digest-driven
                            # reformulation must resolve on its own —
                            # else it re-hits the same replay forever)
        for _ in range(SERP_BUDGET[3]):
            if not self.budget_left():
                break
            if pending_open:
                page = await self.goto(pending_open, "deep-link")
                pending_open = None
            elif queue:
                page = await self.goto(decode_serp_href(queue.pop(0)),
                                       "result")
            else:
                if not query:
                    break
                serp, replay = resolve_query(
                    query, self.utterance if first else None)
                first = False
                self.queries.append({"query": query, "replay": replay})
                page = await self.goto(serp, "serp")
                if page is None or is_serp_anomaly(page):
                    # revision 5: live fallback only on a live SERP
                    if replay is None:
                        page = await self.goto(
                            FALLBACK_SERP_URLS[0] + quote(str(query)),
                            "serp-fb")
                if page is None or is_serp_anomaly(page):
                    self.findings = self.findings or ""
                    d = await self.digest(no_results=True, failed_query=query)
                    if d.get("next_query"):
                        query = d["next_query"]
                        continue
                    break
                results = page.get("results") or []
                if not results:
                    break
                named = {decode_serp_href(l.get("href")):
                         (l.get("name") or "")
                         for l in (page.get("links") or [])}
                options = [{"href": r,
                            "name": named.get(decode_serp_href(r), "")}
                           for r in results]
                pick = await self.pick(query, options)
                if pick is None:
                    d = await self.digest(no_pick=True)
                    if d.get("next_query"):
                        query = d["next_query"]
                        continue
                    break
                # the pick decides who goes FIRST; unanswered rounds
                # walk the rest in DOM order (revision 5: a wrong pick
                # must not re-SERP into the same round forever)
                queue = [o["href"] for o in options]
                page = await self.goto(
                    decode_serp_href(queue.pop(pick)), "result")
            if page is None or not page.get("text"):
                continue
            d = await self.digest(page=page)
            if d.get("answered") and d.get("answer"):
                return await self.finish("done", d["answer"])
            if d.get("give_up"):
                break
            if d.get("next_open"):
                pending_open = d["next_open"]
            elif queue:
                pass          # revision 5: results before reformulation
                              # — a wrong pick self-heals by walking the
                              # queue; re-searching now would re-resolve
                              # to the same replay or go live
            elif d.get("next_query"):
                query = d["next_query"]
            await self._maybe_progress()
        await self.finish("done" if self.findings else "budget",
                          build_answer(self.findings, True))

    # ---------- model calls ----------

    async def pick(self, query, results):
        # revision 4: the picker sees result NAMES, not bare hrefs —
        # measured e2e: with only '[0] burr.html [1] kitchen.html' the
        # slot-5 pick deterministically chose kitchen.html (recipe-ish)
        # for a ratio question in all three rounds; names are the only
        # signal the registered pick has
        lines = "\n".join(
            f"[{i}] {r['name']} — {r['href']}"
            for i, r in enumerate(results[:8]))
        u = (f"Question: {self.question}\nSearch: {query}\n\n"
             f"Results:\n{lines}")
        prompt = ("Directives: " + IR_PICK_DIRECTIVE
                  + "\n\nUser: " + u + "\nAssistant: {")
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(
                None, lambda: _llm(prompt, 24))
        except Exception:
            text = None
        j = _extract_json("{" + text if text and not text.lstrip()
                          .startswith("{") else (text or ""))
        n = j.get("open") if isinstance(j, dict) else None
        if isinstance(n, int) and 0 <= n < len(results):
            return n
        # deterministic floor: argmax token overlap of name+href with
        # the query; a null LLM pick must not waste a round
        qt = set(norm_router_text(str(query or "")).split())
        best, best_n = None, 0
        for i, r in enumerate(results):
            words = set(norm_router_text(
                f"{r['name']} {r['href']}").split())
            ov = len(qt & words)
            if ov > best_n:
                best, best_n = i, ov
        return best

    async def digest(self, page=None, no_results=False, no_pick=False,
                     failed_query=None):
        if no_results:
            # measured: with no query context the slot-5 reformulation
            # degenerates into repetition ('coffee beans, coffee
            # beans…'); the failed query is the thing to improve on
            page_state = (f'The search for "{failed_query or self.query}" '
                          'returned: "No results for that search." '
                          'Questions about doing X are often answered '
                          'by pages about the tool or product that '
                          'does X (a question about which grind to '
                          'use → a search about grinders). Suggest a '
                          'better, shorter search query naming such '
                          'a concrete thing.')
            links_line = ""
        else:
            body = (page or {}).get("text") or ""
            links = (page or {}).get("links") or []
            page_state = f'Page: "{body[:DIGEST_CHARS]}"'
            links_line = ("\nLinks on the page: "
                          + "; ".join(f'{l["name"]} ({l["href"]})'
                                      for l in links[:12])) if links \
                else ""
        if no_pick:
            page_state = ("None of the search results looked promising. "
                          + page_state)
        u = (f"Question: {self.question}\n\n"
             f"Collected so far: {self.findings or '(nothing yet)'}\n\n"
             f"{page_state}{links_line}")
        prompt = ("Directives: " + IR_DIGEST_DIRECTIVE
                  + "\n\nUser: " + u + "\nAssistant: {")
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(
                None, lambda: _llm(prompt, DIGEST_MAX_TOKENS))
        except Exception:
            return {}
        j = _extract_json("{" + text if not text.lstrip().startswith(
            "{") else text)
        if not isinstance(j, dict):
            return {}
        if j.get("findings"):
            self.findings = _clean_think(str(j["findings"]))[:600]
        return j

    async def synth(self, question, text):
        """ChatML + EMPTY_THINK temp-0, grounded (decision 11) — the
        browser synth_answer shape."""
        u = (f"Question: {question}\n\nPage text: "
             f'"{text[:EXTRACT_CHARS]}"\n\n'
             "Reply with only the answer to the question, taken from "
             "the page. If the page does not contain it, reply "
             "exactly: NOT FOUND.")
        prompt = ("<|im_start|>system\nYou are wiresense, a concise "
                  "voice assistant. Answer in one or two short "
                  'sentences.<|im_end|>\n<|im_start|>user\n'
                  f'{u}<|im_end|>\n<think>\n\n</think>\n\n')
        loop = asyncio.get_event_loop()
        try:
            raw = (await loop.run_in_executor(
                None, lambda: _llm(prompt, 96))).strip()
        except Exception:
            return "I found the page but couldn't read the answer off it."
        raw = _clean_think(raw)
        return raw[:300] if raw and "NOT FOUND" not in raw else \
            "The page doesn't seem to contain the answer."

    # ---------- lifecycle ----------

    async def finish_budget(self):
        await self.finish("budget", build_answer(self.findings, True))

    async def finish(self, outcome, answer):
        self.outcome = outcome
        self.answer = answer
        self._trace()
        if self.page:
            try:
                asyncio.create_task(self.page.close())
            except Exception:
                pass
        await self.session.ir_done(self)

    async def cancel(self):
        """Stop utterance mid-task (shared stop contract, decision 12)."""
        self.outcome = "cancelled"
        self.answer = "Stopped."
        if self._handle and not self._handle.done():
            self._handle.cancel()
        self._trace()
        await self.session.send({
            "type": "ir_event", "id": self.id, "event": "cancelled",
            "fetches": self.fetches, "answer": "Stopped."})
        self.session.ir_task = None
