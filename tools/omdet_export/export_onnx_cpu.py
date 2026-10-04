"""Export OmDet-Turbo-Tiny + CLIP text encoders to ONNX for the wiresense
detector sidecar, then verify parity torch vs onnxruntime.

Graphs (all opset 17, CPU):
  detector_enc: img[1,3,640,640], label_feats[N,1,512]
                -> enc_feats[1,8400,256]   (decoder cross-attn values)
                   sel_feats[1,8400,256]   (query-embedding source)
                   sel_bboxes[1,8400,4]    (unsigmoided cxcywh, normalized)
                   sel_scores[1,8400,N]    (cosine logits)
  detector_dec: embed[1,900,256], refer_bbox[1,900,4], enc_feats[1,8400,256],
                label_feats[N,1,512], task_feats[77,1,512], task_mask[1,77]
                -> box_pred[1,900,4], box_cls[1,900,N]
  clip_label:   input_ids[B,77] -> emb[B,512]           (EOT projection)
  clip_task:    input_ids[B,77] -> feats[77,B,512], mask[B,77]

Why split: exporting the stock forward diverges (box_pred ~5e-01). A
stage-by-stage bisect (probe_stages.py) traced it to the in-graph top-900
query selection in ELADecoder._get_decoder_input: ~1e-6 ORT-vs-torch score
noise re-ranks candidates at the selection cutoff, flipping whole queries,
and the (otherwise exact) decoder amplifies the flip. Everything up to the
selection and everything after it is exact in ONNX. So the graphs stop at
the selection boundary and the sidecar picks the top-900 queries in numpy
with deterministic semantics; parity is verified per graph (stages C, D)
and end-to-end at the detection level (tools/parity_e2e.py, stage F).
"""
import os

import numpy as np
import onnxruntime as ort
import torch

from detectron2.checkpoint import DetectionCheckpointer
from detectron2.config import get_cfg
from detectron2.engine import DefaultTrainer as Trainer
from omdet.omdet_v2_turbo.config import add_omdet_v2_turbo_config
from omdet.omdet_v2_turbo.infer_model import OmDetV2TurboInfer  # registers meta arch
from omdet.omdet_v2_turbo import torch_utils as TU
from omdet.modeling.language_backbone.clip.models import clip as clip

MODEL_ID = "OmDet-Turbo_tiny_SWIN_T"
OUT = os.environ.get("OMDET_ONNX_OUT", "/home/sahal/workspace/wiresense/models/omdet")
SIZE = int(os.environ.get("OMDET_EXPORT_SIZE", "640"))
NUM_QUERIES = 900


def build_model():
    cfg = get_cfg()
    add_omdet_v2_turbo_config(cfg)
    cfg.merge_from_file(f"configs/{MODEL_ID}.yaml")
    cfg.MODEL.META_ARCHITECTURE = "OmDetV2TurboInfer"
    cfg.MODEL.WEIGHTS = os.path.join("resources", MODEL_ID + ".pth")
    cfg.MODEL.DEVICE = "cpu"
    cfg.MODEL.DEPLOY_MODE = True
    cfg.freeze()
    model = Trainer.build_model(cfg)
    DetectionCheckpointer(model).load(cfg.MODEL.WEIGHTS)
    model.eval()
    return model


class ClipLabel(torch.nn.Module):
    def __init__(self, clip_model):
        super().__init__()
        self.m = clip_model

    def forward(self, ids):
        return self.m.forward(ids, return_tokens=False)


class ClipTask(torch.nn.Module):
    """Full-77 token features + mask (their max_len slice is data-dependent
    and un-exportable; masked padding is equivalent for cross-attention)."""

    def __init__(self, clip_model):
        super().__init__()
        self.m = clip_model

    def forward(self, ids):
        x = self.m.token_embedding(ids).type(self.m.dtype)
        x = x + self.m.positional_embedding.type(self.m.dtype)
        x = x.permute(1, 0, 2)
        x = self.m.transformer(x)
        x = x.permute(1, 0, 2)
        x = self.m.ln_final(x).type(self.m.dtype)
        mask = (ids != 0).int()
        return x.transpose(0, 1).float(), mask


class EncStage(torch.nn.Module):
    """backbone -> neck -> input_proj -> enc_output -> heads (query source)."""

    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, x, label_feats):
        m = self.m
        body = m.backbone(x)
        if isinstance(body, dict):
            body = [body[k] for k in body.keys()]
        enc = m.neck(body)
        feats, shapes = m.decoder._get_encoder_input(enc)
        anchors, valid_mask = m.decoder._generate_anchors(
            shapes, dtype=feats.dtype, device=feats.device)
        features = m.decoder.enc_output(
            torch.where(valid_mask, feats,
                        torch.tensor(0.0, dtype=torch.float32)))
        clas_proj = m.decoder.enc_score_head(label_feats).permute(1, 2, 0)
        sel_scores = TU.cls_score(m.decoder.cls_type, features, clas_proj,
                                  m.decoder.logit_scale)
        sel_bboxes = m.decoder.enc_bbox_head(features) + anchors
        return feats, features, sel_bboxes, sel_scores


class DecStage(torch.nn.Module):
    """Fusion mask + task projection + 6-layer decoder + eval heads."""

    def __init__(self, m, shapes):
        super().__init__()
        self.m = m
        self.shapes = shapes  # [[h, w], ...] baked at trace (fixed input size)

    def forward(self, embed, refer_bbox, enc_feats, label_feats,
                task_feats, task_mask):
        m = self.m
        if m.decoder.fuse_type == "merged_attn":
            if m.decoder.task_encoder is not None:
                task_feats = m.decoder.task_encoder(task_feats)
            if m.decoder.task_project is not None:
                task_feats = m.decoder.task_project(task_feats)
            bs = task_mask.shape[0]
            src_key_mask = (task_mask == 0).detach()
            attn_mask_len = m.decoder.num_queries
            fusion_size = attn_mask_len + task_feats.shape[0]
            attn_mask = torch.zeros([bs, fusion_size, fusion_size],
                                    dtype=torch.bool)
            attn_mask[:, :, attn_mask_len:] = src_key_mask.unsqueeze(1)
            attn_mask = attn_mask.repeat(m.decoder.nhead, 1, 1)

        dec = m.decoder.decoder
        output = embed
        refer_bbox = refer_bbox.sigmoid()
        shapes = self.shapes
        for i, layer in enumerate(dec.layers):
            output, task_feats = layer(output, task_feats, refer_bbox,
                                       enc_feats, shapes, None, attn_mask,
                                       m.decoder.query_pos_head(refer_bbox))
            refined_bbox = torch.sigmoid(
                m.decoder.dec_bbox_head[i](output) +
                TU.inverse_sigmoid(refer_bbox))
            clas_proj = m.decoder.dec_score_head[i](label_feats).permute(
                1, 2, 0)
            if i == dec.eval_idx:
                box_cls = TU.cls_score(dec.cls_type, output, clas_proj,
                                       dec.logit_scale)
                break
            refer_bbox = refined_bbox
        return refined_bbox, box_cls


def eager_decoder_inputs(m, x, label_feats):
    """Exact torch path through _get_decoder_input (topk selection)."""
    with torch.no_grad():
        body = m.backbone(x)
        if isinstance(body, dict):
            body = [body[k] for k in body.keys()]
        enc = m.neck(body)
        feats, shapes = m.decoder._get_encoder_input(enc)
        embed, refer_bbox, _, _ = m.decoder._get_decoder_input(
            feats, shapes, label_feats, None, None)
        return embed.detach(), refer_bbox.detach(), feats.detach(), shapes


def export(model, clip_model):
    os.makedirs(OUT, exist_ok=True)
    ids = torch.from_numpy(
        clip.tokenize(["person", "coffee mug", "orange"]).numpy())

    with torch.no_grad():
        torch.onnx.export(
            ClipLabel(clip_model).eval(), (ids,),
            f"{OUT}/clip_text_label.onnx",
            input_names=["input_ids"], output_names=["emb"],
            dynamic_axes={"input_ids": {0: "batch"}, "emb": {0: "batch"}},
            opset_version=17, do_constant_folding=True, dynamo=False)

        torch.onnx.export(
            ClipTask(clip_model).eval(), (ids,),
            f"{OUT}/clip_text_task.onnx",
            input_names=["input_ids"],
            output_names=["feats", "mask"],
            dynamic_axes={"input_ids": {0: "batch"},
                          "feats": {1: "batch"}, "mask": {0: "batch"}},
            opset_version=17, do_constant_folding=True, dynamo=False)

        img = torch.rand(1, 3, SIZE, SIZE)
        label_feats = torch.rand(8, 1, 512)
        torch.onnx.export(
            EncStage(model).eval(), (img, label_feats),
            f"{OUT}/detector_enc_{SIZE}.onnx",
            input_names=["img", "label_feats"],
            output_names=["enc_feats", "sel_feats", "sel_bboxes", "sel_scores"],
            dynamic_axes={"label_feats": {0: "num_labels"},
                          "sel_scores": {2: "num_labels"}},
            opset_version=17, do_constant_folding=True, dynamo=False)

        # real spatial shapes from a real forward, baked into the dec graph
        body = model.backbone(img)
        if isinstance(body, dict):
            body = [body[k] for k in body.keys()]
        _, shapes = model.decoder._get_encoder_input(model.neck(body))
        task_feats = torch.rand(77, 1, 512)
        task_mask = torch.ones(1, 77, dtype=torch.int32)
        embed = torch.rand(1, NUM_QUERIES, 256)
        refer_bbox = torch.rand(1, NUM_QUERIES, 4)
        torch.onnx.export(
            DecStage(model, shapes).eval(),
            (embed, refer_bbox, torch.rand(1, 8400, 256), label_feats,
             task_feats, task_mask),
            f"{OUT}/detector_dec_{SIZE}.onnx",
            input_names=["embed", "refer_bbox", "enc_feats", "label_feats",
                         "task_feats", "task_mask"],
            output_names=["box_pred", "box_cls"],
            dynamic_axes={"label_feats": {0: "num_labels"},
                          "box_cls": {2: "num_labels"}},
            opset_version=17, do_constant_folding=True, dynamo=False)
    print("exported to", OUT)


def sessions():
    so = ort.SessionOptions()
    so.intra_op_num_threads = 8
    p = lambda n: ort.InferenceSession(f"{OUT}/{n}.onnx", so,
                                       providers=["CPUExecutionProvider"])
    return {"det": None, "enc": p(f"detector_enc_{SIZE}"),
            "dec": p(f"detector_dec_{SIZE}"),
            "lab": p("clip_text_label"), "tsk": p("clip_text_task")}


def parity(model, clip_model, img_path, labels):
    """Stages A-D: per-graph parity on a real image. Stage E (end-to-end
    detections vs the reference engine) lives in tools/parity_e2e.py."""
    from PIL import Image
    im = Image.open(img_path).convert("RGB").resize((SIZE, SIZE), Image.BILINEAR)
    x = torch.as_tensor(np.ascontiguousarray(
        np.array(im).transpose(2, 0, 1))).float()[None]
    x = (x - torch.Tensor(model.cfg.MODEL.PIXEL_MEAN).view(3, 1, 1)) / \
        torch.Tensor(model.cfg.MODEL.PIXEL_STD).view(3, 1, 1)

    task = "Detect {}.".format(",".join(labels))
    ids = torch.from_numpy(clip.tokenize(labels + [task]).numpy())
    s = sessions()

    # stage A: CLIP label graph vs eager
    with torch.no_grad():
        lb_ref = ClipLabel(clip_model).eval()(ids[:len(labels)])
    ort_lb = s["lab"].run(["emb"], {"input_ids": ids[:len(labels)].numpy()})[0]
    d = np.abs(lb_ref.numpy() - ort_lb).max()
    print(f"parity clip_label N={len(labels)}: max|diff|={d:.2e}")
    assert d < 1e-3, "clip_label parity broken"

    # stage B: CLIP task graph vs eager
    with torch.no_grad():
        tk_ref, _ = ClipTask(clip_model).eval()(ids[[-1]])
    ort_tk, ort_mask = s["tsk"].run(["feats", "mask"],
                                    {"input_ids": ids[[-1]].numpy()})
    d = np.abs(tk_ref.numpy() - ort_tk).max()
    print(f"parity clip_task N={len(labels)}: max|diff|={d:.2e}")
    assert d < 1e-3, "clip_task parity broken"

    lb_in = torch.from_numpy(ort_lb[:, None, :])
    tk_in = torch.from_numpy(ort_tk)
    tm_in = torch.from_numpy(ort_mask.astype(np.int32))

    # stage C: encoder graph vs eager (same inputs both sides)
    encmod = EncStage(model).eval()
    with torch.no_grad():
        ref = [t.numpy() for t in encmod(x, lb_in)]
    ort_enc = s["enc"].run(["enc_feats", "sel_feats", "sel_bboxes", "sel_scores"],
                           {"img": x.numpy(), "label_feats": ort_lb[:, None, :]})
    for name, a, b in zip(["enc_feats", "sel_feats", "sel_bboxes", "sel_scores"],
                          ref, ort_enc):
        a, b = np.asarray(a), np.asarray(b)
        fa, fb = np.isfinite(a), np.isfinite(b)
        if not np.array_equal(fa, fb):
            raise AssertionError(f"encoder {name}: inf pattern differs")
        d = np.abs(a[fa] - b[fb]).max() if fa.any() else 0.0
        print(f"parity encoder {name}: max|diff|={d:.2e} "
              f"(+{int((~fa).sum())} inf anchors, pattern matched)")
        assert d < 1e-3, f"encoder {name} parity broken"

    # stage D: decoder graph vs eager on IDENTICAL eager-selected queries
    embed0, refer0, feats_eager, shapes_eager = eager_decoder_inputs(
        model, x, lb_in)
    decmod = DecStage(model, shapes_eager).eval()
    with torch.no_grad():
        ref = [t.numpy() for t in decmod(embed0, refer0, feats_eager, lb_in,
                                         tk_in, tm_in)]
    ort_dec = s["dec"].run(["box_pred", "box_cls"], {
        "embed": embed0.numpy(), "refer_bbox": refer0.numpy(),
        "enc_feats": feats_eager.numpy(), "label_feats": ort_lb[:, None, :],
        "task_feats": ort_tk, "task_mask": ort_mask.astype(np.int32)})
    for name, a, b in zip(["box_pred", "box_cls"], ref, ort_dec):
        d = np.abs(a - np.asarray(b)).max()
        print(f"parity decoder {name}: max|diff|={d:.2e}")
        assert d < 1e-3, f"decoder {name} parity broken"

    # stage E (informational): numpy selection vs torch topk on identical
    # ORT encoder outputs — expected to differ at a handful of cutoff ranks.
    sel_scores = ort_enc[3]
    maxsc = sel_scores[0].max(-1)
    np_top = np.argsort(-maxsc, kind="stable")[:NUM_QUERIES]
    with torch.no_grad():
        torch_top = torch.topk(torch.from_numpy(maxsc), NUM_QUERIES,
                               dim=0).indices.numpy()
    overlap = len(set(np_top.tolist()) & set(torch_top.tolist()))
    print(f"selection overlap torch-topk vs numpy: {overlap}/{NUM_QUERIES} "
          "(cutoff-rank flips expected; detection-level gate is stage F)")


if __name__ == "__main__":
    model = build_model()
    # the Infer wrapper has no language backbone — load CLIP text directly
    from omdet.modeling.language_backbone.clip.models import clip as clipmod
    clip_model, _ = clipmod.load("resources/ViT-B-16.pt",
                                 device=torch.device("cpu"), jit=False)
    clip_model.visual = None
    clip_model.logit_scale = None
    clip_model.eval()
    export(model, clip_model)
    parity(model, clip_model, "./sample_data/000000574769.jpg",
           ["person", "cat", "orange", "pizza", "car", "dog", "clock", "vase"])
    parity(model, clip_model, "./sample_data/000000574769.jpg",
           ["cup", "laptop", "banana"])
    print("PARITY OK")
