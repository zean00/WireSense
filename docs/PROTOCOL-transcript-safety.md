# Protocol — transcript-safety layer (clarify-on-uncertainty + high-risk tool read-back)

Status: PRE-REGISTERED before any gate data. Frozen evals untouched. House
rules apply: deviations get registered amendments before reruns; failures
get recorded, not patched around.

## Motivation (all measured, prior phases)

- Phonon-2 garbles "Who am I?" -> "Two am I?" (deterministic for fixed
  bytes); the decide layer routed the garble DIRECT at confidence 0.82 —
  routing confidence is an intent-class signal, not a transcript-sanity
  signal, so it is disqualified as the clarify judge.
- The repair-by-rule layer (PROTOCOL-asr-normalizer.md) only covers rules
  with logged real firings. The general case needs a semantic judge.
- New calibration finding (this phase, pre-registration): a fresh Kokoro
  render of "Who am I?" transcribed CLEAN while the archived harness bytes
  still garble ("Two am I?"). The decision boundary is sharp enough that
  two renders of the same sentence land on opposite sides — single rules
  can never cover this class; a judge can.
- Silent wrong-number harm: a misheard "15 seconds" in a timer/reminder
  executes the wrong timed action with no chance to catch it (REL_TIME
  extracts from the transcript; the reply confirms whatever was heard).
  This is the high-risk class the user asked to gate behind confirmation.

## Pre-registration calibration (frozen; run 2026-10-05, slot 4, temp 0)

Trigger candidate — distinct-partial instability (DISQUALIFIED): clean
short utterances scored 1-3 distinct partial texts (n=12), the garbling
render scored 2, archived garble bytes scored 2. No separating threshold
exists. -> M1 cannot be triggered cheaply; it judges every eligible turn.

Judge candidates (corpus: 24 sensible + 12 fluent-nonsense, text-level):

| shape                                  | acc    | mean ms |
|----------------------------------------|--------|---------|
| 1-token logprob Yes/No margin          | 23/30  | 68      |
| chat YES/NO zero-shot, thinking off    | 33/36  | 92      |
| chat YES/NO adversarial system prompt  | 35/36  | 106     |
| chat YES/NO 4-shot (variant C)         | 36/36  | 115     |

Registered: the 4-shot judge. Stage 2 (garble repair) verified separately:
"Yes, you are two." -> "Yes, you are too." (~190 ms). Raw-completions
prompting drops the model into think-mode and is disqualified; the judge
uses /v1/chat/completions with enable_thinking:false (same as vision).

## Decisions

1. **Two mechanisms, one pending state machine.** M1 clarify-on-uncertainty
   for short non-tool finals; M2 mandatory read-back for high-risk tools.
   Both park a pending resolution on the session; the next utterance
   resolves it.
2. **M1 trigger (frozen):** utterance has <= 4 words AND the normalizer did
   NOT fire this turn (repaired finals trust layer 1) AND no pending state
   is active AND the turn is not itself a resolution (`_resolved`). All
   other turns never see the judge.
3. **Judge (frozen verbatim):** system "You judge transcripts from speech
   recognition. Grammatical sentences can still be recognition errors.
   Answer with only YES or NO."; user turn carries 4 frozen examples ("The
   purple sleeps loudly" -> NO, "What time is it?" -> YES, "Yes, you are
   two." -> NO, "Who is this?" -> YES) then the target. max_tokens 4,
   temperature 0, dedicated slot 4, cache_prompt. Verdict parse: strip,
   upper, strip trailing ".!"; startswith YES -> sensible, startswith NO ->
   not sensible, anything else -> fail-open sensible.
4. **Stage 2 only on a NO verdict:** system "You repair garbled
   speech-recognition transcripts."; user asks for the most likely intended
   sentence, nothing else. max_tokens 24. Sanitize: strip, strip quotes,
   reject empty or > 80 chars -> fail-open pass-through.
5. **Clarify reply (frozen):** "Did you mean: {likely}?" — the turn's full
   reply (no routing, no LLM answer); route event mode "clarify"; history
   appends (garbled, clarify-reply); pending = {"kind":"intent",
   "likely":...}; metrics carry judge {ms, sensible}.
6. **Resolution vocabulary (frozen, exact match after lowercase/strip
   punctuation/space-squeeze).** YES: yes yeah yep yup sure ok okay right
   correct confirm confirmed "do it" "go ahead" "yes please" "that's right"
   "sounds good" "please do". NO: no nope nah cancel stop wrong don't dont
   "no thanks" "that's wrong".
   - intent-pending + YES -> full fresh respond(likely) (routing, ambient
     context, tools — everything; judge skipped on it via _resolved). Max
     one clarify per chain.
   - intent-pending + NO -> "Okay — say it again." (route mode "resolve").
   - tool-pending + YES -> run_tool + tool_call event + the normal
     _confirm_tool sentence (route mode "resolve").
   - tool-pending + NO -> "Okay, cancelled." Nothing executed (route mode
     "resolve").
   - Anything else -> pending dropped silently, utterance processed
     normally (the pending never hijacks an unrelated turn).
7. **One pending slot per session; latest wins; resolves on the next
   utterance only; no timer-based expiry** (a stale pending is dropped by
   the first unrelated utterance).
8. **M2 scope frozen: {"timer", "reminder"}.** Rationale: numeric args,
   timed real-world effects, silently wrong on a misheard number. note
   (no timed effect), detect (read-only), face tools (own confirm flow)
   are excluded.
9. **M2 flow:** tool_flow validate -> if high-risk: NO tool_call event, NO
   execution; the turn's reply is the read-back template (frozen):
   timer — "Just to confirm — a timer for {label or N seconds}?";
   reminder — "Just to confirm — a reminder to {text}, {when}?" — and
   pending = {"kind":"tool", tool, args}. Execution happens only on the
   YES resolution (decision 6).
10. **Fail-open everywhere:** any judge/repair exception, junk verdict, or
    empty repair passes the turn through as today. The layer may only
    remove harm (catch a garble), never add it (clarify a clean turn).
    The false-clarification rate on clean speech is the killing metric.
11. **Router untouched.** The pending interception and the judge are the
    only pre-routing branches; resolution turns emit route mode "resolve",
    clarify turns "clarify" (conf null), everything else identical.
12. **Layering:** normalizer (layer 1) runs in phonon_reader before
    respond() and its firing suppresses the judge for that turn; M2's
    read-back catches misheard NUMBERS on tool turns, which no semantic
    judge can see (the sentence is fluent; only the value is wrong).
13. **Registered harness changes (this phase):** timer and reminder e2e
    cases become two-turn (confirm read-back, then resolve "Yes.", then
    the existing tool checks); new M1 positive case "Yes, you are two."
    (TTS-rendered fluent nonsense transcribes faithfully — deterministic,
    no render hunting); new M2 decline control (resolve "No." -> no
    tool_event, no memory write); FACE-suite timer case gains the resolve
    turn. The default-run pass count changes accordingly and is
    re-registered from this protocol.
14. **Known limits (registered, not fixed here):** no judge on utterances
    over 4 words (long fluent garbles stay wrong); fluent-but-wrong
    non-numeric transcripts pass; judge is English-centric; every
    timer/reminder now costs one extra user turn (accepted UX cost of
    explicit confirmation).

## Gates

- G1 offline units: eligibility, verdict parse (fail-open), resolution
  classifier, read-back templates + live judge spot-checks 6/6 (3+3 frozen
  corpus) against the real slot-4 endpoint.
- G2 M2 e2e: timer confirm -> "Yes." -> tool_event seconds correct; timer
  confirm -> "No." -> no tool_event, no memory write; reminder confirm ->
  "Yes." -> saved with due + fires reminder_due.
- G3 M1 e2e: "Yes, you are two." -> route clarify + "Did you mean" reply;
  resolve "Yes." -> normal non-clarify routed non-empty reply; clean short
  "Who am I?" -> judge ran (metrics judge field), NO clarification,
  identity answer intact.
- G4 default run all-pass under decision 13; FACE run all-pass (ambient
  semantics unchanged).
- G5 latency: judge ms <= 200 on every judged turn (metrics); clarify
  turns final->reply <= 1200 ms; ambient G4 pair within its +15% bar.
- G6 no regressions: eval_router / think / followup bars identical;
  normalizer 22/22; face units 472/472.

## Results

(filled after adjudication)
