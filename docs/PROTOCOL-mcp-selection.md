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

### Amendment MS-16c (registered pre-scoring): per-server eval generator retry

The first gen_eval.py pass froze incomplete evals: 4/41 tools produced
unparseable generations (diagnosed verbatim: the model emits EOS mid-JSON —
finish=stop at 151 tokens with no closing brace — a seed-2 quirk on specific
tool cards; one character of description difference flips it). Zero cases
were scored. Remedy, registered before any data: gen_eval.gen retries ONCE at
seed+1 on parse failure (the same 2-attempt pattern as every other llama
call in this repo); the four incomplete freeze files are archived to
evals/_invalid_gen/ and all four servers regenerated + refrozen pre-scoring.
No other recipe change.

## Implementation (entries AT-BB, 2026-10-06)

The measured verdicts are now running code. Opt-in like face: a `"mcp"` key in
web/config.json (gitignored; web/config.example.json documents the format),
absent/disabled leaves both MCP globals None and the manifest byte-identical.

- `web/mcp_client.py` — stdio transport + lifecycle. One MCPServer per config
  entry (spawn with own process group, 2024-11-05 handshake, id-matched
  newline-JSON-RPC, tools/call -> joined text). Blocking reads run under
  to_thread; a wedged server is unblocked by killing its process group.
  Secrets: config env values must be "$ENV_NAME"; literals for
  TOKEN/KEY/SECRET/PASSWORD keys are refused at load. Dead servers: lazy
  reconnect on the next call plus a 60 s supervisor task; a deliberate
  kill() clears the respawn cooldown. Smoke: 9/9 corpus servers connect,
  8/9 tool counts exact, live reconnect mid-session verified.
- `web/mcp_gate.py` — the coarse gate, port of the frozen probe. RECIPE_A
  pools verbatim (temp 0.8 seed 1, build_prompt prefix), slot-6 embeddings
  via norm_router_text, k-means VERBATIM port (an argmin/argmax init
  divergence the equivalence check caught: 30/30 shortlists now match the
  recorded run-2 routes), top-2 union, C=200 lines, `{server}__{tool}`
  namespacing (MS-16a-adopted: 0.6763 vs 0.7014 bare, p=0.281, contract
  stable 278/278). Index cached in web/mcp_index.json keyed on a tools/list
  content hash; a 60 s watcher rebuilds when a server changes its tools.
  Flat manifest at <= 20 MCP tools (registered threshold); credential-bearing
  schemas refused at load; write-heuristic confirm flag per tool with
  per-server override.
- `web/server.py` — integration surface only: main() connects + builds the
  index (to_thread); tool_flow swaps `tool_manifest_directive()` for
  `MCP_GATE.directive_for(utterance)`; validate_tool delegates to the gate's
  inputSchema check (required args, types, caps, unknown keys dropped);
  run_tool calls tools/call (30 s default, result truncated to 300 chars,
  honest failure lines); risky MCP tools park for the spoken read-back via
  the existing pending machinery ("Just to confirm — run <name>?").
- `tools/mcp_eval/` — MS-16b deployment gate. gen_eval.py generates per-server
  frozen evals from the LIVE tools/list (RECIPE_B8 = MS-4 recipe B widened to
  the registered 8+8, seed 2, one retry at seed+1 after the diagnosed
  EOS-mid-JSON quirk, MS-16c); run_eval.py scores through the PRODUCTION gate
  over the combined server set; results append-only.

First deployment-gate run (bar: pos >= 0.60 AND neg >= 20/24):
time PASS (0.719), everything PASS (0.656), filesystem FAIL (0.422,
read/write near-duplicate confusion), git FAIL (0.531, log/show/add
disambiguation). Per registration, filesystem and git stay DISABLED. Note on
negatives: 3 of the 4 neg failures per server are the model correctly picking
native tools (timer/reminder/note are in the combined manifest); true null
discipline is 23/24 everywhere. The bar's 20/24 was set knowing this.

Live smoke (tools/mcp_smoke.py, fake WS, no audio path): direct call with
spoken confirmation, risky write parked + executed on YES, chat falls
through, mid-session server kill reconnects transparently. SMOKE PASS.

Not enabled anywhere yet: config.example defaults off. The user's live
config.json is untouched; the running web server still has pre-MCP code
until its next restart.

Hygiene note (post-BB, entry BC): the MS-16a prefixed rerun had also
overwritten tools/mcp_probe/summary.json in the working tree (summarize()
writes the default path unless MCP_PROBE_SUMMARY is set — same clobber class
as the search-arm rerun). No data or verdict was affected (adjudication reads
results_prefix.jsonl); the prefixed summary is preserved as summary_prefix.json
and summary.json restored to the committed run-2 state.

## Entry BD (registered before implementation): remote transport — Streamable HTTP, no auth

User ask: support remote MCP servers. Scope registered BEFORE code:

- Transport: Streamable HTTP (spec 2025-03-26) — POST one endpoint,
  JSON-RPC; server answers either `application/json` or an SSE stream on
  the same response; session id via the optional `Mcp-Session-Id` response
  header, echoed back on later requests; DELETE ends the session. Legacy
  2024-11-05 HTTP+SSE is NOT implemented (fallback only if a real server
  demands it).
- Auth DEFERRED (user decision): no headers/bearer/OAuth in this entry.
  Only no-auth remote servers can connect; anything requiring a token
  fails honestly at initialize. When auth lands it will reuse the
  $ENV_NAME expansion rule.
- Non-goals held: no GET SSE channel (server-initiated notifications),
  no streaming results (read the full body, then parse).

Design: `url` in a server's config selects remote mode; everything above
the transport (tools/list contract, gate, namespace, confirm policy,
supervisor/cooldown state machine) is unchanged — ZERO server.py edits
expected. Remote `kill()` = best-effort DELETE + clear session; a mid-call
404 (server restarted / session expired) gets ONE transparent re-init +
retry, then the existing honest-failure line. `alive()` = session open
and not down.

Verification (in order, all before any enablement):
1. Hand-rolled stateful streamable-HTTP server (stdlib, thread) in
   tools/mcp_http_smoke.py: initialize+session-id, tools/list, tools/call,
   one SSE-response call, session invalidation mid-session (client must
   re-init transparently), DELETE honored.
2. Regression: existing stdio smoke (tools/mcp_smoke.py) re-run must still
   pass unchanged.
3. Real-world probe: https://mcp.deepwiki.com/mcp (no auth, stateless,
   SSE-response): initialize, tools/list, one real call. Logged, not
   frozen — deployment-gate (MS-16b) applies before any real enablement.
Prediction: no gate impact of any kind (tools/list content is
transport-independent); new failure modes are network-level only.

### Entry BD results (run of 2026-10-06)

- tools/mcp_http_smoke.py (hand-rolled stateful streamable-HTTP server,
  stdlib thread, driven MCPServer-direct): SMOKE PASS 8/8 — initialize +
  session-id issue, JSON tools/call round-trip, MCP-Protocol-Version header
  after negotiate, SSE-response call parsed, isError raised as MCPError,
  mid-session 404 -> transparent re-init + retry (server saw exactly 2
  initializes), kill() -> DELETE seen, fresh restart.
- Regression: tools/mcp_smoke.py (stdio, live llama + gate) re-run
  unchanged: SMOKE PASS all four scenarios.
- Real-world probe (logged, not frozen, per registration):
  https://mcp.deepwiki.com/mcp — initialize 200 with SSE body, NO session
  id (stateless server; client handles: alive() keys on the handshake flag,
  not the id), protocol negotiated 2025-03-26; tools/list = 3 tools
  (ask_wiki_question, read_wiki_contents, read_wiki_structure);
  read_wiki_structure(fastapi/fastapi) returned 1818 chars in 0.8 s.
- Prediction held: zero server.py/mcp_gate.py edits (grep-verified — no
  references to argv/proc outside the client); gate untouched because
  tools/list content is transport-independent.

deepwiki is NOT enabled anywhere (MS-16b deployment gate still applies
before any real enablement; if it's ever enabled, its frozen eval gets
generated from the live tools/list like any stdio server). Auth remains
deferred: config has no headers field yet; a 401/403 at initialize fails
honestly with the "auth required? (deferred, entry BD)" hint.

## Entry BE (registered before any camera contact): probe onvif-mcp (user-supplied third-party server)

User ask: test the MCP implementation against github.com/qianlima365/onvif-mcp
and their ONVIF camera (creds known to the user; supplied per-call by the
probe harness, never through the model).

Source review BEFORE running (disclosed in full in the probe report):
- every tool takes host/usr/pwd AS ARGUMENTS — real voice use would push
  the camera password through the planner and (on risky tools) the spoken
  readback; conflicts with the house secrets rule. Probe passes creds in
  the harness call only.
- snap_image_to_minio uploads camera frames to MINIO_ENDPOINT and returns
  a URL via MINIO_PROXY_ENDPOINT (example defaults: third-party cloud
  ai.isstech.com) — NOT CALLED, ever.
- get_camera_list / get_camera_live hardcode a third party's creds
  (admin:qwer1234@10.17.20.110) and an external stream URL — NOT CALLED.
- handle_ptz_control physically moves the camera; focus_move likewise
  (imaging service). NOT CALLED without an explicit user ask. Note:
  handle_ptz_control does NOT match the RISKY_RE heuristic by name;
  focus_move does ("move") — recorded for the report.
- minio.py prints MINIO_SECRET_KEY to stdout at import (= the stdio
  JSON-RPC channel); only reachable via the snap tool, so moot here.
- handle_discover_devices is read-only WS-Discovery (also publishes a
  harmless dummy service on the LAN) — allowed.

Whitelist for this probe: tools/list; get_deviceInfo, get_rtsp,
handle_discover_devices (read-only, against 192.168.110.83 or LAN-wide for
discovery). Everything else refused by the probe script. Launcher:
`uv run --directory <repo> main_stdio.py` (stdio transport; --directory
keeps the client unmodified — no cwd support added for one probe).

No MS-16b deployment gate in this entry: enablement is a separate decision
given the findings; if wanted, the registered gate runs on the frozen eval
generated from the live tools/list like any other server.

### Entry BE results (run of 2026-10-06, camera 192.168.110.83)

- OUR CLIENT: clean pass. stdio connect 0.4 s, 8 tools listed, three
  whitelisted calls round-tripped, server process group killed at exit.
  First third-party stdio server beyond the probe corpus.
- THE SERVER'S stdio entry is broken: main_stdio.py re-creates
  `FastMCP('onvif-mcp')` AFTER `from main import *`, orphaning all real
  tools — it serves only a do-nothing `onvif_tool` stub. A 2-line launcher
  (/tmp/onvif-mcp/run_stdio.py: `from main import mcp;
  mcp.run(transport='stdio')`) serves the real 8; repo unmodified
  otherwise.
- CAMERA (ONVIF on :5000, confirmed by ws-discovery XAddrs; RTSP on
  :8554): get_rtsp returned 2 streams (H264 2304x1296, PROFILE_0/…) —
  fully working data. get_deviceInfo answered (valid ONVIF round trip)
  but with ALL FIELDS NULL — some cameras report empty DeviceInfo; the
  pipeline faithfully relayed the camera's answer, not a client bug.
  handle_discover_devices: 3.2 s, found the camera plus its own dummy
  published service.
- SCHEMA FINDING (the creds-as-args design, now measured): only `host` is
  required on get_deviceInfo/get_rtsp — usr/pwd carry defaults
  ('admin'/'admin'). A voice planner would pass host and credentials
  would either default wrong or have to be MODEL-FILLED; and get_rtsp
  embeds user:pass in the returned URI, which run_tool would speak aloud
  in the reply. Two leak paths through the model/TTS, as predicted.
- Probe script /tmp/probe_onvif.py (creds inline, deliberately NOT in the
  repo tree). Repo clone at /tmp/onvif-mcp (uv.lock synced).

VERDICT: implementation verified against a real camera; onvif-mcp AS-IS is
not shippable under house rules. If camera control is wanted for real: a
thin wrapper server (env-bound ONVIF_HOST/ONVIF_USER/ONVIF_PASS at spawn —
the $ENV_NAME rule; read-only tool subset; snapshot to local file, not
MinIO; PTZ behind the confirm park with per-server "confirm": true, since
"handle_ptz_control" does not match RISKY_RE by name). Wrapper would be a
separate registered entry.

## Entry BF (registered before actuation): user-authorized PTZ/focus attempt

User explicitly asked to test camera control. Sequence, gentlest first:
1. handle_ptz_control(direction="Left", speed=5) — if PTZ existed, the tool
   itself auto-stops after 3 s at 5% speed; expected per the null PTZ node
   in get_rtsp: server raises "device does not support PTZ control".
2. focus_move(speed=0.2) → wait ~2.5 s → focus_move(speed=0.0) to halt.
   DISCLOSED BUG: the tool's documented stop (speed=None) is unreachable
   through MCP — the schema type is strict float, so None fails validation;
   speed=0 (motor commanded to zero) is the only in-protocol stop. If the
   camera lacks an imaging service, OnvifClient construction fails first
   and nothing actuates.
Both calls go through the same client path as entry BE (whitelist extended
by this registration only). Every outcome recorded honestly, including
"camera moved" if it does.

### Entry BF results (run of 2026-10-06, camera 192.168.110.83)

- handle_ptz_control(Left, 5%): MCPError in 0.2 s — server raised
  "当前设备不支持云台控制" (device does not support PTZ control). No
  movement.
- focus_move(0.2 → 0.0 halt): MCPError in 0.3 s — ONVIF imaging service
  answered "The requested VideoSource does not support imaging settings".
  Motor never started; the halt leg was unnecessary.
- Error path validated end-to-end for real: FastMCP wraps tool exceptions
  as isError content; our client surfaced the device's own error text as
  MCPError within 0.3 s.

CONCLUSION: the camera is a fixed unit — no pan/tilt (no PTZ node) and no
motorized focus/zoom (imaging refused per-source). This server offers it
nothing beyond get_rtsp (working) / discovery (working) / deviceInfo
(nulls quirk); the actuation tools can never do anything on this hardware.
If streams-in-voice is the goal, the wrapper entry shrinks to: env-bound
creds + get_rtsp + local snapshot (HTTP digest fetch of GetSnapshotUri,
no MinIO), MS-16b gate before enablement.

## Entry BF2 (registered before actuation): raw ContinuousMove against the hollow PTZ stub

Capability diagnostic (read-only, direct ONVIF, bypassing the MCP server):
device DOES advertise PTZ XAddr (.../onvif/ptz_services) but GetNodes=[]
and GetConfigurations=[] and neither profile carries a PTZConfiguration —
user's Tuya hypothesis confirmed: motors are Tuya-protocol-driven, the
ONVIF layer is a stub. Also: GetDeviceInformation all-null confirmed
direct (camera quirk, not the MCP path); GetServices errors
(Missing element IncludeCapability — shim's schema sloppiness); imaging
service present but refused per-source earlier.

Now testing whether the stub is dead or merely unadvertised: raw
ptz.ContinuousMove(ProfileToken=PROFILE_0, PanTilt x=0.1 ~10% speed,
y=0, Zoom 0) for ~1.5 s, then Stop. Outcomes recorded honestly either
way; if it moves, a wrapper could drive PTZ via raw ONVIF despite the
empty node registry.

### Entry BF2 results (run of 2026-10-06)

- PTZ GetServiceCapabilities: responds (endpoint alive) — EFlip/Reverse
  False, nothing move-related enabled.
- RAW ContinuousMove(ProfileToken=PROFILE_0, PanTilt x=0.1, 1.5 s):
  SOAP Fault **"PTZ Profile Not Exist"** before any motion. The stub is
  dead, not merely unadvertised — the firmware itself refuses PTZ on a
  real video profile.

FINAL: three independent confirmations the camera's ONVIF cannot steer it
(0 nodes/0 configs; no profile binding; firmware faults on raw move).
Tuya app drives the motors via Tuya's own protocol. Voice PTZ for this
device would require the Tuya side (tinytuya local control with the
device's local key), or a firmware/ONVIF toggle if the camera's settings
expose one — not an ONVIF wrapper. Streams + snapshot wrapper remains the
only shippable ONVIF shape for this camera.

## MS-17a (registered before any generation): mid-loop gate keying — continuation text vs utterance text

Design context (session discussion, no code yet): the deep loop's per-step
decisions (need a tool? which one?) are candidates for the tree/gate
machinery as a System-1 layer. Load-bearing assumption: the gate's
shortlist, keyed on the MODEL'S CONTINUATION TEXT (what it says/thinks
right before acting) rather than the user utterance, still recalls the
right tool. This entry measures exactly that, retrieval-only — no loop
changes, no need-check classifier (that is MS-17b).

Generation (RECIPE_C, verbatim):
  'Write 8 different short things an assistant might SAY or THINK
  mid-answer, right before it should use this tool. One or two sentences
  of the assistant's own reasoning or speech that leads naturally into
  the call; never the tool name; max 24 words each. Reply with ONLY
  JSON: {"utterances": ["...", ...]}'
- 24-word cap vs RECIPE_A's 12 is deliberate: continuations are
  reasoning-shaped sentences, and length/register drift is part of what
  is being tested.
- Mechanics mirror the frozen pool gen exactly (card Name/Server/
  Description/Required args; build_prompt([], card, RECIPE_C); max_tokens
  220, temp 0.8, id_slot 5); seed 3 (unused: pools seed 1, eval seed 2);
  parse via mcp_gate.parse_json_obj, >=5 utterances else ONE retry at
  seed 4 (MS-16c rule), else empty list + recorded failure. All 121
  corpus tools. Output frozen to tools/mcp_loop/continuations.json;
  the generator refuses to overwrite.

Index: rebuilt STRICTLY from the committed synth_pools.json via the
probe's own code path (run_eval: embed+norm_router_text slot 6,
mean-of-L2, committed kmeans, MCP_PROBE_K=5 asserted) — the shipped gate
recipe, unchanged. NO continuation text enters the index (no-leak rule;
that would be an index-recipe change = MS-18, separately registered).

Scoring (retrieval-only; recall = want in top-2 cluster-union shortlist):
- Set A (baseline): the 242 frozen eval positives, utterance-keyed,
  recomputed on THIS build.
- Set B (treatment): every frozen continuation text, keyed to its tool.
- Secondary: mean shortlist size both sets; per-tool recall deltas;
  utterance-vs-continuation shortlist Jaccard per tool.

Bars (set BEFORE data): primary recall_B >= 0.80; non-inferiority
recall_B - recall_A >= -0.05. Both pass -> MS-17b (need-check readout)
is worth registering. recall_B collapses (< recall_A - 0.10) ->
continuation keying is dead; the loop keeps marker legs only. Middle
band -> candidate remedy is RECIPE_C-augmented pools, but that is an
index-recipe change: MS-18, separately registered, never a silent edit.

Files: tools/mcp_loop/{gen_cont.py, run_17a.py, continuations.json,
results_17a.jsonl, summary_17a.json}. Runners committed before data.

### MS-17a results (run of 2026-10-06)

Generation: 968 continuation texts over all 121 tools, seed 3, ZERO
retries and zero failures (the MS-16c rule never fired). Texts read as
intended — assistant-voice reasoning leading into a call ("I need to save
this summary to the notes file").

Hygiene disclosure: the scorer crashed AFTER all 1210 rows were written
(summary-only code, `jac` initializer lost in an edit); the runner now
resumes from complete rows (deterministic frozen inputs) and the
aggregation was re-run from them. Rows themselves were untouched.

Primary numbers (index rebuilt strictly from committed pools, k=5
asserted; cluster sizes [5, 46, 29, 3, 38]):
- recall_A (utterance-keyed, 242 frozen positives): 0.8884 — reproduces
  the committed ~0.88 recall; build sanity holds.
- recall_B (continuation-keyed, 968 texts): 0.8058.
- delta_pooled −0.083; delta_per_tool −0.083; mean shortlist 70.9 (A) vs
  74.4 (B) tools; utterance-vs-continuation shortlist Jaccard 0.601.

ADJUDICATION against the registered bars:
- Primary (recall_B >= 0.80): PASS, by 0.006 — statistically AT the bar
  (n=968, binomial SE ~0.013), not comfortably above it.
- Non-inferiority (delta >= −0.05): FAIL (−0.083).
- Collapse band (recall_B < recall_A − 0.10 = 0.788): NOT entered.
Registered middle band, so: the candidate remedy (RECIPE_C-augmented
pools — an index-recipe change) is NOT applied; it goes to MS-18 as a
separate registration if pursued. No silent edits.

Per-tool shape: 56 tools degrade, 19 improve, 45 flat (n_A=2/tool —
per-tool deltas are ±0.5-noisy; trust the aggregate). Worst: jargon-heavy
GitHub-class tools (simulate-research-query, get_job_logs, PR-comment
family) and convert_time/list_notifications.

Design reading for the System-1 loop: continuation keying WORKS at a
usable level — 0.81 shortlist recall is far above the model's own
retrieval (0.51 target-in-top8, MS-15) with zero generation cost, and the
0.60 Jaccard confirms shortlists genuinely differ mid-loop (re-keying on
state is necessary, not optional). The open question MS-18 would answer:
does an index built from both text kinds close the −0.08?

## MS-17b (registered before any generation): the need-check readout — {tool, continue, done} over model-state text

Design context: the System-1 loop's first classifier decides, at a round
boundary, what the model's just-produced text means: a tool is needed
(tool), reasoning is mid-flight (continue), or the answer is complete
(done). This entry measures whether the DEPLOYED tree mechanism can make
that call — before any loop wiring exists.

Classes and data:
- tool: MS-17a's frozen RECIPE_C texts (968, tools/mcp_loop/
  continuations.json) — reused verbatim, NOT regenerated.
- continue: RECIPE_D (verbatim): 'Write 8 different short things an
  assistant might say or think MID-ANSWER while reasoning from what it
  already knows, where it does NOT need any tool, lookup, or outside
  information. It is working on this tool's topic but should NOT use the
  tool or mention it. One or two sentences; never the tool name; max 24
  words each. Reply with ONLY JSON: {"utterances": ["...", ...]}'
- done: RECIPE_E (verbatim): 'Write 8 different short COMPLETE final
  answers (1-2 sentences) an assistant might give a user on this tool's
  topic, fully resolving the question from known information WITHOUT
  calling this tool or any other; never mention the tool. Max 30 words
  each. Reply with ONLY JSON: {"utterances": ["...", ...]}'
- RECIPE_D/E: per-tool conditioning over all 121 corpus tools (domain-
  matched hard negatives), card/generation mechanics identical to
  MS-17a (build_prompt, 220 max_tokens, temp 0.8, id_slot 5); continue
  seed 5, done seed 7, ONE retry at seed+1 each (MS-16c rule), >=5
  utterances per call, frozen to tools/mcp_loop/state_texts.json
  ({"continue": {...}, "done": {...}}), generator refuses overwrite.

Split rule (registered, no-leak): per key, list order as generated —
pool = texts[:n//2], eval = texts[n//2:]. Same split for the reused
RECIPE_C texts (0-3 pool / 4-7 eval).

Readout: the deployed tree mechanics VERBATIM (TreeWalker/DecideReadout):
embed norm_router_text(text) slot 6 -> arm-C head _project -> norm_rows;
per-class centroid = mean of pool vectors -> norm_rows; classify =
cosine argmax. No projection/training changes; no confidence gating
(tree-probe rule).

Eval: held-out halves only (484/class), balanced. Report confusion
matrix, per-class recall, overall accuracy. Known limitation, disclosed:
texts are synthetic approximations of the 4B model's in-loop register;
MS-19's canaries (2 live rounds per class, diagnostic-not-scored, the
MS-10 precedent) would catch register mismatch before any scored run.

Bars (set BEFORE data):
- overall accuracy >= 0.75;
- tool-class recall >= 0.80;
- false-tool rate (continue or done classified tool) <= 0.15.
All three pass -> MS-19 loop wiring is justified. tool recall passes but
false-tool fails -> candidate remedy is a two-stage readout (binary
tool/no-tool, then done/continue) = MS-17c, separately registered. tool
recall fails -> the need-check is dead; the loop keeps model-driven
markers only.

Files: tools/mcp_loop/{gen_state.py, state_texts.json, run_17b.py,
results_17b.jsonl, summary_17b.json}. Runner committed before data.

## MS-17b (results + adjudication)

Run mechanics, two crashes disclosed (neither touched scored data):
1. `NameError: time` during centroid building — the runner had a
   `__import__("time").time()` initializer but a bare `time.time()` in
   the progress print. Crash BEFORE any row was written; fixed with a
   normal import; the retry was a clean full run (no rows existed to
   resume or discard).
2. `TypeError` in summary aggregation — `sum()` over a generator of
   `dict_values` objects. Crash AFTER all 1448 rows were written and
   flushed; fixed; the retry took the registered resume path
   (aggregate-only, row count validated == 1448, no re-embedding).

Results (results_17b.jsonl, 1448 held-out rows; summary_17b.json):

```
              pred tool  cont  done | recall
true tool          252   129   103 |  0.5207
true continue      123   244   113 |  0.5083
true done           71    83   330 |  0.6818
overall accuracy 0.5704    false-tool rate 0.2012
bars: overall>=0.75 FAIL, tool-recall>=0.80 FAIL, false-tool<=0.15 FAIL
```

Adjudication (registered fork, applied verbatim): tool-class recall
fails -> the need-check readout is DEAD. No two-stage rescue is
registered (that fork required tool recall to pass). The deep_turn loop
keeps its model-driven markers (IR_TOOL / BROWSE_TOOL) as the sole
loop-continuation signal; no centroid readout at round boundaries.

Reading (diagnostic, not scored): the confusion is symmetric
tool<->continue (~25% each way) and above chance (0.57 vs 0.33) — the
arm-C head space encodes topic/intent, not "does this text need a
tool"; synthetic state text of all three classes projects into
overlapping regions. This is a structural miss, not a marginal one:
any rescue would be a TRAINED readout head (the arm-C precedent —
supervised, labeled rounds), a different mechanism class with its own
registration, not another centroid/pool recipe. Not justified by
current evidence.

Consequence for the System-1 loop design: what survives measurement is
entry-point tool selection (MS-17a: utterance-keyed readout 0.89/0.81 —
already the shipped gate mechanism). Mid-loop system-1 gating is
rejected by MS-17b. MS-18 (RECIPE_C-augmented pools) remains open as a
selector-quality fork only; it does not rescue this verdict.

Housekeeping: `snapshot.json` (tinytuya device dump from the BF2
diagnostics) sits untracked at the repo root; left alone deliberately —
it may hold device local keys and must not be committed.

## MS-18 / MS-19 closure (decision recorded, no data generated)

- **MS-19 (System-1 loop wiring: need-check readout at round boundaries
  + mid-loop tool selection re-keyed on continuation text): CLOSED —
  premise dead.** MS-17b failed its registered bars (tool recall 0.5207
  vs 0.80); the registered fork applied verbatim: the deep_turn loop
  keeps its model-driven markers (IR_TOOL / BROWSE_TOOL) as the sole
  continuation signal. There is no readout left to wire. Resurrection
  path, for the record: a TRAINED round-classification head (the arm-C
  precedent — supervised over labeled live rounds), which is a
  different mechanism class and would need its own registration from
  scratch. Not justified by current evidence.
- **MS-18 (RECIPE_C-augmented pools index): MOOT — its consumer is
  gone.** Its sole purpose was closing the utterance->continuation
  recall gap (0.888 -> 0.806, Jaccard 0.60) for continuation-keyed
  queries against the gate index. The only component that ever issued
  continuation queries was MS-19's mid-loop selector; with MS-19
  closed, no runtime path queries the gate with model-state text.
  Entry selection runs on utterances (recall 0.8884, shipped mechanism)
  and needs no augmentation. Resurrection path: any future design that
  feeds model-state text to the gate index.

The frozen MS-17a/17b data (continuations.json, state_texts.json,
results, summaries) stays committed and is reusable by either
resurrection path without regeneration.

## MS-16b failure autopsy (post-hoc analysis of frozen data — no new eval data)

Question: filesystem (pos 0.422) and git (pos 0.531) fail the deployment
gate; time (0.719) and everything (0.656) pass. WHY — gate routing or
planner selection?

Method (read-only replay): spawned the same four servers, loaded the
index from the hash-matched cache (b6ac5cf5 — built during the scored
run; "INDEX SOURCE: hash" confirmed identical centroids/assignment), so
`shortlist(utterance)` reproduces the exact manifest the planner saw.
Recomputed shortlists for all 224 pos cases, joined with the frozen
results rows. Replay artifacts in /tmp (replay_gate.py, replay_16b.jsonl,
fail_dump.txt) — diagnostic only, matching the probe precedent; the
committed record is this entry.

Routing recall (want ∈ top-2-union shortlist): filesystem 62/64, time
32/32, everything 59/64, git 56/64 — 210/224 overall (93.8%).

Failure decomposition (mechanical, from the join):
- filesystem: 37 fails = 35 selection + 2 routing; 16 nulls (14 routed)
- git: 30 fails = 22 selection + 8 routing; 16 nulls (8 routed)
- time: 9 fails = 9 selection + 0 routing; 5 nulls (5 routed)
- everything: 22 fails = 17 selection + 5 routing; 12 nulls (8 routed)

Finding 1 — the gate is exonerated. When a case fails, the wanted tool
was ON the planner's manifest 79/94 times; routing misses are 15. The
MS-12/MS-15 selector delivers at deployment scale. What fails is the
4B planner's pick among visible siblings (or its null escape).

Finding 2 — most selection "failures" are eval artifacts (qualitative
read of all 94, my post-hoc judgment, NOT a scored measurement):
- Deprecated wants: filesystem read_file says "DEPRECATED: Use
  read_text_file instead" — 3 frozen wants; an ideal planner preferring
  the canonical tool scores 0 on all three.
- Defensible siblings: "Show me the commit history here." (want
  git_show, model's git_log at least as correct); "List everything in
  the Music folder." (want list_directory_with_sizes, plain
  list_directory answers the user); "Can you remind me when it's 5 PM?"
  (want time get_current_time, the model's native `reminder` pick is
  the right assistant behavior); "Execute the research query now."
  (want everything__simulate-research-query, the model picked the REAL
  native ir_research).
- Utterances without the discriminator: the show/diff/log triangle
  ("What changes are in this commit?"), staged vs unstaged idioms.
- Arg-grounding nulls: the eval is history-free, but utterances like
  "read my notes file" carry no path; the production null contract
  fires, and in production that is a clarifying turn, not a failure.
- Demo-tool wants (everything server): get-annotated-message,
  toggle-subscriber-updates, get-resource-reference are MCP demo
  fixtures with demo descriptions.

Adjudicated tally: ~75/94 failures eval-borne or defensible; ~19
genuine planner failures, concentrated in the filesystem edit/write
family (8 of fs's 16 nulls are edit/write utterances — the model
under-triggers on mutations), git jargon ("stage" 5/5 -> git_status,
"check out" -> git_branch), and verbatim-request nulls (convert_time,
directory_tree). Under a hypothetical perfect planner (only the ~19
fixed), filesystem would score 34/64 (0.53 — STILL failing), git
44/64 (0.69 — passing), time 24/32, everything 44/64. The filesystem
failure is substantially eval-borne: its frozen set wants a deprecated
alias and ambiguous listing variants.

Schema discipline where the planner did pick: 100% (scored run's
schema_ok).

## MS-16b-2 (registered before any generation): eval-recipe amendment B9

Scope: change the EVAL GENERATION only. No gate code, no planner, no
model, no description rewriting (desc rewriting is treatment — its own
registration if ever). Bars and adjudication rule unchanged:
pos >= 0.60 AND neg >= 20/24; PASS enables the server per the original
MS-16b rule.

Amendments (both registered here, before any new data):
1. RECIPE_B9 (verbatim) amends B8 with self-containment + uniqueness:
   'Write 8 short things a user might say to a voice assistant for this
   tool. "directs": plain direct requests for what the tool does, do
   not copy the tool name, max 14 words each. "indirects": goal-oriented
   phrasings where the tool is implied but never named, max 16 words
   each. Two rules: every utterance must be self-contained — include
   the concrete details the tool needs (file or folder names, content
   to write) so nothing needs a follow-up question; and phrase it so
   THIS tool is the only reasonable choice among similar tools —
   include what makes it distinct (sizes, recursion, staging,
   conversion, whatever separates it). Reply with ONLY JSON:
   {"directs": ["...", ...], "indirects": ["...", ...]}'
2. Tool-level exclusion (mechanical, desc-based): a tool whose
   description contains "DEPRECATED" is excluded from eval generation
   (an alias of a canonical sibling; a case can only invite the wrong
   pick). Applies to filesystem read_file. Demo/simulation tools are
   NOT excluded — the everything server's content is what it is.

Mechanics unchanged: one call per tool, build_prompt card format, temp
0.8, max_tokens 560, seed 2 with one retry at seed+1 (MS-16c), id_slot
5; frozen to evals/<server>.b9.json BEFORE any scoring, refuses
overwrite, pos stride-sample cap 64, same 24 hand negatives verbatim.
Runner: eval/results/verdict paths take the suffix from MCP_EVAL_SUFFIX
(default empty = B8 files, byte-identical behavior). Scoring through
the production gate over the COMBINED four-server manifest (the B8 run
was combined — cross-server picks in the frozen rows confirm it; B9
must match). All four servers regenerate: a recipe change re-freezes
the eval set, and the two passing servers must face the same recipe
for like-for-like comparison. The B8 frozen evals and results stay
committed and untouched.

Files: gen_eval.py + run_eval.py amendments committed before data;
evals/<server>.b9.json; results/<server>.b9.{jsonl,verdict}.

## MS-16b-2 (results + adjudication)

One launch hiccup, disclosed: the first background launch passed
MCP_EVAL_SUFFIX=b9 (no dot), giving evals/filesystemb9.json ->
FileNotFoundError. No cases were scored; relaunched with MCP_EVAL_SUFFIX=.b9.
Runner code unchanged (registered contract: suffix splices into
{name}{SUFFIX}.json; default empty = B8 paths).

Results (320 cases = 224 pos + 96 neg; combined four-server manifest;
"index from cache (hash b6ac5cf5)" — the SAME index the B8 run scored
against; no pool regeneration):

```
server      B8 pos   B9 pos   neg     schema    verdict
filesystem  0.4219   0.6406   20/24   41/41     FAIL -> PASS
time        0.7188   0.7500   20/24   24/24     PASS
everything  0.6562   0.4375   20/24   28/28     PASS -> FAIL (flipped)
git         0.5312   0.6406   20/24   37/41     FAIL -> PASS
```

The autopsy's prediction held: with self-contained, uniquely-resolved
utterances and the deprecated alias excluded — same gate, same planner,
same index, zero code change — filesystem (+0.22) and git (+0.11) clear
the registered bar. The B8 failures were substantially eval-borne.

The everything flip is a true measurement, not a recipe artifact. Its
tools are MCP SDK demo fixtures (echo / annotated-message / resource
links / long-op trigger / simulated research); under the uniqueness
rule the frozen cases demand demo-fixture precision, and the model
repeatedly does the CORRECT assistant thing: it picks the real native
research tool over simulate-research-query (4x) and refuses demo-fixture
utterances (trigger-long-running-operation 6x null). Demo tools do not
map to distinct user intents; the B8 pass was vagueness dressed as
coverage. everything stays gated off — correct for a server that exists
to exercise protocol features, not serve users.

Residual concentrations (recorded for the future, all within PASSing
servers): git git_commit -> native note (3x) and the show/log/diff
triangle persisting despite uniqueness phrasing (the three descriptions
genuinely overlap); git 4 schema misses (right tool, required arg
absent — 37/41); filesystem edit_file -> read_text_file (2x);
time get_current_time -> null / ir_research / convert_time ("what time
is it in X" vs conversion ambiguity persists).

Deployment state per the registered rule (PASS enables): filesystem,
git, and time are CLEARED FOR ENABLEMENT; everything remains disabled.
The user's live config.json is untouched — flipping the enable switch
is the user's call. When enabled with <= 20 MCP tools total the gate
runs flat; the four-server set (41 tools) runs through the cached index.

Files: tools/mcp_eval/results/{filesystem,time,everything,git}.b9.{jsonl,verdict}.

## INCIDENT (2026-10-07): the gate ground flaps — unpinned npx/uvx spawns

During the memory-feature deployment gate (PROTOCOL-memory.md B4a) the
filesystem re-run FAILED (pos 0.5625, bar 0.60) against the registered
0.6406 PASS, while git (0.7031) and time (0.7500) re-ran IDENTICAL to
registration — four verdict rows each, byte-stable. The flapping is in
the ground, not any code change:

- The gate cache is keyed on a content hash of the spawned tools/list
  (registered ground: 473e375b). During investigation, back-to-back
  spawn sets produced the cached hash on one run and a fresh rebuild on
  the next; an instrumented replication produced yet another payload
  hash. Fresh rebuilds are deterministic per tool set (rerun3 ≡ rerun4
  byte-identical, 88+64+88 rows), so a rebuild itself proves the spawned
  tool set differed from the cached one.
- npm and PyPI show no new releases (filesystem 2026.8.31, time/git
  2026.8.18), so the flap is between locally cached package versions and
  the registry's current resolution: `npx -y` / `uvx` re-resolve against
  registry metadata whose local TTLs expire, then swing back.
- On the new (freshly-resolved) ground filesystem not only loses 5 pos
  cases, it gains schema failures — the newly-resolved server version
  changed tool schemas. The registered 0.6406 was measured on the older
  cached ground and does not transfer.

Consequence for the registered policy (PASS enables, verdicts are
version-scoped): filesystem is currently enabled on a ground where its
deployment gate FAILS. Per policy it should be re-cleared or parked;
disabling it is a config edit — the user's call.

Resolution path (recommended, not yet applied — web/config.json is the
user's live file): pin each MCP server version in the spawn command
(e.g. `npx -y @modelcontextprotocol/server-filesystem@<ver>` /
`uvx mcp-server-time==<ver>`), then re-run the three frozen gates on the
pinned ground as the new registered verdicts. Pinning makes the cached
index and every future spawn agree by construction, which is what the
cache hash assumed all along.
