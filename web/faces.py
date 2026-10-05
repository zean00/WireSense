"""Face recognition capability (docs/PROTOCOL-face-recognition.md).

YuNet detection + SFace embeddings, both OpenCV Zoo (Apache-2.0), CPU
only — the GPU budget stays with the LLM. This module is pure
capability: the server decides enablement (web/config.json `face`
block), gates the tool manifest, and owns opt-in flow. Models load
lazily on first use, so a disabled server never touches cv2.

Local-only by contract: embeddings + names live in web/faces.json
(gitignored); raw frames are never persisted; nothing leaves the
machine. Probe-measured geometry on the frozen portraits: same-person
min 0.888, different-person max 0.221 — threshold 0.50 registered in
the protocol (leans toward stranger rejection).
"""
import base64
import json
import os
import threading
import time

import cv2
import numpy as np

FACES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "faces.json")
_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "models", "face")
DETECT_ONNX = os.path.join(_MODELS_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_ONNX = os.path.join(_MODELS_DIR, "face_recognition_sface_2021dec.onnx")

THRESHOLD = 0.50      # registered (protocol decision 9)
DETECT_CAP = 640      # px longest side before detection (probe finding)
MAX_FACES = 3         # recognized per frame (protocol decision 10)
DETECT_SCORE = 0.6

_lock = threading.Lock()
_det = None
_rec = None


def _sessions():
    global _det, _rec
    with _lock:
        if _det is None:
            _det = cv2.FaceDetectorYN.create(DETECT_ONNX, "", (320, 320),
                                             DETECT_SCORE)
            _rec = cv2.FaceRecognizerSF.create(SFACE_ONNX, "")
    return _det, _rec


def decode_b64(frame_b64):
    """b64 JPEG -> BGR image, longest side capped at DETECT_CAP (YuNet
    misses oversized faces; production frames are <=448 px anyway)."""
    buf = np.frombuffer(base64.b64decode(frame_b64), np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    s = min(1.0, DETECT_CAP / max(h, w))
    if s < 1.0:
        img = cv2.resize(img, (round(w * s), round(h * s)),
                         interpolation=cv2.INTER_AREA)
    return img


def detect_faces(img):
    """All faces in the ALREADY CAPPED image, largest first (boxes are
    in the capped image's coordinates — detect and align share one
    coordinate frame; probe iteration 1 measured the mixed-frame bug as
    ~0.1 cross-resolution cosine)."""
    det, _ = _sessions()
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    if faces is None:
        return []
    return sorted(faces, key=lambda f: f[2] * f[3], reverse=True)


def embed_face(rec, img, face):
    """Unit-normalized 128-d SFace embedding of an aligned crop."""
    vec = rec.feature(rec.alignCrop(img, face)).flatten()
    n = np.linalg.norm(vec)
    return vec / n if n else None


def embed_frames(frames_b64):
    """Largest-face embeddings for a list of b64 JPEGs; skips frames
    with no detectable face. Returns (vecs, n_total_faces)."""
    _, rec = _sessions()
    vecs = []
    for b64 in frames_b64:
        img = decode_b64(b64)
        if img is None:
            continue
        faces = detect_faces(img)
        if faces:
            v = embed_face(rec, img, faces[0])
            if v is not None:
                vecs.append(v)
    return vecs


def match(vec, store, threshold=THRESHOLD):
    """1-NN cosine against the store; name or None (pure, unit-tested)."""
    best, name = -1.0, None
    for cname, cvec in store.items():
        c = float(np.dot(vec, cvec))
        if c > best:
            best, name = c, cname
    return name if best >= threshold else None


def load_store():
    """{name: unit vec} from faces.json; {} when absent/unreadable."""
    try:
        raw = json.load(open(FACES_PATH))
    except (OSError, ValueError):
        return {}
    return {k: np.asarray(v["vec"], dtype=np.float32)
            for k, v in raw.items() if v.get("vec")}


def load_store_full():
    try:
        return json.load(open(FACES_PATH))
    except (OSError, ValueError):
        return {}


def save_store(store, full=None):
    """Persist {name: vec}; `full` preserves metadata (created stamps)
    for entries that survive."""
    full = full if full is not None else load_store_full()
    raw = {k: {"vec": [round(float(x), 5) for x in store[k]],
               "created": full.get(k, {}).get("created")}
           for k in sorted(store)}
    with open(FACES_PATH, "w") as f:
        json.dump(raw, f, indent=1)


def enroll(name, frames_b64):
    """Enroll `name` from up to 3 frames: >=2 must yield a face; stores
    the renormalized mean. Returns (ok, detail)."""
    vecs = embed_frames(frames_b64[:3])
    if len(vecs) < 2:
        return False, "I couldn't see a face clearly — try again in better light."
    vec = np.mean(vecs, axis=0)
    vec /= np.linalg.norm(vec)
    full = load_store_full()
    full[name] = {"vec": [float(x) for x in vec],
                  "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_store({k: np.asarray(v["vec"], dtype=np.float32)
                for k, v in full.items()}, full)
    return True, f"face saved for {name}"


def forget(name=None):
    """Delete one enrollment (name) or all (name=None). Returns
    (ok, detail) — ok False when there was nothing to forget."""
    full = load_store_full()
    if not full:
        return False, "I don't have any faces saved."
    if name and name not in full:
        return False, f"I don't have a face saved for {name}."
    keep = {k: v for k, v in full.items() if name and k != name}
    save_store({k: np.asarray(v["vec"], dtype=np.float32)
                for k, v in keep.items()}, keep)
    return True, (f"forgot {name}" if name
                  else f"forgot {len(full)} saved face(s)")


def recognize(frame_b64):
    """Vision-turn context: detect up to MAX_FACES (largest first),
    1-NN against the store. Returns {"n", "names", "ms"}; names hold
    recognized names or 'an unrecognized person' placeholders."""
    t0 = time.perf_counter()
    store = load_store()
    img = decode_b64(frame_b64)
    if img is None:
        return {"n": 0, "names": [], "ms": _ms(t0)}
    faces = detect_faces(img)[:MAX_FACES]
    names = []
    if faces and store:
        _, rec = _sessions()
        for face in faces:
            v = embed_face(rec, img, face)
            if v is None:
                continue
            names.append(match(v, store) or "an unrecognized person")
    return {"n": len(faces), "names": names, "ms": _ms(t0)}


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 1)
