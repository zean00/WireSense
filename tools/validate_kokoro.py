"""Wiresense — Kokoro-82M (ONNX) TTS sidecar validation.

Checks: model loads on CPU, synthesis works, and measures the latency that
matters for the voice loop — per-chunk wall time vs produced audio duration
(RTF), on sentence-sized chunks (the chunked-streaming pattern).

Usage: .venv/bin/python tools/validate_kokoro.py
Writes audio/kokoro_chunk{1,2}.wav (24 kHz).
"""

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models/kokoro-v1.0.onnx"
VOICES = ROOT / "models/voices.npz"
OUT = ROOT / "audio"

# chunk-sized texts (what the LLM's streamed output will look like)
CHUNKS = [
    "Wiresense is listening. The decide layer watches the context state as "
    "each partial transcript arrives.",
    "When it commits, generation starts, and speech comes out in chunks "
    "like this one.",
]


def main() -> None:
    from kokoro_onnx import Kokoro

    t0 = time.perf_counter()
    tts = Kokoro(str(MODEL), str(VOICES))
    load_s = time.perf_counter() - t0
    print(f"model load: {load_s:.2f}s")

    for i, text in enumerate(CHUNKS, 1):
        t0 = time.perf_counter()
        samples, sr = tts.create(text, voice="af_heart", speed=1.0,
                                 lang="en-us")
        wall = time.perf_counter() - t0
        dur = len(samples) / sr
        path = OUT / f"kokoro_chunk{i}.wav"
        sf.write(path, samples, sr)
        print(f"chunk {i}: {len(text):3d} chars -> {dur:5.2f}s audio "
              f"in {wall * 1000:6.0f} ms  (RTF {dur / wall:5.1f}x)  "
              f"-> {path}")

    assert all((OUT / f"kokoro_chunk{i}.wav").exists() for i in (1, 2)), \
        "no audio written"
    print("KOKORO VALIDATION PASS")


if __name__ == "__main__":
    sys.exit(main())
