"""Full-pipeline stage parity: trace from x with every internal stage as an
output, on a REAL image + real ORT-computed text features (probe_stages.py
fed embed0/refer_bbox as graph inputs, which skipped _get_decoder_input)."""
import warnings

import numpy as np
import onnxruntime as ort
import torch
from PIL import Image

warnings.filterwarnings("ignore")
from export_onnx_cpu import build_model, DeployDetector, SIZE
import omdet.omdet_v2_turbo.torch_utils as TU


class Stages(DeployDetector):
    def forward(self, x, label_feats, task_feats, task_mask):
        m = self.m
        body = m.backbone(x)
        if isinstance(body, dict):
            body = [body[k] for k in body.keys()]
        enc = m.neck(body)
        feats, shapes = m.decoder._get_encoder_input(enc)

        if m.decoder.fuse_type == "merged_attn":
            if m.decoder.task_encoder is not None:
                task_feats = m.decoder.task_encoder(task_feats)
            if m.decoder.task_project is not None:
                task_feats = m.decoder.task_project(task_feats)
            bs = task_mask.shape[0]
            src_key_mask = (task_mask == 0).detach()
            attn_mask_len = m.decoder.num_queries
            fusion_size = attn_mask_len + task_feats.shape[0]
            attn_mask = torch.zeros([bs, fusion_size, fusion_size], dtype=torch.bool)
            attn_mask[:, :, attn_mask_len:] = src_key_mask.unsqueeze(1)
            attn_mask = attn_mask.repeat(m.decoder.nhead, 1, 1)

        embed, refer_bbox, _, _ = m.decoder._get_decoder_input(
            feats, shapes, label_feats, None, None)

        dec = m.decoder.decoder
        output = embed
        refer_bbox = refer_bbox.sigmoid()
        stages = {"s_feats": feats, "s_embed": embed, "s_rbbox0": refer_bbox,
                  "s_task": task_feats, "s_mask": attn_mask}
        for i, layer in enumerate(dec.layers):
            output, task_feats = layer(output, task_feats, refer_bbox, feats,
                                       shapes, None, attn_mask,
                                       m.decoder.query_pos_head(refer_bbox))
            refined_bbox = torch.sigmoid(
                m.decoder.dec_bbox_head[i](output) + TU.inverse_sigmoid(refer_bbox))
            clas_proj = m.decoder.dec_score_head[i](label_feats).permute(1, 2, 0)
            stages[f"L{i}_out"] = output
            stages[f"L{i}_rb"] = refined_bbox
            if i == dec.eval_idx:
                stages["box_cls"] = TU.cls_score(dec.cls_type, output, clas_proj,
                                                 dec.logit_scale)
                break
            refer_bbox = refined_bbox
        stages["box_pred"] = refined_bbox
        return stages


def main():
    m = build_model()
    wrapped = Stages(m).eval()

    im = Image.open("./sample_data/000000574769.jpg").convert("RGB").resize(
        (SIZE, SIZE), Image.BILINEAR)
    x = torch.as_tensor(np.ascontiguousarray(
        np.array(im).transpose(2, 0, 1))).float()[None]
    x = (x - torch.Tensor(m.cfg.MODEL.PIXEL_MEAN).view(3, 1, 1)) / \
        torch.Tensor(m.cfg.MODEL.PIXEL_STD).view(3, 1, 1)

    labels = ["person", "cat", "orange", "pizza", "car", "dog", "clock", "vase"]
    task = "Detect {}.".format(",".join(labels))
    ids = torch.from_numpy(__import__("omdet.modeling.language_backbone.clip.models.clip",
                                     fromlist=["tokenize"]).tokenize(labels + [task]).numpy())
    so = ort.SessionOptions(); so.intra_op_num_threads = 8
    lab = ort.InferenceSession(
        "/home/sahal/workspace/wiresense/models/omdet/clip_text_label.onnx", so,
        providers=["CPUExecutionProvider"])
    tsk = ort.InferenceSession(
        "/home/sahal/workspace/wiresense/models/omdet/clip_text_task.onnx", so,
        providers=["CPUExecutionProvider"])
    ort_lb = lab.run(["emb"], {"input_ids": ids[:len(labels)].numpy()})[0]
    ort_tk, ort_mask = tsk.run(["feats", "mask"], {"input_ids": ids[[-1]].numpy()})
    lf = torch.from_numpy(ort_lb[:, None, :])
    tf = torch.from_numpy(ort_tk)
    tmask = torch.from_numpy(ort_mask.astype(np.int32))

    with torch.no_grad():
        st = wrapped(x, lf, tf, tmask)
        stock = m(x, lf, tf, tmask)
    for name, ref in (("box_pred", stock[0]), ("box_cls", stock[1])):
        d = np.abs(st[name].numpy() - ref.numpy()).max()
        print(f"sanity replication vs stock {name}: max|diff|={d:.3e}")

    names = list(st.keys())
    torch.onnx.export(
        wrapped, (x, lf, tf, tmask), "/tmp/probe_full.onnx",
        input_names=["img", "label_feats", "task_feats", "task_mask"],
        output_names=names, opset_version=17, do_constant_folding=True,
        dynamo=False, keep_initializers_as_inputs=False)

    sess = ort.InferenceSession("/tmp/probe_full.onnx", so,
                                providers=["CPUExecutionProvider"])
    out = sess.run(names, {"img": x.numpy(), "label_feats": lf.numpy(),
                           "task_feats": tf.numpy(), "task_mask": tmask.numpy()})
    for name, b in zip(names, out):
        b = np.asarray(b)
        a = st[name].detach().numpy()
        if a.dtype == np.bool_:
            print(f"STAGE {name}: bool equal={np.array_equal(a, b)}")
            continue
        d = np.abs(a - b).max()
        flag = "OK " if d < 2e-4 else "DIVERGED"
        print(f"STAGE {flag} {name}: max|diff|={d:.3e}")


if __name__ == "__main__":
    main()
