"""Accuracy benchmark: run candidate YOLOv9 ONNX models over the user's own event snapshots.

Honest framing: the snapshots are frames where Frigate already saw something, so this compares
candidates against each other on the user's real scene; it is not ground truth and cannot count
what nobody saw. Each WHOLE frame is letterboxed to the model's input size: that is a harder test
than Frigate's motion-region crops (which give the detector the object at full model resolution),
so absolute counts run below Frigate's and larger inputs (640) gain relative to 320. Inference uses
onnxruntime on the Companion's CPU; the milliseconds are only indicative.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_LOGGER = logging.getLogger(__name__)
VEHICLE_CLASSES = {2, 3, 5, 7}  # coco-80: car, motorcycle, bus, truck
MIN_IMAGE_BYTES = 100  # only to skip empty/truncated writes
MAX_IMAGE_PIXELS = 40_000_000


@dataclass
class Det:
    cls: int
    score: float
    x1: float
    y1: float
    x2: float
    y2: float


def decode(output: np.ndarray, imgsz: int, conf: float = 0.5) -> list[Det]:
    """YOLOv9 (no-NMS export) output -> detections with normalised boxes."""
    arr = np.asarray(output)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.shape[0] != 84 and arr.shape[1] == 84:  # [N, 84] -> [84, N]
        arr = arr.T
    if arr.shape[0] != 84:
        raise ValueError(f"unexpected output shape {tuple(np.asarray(output).shape)}")
    boxes, scores = arr[:4], arr[4:]
    cls = scores.argmax(axis=0)
    best = scores.max(axis=0)
    keep = np.where(best >= conf)[0]
    out: list[Det] = []
    for i in keep:
        cx, cy, w, h = (float(v) for v in boxes[:, i])
        out.append(Det(int(cls[i]), float(best[i]),
                       max(0.0, (cx - w / 2) / imgsz), max(0.0, (cy - h / 2) / imgsz),
                       min(1.0, (cx + w / 2) / imgsz), min(1.0, (cy + h / 2) / imgsz)))
    return out


def _iou(a: Det, b: Det) -> float:
    ix1, iy1, ix2, iy2 = max(a.x1, b.x1), max(a.y1, b.y1), min(a.x2, b.x2), min(a.y2, b.y2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    ua = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter
    return inter / ua if ua > 0 else 0.0


def nms(dets: list[Det], iou: float = 0.45) -> list[Det]:
    """Per-class non-maximum suppression."""
    out: list[Det] = []
    for c in sorted({d.cls for d in dets}):
        cand = sorted((d for d in dets if d.cls == c), key=lambda d: d.score, reverse=True)
        kept: list[Det] = []
        for d in cand:
            if all(_iou(d, k) < iou for k in kept):
                kept.append(d)
        out.extend(kept)
    return out


def find_images(media_dir: str, camera: str, max_images: int) -> list[Path]:
    """Newest event snapshots for the camera: clips/<camera>-*-clean.webp (older Frigate: -clean.png),
    else the boxed <camera>-*.jpg snapshots."""
    clips = Path(media_dir) / "clips"
    if not clips.is_dir():
        return []
    for pattern in (f"{camera}-*-clean.webp", f"{camera}-*-clean.png", f"{camera}-*.jpg", f"{camera}-*.webp"):
        found = [p for p in clips.glob(pattern) if p.is_file() and not p.is_symlink() and p.stat().st_size >= MIN_IMAGE_BYTES]
        if found:
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return found[: max(1, int(max_images))]
    return []


def vehicle_detections(dets: list[Det], iou: float = 0.45) -> list[Det]:
    """Vehicles only, with class-agnostic NMS so one car scored as car+truck counts once (as Frigate does)."""
    veh = sorted((d for d in dets if d.cls in VEHICLE_CLASSES), key=lambda d: d.score, reverse=True)
    kept: list[Det] = []
    for d in veh:
        if all(_iou(d, k) < iou for k in kept):
            kept.append(d)
    return kept


def letterbox(rgb: np.ndarray, size: int, fill: int = 114) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize keeping the aspect ratio and pad to size×size. Returns (image, scale, (pad_x, pad_y))."""
    from PIL import Image

    h, w = rgb.shape[:2]
    scale = size / max(h, w)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    im = Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR)
    canvas = np.full((size, size, 3), fill, dtype=np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = np.asarray(im)
    return canvas, scale, (px, py)


def load_rgb(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        if im.width * im.height > MAX_IMAGE_PIXELS:
            raise ValueError("image too large")
        return np.asarray(im.convert("RGB"))


class YoloOnnx:
    """One ONNX model behind onnxruntime (CPU)."""

    def __init__(self, path: str, threads: int = 2, session_factory: Callable | None = None) -> None:
        if session_factory is None:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
            self.session = ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
        else:
            self.session = session_factory(path)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        shape = list(inp.shape)
        if len(shape) != 4 or not isinstance(shape[-1], int) or not isinstance(shape[-2], int) or shape[-1] != shape[-2]:
            raise ValueError(f"dynamic or non-square input shape {shape}; Plate Gate expects a fixed NCHW square input like YOLOv9 exports")
        self.imgsz = int(shape[-1])
        self.last_ms = 0.0

    def detect(self, rgb: np.ndarray, conf: float = 0.5) -> list[Det]:
        canvas, _scale, _pad = letterbox(rgb, self.imgsz)
        x = np.asarray(canvas, dtype=np.float32) / 255.0
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
        t0 = time.perf_counter()
        out = self.session.run(None, {self.input_name: x})[0]
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return nms(decode(out, self.imgsz, conf))


def run_benchmark(
    media_dir: str, config_dir: str, camera: str, filenames: list[str], max_images: int,
    progress_cb: Callable[[int, str], None] | None = None, session_factory: Callable | None = None,
) -> dict:
    """Per model: frames with a vehicle, vehicles counted, mean top score, mean ms.

    Images are streamed: each snapshot is decoded once and every model runs on it, so memory stays at
    one frame however many snapshots or models are chosen."""
    report = progress_cb or (lambda pct, stage: None)
    from .core import model_dir

    mdir = model_dir(config_dir)
    paths = find_images(media_dir, camera, max_images)
    clips = Path(media_dir) / "clips"
    if not paths:
        return {"camera": camera, "images_used": 0, "skipped": 0, "rows": [],
                "error": (f"No snapshots found in {clips} for camera '{camera}'. Are snapshots enabled in Frigate "
                          "(snapshots: enabled: true), and is Frigate's media folder mapped into the Companion?")}
    # load every model once
    models: dict[str, YoloOnnx] = {}
    rows: dict[str, dict] = {}
    for filename in filenames:
        path = mdir / filename
        if not path.is_file():
            rows[filename] = {"filename": filename, "ok": False, "note": "not on the Frigate machine (install it first)"}
            continue
        try:
            models[filename] = YoloOnnx(str(path), session_factory=session_factory)
        except Exception as err:  # noqa: BLE001
            rows[filename] = {"filename": filename, "ok": False, "note": f"could not load: {type(err).__name__}: {err}"}
    stats = {f: {"with_vehicle": 0, "vehicles": 0, "scores": [], "ms": [], "failed": None} for f in models}
    used = skipped = 0
    total_steps = max(1, len(paths) * max(1, len(models)))
    step = 0
    for p in paths:
        try:
            img = load_rgb(p)
        except Exception as err:  # noqa: BLE001 - one bad file must not stop the run
            _LOGGER.warning("skipping %s: %s", p.name, err)
            skipped += 1
            step += max(1, len(models))
            report(min(99, int(step * 100 / total_steps)), "skipping an unreadable snapshot")
            continue
        used += 1
        for filename, model in models.items():
            st = stats[filename]
            if st["failed"] is None:
                try:
                    dets = vehicle_detections(model.detect(img))
                    st["ms"].append(model.last_ms)
                    if dets:
                        st["with_vehicle"] += 1
                        st["vehicles"] += len(dets)
                        st["scores"].append(max(d.score for d in dets))
                except Exception as err:  # noqa: BLE001 - an incompatible model fails on the first frame
                    st["failed"] = f"not a YOLOv9-style model (expected 84 output rows): {type(err).__name__}: {err}"
            step += 1
            report(min(99, int(step * 100 / total_steps)), f"{filename}: frame {used}/{len(paths)}")
        del img
    for filename, model in models.items():
        st = stats[filename]
        if st["failed"]:
            rows[filename] = {"filename": filename, "ok": False, "note": st["failed"]}
        else:
            rows[filename] = {
                "filename": filename, "ok": True, "imgsz": model.imgsz,
                "images_with_vehicle": st["with_vehicle"], "vehicles_total": st["vehicles"],
                "mean_top_score": round(sum(st["scores"]) / len(st["scores"]), 3) if st["scores"] else None,
                "mean_ms": round(sum(st["ms"]) / len(st["ms"]), 1) if st["ms"] else None, "note": "",
            }
    report(100, "done")
    return {"camera": camera, "images_used": used, "skipped": skipped, "rows": [rows[f] for f in filenames if f in rows]}
