"""Frozen 16-exchange dialogue fixture for the prefix-warm probe
(docs/PROTOCOL-prefix-warm.md). 32 turns; the runner takes the first k
PAIRS as rolling history. Synthetic but realistic; deterministic."""

EXCHANGES = [
    ("what's the weather look like tomorrow morning?",
     "Mostly clear until ten, then light rain through noon. High around eighteen."),
    ("remind me to call the dentist when i'm back from lunch",
     "Done — I'll remind you to call the dentist after lunch."),
    ("how do i make cold brew without special equipment",
     "Coarse-grind a cup of beans, steep in four cups of cold water for twelve "
     "hours, then filter through paper or a fine sieve."),
    ("compare the train and the flight to berlin next friday",
     "The train takes six hours city-center to city-center; the flight is "
     "ninety minutes plus airport time, so door to door they're close, and "
     "the train emits far less."),
    ("set a timer for twelve minutes for the rice",
     "Timer started — twelve minutes for the rice."),
    ("summarize yesterday's meeting in three bullets",
     "Three points: the launch slipped a week, hiring for the platform role "
     "is approved, and the vendor contract renews automatically in March."),
    ("what did the author mean by the quiet part",
     "She means the motivation the text implies but never states aloud — "
     "read the paragraph as an admission, not a description."),
    ("how much paint for a twelve by fifteen room",
     "One gallon covers two coats for a twelve-by-fifteen room with standard "
     "ceilings; buy two if the walls are dark and you're going lighter."),
    ("is it cheaper to rent or buy here",
     "Renting costs less monthly at current prices, but the break-even sits "
     "around six years if values hold."),
    ("walk me through why the build failed last night",
     "A dependency bumped its minimum Python to three-twelve; the image "
     "still pins three-eleven, so the wheel build aborted. Pin the "
     "dependency or bump the base image."),
    ("draft a short note to the landlord about the leak",
     "Short draft: the bathroom ceiling leak returned after last week's "
     "rain; asking for a repair visit this week and a status update."),
    ("explain the difference between http and https simply",
     "HTTPS is HTTP inside an encrypted tunnel — same requests and "
     "responses, but nobody on the wire can read or alter them."),
    ("what's a good pace for a ten k first run",
     "For a first ten-K, aim for a conversational pace around sixty-two to "
     "sixty-five minutes and don't sprint the first kilometre."),
    ("remind me tomorrow to water the plants",
     "Done — I'll remind you tomorrow to water the plants."),
    ("why does my wifi drop in the kitchen",
     "Distance plus two walls; the five-gigahertz band fades fastest. A "
     "wired backhaul access point in the hallway usually fixes it."),
    ("what should i check before the road trip",
     "Tyre pressures including the spare, coolant and oil levels, wipers, "
     "and the roadside kit in the boot."),
]

PAIRS = list(EXCHANGES)
