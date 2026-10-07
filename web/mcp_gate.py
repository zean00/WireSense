"""The measured coarse gate for MCP tool selection (MS-12/MS-15/MS-16).

What the experiment adopted, and nothing else: per-tool synthetic-utterance
pools (RECIPE_A verbatim, temp 0.8 seed 1), mean-of-L2 centroids, cosine
k-means (k=5, farthest-point init, Lloyd 50 — deterministic), top-2 cluster
union shortlist, planner picks from the shortlist + the native tools, null
escape. No search tool: MS-15 measured both search arms worse and an offered
option alone costs ~4 points on turns that never use it.

Flat manifests take over at <= MCP_FLAT_MAX tools (measured fine there);
the gate exists for the 20..150+ regime where flat loses (and collapses
entirely when one server dominates).

Index is cached in mcp_index.json keyed on a content hash of every server's
tools/list: an unchanged hash loads pools/centroids without a single
generation or embedding call. A changed hash (server updated its tools)
rebuilds — pools are treatment, not eval, so they are never hand-audited.
"""
import hashlib
import json
import os
import re
import time

import numpy as np
import requests

MCP_FLAT_MAX = 20     # registered: flat is fine up to here
MCP_K = 5             # registered: k-sweep optimum at 121 tools
MCP_TOPN = 2          # registered: top-2 union shortlist
DESC_CAP = 200        # MS-2 budget C
RECIPE_A = ('Write 8 different short things a user might SAY to a voice '
            'assistant that should trigger this tool. Casual, varied everyday '
            'phrasing, never use the tool name in the text, max 12 words each. '
            'Reply with ONLY JSON: {"utterances": ["...", ...]}')
CONTRACT = ('Reply with ONLY a JSON object: {"tool": "<name>", "args": {...}} '
            'or {"tool": null} if no tool fits. No other text.')
# v1 risk heuristic: a name that implies mutation or navigation parks the
# call for a spoken read-back; a server's "confirm" config overrides.
RISKY_RE = re.compile(
    r"create|write|edit|update|delete|remove|rename|move|commit|push|merge|"
    r"close|fork|submit|navigate|click|fill|subscribe|unsubscribe|trigger|"
    r"send|post|put|patch|cancel", re.I)
SECRET_FIELD_RE = re.compile(r"password|secret|token|credential|api[_-]?key",
                             re.I)


def l2(v):
    return v / (np.linalg.norm(v) + 1e-12)


def parse_json_obj(text):
    """Raw JSON extraction — NOT server.parse_tool_json (that one requires
    a "tool" key; generation prompts ask for other shapes)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def kmeans(vecs, k=MCP_K, iters=50):
    """Verbatim port of the registered probe k-means (run_eval.kmeans):
    greedy farthest-point init — first centroid = argmin cosine to the
    global mean, then max-min-distance picks; Lloyd with cosine assignment
    and renormalized means."""
    V = np.stack([l2(v) for v in vecs])
    mean = l2(V.mean(0))
    first = int(np.argmin(V @ mean))
    idx = [first]
    d = 1.0 - V @ V[first]
    for _ in range(k - 1):
        nxt = int(np.argmax(d))
        idx.append(nxt)
        d = np.minimum(d, 1.0 - V @ V[nxt])
    cents = V[idx].copy()
    assign = None
    for _ in range(iters):
        new_assign = np.argmax(V @ cents.T, axis=1)
        if assign is not None and np.array_equal(new_assign, assign):
            break
        assign = new_assign
        for c in range(k):
            m = assign == c
            if m.any():
                cents[c] = l2(V[m].mean(0))
    return cents, assign


class Gate:
    """Shortlist selector over the connected MCP servers' tools."""

    def __init__(self, llama, norm_text, native_lines, servers,
                 cache_path=None, build_prompt=None):
        self.llama = llama
        self.norm_text = norm_text      # production norm_router_text
        self.native_lines = native_lines  # native manifest lines, verbatim
        self.servers = [s for s in servers if s.tools]
        self.cache_path = cache_path
        self.build_prompt = build_prompt  # production ChatML prefix for pools
        self.tools = []                 # normalized entries, stable order
        self.by_name = {}               # prefixed -> entry
        self.cents = None
        self.assign = None

    # --- tool registry ----------------------------------------------------

    def load_tools(self):
        """Normalize tools/list entries; refuses secret-bearing schemas
        outright (they never reach the manifest or validate_tool)."""
        self.tools, self.by_name = [], {}
        for s in self.servers:
            for t in s.tools:
                schema = t.get("inputSchema") or {}
                props = schema.get("properties") or {}
                req = [p for p in (schema.get("required") or []) if p in props]
                if any(SECRET_FIELD_RE.search(str(p)) for p in props):
                    print(f"[mcp] {s.name}.{t['name']}: skipped "
                          "(credential-bearing schema)", flush=True)
                    continue
                e = {"name": t["name"], "prefixed": f"{s.name}__{t['name']}",
                     "desc": t.get("description") or "", "server": s,
                     "schema": schema, "required": req,
                     "confirm": s.confirm if s.confirm is not None
                     else bool(RISKY_RE.search(t["name"]))}
                self.tools.append(e)
                self.by_name[e["prefixed"]] = e

    def active(self):
        return len(self.tools) > MCP_FLAT_MAX

    def risk(self, entry):
        return entry["confirm"]

    def validate_args(self, entry, args):
        """Required-args + type check against inputSchema, unknown keys
        dropped, strings capped — the MCP counterpart of the native
        validators. Raises ValueError on any violation."""
        props = entry["schema"].get("properties") or {}
        out = {}
        # a server pinned with --repository owns its repo: a spoken request
        # ("show the git status") cannot carry the path (the planner emits
        # "." or nothing), so default the arg from the pin instead of
        # failing validation — a RELATIVE repo_path is meaningless for a
        # stdio server spawned from an arbitrary cwd (capability suite,
        # 2026-10-07; this mcp-server-git build requires repo_path even
        # when pinned)
        if "repo_path" in entry["required"] \
                and getattr(entry["server"], "repository", None):
            given = (args or {}).get("repo_path")
            if not given or not os.path.isabs(str(given)):
                args = {**(args or {}), "repo_path": entry["server"].repository}
        for r in entry["required"]:
            if r not in args or args[r] is None or args[r] == "":
                raise ValueError(f"{entry['prefixed']} needs {r}")
        for k, v in (args or {}).items():
            if k not in props:
                continue  # unknown keys never reach the server
            t = props[k].get("type")
            if t == "string" or (t is None and isinstance(v, str)):
                out[k] = str(v)[:500]
            elif t in ("number", "integer"):
                try:
                    out[k] = int(v) if t == "integer" else float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{entry['prefixed']}.{k} must be {t}")
            elif t == "boolean":
                if not isinstance(v, bool):
                    raise ValueError(f"{entry['prefixed']}.{k} must be boolean")
                out[k] = v
            elif t == "array":
                if not isinstance(v, list):
                    raise ValueError(f"{entry['prefixed']}.{k} must be an array")
                out[k] = [str(x)[:200] for x in v][:20]
            else:
                out[k] = v if isinstance(v, (str, int, float, bool)) \
                    else str(v)[:200]
        return out

    # --- embeddings / pools ------------------------------------------------

    def embed(self, text):
        # the frozen probe embed: norm_router_text + slot 6 + model tag
        r = requests.post(f"{self.llama}/v1/embeddings", json={
            "input": self.norm_text(text), "model": "wiresense-llm",
            "id_slot": 6}, timeout=60)
        r.raise_for_status()
        return np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)

    def _gen_pool(self, e):
        card = (f'Tool "{e["name"]}" from server "{e["server"].name}".\n'
                f'Description: {e["desc"]}\n'
                f'Required args: {", ".join(e["required"]) if e["required"] else "none"}')
        prompt = self.build_prompt([], card, RECIPE_A)
        r = requests.post(f"{self.llama}/v1/completions", json={
            "prompt": prompt, "max_tokens": 220, "temperature": 0.8,
            "seed": 1, "id_slot": 5}, timeout=120)
        r.raise_for_status()
        j = parse_json_obj(r.json()["choices"][0]["text"])
        if j and isinstance(j.get("utterances"), list) \
                and len(j["utterances"]) >= 5:
            return [str(u) for u in j["utterances"]][:8]
        return None

    def _signature(self):
        blob = json.dumps(
            [{s.name: [{"name": t["name"], "desc": t.get("description") or "",
                        "schema": t.get("inputSchema") or {}}
                       for t in s.tools]} for s in self.servers],
            sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()

    # --- index ---------------------------------------------------------------

    def build_index(self, log_every=25):
        """Pools + centroids, cached by tools/list hash. Returns the cache
        source: 'hash' (nothing regenerated), 'cache-file' or 'fresh'."""
        self.load_tools()
        if not self.active():
            print(f"[mcp] gate inactive ({len(self.tools)} tools <= "
                  f"{MCP_FLAT_MAX}): flat manifest", flush=True)
            return "inactive"
        sig = self._signature()
        if os.path.exists(self.cache_path or ""):
            try:
                c = json.load(open(self.cache_path))
                if c.get("hash") == sig and c.get("k") == MCP_K \
                        and len(c.get("assign", [])) == len(self.tools):
                    self.cents = np.array(c["cents"], dtype=np.float32)
                    self.assign = np.array(c["assign"])
                    print(f"[mcp] index from cache (hash {sig[:8]})",
                          flush=True)
                    return "hash"
            except (OSError, ValueError, KeyError):
                pass
        # regenerate pools unless the cache file still holds this tool set
        pools = None
        if os.path.exists(self.cache_path or ""):
            try:
                c = json.load(open(self.cache_path))
                if c.get("hash") == sig:
                    pools = c["pools"]
            except (OSError, ValueError, KeyError):
                pass
        src = "fresh"
        if pools is None:
            pools, t0 = {}, time.time()
            for i, e in enumerate(self.tools):
                p = self._gen_pool(e)
                if p is None:
                    print(f"[mcp] pool generation failed: {e['prefixed']}; "
                          "falling back to description embedding", flush=True)
                    p = [e["desc"] or e["name"]]
                pools[e["prefixed"]] = p
                if (i + 1) % log_every == 0:
                    print(f"[mcp] pools {i+1}/{len(self.tools)} "
                          f"({time.time()-t0:.0f}s)", flush=True)
        t0 = time.time()
        svecs = [l2(np.stack([l2(self.embed(u))
                              for u in pools[e["prefixed"]]]).mean(0))
                 for e in self.tools]
        self.cents, self.assign = kmeans(svecs, k=MCP_K)
        if self.cache_path:
            json.dump({"hash": sig, "k": MCP_K, "pools": pools,
                       "cents": self.cents.tolist(),
                       "assign": self.assign.tolist()},
                      open(self.cache_path, "w"))
        print(f"[mcp] index built ({src}) in {time.time()-t0:.0f}s, "
              f"sizes={np.bincount(self.assign, minlength=MCP_K).tolist()}",
              flush=True)
        return src

    def index_stale(self):
        return self.active() and self._signature() != self._cached_hash()

    def _cached_hash(self):
        try:
            return json.load(open(self.cache_path)).get("hash")
        except (OSError, ValueError):
            return None

    # --- selection ---------------------------------------------------------

    def shortlist(self, utterance):
        """MS-11 form: union of the top-N clusters' TOOLS (cluster ids,
        then all members of those clusters)."""
        v = l2(self.embed(utterance))
        order = np.argsort(-(self.cents @ v))[:MCP_TOPN]
        top = set(int(x) for x in order)
        members = [e for e, a in zip(self.tools, self.assign) if a in top]
        return members, [int(x) for x in order]

    def _line(self, e):
        args = ", ".join(e["required"]) if e["required"] else "none"
        return f"- {e['prefixed']}: {e['desc'][:DESC_CAP]}; args: {args}"

    def directive_for(self, utterance):
        """The tool_manifest_directive replacement: shortlist + native
        tools, production contract, no extra options (MS-15 option tax).
        Falls back to the flat MCP manifest when the index is absent."""
        if self.active() and self.cents is not None:
            short, _ = self.shortlist(utterance)
            lines = [self._line(e) for e in short]
        else:
            lines = [self._line(e) for e in self.tools]
        return ("Choose a tool for this request. Available tools:\n"
                + "\n".join(lines + self.native_lines) + "\n" + CONTRACT)
