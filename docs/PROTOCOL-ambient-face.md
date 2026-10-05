# Protocol — Ambient face context (frame at speech onset)

Pre-registered: 2026-10-05, before any ambient run data. Extends
`PROTOCOL-face-recognition.md` (frozen components, threshold, store) — read
that first; nothing there changes.

## Motivation

Identity questions asked as plain speech ("Who am I?", "Who is this?") route
**direct** (measured 0.876 / 0.792 at the root). Direct turns never grab a
frame, so recognition never runs and the model answers from conversation
history — or honestly not at all in a fresh session. This is the registered
v1 limitation of the face protocol. Both router attempts to fix it by refs
failed on measurement (content beats cues; protocol Rev 1).

The alternative is to remove the routing dependence: grab one frame the
moment the user starts talking, run recognition while the user is still
speaking, and let the `(Camera: ...)` line ride **every** turn. The frame
grab and the 25–49 ms recognition hide entirely behind the 2–5 s utterance;
no router change, no re-record, frozen evals untouched.

## Scope

**v1 is face-only.** No ambient object lists: OmDet is a *prompted* detector
(1–12 requested labels, `run_detect`) with no "list everything" mode, and its
latency under live CPU load (sharing with llama decode) is unmeasured. A
vocabulary-based object probe is a separate pre-registration if wanted.

## Registered decisions

1. **Triple gate.** Ambient context runs only when ALL hold:
   (a) config `face.enabled` is true, (b) config `face.ambient` is true
   (new key, default **false** — absent config = ambient code never runs,
   byte-identical to pre-ambient), (c) the per-session user toggle is ON
   (UI button → `{"type":"ambient_state","on":b}`; the server clamps it to
   the config gates and refuses otherwise). The camera being on is enforced
   by `request_frame` returning None, not by a fourth check.
2. **Probe at VAD wake.** One frame is requested the moment the Silero latch
   (3×100 ms speech) opens the ASR stream — idle→speech only. Fired as a
   background task; best-effort: any failure means *no line*, never a broken
   turn. Every utterance gets a fresh wake (the ASR stream closes per turn),
   so ambient coverage is per-utterance, not continuous video.
3. **Unconditional server-side lookup.** If the frame yields ≥1 face and the
   store is non-empty, `faces.recognize` runs — the LLM never decides to
   trigger it. Recognition stays non-tool; the model only reads the result.
4. **Zero new model code.** Same frozen YuNet+SFace, same 0.50 threshold,
   same `faces.json`, same detect/embed/recognize code paths as the face
   protocol. The only new logic is the trigger point and the injection.
5. **All-route volatile-suffix injection; raw history.** On non-vision
   actions the line `(Camera: <names>)` prepends the utterance in the prompt
   (volatile suffix — the frozen KV prefix of system+history is untouched).
   **History stores the raw utterance**; the camera line is per-turn context,
   never persisted into conversation memory. Vision turns keep their own
   fresh recognition on their own frame and get no ambient injection (no
   double line).
6. **Placeholder names ride along.** An unenrolled face contributes
   "an unrecognized person" to the line — "Who is this?" gets an honest
   answer, same as the vision path today.
7. **Staleness accepted.** The frame is grabbed at speech onset and may be
   1–3 s old by answer time. Identity-only scope makes this immaterial; this
   is not a "what am I holding" feature.
8. **Metrics.** Ambient-context turns carry `face: {n, names, ms,
   ambient: true}`; vision turns unchanged (`face: {n, names, ms}`); turns
   without context carry no face key.
9. **Privacy posture, registered explicitly.** With the toggle on, the
   assistant "knows who is present" on *every* turn regardless of topic —
   that is the feature. Mitigations: the three gates in (1); the toggle is
   per-session (server state resets on disconnect; localStorage restore is
   client convenience only); nothing new is stored (embeddings only,
   unchanged); nothing leaves the machine.
10. **Toggle UX.** On `hello` the server advertises
    `{"type":"capabilities","face":b,"ambient":b}`; the UI shows the ambient
    control only when `capabilities.ambient`. Unknown-type tolerance for
    older clients is inherent (WS JSON events).
11. **Race policy.** If the probe result is not ready when `respond` runs
    (pathologically short utterance), the turn gets no line — honest absence,
    never a wait. A probe result that lands after its turn's metrics is
    dropped at the next metrics reset; the worst case is a one-turn-old
    identity line naming the same person, accepted.

## Gates

- **G1 offline units** (`tools/test_face.py`): `ambient_state` gating refuses
  when either config key is off; camera-line format helper (shared with the
  vision path); injection skips vision-action turns.
- **G2 default-off contract**: the default e2e run (face absent from config,
  `WIRESENSE_TEST_FACE` unset) still passes the existing 12/12; capabilities
  reports `face:false, ambient:false`.
- **G3 enabled e2e** (`face.enabled`+`face.ambient`, `WIRESENSE_TEST_FACE=1`),
  new cases, each after a history reset (fresh-conversation scenario):
  (a) toggle on + camera on + enrolled: "Who am I?" routes **direct**, reply
  names Albert, metrics `face.ambient:true, names:["albert"]`;
  (b) toggle-off control (camera on, frames available): reply must NOT name
  Albert, no face metrics;
  (c) camera-off control (toggle on): no face metrics, honest reply;
  (d) tool turn with ambient on: timer still fires (injection does not break
  tool selection);
  (e) vision turn with ambient on: fresh recognition still yields
  `names:["albert"]` (no regression, no double injection observable).
- **G4 latency paired**: the same direct utterance ("What is the capital of
  France?") once with ambient off, once with ambient on, same run;
  ambient-on `e2e_ms` within noise (bar: ambient-on median ≤ ambient-off
  median + 15%). The probe must never add to e2e — it completes behind
  speech or doesn't finish (honest absence per decision 11).
- **G5 ms budget**: `face.ms` on ambient turns < 50 ms (same bar as the face
  protocol G9).
- **G6 router evals unchanged**: ambient touches no routing code; confirm no
  drift — root ≥ 56/62, think ≥ 24/26, followup ≥ 6/8 and strictly > raw
  (the face-protocol G7 numbers).
- **G7 history invariant (soft)**: after an ambient turn, asking the model
  what was just said must quote the raw utterance, not a "(Camera:" line.
  Checked as a WARN in the harness (LLM phrasing varies), not a hard bar.

## Out of scope (registered)

Continuous frames during speech; ambient object lists (OmDet); liveness /
anti-spoofing (unchanged from the face protocol: a photo of an enrolled
person is recognized); persisting the toggle across server restarts; any
routing change (the whole point is that none is needed).
