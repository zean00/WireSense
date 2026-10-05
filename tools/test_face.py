"""Offline face-recognition gates (docs/PROTOCOL-face-recognition.md,
G1-G4) against the frozen public-domain portraits, through the
PRODUCTION web/faces.py path (decode_b64 -> detect -> align -> embed ->
match). No live server needed:

    make test-face

G1 detection: all 6 frozen renditions (3 portraits x full/cam) >= 1 face
G2 same-person: every same-person pair >= 0.50 (registered threshold)
G3 stranger rejection: every different-person pair < 0.50
G4 decision + persistence + opt-in contract: match() thresholds,
   faces.json round-trip, enroll/forget semantics, multi-face, manifest
   gating (disabled = tools absent + validation refuses)

Exits 1 on any gate failure. Prints measured geometry for the protocol
Results section.
"""
import base64
import os
import sys
import tempfile
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import faces  # production module under test

IMGS = os.path.join(ROOT, "tools", "face_eval", "imgs")
NAMES = ["einstein", "tesla", "curie"]
THR = faces.THRESHOLD

failures = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def b64img(img, q=80):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    assert ok
    return base64.b64encode(buf).decode()


def cam_style(img, cap=448, q=80):
    h, w = img.shape[:2]
    s = cap / max(h, w)
    if s < 1.0:
        img = cv2.resize(img, (round(w * s), round(h * s)),
                         interpolation=cv2.INTER_AREA)
    return cv2.imdecode(cv2.imencode(".jpg", img,
                                     [cv2.IMWRITE_JPEG_QUALITY, q])[1], 1)


def augment(img):
    h, w = img.shape[:2]
    yield "hflip", cv2.flip(img, 1)
    yield "jpeg_q30", cv2.imdecode(
        cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 30])[1], 1)
    m = round(0.1 * min(h, w))
    yield "crop10", img[m:h - m, m:w - m]
    yield "scale0.6", cv2.resize(img, (round(w * 0.6), round(h * 0.6)),
                                 interpolation=cv2.INTER_AREA)


def main() -> int:
    store_bak = faces.FACES_PATH
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    faces.FACES_PATH = tmp.name  # tests never touch the real store
    try:
        run_gates()
    finally:
        faces.FACES_PATH = store_bak
        os.unlink(tmp.name)
    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURES'}")
    return 1 if failures else 0


def run_gates():
    raw = {n: cv2.imread(os.path.join(IMGS, f"{n}.jpg")) for n in NAMES}
    rend = {}
    for n in NAMES:
        rend[(n, "full")] = raw[n]
        rend[(n, "cam")] = cam_style(raw[n])

    # --- G1: detection on all frozen renditions (production path)
    print("G1 detection (6 frozen renditions):")
    for (n, tag), im in rend.items():
        dimg = faces.decode_b64(b64img(im))
        nf = len(faces.detect_faces(dimg))
        check(nf >= 1, f"{n}/{tag}: {nf} face(s) detected")

    # --- G2/G3: geometry through embed_frames (largest face per frame)
    print(f"G2/G3 geometry (threshold {THR}):")
    vecs = {}
    for (n, tag), im in rend.items():
        vs = faces.embed_frames([b64img(im)])
        check(len(vs) == 1, f"{n}/{tag}: exactly the largest face embedded")
        vecs[(n, tag)] = vs[0]
        for aug, im2 in augment(im):
            v2 = faces.embed_frames([b64img(im2)])
            if v2:
                vecs[(n, tag, aug)] = v2[0]

    same, diff = [], []
    for k1, v1 in vecs.items():
        for k2, v2 in vecs.items():
            if k1 >= k2:
                continue
            c = float(np.dot(v1, v2))
            if k1[0] == k2[0]:
                same.append((c, k1, k2))
            else:
                diff.append((c, k1, k2))
    worst_same = min(same)
    worst_diff = max(diff)
    for c, k1, k2 in same:
        check(c >= THR, f"same {k1}/{k2}: {c:.3f} >= {THR}")
    for c, k1, k2 in diff:
        check(c < THR, f"diff {k1}~{k2}: {c:.3f} < {THR}")
    print(f"  measured: same min {worst_same[0]:.3f}, "
          f"diff max {worst_diff[0]:.3f}, "
          f"margin {worst_same[0] - worst_diff[0]:.3f}")

    # --- G4: decision, persistence, enrollment, multi-face, opt-in
    print("G4 decision + persistence + opt-in contract:")
    v = np.ones(128, dtype=np.float32)
    v /= np.linalg.norm(v)
    store = {"albert": v, "bob": -v}
    check(faces.match(v, store) == "albert", "match: 1.0 -> albert")
    u = np.concatenate([np.ones(64), -np.ones(64)]).astype(np.float32)
    u /= np.linalg.norm(u)          # orthogonal to v: exact 0.3 cosine
    below = 0.3 * v + np.sqrt(1 - 0.09) * u
    c = float(np.dot(below, v))
    check(c < THR and faces.match(below, store) is None,
          f"match: cosine {c:.3f} below threshold -> None")
    check(faces.match(v, {}) is None, "match: empty store -> None")

    cam_einstein = b64img(rend[("einstein", "cam")])
    ok, detail = faces.enroll("albert", [cam_einstein] * 3)
    check(ok, f"enroll albert: {detail}")
    st = faces.load_store()
    check(set(st) == {"albert"} and st["albert"].shape == (128,),
          "store round-trips: albert, 128-d")
    got = faces.match(faces.embed_frames([cam_einstein])[0], st)
    check(got == "albert", f"recognize enrolled frame -> {got}")
    ok, detail = faces.forget("albert")
    check(ok and not faces.load_store(), f"forget albert: {detail}")
    ok, detail = faces.forget()
    check(not ok, f"forget on empty store honestly refuses: {detail}")

    # multi-face: einstein + curie side by side; only albert enrolled
    faces.enroll("albert", [cam_einstein] * 3)
    duo = np.hstack([rend[("einstein", "cam")], rend[("curie", "cam")]])
    ctx = faces.recognize(b64img(duo))
    check(ctx["n"] == 2, f"multi-face frame: {ctx['n']} faces "
          f"(names {ctx['names']}, {ctx['ms']} ms)")
    check(ctx["names"][0] == "albert"
          and "unrecognized" in ctx["names"][1],
          f"largest face recognized, other flagged: {ctx['names']}")
    check(ctx["ms"] < 200, f"recognize latency {ctx['ms']} ms < 200 offline")
    faces.forget()

    # opt-in contract at the server layer
    import server
    check("face_enroll" not in server.tool_manifest_directive(),
          "disabled: manifest omits face tools")
    try:
        server.validate_tool("face_enroll", {"name": "albert"})
        check(False, "disabled: validate_tool should refuse")
    except ValueError:
        check(True, "disabled: validate_tool refuses face_enroll")
    server.FACE_ENABLED = True
    try:
        check("face_enroll" in server.tool_manifest_directive(),
              "enabled: manifest offers face tools")
        a = server.validate_tool("face_enroll", {"name": "x" * 50})
        check(len(a["name"]) <= 40, "enabled: overlong name clamped")
        try:
            server.validate_tool("face_enroll", {"name": ""})
            check(False, "enabled: empty name should refuse")
        except ValueError:
            check(True, "enabled: empty name refuses")
        a = server.validate_tool("face_forget", {})
        check(a == {"name": ""}, "enabled: face_forget defaults to all")
    finally:
        server.FACE_ENABLED = False
    # vision-branch gate regex (Rev 1): face-management phrasings must
    # match; ordinary vision talk and past-tense lookalikes must not
    rx = server.FACE_INTENT_RE
    for yes in ("Remember my face.", "Forget my face.",
                "Forget everything you know about my face.",
                "learn my face as Albert", "delete my face please",
                "Save my face for next time"):
        check(bool(rx.search(yes)), f"gate matches {yes!r}")
    for no in ("What do you think about my new glasses?",
               "What do you see right now?",
               "I forgot my face mask at home",
               "Look at my face — is something wrong?",
               "How do I look?"):
        check(not rx.search(no), f"gate does not match {no!r}")


if __name__ == "__main__":
    sys.exit(main())
