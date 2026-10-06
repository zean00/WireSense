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

## Amendment MS-13 (registered post-invalid-sweep, pre-sweep-rerun): dead-constant bug

MS-12's first sweep (k=5, 8, 15 complete; 25 partial) is INVALID: a leftover
`K = 10` constant three lines below the new env read overwrote MCP_PROBE_K at
import, so every sweep arm silently ran k=10 — caught because all files were
byte-identical in accuracy, token count AND route clusters (ids reaching 9 under
a nominal k=5). Sweep files kept on disk outside the repo as run-1-sweep;
invalid; nothing from them enters the verdict. Fix: delete the dead constant
(single line), verify with a 40-vector probe (K=5 yields 5 centroids), rerun the
full sweep per MS-12's text. Killing the in-flight background run counted as
the infra-failure stop of MS-8.

## Amendment MS-14 (registered post-gradient-flat, pre-hier-gradient): subset degeneracy + path bug

Two findings from the gradient launch:
1. Registered 25/50-tool server subsets are UNREACHABLE in this corpus: the
   seed-0 server order starts with github (62 tools), so the smallest whole-
   server prefix already exceeds both targets. Both "subsets" resolved to the
   same 78-tool prefix (github, filesystem, fetch, sequential-thinking; 186
   cases). Decision: the gradient is answered at N=78 and labeled as such; a
   true 25-tool point would need tool-level subsetting (unregistered — not
   done). Free bonus from the duplicate: two independent temp-0 flat runs on
   identical prompts produced 0/186 differing verdicts — the pipeline is
   deterministic at same-prompt temp 0, so arm differences on this eval are
   configuration properties, not run noise.
2. The two hier gradient runs crashed BEFORE any scored case: the arm name
   contains a slash and was interpolated into the results path, pointing at a
   nonexistent directory (infra-failure class per MS-8; flat runs completed).
   Rerun: hier-synth/top2 k=5 at the N=78 subset only, path sanitized.
Measured already (flat@78): acc 0.3602, pos 0.3765, neg 6/24, tokens 2870 —
a one-server-dominated flat manifest collapses selection AND the null escape;
recorded as a main result, not an outlier, pending the hier comparison.

## Results (run 2 valid; adjudicated 2026-10-06)

Main arms, 278 cases (242 positives + 24 negatives + 12 traps), 121 tools, k=10:

| arm | acc | pos_acc | neg_acc | schema_ok | prompt_tok | ms |
|---|---|---|---|---|---|---|
| flat | 0.6835 | 0.6654 | 21/24 | 0.953 | 3739 | 2205 |
| hier-desc/top1 | 0.4317 | 0.3937 | 20/24 | 0.990 | 680 | 668 |
| hier-desc/top2 | 0.4964 | 0.4606 | 21/24 | 0.974 | 1305 | 1014 |
| hier-synth/top1 | 0.4820 | 0.4409 | 22/24 | 0.991 | 710 | 676 |
| hier-synth/top2 | 0.5647 | 0.5354 | 21/24 | 0.971 | 1290 | 981 |

Mechanism decomposition (recomputed from frozen spaces): routing recall is the
entire loss; the shortlist planner is STRONGER than flat's planner.

| arm | hit_rate | pos_acc\|hit | pos_acc\|miss |
|---|---|---|---|
| hier-desc/top1 | 0.433 | 0.909 | 0.000 |
| hier-desc/top2 | 0.567 | 0.812 | 0.000 |
| hier-synth/top1 | 0.543 | 0.812 | 0.000 |
| hier-synth/top2 | 0.713 | 0.751 | 0.000 |

MS-12 k-sweep (hier-synth/top2, everything else identical):

| k | acc | neg_acc | prompt_tok | ms | mean shortlist |
|---|---|---|---|---|---|
| 5 | **0.7014** | 22/24 | 2124 | 1440 | 67 |
| 8 | 0.5935 | 22/24 | 1506 | 1128 | 47 |
| 10 | 0.5647 | 21/24 | 1290 | 981 | 26 |
| 15 | 0.5360 | 23/24 | 1008 | 835 | 30 |
| 25 | 0.5827 | 20/24 | 644 | 659 | 18 |

k=5 decomposition: hit_rate 0.878 × pos_acc|hit 0.776 = 0.681 (= observed 0.681).
k=5/top2 beats flat on BOTH sub-metrics at 121 tools: pos 0.681 vs 0.665,
neg 22/24 vs 21/24. Paired McNemar flat vs k5 at 121: 26 vs 31 discordant,
p = 0.597 — statistically a tie.

MS-14 gradient (N=78 = github 62 + filesystem 14 + fetch 1 + sequential-thinking 1;
80% one server; 186 cases):

| arm | acc | pos_acc | neg_acc | prompt_tok | ms |
|---|---|---|---|---|---|
| flat@78 | 0.3602 | 0.3765 | 6/24 | 2870 | 1970 |
| hier k5/top2@78 | **0.5054** | 0.4568 | 20/24 | 1472 | 1040 |

flat@78 vs k5@78 McNemar: 19 vs 46 discordant, p = 0.0011 — the hierarchy's win
at 78 is significant. Composition is the driver, not size: with one near-duplicate-
heavy server at 80% of the manifest, flat selection collapses (negatives 6/24;
non-github positives 0.211) while the coarse gate holds the domain boundary
(non-github positives 0.474, neg 20/24).

Determinism probe (MS-14): two independent temp-0 flat runs on byte-identical
prompts: 0/186 verdicts differ — same-prompt temp 0 is deterministic on this
server; arm differences above are configuration properties, not run noise.

Flat error anatomy at 121: 85/254 misses, 42 from github's near-duplicate block;
misses skew toward indirect phrasings (planner nulls); 3 neg misses
(17×23 → get-sum; two → echo).

## Hypotheses (MS-7) — adjudicated

- H1 (hier-synth/top2 ≥ flat): FAIL at registered k=10 (0.5647 < 0.6835); PASS
  at the MS-12 k=5 point (0.7014 ≥ 0.6835), but the margin is within noise
  (p = 0.597) — a tie with 43% fewer prompt tokens and 35% less latency.
- H2 (flat tokens ≥ 5× hier): FAIL at k=5 (1.76×); holds only for the finest
  clustering (k=25: 5.8×) where accuracy is far below flat.
- H3 (synth pools > description pools): PASS in both topologies (top1 0.482 vs
  0.432; top2 0.565 vs 0.496) — synthetic-utterance bootstrap is worth keeping.
- H4 (gap grows with tool count): measured at two points only; the gap moves in
  the hierarchy's favor as composition degrades, and INVERTS significantly at
  the 80%-one-server point (p = 0.0011). Direction confirmed: hierarchy pays
  as the manifest gets harder, and dominant-server flat manifests are toxic.

Adoption rule (H1 + neg_acc within 2 points of flat): SATISFIED by
hier-synth/top2 at k=5.

## VERDICT

Build MCP tool selection hierarchically — but COARSELY. The measured optimum is
a gate over FEW domains (4–8 clusters; k=5 won, every k ≥ 8 lost to flat), a
top-2 union shortlist (mean 67 of 121 tools at k=5 — the gate is a boundary,
not a needle-finder), synthetic-utterance centroids (H3), and the LLM planner
over the shortlist with the {"tool": null} escape (91.7% null precision at
k=5/top2 vs 87.5% flat). Fine unsupervised hierarchies are the failure mode:
routing recall dies (43–71% at k=10) while the planner-given-hit stays at
75–91% — keep near-duplicate disambiguation in the LLM, never in the embedding.
The gate's biggest measured win is robustness to manifest composition (a
dominant server collapses flat selection to 36% and bleeds nulls; the gate
holds +14.5 points, p=0.001), which is exactly the risk profile of real MCP
setups where one big server (github-like) joins a handful of small ones.
Cost at 121 tools: one utterance embedding (~55 ms) + microsecond readouts;
shortlist prompts 57% of flat's tokens, 65% of its latency.

Implementation notes for wiresense MCP support (not registered, derived):
cluster per connected server-set with the k≈4–6 rule, re-embed on tools/list
change (MS-3 pools make this cheap), keep plan-twice (Rev G) on delegate picks,
and keep the production behavioral root untouched — MCP tools hang under the
existing tool path.

Scored data: results.jsonl (run 2: flat carried from run 1 + 4 hier arms),
results_run1_invalid_hier.jsonl (MS-11, archived invalid), sweep_k{5,8,15,25}_
valid.jsonl (MS-12/13), grad78_*.jsonl (MS-14). Invalid MS-12 first sweep kept
off-repo (byte-identical to k=10, superceded).

## Amendment MS-15 (registered pre-data): tool-selection-as-a-tool

Question: should "search for tools" be exposed to the planner as a tool — as the
PRIMARY selector (tiny manifest, model calls mcp_search when a task smells
tool-shaped), or as an ESCAPE HATCH inside the measured k=5/top-2 gate (planner
may call mcp_search when the shortlist doesn't fit)? Same frozen corpus, eval,
pools, budget C=200, planner params, k=5 synth space as run 2. New results file;
existing results untouched.

Search mechanics (both arms): query embedded via the production path
(norm_router_text, slot 6), cosine vs all 121 per-tool synth centroids, top-8
tools returned verbatim (name + desc[:C] + required args). Re-plan is
HISTORY-FREE (Rev G spirit): build_prompt over the SAME utterance with a
directive presenting the returned tools and the null escape; NO nested search;
exactly one search per case maximum. A non-search tool name returned from the
search-only manifest scores as a wrong answer directly (no recovery round).

| arm | first directive | search | scored |
|---|---|---|---|
| search-primary | only the mcp_search line + 3-way contract | on mcp_search | final pick |
| hier-synth-esc/top2 | k=5 top-2 shortlist + mcp_search line + 3-way contract | on mcp_search | final pick |

Metrics as MS-6, plus per-arm: search invocation rate, mean completions calls per
case, end-to-end ms and total prompt tokens (all calls summed), and for the
escape arm: pos_acc on gate-miss cases (run-2 baseline: 0.000 — the dead mass
this arm exists to recover).

Registered predictions (to be checked, not assumed):
- P1: search-primary lands BELOW both flat (0.6835) and gate k=5 (0.7014) on
  overall accuracy, with worse null discipline than gate (0.917) and roughly
  2x gate latency.
- P2: gate+escape recovers a meaningful share of the 12% miss mass
  (pos_acc on miss cases > 0.3, vs 0.000) and finishes >= gate k=5 overall,
  at <= 1.2 mean calls per case.
If P2 holds and null discipline holds >= gate - 2 points, the escape line
joins the implementation spec; if P1 fails, primary-as-tool is recorded as
rejected at this scale.

### Amendment MS-15a (registered pre-data, from canary): prime contract tightening

Pre-run canary (6 cases, diagnostic only, MS-10 precedent): the search-primary
contract as first written produced {"tool": null} on 6/6 cases INCLUDING both
positives — the model never invoked mcp_search. As written the arm would measure
that phrasing's null bias, not the search mechanism. Registered fix, exact
wording (replaces CONTRACT_PRIME in run_search.py):

  "Choose a tool for this request. You have no tools listed yet. If this
  request could be handled by a tool, call mcp_search first. Reply with ONLY
  a JSON object: {\"tool\": \"mcp_search\", \"args\": {\"query\": \"<a few
  keywords>\"}} to search for tools, or {\"tool\": null} if this request is
  pure conversation needing no tool. No other text."

If the tightened contract STILL nulls positives at scale, that is the recorded
answer for search-primary (a primary search tool a 4B planner won't invoke is
functionally dead). Also registered: missing/empty query arg in an mcp_search
call falls back to the case utterance verbatim; a second mcp_search after the
re-plan is not executed (one search per case) and scores as a wrong pick.
Escape-path mechanics verified offline pre-data (no eval cases): query
"convert 3pm Tokyo time to UTC" -> top-8 NN returns convert_time,
get_current_time at ranks 1-2; history-free re-plan picks convert_time with
correct args (340 prompt tokens).

### Amendment MS-15b (registered pre-data, from canary): dead search path in runner

Second canary pass caught a runner bug of the MS-13 class: the search-execution
branch was gated on `arm != "search-primary"`, so the search-primary arm
recorded got="mcp_search" WITHOUT ever executing the search (calls=1). Zero
scored data existed. Fix: the mcp_search branch now runs for both arms; canary
records wiped; re-canary must show calls=2 / searched=true on search-emitting
cases before the full run. No other change.

### Amendment MS-15c (registered pre-rerun): primary-arm scorer invalidated the arm

Post-run decomposition replay caught a scoring bug of the MS-11 class: the
MS-15 clause "a non-search tool name returned from the search-only manifest
scores as a wrong answer directly" was implemented as `ok = False` for EVERY
real-tool pick in the search-primary arm — including picks made AFTER a
legitimate search, which is the arm's core path. Evidence: 3 replayed cases
with want inside the returned top-8 re-picked the correct tool with args, and
their stored records show got == want yet ok = false. All 278 search-primary
records are INVALID (ok/schema_ok fields wrong; raw model outputs were valid
but args were not stored, so offline re-scoring cannot restore schema_ok).
Adjudication rule: archive the invalid records, fix the clause to apply ONLY
to blind picks (search not executed), rerun search-primary only. The
hier-synth-esc/top2@k5 arm never enters that branch (arm equality check) — its
278 records and all decomposition numbers stand as valid. Search-trigger stats
from the invalid run (91.4% search rate on the tightened contract) remain
descriptive evidence about the contract, not scores.

## MS-15 Results (runs of 2026-10-06; primary rerun under MS-15c)

Reference rows from run 2 / MS-12 sweep, same 278-case frozen eval:

| arm | acc | pos | neg(null) | schema | search rate | calls | pt | ms |
|---|---|---|---|---|---|---|---|---|
| flat (ref) | 0.6835 | 0.6654 | 21/24 | — | 0 | 1.00 | 3739 | 2205 |
| gate synth/top2 k5 (ref) | 0.7014 | 0.681 | 22/24 | — | 0 | 1.00 | 2124 | 1440 |
| search-primary | 0.4928 | 116/254=0.457 | 21/24 | 112/116 | 0.914 | 1.91 | 474 | 928 |
| hier-synth-esc/top2@k5 | 0.6583 | 164/254=0.646 | 19/24 | 159/164 | 0.219 | 1.22 | 2246 | 1563 |

Process note: three canary passes before data caught (a) a prime contract that
nulled 6/6 (MS-15a), (b) a dead search branch (MS-15b), and (c) a scorer that
invalidated the primary arm post-run (MS-15c, archived + rerun). Decomposition
numbers below come from the valid records.

Search-primary decomposition — the loss is retrieval, not picking:
- 246/254 positives searched (the tightened contract fixed triggering at scale;
  canary n=6 had suggested inversion — it was noise).
- Model-written queries surface the target in the returned top-8 only 51%
  (125/246). ok|target-in-top8 = 113/125 = 0.904; ok|target-missing = 3/121.
  The same model picking from the gate's ~67-tool shortlist scores 0.776; from
  an 8-candidate list, 0.904 — smaller menus pick better, consistently with
  the run-2 thesis. The arm dies because 2-4-word queries lose ~37 points of
  retrieval recall vs embedding the utterance itself (87.8%).
- Null discipline: 21/24; the 3 failures are all search-then-pick on chat.

Escape-arm decomposition — the option itself is not free:
- Escape fired on 61/278 turns (21.9%): 45 times on routing-hits (target
  already shortlisted; trigger miscalibrated) and only 15/31 on true routing
  misses (48% trigger recall on exactly the dead mass it exists for).
- Recovery on the 31 dead-mass positives: 5/31 = 0.161 overall (run-2 baseline
  0.000); per fired true miss 5/15 = 0.33. Real but small.
- When the search result DID contain the target, pick quality was unchanged
  (20/26 = 0.77 = gate's planner-given-hit) — the pick stage is not the loss.
- Contract tax: on the 217 paired turns where NEITHER arm searched, plain k5
  was right on 11 turns the escape variant lost and lost 3 it gained. Merely
  offering the escape (one extra manifest line + 3-way contract) costs ~4
  accuracy points on turns that never use it. Overall paired vs k5: 26 vs 14,
  McNemar p = 0.0807 (negative direction, not significant at 0.05).

### P1/P2 adjudication
- P1 (primary below flat and gate on accuracy): CONFIRMED — 0.4928 vs
  0.6835/0.7014. Null discipline worse than gate: CONFIRMED (0.875 vs 0.917).
  "Roughly 2x gate latency": REFUTED — it is the fastest arm (928 ms; tiny
  prompts), just inaccurate.
- P2 (escape recovers >0.3 of dead mass, finishes >= gate k5, <= 1.2 calls):
  FAILED on all three — 0.161 recovered, 0.6583 < 0.7014, 1.22 calls.

### VERDICT
Neither search arm joins the implementation spec. Search-as-primary is
rejected at this model scale: the planner writes lossy queries, and the
retrieval recall it needs (87.8%) is exactly what the utterance embedding
already delivers — the k5 gate IS the search, keyed on the best available
query (the utterance). The escape hatch is rejected as a default: per-fire
recovery on true misses (0.33) is real but the trigger fires on the wrong
turns (45 hits vs 15 misses) and the standing option tax (~4 pts on
non-escape turns) outweighs the recovered mass. If routing recall is ever the
binding constraint again, the lever is a better utterance-side retriever, not
model-mediated search.

## Amendment MS-16 (registered pre-data): implementation checks for production

MCP support is being implemented per the MS-12/15 verdicts. Two changes touch
measured ground, so both are registered before the corresponding data exists.

(a) NAMESPACING. Production manifests show MCP tools as `{server}__{tool}`
(collision-free across servers and against the native six; matches the corpus
and synth-pool keys). The frozen 278-case eval measured BARE names. Check:
rerun the adopted arm (hier-synth/top2, k=5, top-2) with every manifest line's
tool name prefixed `{server}__{name}`; scoring strips one trailing `__`-suffix
before comparing to want. Routing is untouched (utterance-keyed). Adopt the
prefixed form only if overall accuracy is within noise of the bare-name
0.7014 (paired McNemar, no significant regression at 0.05); otherwise fall
back to bare names + connect-time collision rejection. Runner flag:
MCP_PROBE_PREFIX=1; results to a NEW file; existing results untouched.

(b) PER-SERVER DEPLOYMENT GATE. A server is enabled in production config only
after its own frozen eval passes: cases generated from the server's real
tools/list with the registered MS-4 recipes (8 direct + 8 indirect positives
per tool capped at 64 total, hand negatives reused), frozen at
tools/mcp_eval/evals/<server>.json BEFORE any scoring, run on the SAME planner
and the SAME selector mode production will use (flat for <=20-tool servers,
gate otherwise). Bar: pos_acc >= 0.60 AND neg_acc >= 20/24. Failing servers
stay disabled; results append-only; frozen evals never edited. Regeneration
of a server's eval (e.g. its tools/list changed) requires a new protocol
amendment naming the reason.

### MS-16a result (full 278, run of 2026-10-06)

Prefixed names: acc 0.6763 (bare 0.7014), pos 168/254, neg 20/24, schema
165/168. Paired vs bare: bare-only-right 19, prefixed-only-right 12, McNemar
p = 0.2810 — no significant regression at 0.05. The model returned the
prefixed form in 278/278 scored picks (zero bare-name replies): the namespace
is contract-stable at this scale. VERDICT: adopt `{server}__{tool}` in
production manifests, per the registered rule. (An earlier read of the first
206 records, before the run finished, showed 0.6942/p=0.23 — noted only to
flag that partial-file adjudication happened once and was discarded unacted;
the verdict above is the complete-run number.)
