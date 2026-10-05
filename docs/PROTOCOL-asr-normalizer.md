# Protocol — ASR transcript normalizer ("Two am I?" class)

Pre-registered: 2026-10-05, before any normalizer gate data. Companion to
`PROTOCOL-ambient-face.md`, whose identity e2e surfaced the failure.

## Motivation (all measured, 2026-10-05)

The harness's synthesized "Who am I?" transcribes as **"Two am I?"** —
deterministically. The diagnosis chain, each step isolated:

- Not the harness's naive linear resampler: proper anti-alias resampling
  and untouched 24k audio garble identically.
- Not Kokoro nondeterminism: renders are byte-identical across runs;
  Phonon's file decode is deterministic per file.
- Not a silence threshold: bisecting leading-silence duration flips the
  outcome non-monotonically (0 ms ✗, 40 ✓, 60 ✓, 80 ✗, 100 ✗). The
  utterance sits **on Phonon's "who"~"two" decision boundary** (/h u/ vs
  /t u/ differ only in onset voicing; af_heart's fast "who" is a weak
  contrast). No acoustic-side trim is reliable against a chaotic
  boundary — that whole fix family is rejected on this measurement.
- The exposure is narrow: of the natural identity-question class
  ("Who am I?", "Who is this?", "Who's this?", "Who are you?", "Who is in
  front of the camera?") transcribed production-shaped (0.2 s leading
  pad), **exactly one member garbles**.
- Production relevance: the wake path always feeds up to 1 s of VAD
  preroll before speech, so real users asking short questions in quiet
  rooms hit exactly this shape. Also measured: "Do you know who I am?" /
  "Tell me who I am." transcribe cleanly but route vision — identity
  *content* is routing-unstable, which is why ambient context (not ASR)
  carries the identity answer; the normalizer only needs to repair the
  words.

Rejected alternatives, recorded: hotword biasing exists only on the
one-shot `fermion transcribe` CLI — the `serve` path we stream through
has no knob (upstream change), and a static per-session "who" prior would
bias every turn in an assistant whose number-heavy utterances (timers,
reminders) are load-bearing. Preroll trimming: rejected by the
non-monotonic bisect. Behavioral clarify-on-nonsense: out of scope here
(broader blast radius; own protocol if wanted).

## Registered decisions

1. **Post-ASR text repair only, always-on, no config gate.** A text
   normalizer needs no consent surface; it is inert unless the exact
   family matches, and the default e2e run guards non-regression.
2. **Single repair point**: the Phonon **final** is normalized once,
   before `respond()` — routing, history, UI and the reply all see the
   corrected text. Partials are display-only and untouched.
3. **The complete v1 rule set** (case-insensitive, full-utterance match):
   `^\s*(two|to|too|2)\s+am\s+i\s*[?!.]*\s*$` → `"Who am I?"`.
   Full-match is the safety property: "It's two a.m.", "Two am I late?",
   "wake me at two am" cannot match. No other rules. **A new rule
   requires a logged real firing to justify it** — rules are derived from
   observed traffic, not from speculation or synthetic-variant sweeps
   (our one garble is testbench speech; that bias is acknowledged and is
   why the rule ships instrumented, not extended).
4. **Instrumentation with a deletion criterion**: every firing prints a
   server log line (raw → normalized). A rule that never fires on real
   traffic is a dead rule: at the next protocol revision it is removed.
   Firing counts live in logs only — no metrics events.
5. **Known limit, stated**: only the observed family is repaired; novel
   garbles pass through untouched. The normalizer exists to repair words,
   not to make the ASR better.

## Gates

- **G1 offline** (`tools/test_normalizer.py`, `make test-normalizer`):
  positives (two/too/to/2 × case × punctuation), negatives ("It's two
  a.m.", "Two am I late?", "wake me at two am", "set a timer for 10
  minutes", a spread of real suite utterances), byte-identical
  passthrough when unmatched.
- **G2 natural-phrase e2e** (enabled run, `WIRESENSE_TEST_FACE=1`): a new
  case speaks "Who am I?" (the deterministically garbled audio) with
  ambient on, store populated, reset history — asserts the **final event
  text is "Who am I?"** (normalizer fired), route direct, reply names
  Albert, `face.ambient:true`. The robust-phrasing case
  ("Who am I, do you know?") stays as the ASR-agnostic workhorse.
- **G3 default run**: existing 12/12 unchanged (normalizer inert on all
  suite text — asserted by the run passing).
- **G4 router evals unchanged**: root ≥ 56/62, think ≥ 24/26, followup
  ≥ 6/8 and strictly > raw. The normalizer runs before the router on
  live text only; frozen evals feed their own strings, so they are
  unaffected by construction — the run confirms no drift anyway.

## Results (2026-10-05, adjudicated)

All gates pass. Runtime state after adjudication: face block removed from
config, `faces.json` deleted, server restarted — boot line "face
recognition: disabled" (the normalizer itself is always-on by decision 1;
it is inert unless the exact garble family appears).

- **G1 offline**: `make test-normalizer` — 22 checks ALL PASS (8 positives
  across the two/too/to/2 × case × punctuation family, 13 negatives
  including "It's two a.m.", "Two am I late?", "wake me at two am" and
  suite utterances byte-identical, plus the registered single-rule table
  check).
- **G3 default run**: 12/12 (normalizer loaded, zero firings on suite
  text).
- **G2 enabled run**: 15/15 **twice consecutively**. The new natural-
  phrase case — the exact deterministically-garbled audio — now reads:
  `final: 'Who am I?'` (server log: `[asr-normalizer] 'Two am I?' ->
  'Who am I?'`), route direct 0.876, reply **"You are Albert."**, metrics
  `face {n:1, names:["Albert"], ms:16.2, ambient:true}` — the full
  pipeline (repair → route → ambient probe → identity answer) on the
  phrase that started this. The robust-phrasing case still passes
  (ASR-agnostic workhorse). Firings across both enabled runs + the
  default run: exactly one per run, the garbled phrase, no false
  positives.
- **G4 router evals**: root 56/62, think 24/26, followup 6/8 > raw 4/8 —
  identical frozen bars (the intermittent coffee-beans drift case passed
  this time, consistent with its documented serving-numerics flakiness).
- Latency note (unregistered observation from the same runs): ambient
  latency pair in the second enabled run 743.1 vs 739.9 ms — normalizer
  adds nothing measurable.

**Verdict**: the who/two garble is repaired at the single registered
point, provably inert everywhere else, with the deletion criterion
instrumented (log line per firing). The rule is one line; its lifecycle
now depends on real traffic — if no one ever says the phrase, the next
revision deletes it.
