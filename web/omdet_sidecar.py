"""OmDet-Turbo-Tiny detector sidecar for wiresense — CPU onnxruntime.

Owns the split ONNX graphs exported by the omdet workspace
(models/omdet/detector_{enc,dec}_640.onnx + clip_text_{label,task}.onnx).
The top-900 query selection runs here in numpy, NOT in-graph: the in-graph
topk is the one op ORT lowers non-paritically (~1e-6 score noise re-ranks
the selection cutoff and the decoder amplifies the flip). numpy selection
is deterministic and matches torch topk 900/900 on identical scores.

HTTP (stdlib, :8011):
  GET  /health -> {"ok": true, "model": ..., "size": 640}
  POST /detect {"frame": b64 jpeg, "width": W, "height": H,
                "labels": [...], "score_threshold"?: 0.30,
                "nms_threshold"?: 0.5}
            -> {"detections": [{"label","conf","xmin","ymin","xmax","ymax"}],
                "ms": {"decode","preprocess","enc","select","dec","post","total"}}
Boxes come back in ORIGINAL frame pixels (the 640x640 square squash is
undone per-axis). Numerics: graph parity <=1.4e-04 vs torch at every stage
(excluding the deterministic selection step); end-to-end detection parity
against the reference DetEngine in tools/parity_e2e.py.
"""
import base64
import gzip
import html
import io
import json
import os
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "..", "models", "omdet")
SIZE = 640
NUM_QUERIES = 900
MAX_LABELS = 12
PIXEL_MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
PIXEL_STD = np.array([58.395, 57.12, 57.375], dtype=np.float32)

_sessions = None
_lang_lock = threading.Lock()
_lang_cache = {}  # labels tuple -> {"label_feats","task_feats","task_mask"}


# ---------------------------------------------------------------- tokenizer
# Minimal vendoring of CLIP's SimpleTokenizer (no ftfy: label words never
# hit the mojibake paths; token parity vs the reference is unit-tested).
@lru_cache()
def _bytes_to_unicode():
    bs = list(range(ord("!"), ord("~") + 1)) + \
         list(range(ord("¡"), ord("¬") + 1)) + \
         list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(2 ** 8):
        if b not in bs:
            bs.append(b)
            cs.append(2 ** 8 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))


def _load_bpe():
    path = os.path.join(MODEL_DIR, "bpe_simple_vocab_16e6.txt.gz")
    merges = gzip.open(path).read().decode("utf-8").split("\n")
    merges = merges[1:49152 - 256 - 2 + 1]
    merges = [tuple(m.split()) for m in merges]
    vocab = list(_bytes_to_unicode().values())
    vocab = vocab + [v + "</w>" for v in vocab]
    for merge in merges:
        vocab.append("".join(merge))
    vocab.extend(["<|startoftext|>", "<|endoftext|>"])
    encoder = dict(zip(vocab, range(len(vocab))))
    return encoder, dict(zip(merges, range(len(merges))))


_ENCODER, _BPE_RANKS = _load_bpe()
_BYTE_ENCODER = _bytes_to_unicode()
import regex as _re  # \p{L}/\p{N} classes, same as the reference tokenizer

_PAT = _re.compile(
    r"<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|"
    r"[\p{L}]+|[\p{N}]|[^\s\p{L}\p{N}]+", _re.IGNORECASE)
_SOT = _ENCODER["<|startoftext|>"]
_EOT = _ENCODER["<|endoftext|>"]


def _get_pairs(word):
    pairs = set()
    prev = word[0]
    for ch in word[1:]:
        pairs.add((prev, ch))
        prev = ch
    return pairs


def _bpe(token):
    word = tuple(token[:-1]) + (token[-1] + "</w>",)
    pairs = _get_pairs(word)
    if not pairs:
        return token + "</w>"
    while True:
        bigram = min(pairs, key=lambda p: _BPE_RANKS.get(p, float("inf")))
        if bigram not in _BPE_RANKS:
            break
        first, second = bigram
        new_word = []
        i = 0
        while i < len(word):
            try:
                j = word.index(first, i)
            except ValueError:
                new_word.extend(word[i:])
                break
            new_word.extend(word[i:j])
            i = j
            if word[i] == first and i < len(word) - 1 and word[i + 1] == second:
                new_word.append(first + second)
                i += 2
            else:
                new_word.append(word[i])
                i += 1
        word = tuple(new_word)
        if len(word) == 1:
            break
        pairs = _get_pairs(word)
    return " ".join(word)


def _encode_word(token):
    out = []
    for b in token.encode("utf-8"):
        out.append(_BYTE_ENCODER[b])
    return _bpe("".join(out))


def tokenize(texts, context_length=77):
    """CLIP tokenize -> int64 [B, 77], SOT/EOT framed, zero padded."""
    if isinstance(texts, str):
        texts = [texts]
    rows = []
    for text in texts:
        text = html.unescape((text or "").strip())
        tokens = [_SOT]
        for m in _PAT.findall(text.lower()):
            tokens.extend(_ENCODER[t] for t in _encode_word(m).split(" "))
        tokens.append(_EOT)
        if len(tokens) > context_length:
            raise ValueError(f"text exceeds {context_length} tokens: {text!r}")
        row = np.zeros(context_length, dtype=np.int64)
        row[:len(tokens)] = tokens
        rows.append(row)
    return np.stack(rows)


# -------------------------------------------------------------- postprocess
def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _nms_per_class(boxes, scores):
    """Greedy NMS on one class. boxes [K,4] xyxy; returns kept indices."""
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    order = np.argsort(-scores, kind="stable")
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(xx2 - xx1, 0) * np.maximum(yy2 - yy1, 0)
        iou = inter / np.maximum(areas[i] + areas[order[1:]] - inter, 1e-9)
        order = order[1:][iou <= 0.5]
    return keep


def postprocess(box_pred, box_cls, score_thresh=0.30, nms_iou=0.5,
                in_w=SIZE, in_h=SIZE, out_w=None, out_h=None):
    """Mirrors OmDetV2Turbo.inference_single_image (FOCAL path): sigmoid
    scores, top-num_proposals over the proposal x class grid, cxcywh ->
    xyxy at input scale, score filter, class-aware NMS, clip, rescale.
    Returns original-pixel int boxes sorted by score desc."""
    out_w = out_w or in_w
    out_h = out_h or in_h
    boxes = np.asarray(box_pred).reshape(-1, 4)
    n_prop = boxes.shape[0]
    scores = _sigmoid(np.asarray(box_cls).reshape(n_prop, -1))
    n_cls = scores.shape[1]

    flat = scores.reshape(-1)
    top = np.argsort(-flat, kind="stable")[:n_prop]
    sc = flat[top]
    prop = top // n_cls
    cls = top % n_cls

    cx, cy, w, h = boxes[prop].T
    x1 = np.clip((cx - w / 2) * in_w, 0, in_w)
    y1 = np.clip((cy - h / 2) * in_h, 0, in_h)
    x2 = np.clip((cx + w / 2) * in_w, 0, in_w)
    y2 = np.clip((cy + h / 2) * in_h, 0, in_h)
    bx = np.stack([x1, y1, x2, y2], 1)

    mask = sc > score_thresh
    sc, cls, bx = sc[mask], cls[mask], bx[mask]

    keep = []
    for c in np.unique(cls):
        idx = np.where(cls == c)[0]
        keep.extend(idx[_nms_per_class(bx[idx], sc[idx])].tolist())
    keep = sorted(keep, key=lambda i: -sc[i])

    sx, sy = out_w / in_w, out_h / in_h
    return [{"label": None, "conf": float(sc[i]),
             "xmin": int(bx[i, 0] * sx), "ymin": int(bx[i, 1] * sy),
             "xmax": int(bx[i, 2] * sx), "ymax": int(bx[i, 3] * sy),
             "class_idx": int(cls[i])} for i in keep]


def name_labels(dets, labels):
    for d in dets:
        d["label"] = labels[d.pop("class_idx")]
    return dets


# ---------------------------------------------------------------- pipeline
def get_sessions():
    global _sessions
    if _sessions is None:
        so = ort.SessionOptions()
        so.intra_op_num_threads = 8
        p = lambda n: ort.InferenceSession(
            os.path.join(MODEL_DIR, n), so,
            providers=["CPUExecutionProvider"])
        _sessions = {
            "enc": p(f"detector_enc_{SIZE}.onnx"),
            "dec": p(f"detector_dec_{SIZE}.onnx"),
            "lab": p("clip_text_label.onnx"),
            "tsk": p("clip_text_task.onnx"),
        }
    return _sessions


def language_inputs(labels):
    """label_feats [N,1,512], task_feats [77,1,512], task_mask [1,77],
    cached per label tuple (deterministic; no LLM anywhere downstream)."""
    key = tuple(labels)
    with _lang_lock:
        hit = _lang_cache.get(key)
    if hit is not None:
        return hit
    s = get_sessions()
    task = "Detect {}.".format(",".join(labels))
    ids = tokenize(labels + [task])
    lb = s["lab"].run(["emb"], {"input_ids": ids[:-1]})[0][:, None, :]
    tf, tm = s["tsk"].run(["feats", "mask"], {"input_ids": ids[[-1]]})
    out = {"label_feats": lb.astype(np.float32),
           "task_feats": tf.astype(np.float32),
           "task_mask": tm.astype(np.int32)}
    with _lang_lock:
        _lang_cache[key] = out
    return out


def detect(frame_b64, width, height, labels, score_threshold=0.30,
           nms_threshold=0.5):
    """Full pipeline; returns (detections, ms dict)."""
    t0 = time.perf_counter()
    raw = base64.b64decode(frame_b64)
    im = Image.open(io.BytesIO(raw)).convert("RGB").resize(
        (SIZE, SIZE), Image.BILINEAR)
    t1 = time.perf_counter()

    x = np.asarray(im, dtype=np.float32).transpose(2, 0, 1)[None]
    x = (x - PIXEL_MEAN[:, None, None]) / PIXEL_STD[:, None, None]
    x = np.ascontiguousarray(x)
    lang = language_inputs(labels)
    t2 = time.perf_counter()

    s = get_sessions()
    enc_feats, sel_feats, sel_bboxes, sel_scores = s["enc"].run(
        ["enc_feats", "sel_feats", "sel_bboxes", "sel_scores"],
        {"img": x, "label_feats": lang["label_feats"]})
    t3 = time.perf_counter()

    maxsc = sel_scores[0].max(-1)
    top = np.argsort(-maxsc, kind="stable")[:NUM_QUERIES]
    embed = np.ascontiguousarray(sel_feats[0][top][None])
    refer = np.ascontiguousarray(sel_bboxes[0][top][None])
    t4 = time.perf_counter()

    box_pred, box_cls = s["dec"].run(
        ["box_pred", "box_cls"],
        {"embed": embed, "refer_bbox": refer, "enc_feats": enc_feats,
         "label_feats": lang["label_feats"], "task_feats": lang["task_feats"],
         "task_mask": lang["task_mask"]})
    t5 = time.perf_counter()

    dets = name_labels(postprocess(box_pred, box_cls, score_threshold,
                                   nms_threshold, out_w=width, out_h=height),
                       labels)
    t6 = time.perf_counter()
    ms = {"decode": round((t1 - t0) * 1000, 1),
          "preprocess": round((t2 - t1) * 1000, 1),
          "enc": round((t3 - t2) * 1000, 1),
          "select": round((t4 - t3) * 1000, 1),
          "dec": round((t5 - t4) * 1000, 1),
          "post": round((t6 - t5) * 1000, 1),
          "total": round((t6 - t0) * 1000, 1)}
    return dets, ms


# -------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "model": "OmDet-Turbo_tiny_SWIN_T",
                             "size": SIZE, "queries": NUM_QUERIES})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/detect":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n))
            labels = req["labels"]
            if not labels or len(labels) > MAX_LABELS:
                return self._json(400, {"error": f"1..{MAX_LABELS} labels"})
            dets, ms = detect(req["frame"], int(req.get("width", SIZE)),
                              int(req.get("height", SIZE)),
                              list(labels),
                              float(req.get("score_threshold", 0.30)),
                              float(req.get("nms_threshold", 0.5)))
            self._json(200, {"detections": dets, "ms": ms})
        except Exception as e:  # noqa: BLE001 — surface to client, keep serving
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("OMDET_PORT", "8011"))
    get_sessions()
    print(f"omdet sidecar on :{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
