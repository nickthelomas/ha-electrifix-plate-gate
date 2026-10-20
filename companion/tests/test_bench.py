"""Accuracy benchmark: decode, NMS, image discovery, runner (fake session), real-model smoke."""
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from plate_gate_companion import bench

MODELS_DIST = Path(__file__).resolve().parents[2] / "models-dist"


def raw(n, dets, layout="c_first"):
    """Build a YOLOv9 output [1,84,n] (or [1,n,84]) with the given (cls, score, cx, cy, w, h) in pixels of a 320 grid."""
    out = np.zeros((84, n), dtype=np.float32)
    for i, (cls, score, cx, cy, w, h) in enumerate(dets):
        out[0:4, i] = (cx, cy, w, h)
        out[4 + cls, i] = score
    return out[None] if layout == "c_first" else out.T[None]


@pytest.mark.parametrize("layout", ["c_first", "n_first"])
def test_decode_both_layouts_and_class_filter(layout):
    out = raw(2100, [(2, 0.9, 160, 160, 100, 60), (0, 0.95, 50, 50, 20, 40), (7, 0.3, 200, 200, 50, 50)], layout)
    dets = bench.decode(out, imgsz=320, conf=0.5)
    assert [(d.cls, round(d.score, 2)) for d in dets] == [(2, 0.9), (0, 0.95)]  # 0.3 truck below threshold
    car = dets[0]
    assert abs(car.x1 - (160 - 50) / 320) < 1e-6 and abs(car.y2 - (160 + 30) / 320) < 1e-6
    vehicles = [d for d in dets if d.cls in bench.VEHICLE_CLASSES]
    assert len(vehicles) == 1


def test_nms_merges_overlapping_same_class_only():
    out = raw(8400, [(2, 0.9, 300, 300, 200, 100), (2, 0.8, 310, 305, 200, 100), (7, 0.7, 310, 305, 200, 100), (2, 0.85, 600, 100, 50, 50)])
    dets = bench.nms(bench.decode(out, imgsz=640), iou=0.45)
    assert sorted((d.cls, round(d.score, 2)) for d in dets) == [(2, 0.85), (2, 0.9), (7, 0.7)]


def test_find_images_newest_first_webp_before_jpg_and_skips_tiny(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    for i, name in enumerate(["driveway-1.0-a-clean.webp", "driveway-2.0-b-clean.webp", "driveway-3.0-c.jpg", "yard-9.0-z-clean.webp"]):
        p = clips / name
        p.write_bytes(b"x" * 5000)
        t = 1_700_000_000 + i
        import os
        os.utime(p, (t, t))
    (clips / "driveway-4.0-d-clean.webp").write_bytes(b"tiny")
    got = [p.name for p in bench.find_images(str(tmp_path), "driveway", 10)]
    assert got == ["driveway-2.0-b-clean.webp", "driveway-1.0-a-clean.webp"]
    (clips / "driveway-1.0-a-clean.webp").unlink()
    (clips / "driveway-2.0-b-clean.webp").unlink()
    assert [p.name for p in bench.find_images(str(tmp_path), "driveway", 10)] == ["driveway-3.0-c.jpg"]  # fallback
    assert bench.find_images(str(tmp_path / "nope"), "driveway", 10) == []


class FakeSession:
    """Looks like an onnxruntime session: fixed 320 input, returns a car for 'car' images, nothing otherwise."""

    def __init__(self, path):
        self.path = path
        self.calls = 0

    def get_inputs(self):
        class I:
            name = "images"
            shape = [1, 3, 320, 320]
        return [I()]

    def run(self, _outs, feeds):
        self.calls += 1
        x = feeds["images"]
        assert x.shape == (1, 3, 320, 320) and x.dtype == np.float32 and 0.0 <= float(x.max()) <= 1.0
        bright = float(x.mean()) > 0.5
        dets = [(2, 0.88, 160, 160, 120, 80)] if bright else []
        return [raw(2100, dets)]


def _write_images(clips: Path, n_bright=3, n_dark=2):
    clips.mkdir(parents=True, exist_ok=True)
    for i in range(n_bright):
        Image.new("RGB", (640, 360), (240, 240, 240)).save(clips / f"cam-{i}.0-b{i}-clean.webp")
    for i in range(n_dark):
        Image.new("RGB", (640, 360), (5, 5, 5)).save(clips / f"cam-9{i}.0-d{i}-clean.webp")


def test_run_benchmark_with_fake_session(tmp_path):
    clips = tmp_path / "media" / "clips"
    _write_images(clips)
    mdir = tmp_path / "cfg" / "model_cache" / "plate_gate"
    mdir.mkdir(parents=True)
    (mdir / "a.onnx").write_bytes(b"fake")
    seen = []
    res = bench.run_benchmark(str(tmp_path / "media"), str(tmp_path / "cfg"), "cam", ["a.onnx", "missing.onnx"], 10,
                              progress_cb=lambda pct, stage: seen.append(pct), session_factory=FakeSession)
    assert res["images_used"] == 5
    a, missing = res["rows"]
    assert a["ok"] and a["filename"] == "a.onnx" and a["images_with_vehicle"] == 3 and a["vehicles_total"] == 3
    assert abs(a["mean_top_score"] - 0.88) < 1e-6 and a["mean_ms"] >= 0
    assert not missing["ok"] and "not on" in missing["note"].lower() or "missing" in missing["note"].lower()
    assert seen and seen[-1] == 100


def test_corrupt_image_is_skipped_with_a_note(tmp_path):
    clips = tmp_path / "media" / "clips"
    _write_images(clips, n_bright=1, n_dark=0)
    (clips / "cam-5.0-x-clean.webp").write_bytes(b"\xff" * 4000)  # not an image
    mdir = tmp_path / "cfg" / "model_cache" / "plate_gate"
    mdir.mkdir(parents=True)
    (mdir / "a.onnx").write_bytes(b"fake")
    res = bench.run_benchmark(str(tmp_path / "media"), str(tmp_path / "cfg"), "cam", ["a.onnx"], 10, session_factory=FakeSession)
    assert res["images_used"] == 1 and res["skipped"] == 1 and res["rows"][0]["images_with_vehicle"] == 1


@pytest.mark.skipif(not (MODELS_DIST / "yolov9-t-320.onnx").exists(), reason="real model not built here")
def test_real_model_smoke(tmp_path):
    clips = tmp_path / "media" / "clips"
    clips.mkdir(parents=True)
    rng = np.random.default_rng(1)
    Image.fromarray(rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)).save(clips / "cam-1.0-a-clean.webp")
    mdir = tmp_path / "cfg" / "model_cache" / "plate_gate"
    mdir.mkdir(parents=True)
    (mdir / "yolov9-t-320.onnx").write_bytes((MODELS_DIST / "yolov9-t-320.onnx").read_bytes())
    t0 = time.time()
    res = bench.run_benchmark(str(tmp_path / "media"), str(tmp_path / "cfg"), "cam", ["yolov9-t-320.onnx"], 5)
    row = res["rows"][0]
    assert row["ok"] and res["images_used"] == 1 and row["mean_ms"] > 0 and row["imgsz"] == 320
    assert time.time() - t0 < 60


# ---- review fixes ------------------------------------------------------------------
def test_images_are_decoded_once_and_streamed(tmp_path, monkeypatch):
    """C1: memory must stay O(1 image): each snapshot is decoded once, however many models run."""
    clips = tmp_path / "media" / "clips"
    _write_images(clips, n_bright=4, n_dark=1)
    mdir = tmp_path / "cfg" / "model_cache" / "plate_gate"
    mdir.mkdir(parents=True)
    for n in ("a.onnx", "b.onnx", "c.onnx"):
        (mdir / n).write_bytes(b"fake")
    calls = {"n": 0}
    real = bench.load_rgb

    def counting(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(bench, "load_rgb", counting)
    pcts = []
    res = bench.run_benchmark(str(tmp_path / "media"), str(tmp_path / "cfg"), "cam", ["a.onnx", "b.onnx", "c.onnx"], 10,
                              progress_cb=lambda pct, stage: pcts.append(pct), session_factory=FakeSession)
    assert calls["n"] == 5 and res["images_used"] == 5
    assert [r["images_with_vehicle"] for r in res["rows"]] == [4, 4, 4]
    assert pcts == sorted(pcts) and pcts[-1] == 100


def test_zero_snapshots_is_a_failure_not_a_result(tmp_path):
    (tmp_path / "media" / "clips").mkdir(parents=True)
    mdir = tmp_path / "cfg" / "model_cache" / "plate_gate"
    mdir.mkdir(parents=True)
    (mdir / "a.onnx").write_bytes(b"fake")
    res = bench.run_benchmark(str(tmp_path / "media"), str(tmp_path / "cfg"), "cam", ["a.onnx"], 10, session_factory=FakeSession)
    assert res["images_used"] == 0 and res["rows"] == [] and "error" in res
    assert "clips" in res["error"] and "snapshots" in res["error"].lower()


class WrongShapeSession(FakeSession):
    def run(self, _outs, feeds):
        return [np.zeros((1, 5, 2100), dtype=np.float32)]  # a single-class plate model, not YOLOv9-80


class DynamicSession(FakeSession):
    def get_inputs(self):
        class I:
            name = "images"
            shape = ["batch", 3, "height", "width"]
        return [I()]


def test_incompatible_models_are_reported_not_counted_as_zero(tmp_path):
    clips = tmp_path / "media" / "clips"
    _write_images(clips, n_bright=2, n_dark=0)
    mdir = tmp_path / "cfg" / "model_cache" / "plate_gate"
    mdir.mkdir(parents=True)
    (mdir / "plate.onnx").write_bytes(b"fake")
    (mdir / "dyn.onnx").write_bytes(b"fake")
    res = bench.run_benchmark(str(tmp_path / "media"), str(tmp_path / "cfg"), "cam", ["plate.onnx"], 10, session_factory=WrongShapeSession)
    assert res["rows"][0]["ok"] is False and "84" in res["rows"][0]["note"]
    res = bench.run_benchmark(str(tmp_path / "media"), str(tmp_path / "cfg"), "cam", ["dyn.onnx"], 10, session_factory=DynamicSession)
    assert res["rows"][0]["ok"] is False and "dynamic" in res["rows"][0]["note"].lower()


def test_letterbox_keeps_aspect_ratio():
    """I2: a 2:1 frame is padded to a square, not squashed."""
    img = np.full((100, 200, 3), 255, dtype=np.uint8)
    x, scale, pad = bench.letterbox(img, 320)
    assert x.shape == (320, 320, 3) and abs(scale - 1.6) < 1e-6
    assert pad == (0, 80)  # 80 px of padding top and bottom
    assert x[0:80].max() == 114 and x[80:240].min() == 255 and x[240:].max() == 114


def test_nms_is_class_agnostic_across_vehicle_classes():
    out = raw(8400, [(2, 0.9, 300, 300, 200, 100), (7, 0.8, 305, 302, 200, 100)])  # same vehicle, car vs truck
    dets = bench.vehicle_detections(bench.decode(out, imgsz=640))
    assert len(dets) == 1 and dets[0].cls == 2


def test_find_images_prefers_clean_png_over_boxed_jpg(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "cam-1.0-a-clean.png").write_bytes(b"x" * 500)
    (clips / "cam-1.0-a.jpg").write_bytes(b"x" * 500)
    assert [p.name for p in bench.find_images(str(tmp_path), "cam", 5)] == ["cam-1.0-a-clean.png"]
