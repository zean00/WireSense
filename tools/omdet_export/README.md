# OmDet ONNX export + parity (build provenance for models/omdet/)

`models/omdet/*.onnx` are gitignored; these scripts rebuild and verify them.
They run inside an OmDet workspace clone (detectron2 + torch + onnxruntime
venv), NOT in wiresense `.venv`:

1. clone github.com/om-ai-lab/OmDet, install per its README into `.venv`
   (plus `joblib requests timm regex onnxscript`), build detectron2 from git
   with `--no-build-isolation`
2. download weights into `resources/`: `ViT-B-16.pt` (CLIP) and
   `OmDet-Turbo_tiny_SWIN_T.pth`
3. `python export_onnx_cpu.py` — exports clip_text_label / clip_text_task /
   detector_enc / detector_dec into wiresense `models/omdet/` and runs
   parity stages A-E (gates: CLIP <=1e-3, encoder <=1e-3 finite parts with
   matching inf-anchor pattern, decoder <=1e-3 on identical eager-selected
   queries; selection overlap printed, 900/900)
4. copy `omdet/modeling/language_backbone/clip/models/bpe_simple_vocab_16e6.txt.gz`
   into wiresense `models/omdet/` (the sidecar's vendored tokenizer reads it)
5. `python parity_e2e.py` — stage F: detection-level parity vs the reference
   DetEngine (exact counts, IoU >= 0.9, |dconf| <= 0.05)

Why the split graphs (probe_stages.py): the stock forward's ONNX export
diverges (box_pred ~5e-01). Bisected to the in-graph top-900 query
selection in `ELADecoder._get_decoder_input` — ~1e-6 ORT-vs-torch score
noise re-ranks the selection cutoff and the (otherwise exact) decoder
amplifies the flip. Everything up to the selection and everything after it
is exact; the sidecar owns the selection in numpy.
