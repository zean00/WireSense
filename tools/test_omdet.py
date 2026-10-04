"""Detector sidecar component test + benchmark (make test-omdet).

Pre-registered before the first measured run (bring-up evidence: encoder
~300 ms + decoder ~170 ms per frame on this Ryzen AI 7 350, 8 ORT threads;
the draft 100-250 ms estimate from the VRAM survey was CPU-agnostic and
wrong):
  - on-demand single-shot: p95 total <= 900 ms over 20 timed HTTP runs
    (voice budget: detect runs behind a spoken filler; the VL fallback
    path measures 1.0-1.4 s, so 900 ms keeps detect strictly faster)
  - sustained loop: >= 1.5 FPS mean over 10 consecutive runs
    (language cache hot; the realistic trigger-loop floor)
Gates fail with exit 1.

Also asserts: /health, frozen CLIP token ids (vendored BPE, verified
token-exact vs the reference tokenizer at export time), and — when the
omdet sample image is present — a person detection at conf >= 0.5 with
labels ["person", "cat", "orange"] (measured 0.89 at export parity).
"""
import base64
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))
import omdet_sidecar as sc  # noqa: E402

BASE = "http://127.0.0.1:8011"
SAMPLE = os.environ.get("OMDET_TEST_IMAGE",
                        "/home/sahal/workspace/omdet/sample_data/000000574769.jpg")
P95_BUDGET_MS = 900.0
FPS_FLOOR = 1.5

fails = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        fails.append(name)


def http(path, payload=None, timeout=120):
    if payload is None:
        return json.loads(urllib.request.urlopen(f"{BASE}{path}",
                                                 timeout=timeout).read())
    req = urllib.request.Request(f"{BASE}{path}", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def main():
    check("health", http("/health").get("ok") is True)

    for text, want in (("person", [49406, 2533, 49407]),
                       ("coffee mug", [49406, 2453, 9722, 49407])):
        ids = sc.tokenize([text])[0]
        n = int((ids != 0).sum())
        check(f"tokens {text!r}", ids[:n].tolist() == want, str(ids[:n].tolist()))

    if not os.path.exists(SAMPLE):
        print(f"SKIP detection content check ({SAMPLE} missing)")
        # synthetic frame so the latency benchmark still runs
        from PIL import Image
        import io
        im = Image.new("RGB", (480, 640), (40, 90, 60))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        frame = base64.b64encode(buf.getvalue()).decode()
        w, h = 480, 640
    else:
        raw = open(SAMPLE, "rb").read()
        frame = base64.b64encode(raw).decode()
        from PIL import Image
        w, h = Image.open(SAMPLE).size
        out = http("/detect", {"frame": frame, "width": w, "height": h,
                               "labels": ["person", "cat", "orange"]})
        persons = [d for d in out["detections"] if d["label"] == "person"]
        check("person detected", bool(persons) and
              max(d["conf"] for d in persons) >= 0.5,
              f"best conf {max((d['conf'] for d in persons), default=0):.2f}")

    # benchmark — protocol fixed above, measured below
    for _ in range(5):
        http("/detect", {"frame": frame, "width": w, "height": h,
                         "labels": ["person", "cat", "orange"]})
    totals = []
    for _ in range(20):
        totals.append(http("/detect", {"frame": frame, "width": w,
                                       "height": h,
                                       "labels": ["person", "cat", "orange"]}
                           )["ms"]["total"])
    totals.sort()
    p50 = totals[10]
    p95 = totals[-1]
    check("p95 on-demand <= %.0f ms" % P95_BUDGET_MS, p95 <= P95_BUDGET_MS,
          f"p50 {p50:.0f} p95 {p95:.0f} (min {totals[0]:.0f})")

    t0 = time.perf_counter()
    n = 10
    for _ in range(n):
        http("/detect", {"frame": frame, "width": w, "height": h,
                         "labels": ["person", "cat", "orange"]})
    fps = n / (time.perf_counter() - t0)
    check(f"sustained >= {FPS_FLOOR} FPS", fps >= FPS_FLOOR, f"{fps:.2f} FPS")

    print("BENCH", "PASS" if not fails else f"FAIL {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
