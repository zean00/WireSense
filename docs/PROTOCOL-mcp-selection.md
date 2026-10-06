# PROTOCOL: MCP tool-selection experiment (flat vs description-built hierarchy)

Status: REGISTERED 2026-10-06, pre-data. Question: before implementing MCP support,
does hierarchical tool selection built from tool descriptions beat a flat full-manifest
planner on accuracy, and at what token/latency cost — on the production 4B planner.

## Motivation

Stage 3 measured that growing the manifest 4→6 tools caused a real bleed
(weather → ir_research, caught by battery, fixed by Rev G plan-twice). MCP support
would mean 30–100+ tools from third-party servers. This experiment measures the
selection surface BEFORE any integration code exists. n_ctx = 8192 (measured via
/props at registration) means a 100-tool flat manifest cannot carry full
descriptions; the truncation budget is part of the registered design, not an
improvisation.

## Corpus (MS-1)

Real public MCP servers, runnable locally without credentials, executed once via a
minimal stdio MCP client (initialize → tools/list), their tool JSON dumped VERBATIM
to `tools/mcp_probe/corpus/<server>.json`. No hand-edited descriptions, no synthetic
tools. Target: ≥100 tools across ≥8 servers. Near-duplicate-rich servers (github,
git) included DELIBERATELY — within-domain confusion is a claim under test.

Registered server list (with named alternates if one fails to start):

| server | runner | note |
|---|---|---|
| filesystem | npx @modelcontextprotocol/server-filesystem | official |
| memory | npx @modelcontextprotocol/server-memory | official |
| sequential-thinking | npx @modelcontextprotocol/server-sequential-thinking | official |
| puppeteer | npx @modelcontextprotocol/server-puppeteer | official |
| everything | npx @modelcontextprotocol/server-everything | official test server |
| git | uvx mcp-server-git | python ref |
| time | uvx mcp-server-time --local-timezone Asia/Jakarta | python ref |
| fetch | uvx mcp-server-fetch | python ref |
| sqlite | uvx mcp-server-sqlite --db-path (empty temp db) | python ref |
| github | github/github-mcp-server release binary, stdio mode | ~40 tools, Go |

Alternates (only if a listed server cannot start): mcp-server-postgres (temp db),
archived @modelcontextprotocol/server-github (dummy token). Skipped for credentials:
slack, gdrive, brave-search. A failed server is recorded with its error; the corpus
commit records final membership and per-server tool counts.

## Arms (MS-5)

All arms share: production `build_prompt(history=[], utterance, directive)` with the
manifest swapped (MCP tools instead of TOOL_MANIFEST), production `parse_tool_json`,
temperature 0, id_slot 5, max_tokens 120 (production uses 80; raised only because
MCP arg schemas demand more required-arg emission — tool choice is unaffected),
history-free planning (Rev G rationale: the job must stand on its own words).

| arm | routing | planner sees |
|---|---|---|
| flat | none | all N tools |
| hier-desc/top1 | centroid route, argmax cluster | top-1 cluster's tools |
| hier-desc/top2 | centroid route, top-2 clusters | union of top-2 clusters' tools |
| hier-synth/top1 | synth-utterance centroid route | top-1 cluster's tools |
| hier-synth/top2 | synth-utterance centroid route | union of top-2 clusters' tools |

Every arm's directive ends with the production JSON contract, including
`{"tool": null}` escape. Hier routing embeds the utterance once (norm_router_text →
POST /v1/embeddings id_slot 6, production path) — cluster readouts are numpy matmuls.

## Context budget (MS-2)

Planner-visible per-tool fields: name + description truncated to C chars + names of
required args from inputSchema. C chosen once at runner freeze from {200, 160, 120,
100, 80} as the LARGEST value whose measured flat-arm prompt_tokens ≤ 7168 (8192
minus headroom for template + utterance + 120 output). Measured on one canary case,
recorded in the results commit BEFORE any scored run. The same per-tool truncation
applies inside hier shortlists. Embedding pools always use the FULL description
(that is the hierarchy's selling point: full info at routing, shortlist at planning).

## Routing pools (MS-3)

- hier-desc: one vector per tool = L2-normalized embedding of `"{name}: {description}"`.
- hier-synth: per-tool pool of 8 synthetic user utterances (LLM slot 5, temp 0.8,
  seed 1, recipe A: typical direct request phrasing, ≤12 words, no tool name),
  centroid = mean of L2-normalized embeddings. KNOWN BIAS, disclosed: eval
  direct-slice shares this register by construction; counterweights are the
  indirect eval slice, hand-written traps, and hand-written negatives.
- Clustering: k-means, k=10, cosine space, greedy farthest-point init seed 0,
  50 iterations, numpy only (deterministic; k fixed at corpus freeze from
  k ≈ sqrt(N_tools)).

## Eval set (MS-4)

Per tool, 2 positives (slot-5 generated, temp 0.8, seed 2, recipe B; ground truth =
tool name): B-direct (direct request; tool name not copied verbatim where unnatural)
and B-indirect (goal-oriented, tool unnamed). Plus hand-written: 24 negatives
(production-style no-tool utterances: chit-chat, math, and the domains wiresense
already owns — timer/reminder/note/detect/weather) expecting `{"tool": null}`;
12 traps (cross-server confusable but decidable). ALL cases hand-audited for
ground-truth validity BEFORE any scored run; corrections only pre-freeze; the
freeze commit is immutable thereafter. ~236 cases total.

## Metrics (MS-6)

Primary: overall accuracy = (positives with exact tool match + negatives answered
null) / all cases. Secondary: positive accuracy, negative accuracy (null precision),
args-schema-validity rate among correct picks (required fields present and typed),
mean prompt_tokens, mean wall-clock ms per decision, hier cluster-assignment accuracy.

## Hypotheses and verdict rule (MS-7)

- H1: hier-synth/top2 overall accuracy ≥ flat.
- H2: flat mean prompt_tokens ≥ 5× hier mean prompt_tokens.
- H3: hier-synth routes better than hier-desc (cluster-assignment accuracy).
- H4: the accuracy gap grows with tool count (gradient arm: flat vs best hier on
  seed-0 server subsets at ~25/50/100 tools; runs after the main verdict, same rules).

Adoption rule: recommend building MCP selection hierarchically iff H1 holds AND
hier negative-accuracy ≥ flat negative-accuracy − 2 points (hierarchy must not
buy accuracy by bleeding more on no-tool cases). Otherwise recommend flat with
truncation, and record the measured token cost.

## Adjudication clauses (MS-8)

- Single-case temp-0 serving variance: one identical-prompt rerun permitted, both
  results recorded, rerun binding (stage-3 precedent).
- Infra failure (server down, spawn fail, timeout): case rerun, logged.
- Corpus and eval are immutable after their freeze commits; rule changes require a
  new numbered decision committed BEFORE the affected data exists.
- Gate data = scored eval results. Corpus counts and canary timings are not gate data.

## Out of scope (MS-9)

Live server lifecycle (connect/disconnect/rebuild-on-change), authenticated servers,
args QUALITY (only schema-validity is scored), production integration point
(think.tool subtree vs new root class), multi-tool chains. These are
implementation-stage decisions, taken after this read.

## Runner freeze (MS-10)

Runners committed before scored runs; 6-case canary green; then one full pass over
all arms in a single session; results appended to this document with a VERDICT line;
failures recorded as failures.

## Results

(appended post-run)

## Amendment MS-11 (registered post-run-1, pre-any-hier-rerun-data): routing union bug

Run 1 (2026-10-06): canary green mechanically (budget C=200, flat prompt 3738
tokens; parse/scoring/resume all exercised), full 5-arm pass completed — but the
hier arms are INVALID. run_eval.py's shortlist union indexed the per-tool cluster
assignment with centroid indices (`np.argsort(-sims)` iterates k centroids, used
as tool indices), so hier shortlists were 2–4 arbitrary tools from the corpus head
(mean hier prompts 130–196 tokens vs an expected ~700–2500). Diagnosed from the
route records: positives answered null at ~80%, pos_acc 3.5–6.7% ≈ random over
tiny wrong shortlists. Flat arm is unaffected by the bug (no routing) and is
CARRIED OVER byte-identical from run 1 (278 records). Decision: fix the union to
`members = [i for i, a in enumerate(assign) if a in top_clusters]`; archive run-1
hier records verbatim as results_run1_invalid_hier.jsonl (committed); rerun the
four hier arms in full under the same rules. No arm definition, eval case, budget,
or metric changes.

## Amendment MS-12 (registered post-run-2, pre-sweep-data): granularity sweep + scale gradient

Run 2 decomposition (analysis, not gate data): hier loss vs flat is entirely
routing recall — hit rate 43.3–71.3% across hier arms (was the right tool in the
shortlist at all), while pos_acc|hit is 75–91%, ABOVE flat's 66.5% overall
(shortlists de-crowd the planner). pos_acc|miss = 0.000 in all arms. Cluster
granularity (k=10, fixed by MS-3 heuristic) is therefore the load-bearing design
parameter, and the verdict "hierarchy loses" could be an artifact of k.

Registered follow-ups, both pre-data:
1. k-sweep: hier-synth/top2 (best arm by overall accuracy) re-run with
   k in {5, 8, 15, 25}, everything else identical. Runner gains env MCP_PROBE_K
   (clustering k only). Question: does any k beat flat's 68.4% overall?
   Note the monotone intuition this tests: smaller k means higher recall but
   weaker de-crowding; k=1 degenerates toward flat-with-truncated-descriptions.
2. H4 gradient (registered in MS-7, now runnable): flat vs hier-synth/top2 at
   the best k from (1), on seed-0 SERVER subsets (np.random.RandomState(0)
   shuffle of server names, cumulative prefix >= 25 and >= 50 tools; k for a
   subset = round(sqrt(N_subset)), consistent with MS-3's rule). Cases whose
   want-tool is not in the subset are DROPPED for positives/traps; all 24
   negatives stay (bleed test is valid — and relatively harder — at every
   size). Runner gains env MCP_PROBE_SUBSET=<N>. Question: does the
   flat-vs-hier gap shrink or invert as the manifest shrinks?
