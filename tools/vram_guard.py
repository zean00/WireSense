#!/usr/bin/env python3
"""VRAM placement guard for the fp4 fault — see docs/INCIDENT-2026-10-04-GPU.md.

The production model's NVFP4 weights produce NaN when llama-server places them
at the GPU's natural base address (marginal VRAM cells; the natural layout
fails on every boot, and two other NVFP4 models run clean — it is placement,
not a dead datapath). Reserving this block first shifts every later
allocation ~0.3 GB up, a placement that passed the full corruption stress
(3 x 227 sig embeddings + 60 generations, zero faults, across two fresh
server instances).

Stays resident until killed; `make start` waits for the ready file before
launching llama-server.
"""
import sys
import time

import torch

gb = float(sys.argv[1]) if len(sys.argv) > 1 else 0.3
ready = sys.argv[2] if len(sys.argv) > 2 else "/tmp/wiresense_guard.ready"

a = torch.full((int(gb * 2**30) // 4,), 0.5, dtype=torch.float32, device="cuda:0")
torch.cuda.synchronize()
assert not bool((a != 0.5).any().any()), "guard block failed self-check"
open(ready, "w").write("ok")
while True:
    time.sleep(3600)
