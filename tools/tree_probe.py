"""Tree probe — offline experiment, no server integration.

Question: how effective is a decision TREE of CLM readouts (vs the flat
5-way router) at deciding which component to activate — LLM direct path,
long thinking, a tool call, or a specific visual service?

Protocol (pre-registered in this file):
- Root node reuses tools/eval_router.py's 56 held-out utterances and the
  committed refs (decision_tree.json pools + router_embeddings.npz).
- Two simulated subtree nodes get their own pools + held-out evals here:
  think -> {long_think, tool}, vision -> {vqa, object, face_id,
  face_track, expression}. Pools are ASR-normalized like production.
- A leaf-override table labels the unambiguous root-eval think/vision
  items with their subtree leaf; unlabeled ones are excluded from leaf
  scoring and only scored on branch correctness.
- Metrics: per-node accuracy + confusion, end-to-end component accuracy,
  flat-vs-tree delta, confidence-floor sweep (accuracy vs fall-through).

Run against the live serving model:
    make tree-probe
"""
import json
import sys
import time

import numpy as np
import requests

sys.path.insert(0, "tools")
from eval_router import EVAL as ROOT_EVAL, norm_router_text  # noqa: E402

LLAMA = "http://127.0.0.1:8998"
TREE_CFG = "tools/decision_tree.json"
REFS = "models/router_embeddings.npz"
HEADS = "models/arm_C_heads.npz"

# --- subtree pools (pre-registered; not yet in decision_tree.json —
# they land there with their own refs in the think/vision phases) ---
THINK_POOLS = {
    "long_think": [
        "Plan a three-day trip to Tokyo.",
        "Write a short story about a dragon.",
        "Walk me through why the experiment failed.",
        "Explain the difference between HTTP and HTTPS.",
        "Work out the monthly payment on a 20 thousand dollar loan.",
        "Draft an email to my landlord about the leak.",
        "Summarize this article for me.",
        "How much paint do I need for a 12 by 15 foot room?",
        "Compare renting versus buying a house.",
        "Count the vowels in the word extraordinary.",
    ],
    "tool": [
        "Set a timer for 10 minutes.",
        "Set an alarm for six tomorrow morning.",
        "Add a calendar event for lunch tomorrow at noon.",
        "Search for flights to Denver next week.",
        "Remind me to take my pills at 9.",
        "Turn on the living room lights.",
        "What's the weather in Jakarta?",
        "Play some jazz music.",
        "Text my brother that I'm running late.",
        "Order more coffee beans.",
    ],
}
THINK_EVAL = [
    ("How long would it take to read a 400 page book?", "long_think"),
    ("Turn this recipe into a shopping list.", "long_think"),
    ("Write a haiku about autumn.", "long_think"),
    ("If today is Tuesday, what day is it in 100 days?", "long_think"),
    ("Work out the tip on an 84 dollar bill.", "long_think"),
    ("Explain how a bill becomes law, step by step.", "long_think"),
    ("How much would a 30 kilogram suitcase weigh on Mars?", "long_think"),
    ("Plan my week around three meetings.", "long_think"),
    ("Count the vowels in onomatopoeia.", "long_think"),
    ("Draft a cover letter for a barista job.", "long_think"),
    ("Set a timer for 20 minutes.", "tool"),
    ("Wake me up at 7 am.", "tool"),
    ("Add milk to my shopping list.", "tool"),
    ("What's the weather tomorrow?", "tool"),
    ("Turn off the lights.", "tool"),
    ("Play the Beatles.", "tool"),
    ("Send a message to Sarah.", "tool"),
    ("Remind me about the dentist on Friday.", "tool"),
    ("Order a taxi to the airport.", "tool"),
    ("Start a 5 minute plank timer.", "tool"),
]

VISION_POOLS = {
    "vqa": [
        "Does the color of my clothes match my skin tone?",
        "What do you think about my new glasses?",
        "How does my new haircut look from the side?",
        "Is my tie crooked?",
        "What does the error on my screen say?",
        "Can you read the label on this bottle?",
        "What do you see on my desk right now?",
        "Is this plant healthy?",
    ],
    "object": [
        "What is this thing I'm holding?",
        "What brand is this product?",
        "How many people are in the room?",
        "What kind of bird is that outside my window?",
        "Is this a pothos or a philodendron?",
        "What model of phone is this?",
    ],
    "face_id": [
        "Who is this person standing next to me?",
        "Do you recognize my friend here?",
        "Is this the same guy from yesterday's meeting?",
        "Who's at the door?",
    ],
    "face_track": [
        "Keep my face centered in the frame.",
        "Follow me with the camera when I move.",
        "Keep the camera on whoever is talking.",
        "Crop the frame around my face.",
    ],
    "expression": [
        "Am I smiling in this shot?",
        "Do I look tired today?",
        "Can you tell if I look confused?",
        "Does this person look angry?",
        "How do I look, happy or sad?",
    ],
}
VISION_EVAL = [
    ("Do these shoes go with this outfit?", "vqa"),
    ("How does my posture look in this video call?", "vqa"),
    ("Does this color suit me?", "vqa"),
    ("What do you think of my desk setup?", "vqa"),
    ("Is the picture hanging straight behind me?", "vqa"),
    ("Is my shirt tucked in properly?", "vqa"),
    ("What species is this flower?", "object"),
    ("How many fingers am I holding up?", "object"),
    ("What's written on this sign?", "vqa"),
    ("Which one of these is the wrench?", "object"),
    ("What is this tool called?", "object"),
    ("Is this my sister?", "face_id"),
    ("Who just walked in?", "face_id"),
    ("Do you know this person?", "face_id"),
    ("Pan the camera to follow my hand.", "face_track"),
    ("Keep me in frame while I cook.", "face_track"),
    ("Zoom in on my face.", "face_track"),
    ("Do I look annoyed right now?", "expression"),
    ("Is the baby happy or upset?", "expression"),
    ("Can you tell my mood from my face?", "expression"),
]

# leaf labels for unambiguous root-eval items (pre-registered; unlabeled
# items are only scored on branch correctness)
ROOT_LEAF = {
    "Add a calendar event for lunch tomorrow at noon.": "tool",
    "Plan my week around three meetings.": "long_think",
    "Write a haiku about autumn.": "long_think",
    "Work out the monthly payment on a 20 thousand dollar loan at 6 percent.": "long_think",
    "Draft a cover letter for a barista job.": "long_think",
    "How long would it take to read a 400 page book?": "long_think",
    "Explain how a bill becomes law, step by step.": "long_think",
    "How much would a 30 kilogram suitcase weigh on Mars?": "long_think",
    "Count the vowels in the word extraordinary.": "long_think",
    "What's 15 percent tip on an 84 dollar bill?": "long_think",
    "If today is Tuesday, what day is it in 100 days?": "long_think",
    "What is this plant I'm showing you?": "object",
    "What does the error on my screen say?": "vqa",
    "Am I holding the wrench the right way?": "vqa",
    "Is the picture hanging straight behind me?": "vqa",
    "Can you read the label on this bottle?": "vqa",
    "Do these shoes go with this outfit?": "vqa",
    "Is my tie crooked?": "vqa",
    "How does my new haircut look from the side?": "vqa",
    "What do you see on my desk right now?": "object",
}

# subtree structure: child name -> sub-node pool dict or None (leaf)
TREE = {
    "direct": None, "decide": None, "social": None,
    "think": THINK_POOLS,
    "vision": VISION_POOLS,
}
SUBTREE_EVALS = {"think": THINK_EVAL, "vision": VISION_EVAL}


def emb(text):
    r = requests.post(f"{LLAMA}/v1/embeddings",
                      json={"input": text, "model": "wiresense-llm",
                            "id_slot": 6}, timeout=60)
    v = r.json()["data"][0]["embedding"]
    if v is None or any(x is None for x in v):
        raise RuntimeError("null embedding")
    a = np.asarray(v, dtype=np.float32)
    if np.isnan(a).any() or not a.any():
        raise RuntimeError("NaN/zero embedding")
    return a


class Head:
    def __init__(self):
        z = np.load(HEADS)
        self.fc1_w, self.fc1_b = z["fc1.weight"], z["fc1.bias"]
        self.ln_w, self.ln_b = z["ln.weight"], z["ln.bias"]
        self.fc2_w, self.fc2_b = z["fc2.weight"], z["fc2.bias"]

    def project(self, x):
        h = x @ self.fc1_w.T + self.fc1_b
        h = 0.5 * h * (1.0 + np.tanh(0.7978845608 * (h + 0.044715 * h ** 3)))
        ln = (h - h.mean(-1, keepdims=True)) / np.sqrt(h.var(-1, keepdims=True) + 1e-5)
        h = ln * self.ln_w + self.ln_b + h
        return h @ self.fc2_w.T + self.fc2_b


def norm_rows(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


class Node:
    """A CLM readout: head-projected class centroids over embeddings."""

    def __init__(self, name, classes, cents):
        self.name, self.classes, self.cents = name, classes, cents

    def route(self, vec):
        sims = self.cents @ vec
        i = int(np.argmax(sims))
        return self.classes[i], float(sims[i])


def build_node(name, pools, class_vecs, project):
    cents = norm_rows(np.stack(
        [norm_rows(project(v)).mean(0) for v in class_vecs]))
    return Node(name, list(pools), cents)


def main():
    head = Head()

    def project(x):
        return norm_rows(head.project(x))

    # ---- embed every pool + eval text once (slot 6) ----
    tree = json.load(open(TREE_CFG))
    root_pools = tree["nodes"]["root"]["pools"]
    uniq, pool_norm = {}, set()

    def keep(t, is_pool=False):
        uniq.setdefault(norm_router_text(t), t)
        if is_pool:
            pool_norm.add(norm_router_text(t))

    for ts in root_pools.values():
        for t in ts:
            keep(t, is_pool=True)
    for node_pools in TREE.values():
        if node_pools:
            for ts in node_pools.values():
                for t in ts:
                    keep(t, is_pool=True)
    for evs in SUBTREE_EVALS.values():
        for t, _ in evs:
            keep(t)
    for t, _ in ROOT_EVAL:
        keep(t)

    print(f"embedding {len(uniq)} unique texts once (slot 6)...")
    t0 = time.perf_counter()
    vecs = {t: emb(t) for t in uniq}
    emb_s = time.perf_counter() - t0
    print(f"  {emb_s:.1f}s ({emb_s / len(uniq) * 1000:.0f} ms/emb)")

    # ---- build the tree ----
    z = np.load(REFS)
    nodes = {}
    nodes["root"] = build_node("root", root_pools,
                               [z[f"root.{c}"] for c in root_pools],
                               project)
    for node_name, node_pools in TREE.items():
        if not node_pools:
            continue
        class_vecs = [np.stack([vecs[norm_router_text(t)] for t in ts])
                      for ts in node_pools.values()]
        nodes[node_name] = build_node(node_name, node_pools, class_vecs, project)

    def route_at(name, text):
        v = project(vecs[norm_router_text(text)][None, :])[0]
        return nodes[name].route(v)

    # ---- protocol guard: subtree evals must not appear in any pool ----
    for evs in SUBTREE_EVALS.values():
        clash = [t for t, _ in evs if norm_router_text(t) in pool_norm]
        if clash:
            print(f"PROTOCOL VIOLATION — subtree eval in pools: {clash}")
            return 1

    # ---- per-node evals ----
    def eval_node(name, evals, tag):
        idx = {c: i for i, c in enumerate(nodes[name].classes)}
        cm = np.zeros((len(idx), len(idx)), int)
        misses = []
        for t, gold in evals:
            pred, conf = route_at(name, t)
            cm[idx[gold], idx[pred]] += 1
            if pred != gold:
                misses.append((gold, pred, round(conf, 3), t))
        acc = cm.diagonal().sum() / len(evals)
        print(f"\n[{tag}] node '{name}': {int(cm.diagonal().sum())}/{len(evals)} = {acc:.0%}")
        print("gold\\pred  " + "  ".join(f"{c:>11}" for c in nodes[name].classes))
        for c, row in zip(nodes[name].classes, cm):
            print(f"{c:>9}  " + "  ".join(f"{n:>11}" for n in row))
        for g, p, cf, t in misses:
            print(f"  miss [{g}]->{p} ({cf}): {t}")
        return acc

    eval_node("root", ROOT_EVAL, "committed refs")
    for node_name, evs in SUBTREE_EVALS.items():
        eval_node(node_name, evs, "probe")

    # ---- end-to-end: final COMPONENT activation, flat vs tree ----
    # component id: llm_direct / llm_think / llm_decide / llm_social /
    # tool / vqa / object / face_id / face_track / expression
    def component(branch, leaf):
        if branch == "think":
            return leaf if leaf in ("tool", "long_think") else "llm_think"
        if branch == "vision":
            return leaf if leaf in nodes["vision"].classes else "vision_generic"
        return f"llm_{branch}"

    print("\n=== end-to-end: final component activation ===")
    flat_ok = tree_ok = branch_ok = 0
    leaf_scored = leaf_ok = 0
    interesting = []
    for t, gold_branch in ROOT_EVAL:
        pred_root, conf = route_at("root", t)
        gold_leaf = ROOT_LEAF.get(t)
        gold_final = component(gold_branch, gold_leaf)
        flat_final = component(pred_root, None)
        leaf = None
        if pred_root in nodes and TREE.get(pred_root):
            leaf, _ = route_at(pred_root, t)
        tree_final = component(pred_root, leaf)
        flat_ok += flat_final == gold_final
        tree_ok += tree_final == gold_final
        branch_ok += pred_root == gold_branch
        if gold_leaf is not None:
            leaf_scored += 1
            leaf_ok += tree_final == gold_final
        if gb_changed := (pred_root != gold_branch or tree_final != flat_final):
            interesting.append((t, gold_branch, pred_root, round(conf, 3), tree_final))
    n = len(ROOT_EVAL)
    print(f"root branch correct:                    {branch_ok}/{n}")
    print(f"flat 5-way final component correct:     {flat_ok}/{n}")
    print(f"tree final component correct:           {tree_ok}/{n}")
    print(f"tree, leaf-labeled subset only:         {leaf_ok}/{leaf_scored}")
    print("(differences and misroutes):")
    for t, gb, pr, cf, fin in interesting:
        print(f"  gold={gb:>7} root={pr:>7} ({cf}) -> {fin:>11} :: {t[:56]}")

    # ---- confidence-floor sweep ----
    print("\n=== confidence floor sweep (root, tau = fall-through to default) ===")
    print("tau    correct  fallthrough  wrong")
    for tau in (0.0, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9):
        corr = ft = wrong = 0
        for t, gold in ROOT_EVAL:
            pred, conf = route_at("root", t)
            if conf < tau:
                ft += 1
            if pred == gold:
                corr += 1
            else:
                wrong += 1
        print(f"{tau:.2f}  {corr:>7}/{n}  {ft:>10}  {wrong:>5}")

    print("\n=== confidence floor sweep (vision node over its own eval) ===")
    for tau in (0.0, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9):
        corr = ft = 0
        for t, gold in SUBTREE_EVALS["vision"]:
            pred, conf = route_at("vision", t)
            if conf < tau:
                ft += 1
            if pred == gold:
                corr += 1
        print(f"tau={tau:.2f}  correct {corr}/{len(SUBTREE_EVALS['vision'])}  fallthrough {ft}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
