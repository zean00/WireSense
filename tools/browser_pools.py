"""Frozen pools for voice-driven browser use (PROTOCOL-browser-use.md,
decisions 7/11). Committed BEFORE any gate run; single source for
web/server.py (embedded lazily on the first browser task) and
tools/eval_browser.py (gate harness). Texts are scored after
norm_router_text, same as routing pools.

STOP/NOT_STOP: mid-task voice control — a final while a browser task is
running scores 1-NN against both sides; stop wins only if its best
similarity beats the non-stop side by at least the registered margin.
Gate G2 bar lives in eval_browser.py.

GOAL_YES/GOAL_NO: the decide-layer goal watcher (the Noul analog) —
state descriptions near what a fixture task's satisfied (or unsatisfied)
page looks like. Scored as 1-NN cosine between the scored state line and
both sides. Ships only if gate G3 passes; otherwise the slot-4 LLM judge
ships and this head is recorded as NOT adopted.
"""

STOP = [
    "Stop the browser task",
    "Cancel that search",
    "Stop",
    "Never mind, forget the page",
    "Cancel what you're doing",
    "Stop looking",
    "Forget it, stop the task",
    "Cancel the lookup",
    "That's enough, stop",
    "Stop the search",
]

NOT_STOP = [
    "Stop the timer",
    "Set a timer for five minutes",
    "What time is it?",
    "Read the page again",
    "What's the price?",
    "Tell me a joke",
    "Scroll down",
    "Click the first result",
    "What's the weather?",
    "Remind me at six",
]

GOAL_YES = [
    "the page shows the exact answer to the question",
    "the requested price is visible on the page",
    "the article headline the user asked about is on screen",
    "the confirmation page thanks the user by name",
    "the page lists the information the task asked to find",
    "the documentation section matching the question is shown",
    "the product page shows the price of the requested item",
    "the search results directly contain the requested fact",
    "the form was submitted and a receipt is shown",
    "the page states the schedule the user asked for",
]

GOAL_NO = [
    "the page is a login form asking for credentials",
    "the page is an unrelated article about something else",
    "the requested information is absent from the page",
    "the page shows a navigation menu with no content yet",
    "the form is empty and unfilled",
    "the page shows an error message",
    "the search results list products but not their prices",
    "the page is a homepage with general links and no answer",
    "the page holds the right topic but a different section",
    "the task needs a detail the page never displays",
]
