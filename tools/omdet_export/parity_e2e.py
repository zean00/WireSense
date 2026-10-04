"""Stage F — end-to-end detection parity: reference DetEngine (torch) vs
the wiresense sidecar pipeline (ORT + numpy selection/postprocess).

Gate: for every reference detection (conf_threshold=0.30, nms=0.5) there
must be an unused sidecar detection with the same label, IoU >= 0.9 and
|dconf| <= 0.05 — and the counts must be equal. Query selection is
discrete (top-900), so exact tensor parity is not the contract here;
per-graph tensor parity (stages A-E in export_onnx_cpu.py) is already
asserted and the selection agrees with torch topk 900/900 on identical
scores.
"""
import base64
import json
import sys

sys.path.insert(0, "/home/sahal/workspace/wiresense/web")
import omdet_sidecar as sc  # noqa: E402
from omdet.inference.det_engine import DetEngine  # noqa: E402
from PIL import Image  # noqa: E402

CASES = [
    ("./sample_data/000000574769.jpg",
     ["person", "cat", "orange"]),
    ("./sample_data/000000574769.jpg",
     ["person", "cat", "orange", "pizza", "car", "dog", "clock", "vase"]),
    ("./sample_data/000000574769.jpg",
     ["cup", "laptop", "banana", "bottle"]),
]
CONF, NMS = 0.30, 0.5


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    ua = ((ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter)
    return inter / ua if ua > 0 else 0.0


def main():
    engine = DetEngine(batch_size=1, device="cpu")
    all_ok = True
    for path, labels in CASES:
        prompt = "Detect {}.".format(",".join(labels))
        ref = engine.inf_predict("OmDet-Turbo_tiny_SWIN_T", task=prompt,
                                 data=[path], labels=labels,
                                 src_type="local",
                                 conf_threshold=CONF, nms_threshold=NMS)[0]

        w, h = Image.open(path).size
        frame = base64.b64encode(open(path, "rb").read()).decode()
        dets, ms = sc.detect(frame, w, h, labels, CONF, NMS)

        unused = list(range(len(dets)))
        misses = []
        for r in ref:
            hit = None
            for j in unused:
                d = dets[j]
                if (d["label"] == r["label"]
                        and iou((d["xmin"], d["ymin"], d["xmax"], d["ymax"]),
                                (r["xmin"], r["ymin"], r["xmax"], r["ymax"])) >= 0.9
                        and abs(d["conf"] - r["conf"]) <= 0.05):
                    hit = j
                    break
            if hit is None:
                misses.append(r)
            else:
                unused.remove(hit)
        extra = len(unused)
        ok = not misses and extra == 0 and len(ref) == len(dets)
        all_ok &= ok
        print(f"{'PASS' if ok else 'FAIL'} {path.rsplit('/', 1)[-1]} "
              f"N={len(labels)}: ref={len(ref)} sidecar={len(dets)} "
              f"missed={len(misses)} extra={extra} "
              f"total_ms={ms['total']} (enc {ms['enc']} dec {ms['dec']})")
        if misses:
            print("  missed:", json.dumps(misses))
        if extra:
            print("  extra:", json.dumps([dets[j] for j in unused]))
        else:
            print("  dets:", json.dumps(dets))
    print("E2E PARITY", "OK" if all_ok else "BROKEN")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
