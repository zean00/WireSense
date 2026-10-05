"""Reconnaissance probe (pre-protocol): do face enroll/forget phrasings
reach the tool flow under the CURRENT committed tree? Not an eval — this
measures existing behavior to inform the design decision (tree edit vs
none), exactly like the deep-think live probes before Revision 1. Uses
the production readout + TreeWalker verbatim.

Run against the live serving model (slot 6 embeddings):
    .venv/bin/python tools/probe_face_routing.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from eval_router import emb  # production embed path: norm -> slot 6
from server import TreeWalker, DecideReadout

PROBES = [
    "learn my face as albert",
    "remember my face",
    "forget my face",
    "who am i",
    "what do you see right now",
    "set a timer for 10 minutes",
]


def main():
    walker = TreeWalker(DecideReadout()._project)
    for text in PROBES:
        cls, spec, conf = walker.route(emb(text))
        line = f"{text!r:38s} root -> {cls} ({conf:.3f})"
        if cls == "think":
            scls, _, sconf = walker.route(emb(text), "think")
            line += f"  subtree -> {scls} ({sconf:.3f})"
        print(line)


if __name__ == "__main__":
    main()
