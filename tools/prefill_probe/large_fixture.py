"""Deterministic large-history fixture for the prefix-warm large-context
amendment (docs/PROTOCOL-prefix-warm.md, AMENDMENT section).

No RNG: pair(i) is pure index arithmetic over fixed word pools, so the
history text is byte-identical on every machine and every run. The runner
freezes sha256 of the assembled fixture text against the value recorded
in the amendment BEFORE any request is fired, and aborts on mismatch.

200 pairs; cycle length of (family, item, day, adj) combos is 840 > 200,
so no pair repeats."""

FAMILIES = [
    ("remind me about {item} on {day}",
     "Done — {item} is on my list for {day}."),
    ("what's the {adj} way to deal with {item} before {day}",
     "The {adj} route: handle {item} early on {day}, keep it short, and confirm it's closed afterwards."),
    ("should I do {item} myself or wait until {day}",
     "If {item} is quick, do it today; otherwise first thing {day} — either way it stays off the critical path."),
    ("put {item} on the list for {day}",
     "Added: {item}, scheduled for {day}."),
    ("how long does {item} usually take",
     "For {item}, plan {adj} time — most of it is waiting, the active part is minutes."),
    ("what did I say about {item} last week",
     "You flagged {item} as {adj} priority and wanted it settled before {day}."),
    ("is {item} urgent or can it wait until {day}",
     "Not urgent — {item} holds until {day} without consequence."),
    ("note that {item} is done as of {day}",
     "Noted: {item} closed on {day}. I'll stop tracking it."),
    ("compare doing {item} now versus {day}",
     "Now buys {adj} margin; {day} risks stacking with the rest. Now, if you have twenty minutes."),
    ("anything pending on {item} before {day}",
     "One thing: {item} needs a {adj} check before {day}, then it's clear."),
]

ITEMS = [
    "the kitchen faucet", "the car tyres", "the dentist appointment",
    "the landlord email", "the passport renewal", "the pantry inventory",
    "the bike brake pads", "the insurance quote", "the library books",
    "the smoke alarms", "the gutters", "the printer driver",
    "the plant repotting", "the fridge seal", "the window latch",
    "the recipe box", "the tool restock", "the garage shelf",
    "the mail pile", "the shoe rack", "the curtain rod",
    "the router firmware", "the first-aid kit", "the winter blankets",
]

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday"]

ADJS = ["low-effort", "thorough", "quick", "careful", "minimal",
        "steady", "practical", "quiet"]


def pair(i):
    fam = FAMILIES[i % len(FAMILIES)]
    item = ITEMS[(i // len(DAYS)) % len(ITEMS)]
    day = DAYS[(i // (len(DAYS) * len(ITEMS))) % len(DAYS)]
    adj = ADJS[(i // (len(DAYS) * len(ITEMS) * len(DAYS))) % len(ADJS)]
    return (fam[0].format(item=item, day=day, adj=adj),
            fam[1].format(item=item, day=day, adj=adj))


PAIRS_LARGE = [pair(i) for i in range(200)]


def fixture_sha256():
    import hashlib
    blob = "\n".join(f"{u}\n{a}" for u, a in PAIRS_LARGE)
    return hashlib.sha256(blob.encode()).hexdigest()


if __name__ == "__main__":
    print(f"pairs={len(PAIRS_LARGE)} sha256={fixture_sha256()}")
