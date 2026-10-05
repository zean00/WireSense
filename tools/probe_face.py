"""Component probe for face recognition: YuNet detector + SFace embedder
(OpenCV Zoo, both Apache-2.0). Run before the protocol commits so the
registered threshold comes from measured geometry, not the datasheet.

Measures, at BOTH full resolution and camera resolution (<=448 px JPEG,
the production frame budget from the vision path):
  - detection on the three frozen probe portraits
  - same-person cosine across augmentations (hflip, jpeg q30, 10% crop,
    scale 0.6)
  - different-person cosine across portrait pairs
  - per-stage latency on CPU

This is selection validation, not a gate — gates are pre-registered in
docs/PROTOCOL-face-recognition.md.

    .venv/bin/python tools/probe_face.py
"""
import io
import pathlib
import time

import cv2
import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
MODELS = HERE.parent / "models" / "face"
IMGS = HERE / "face_eval" / "imgs"

DETECT = str(MODELS / "face_detection_yunet_2023mar.onnx")
SFACE = str(MODELS / "face_recognition_sface_2021dec.onnx")

NAMES = ["einstein", "tesla", "curie"]


def cam_style(img, q=80, cap=448):
    """Production frame budget: longest side <= cap px, JPEG re-encode."""
    h, w = img.shape[:2]
    s = cap / max(h, w)
    if s < 1.0:
        img = cv2.resize(img, (round(w * s), round(h * s)),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    assert ok
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def augment(img):
    """Same-person probes, applied at whichever resolution we're testing."""
    h, w = img.shape[:2]
    yield "hflip", cv2.flip(img, 1)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 30])
    yield "jpeg_q30", cv2.imdecode(buf, cv2.IMREAD_COLOR)
    m = round(0.1 * min(h, w))
    yield "crop10", img[m:h - m, m:w - m]
    yield "scale0.6", cv2.resize(img, (round(w * 0.6), round(h * 0.6)),
                                 interpolation=cv2.INTER_AREA)


def detect_face(img, detector, cap=640):
    """Detect on the capped image, align+embed THAT SAME image — detector
    boxes are in capped coordinates; mixing frames feeds alignCrop garbage
    (probe iteration 1 measured that as cross-resolution cosine ~0.1).
    Production frames are <=448 px anyway; the cap only guards big files."""
    h, w = img.shape[:2]
    s = min(1.0, cap / max(h, w))
    if s < 1.0:
        img = cv2.resize(img, (round(w * s), round(h * s)),
                         interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None
    return img, max(faces, key=lambda f: f[2] * f[3])


def embed_face(rec, detector, img):
    """Aligned 128-d SFace embedding, unit-normalized. None if no face."""
    d = detect_face(img, detector)
    if d is None:
        return None
    im, face = d
    vec = rec.feature(rec.alignCrop(im, face)).flatten()
    return vec / np.linalg.norm(vec)


def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def main():
    det = cv2.FaceDetectorYN.create(DETECT, "", (320, 320), 0.6)
    rec = cv2.FaceRecognizerSF.create(SFACE, "")

    embeds = {}   # (variant, aug) -> vec; variant like "einstein" | "einstein@cam"
    n_faces = {}
    t_detect = 0.0

    for name in NAMES:
        img = cv2.imread(str(IMGS / f"{name}.jpg"))
        for tag, im in (("", img), ("@cam", cam_style(img))):
            key = name + tag
            t0 = time.perf_counter()
            vec = embed_face(rec, det, im)
            t_detect += time.perf_counter() - t0
            if vec is None:
                n_faces[key] = 0
                continue
            n_faces[key] = n_faces.get(key, 0) + 1
            embeds[key] = vec
            # same-person augs
            for aug, im2 in augment(im):
                v2 = embed_face(rec, det, im2)
                if v2 is None:
                    print(f"  MISS {key} aug={aug}: detector lost the face")
                    continue
                embeds[f"{key}|{aug}"] = v2

    print(f"latency CPU: detect+embed {t_detect * 1e3 / (2 * len(NAMES)):.1f} "
          f"ms per frame avg\n")

    print("detection (n faces found):")
    for k, v in n_faces.items():
        print(f"  {k:20s} {v}")

    print("\nsame-person cosines (base vs augs):")
    same = []
    for base in list(embeds):
        if "|" in base:
            continue
        for aug in list(embeds):
            if aug.startswith(base + "|"):
                c = cos(embeds[base], embeds[aug])
                same.append(c)
                print(f"  {base:12s} vs {aug.split('|', 1)[1]:10s} {c:.3f}")
    # also cam-base vs full-base
    print("\ncross-resolution (full vs cam):")
    xres = []
    for name in NAMES:
        if name in embeds and name + "@cam" in embeds:
            c = cos(embeds[name], embeds[name + "@cam"])
            xres.append(c)
            print(f"  {name:12s} {c:.3f}")
        else:
            print(f"  {name:12s} n/a (no detection at one resolution)")

    print("\ndifferent-person cosines (min over aug variants of each):")
    diff = []
    for i, a in enumerate(NAMES):
        for b in NAMES[i + 1:]:
            cs = [cos(x, y)
                  for xk, x in embeds.items()
                  for yk, y in embeds.items()
                  if xk.split("@")[0] == a and yk.split("@")[0] == b
                  and xk != yk]
            if cs:
                print(f"  {a:12s} vs {b:12s} min {min(cs):.3f}  ({len(cs)} pairs)")
            diff.extend(cs)

    pooled_same = same + xres
    if not diff or not pooled_same:
        print("\nsummary: incomplete (missing detections)")
        return
    print(f"\nsummary: same min {min(pooled_same):.3f}, "
          f"diff max {max(diff):.3f}")
    print(f"datasheet SFace cosine threshold: 0.363")
    print(f"measured margin: {min(min(same), min(xres)) - max(diff):.3f}")


if __name__ == "__main__":
    main()
