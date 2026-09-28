"""Hardware report + model recommendation, seeded with numbers measured on the reference machines."""
from custom_components.electrifix_plate_gate import hardware as h
from custom_components.electrifix_plate_gate.models import MODEL_CATALOGUE

K8_CONFIG = {
    "detectors": {"ov": {"type": "openvino", "device": "CPU"}, "ov_1": {"type": "openvino", "device": "CPU"}},
    "model": {"path": "/config/model_cache/yolov9-m-640.onnx", "width": 640, "height": 640, "model_type": "yolo-generic"},
    "lpr": {"enabled": True},
    "cameras": {"driveway": {"detect": {"width": 2304, "height": 1296}}},
}
K8_STATS = {
    "detectors": {"ov": {"inference_speed": 42.4, "cpu": 20.0, "mem": 1.5}, "ov_1": {"inference_speed": 42.1, "cpu": 19.0, "mem": 1.5}},
    "cameras": {"driveway": {"camera_fps": 10.0, "process_fps": 10.0, "skipped_fps": 0.0, "detection_fps": 3.0, "detect_cpu": 4.0}},
    "detection_fps": 3.0,
    "gpu_usages": {"amd-vaapi": {"gpu": "3%", "mem": "1%"}},
    "service": {"version": "0.18.0-abc", "uptime": 5000},
    "embeddings": {"plate_recognition_speed": 18.2, "yolov9_plate_detection_speed": 9.8},
}
CORAL_CONFIG = {"detectors": {"coral": {"type": "edgetpu", "device": "usb"}}, "model": {"path": "/edgetpu_model.tflite", "width": 320, "height": 320}, "cameras": {"front": {}}}
CORAL_STATS = {"detectors": {"coral": {"inference_speed": 8.1}}, "cameras": {"front": {"camera_fps": 5, "detection_fps": 2.0, "skipped_fps": 0}}, "detection_fps": 2.0, "service": {"version": "0.18.0"}}
SLOW_CONFIG = {"detectors": {"cpu1": {"type": "cpu", "num_threads": 3}}, "model": {"path": "/config/model_cache/yolov9-s-640.onnx", "width": 640, "height": 640}, "cameras": {"gate": {}}}
SLOW_STATS = {"detectors": {"cpu1": {"inference_speed": 90.0}}, "cameras": {"gate": {"camera_fps": 5, "detection_fps": 5.0, "skipped_fps": 2.0}}, "detection_fps": 5.0, "service": {"version": "0.17.0"}}


def test_report_parses_k8_shape():
    r = h.HardwareReport.from_frigate(K8_CONFIG, K8_STATS)
    assert r.frigate_version == "0.18.0-abc"
    assert [d.name for d in r.detectors] == ["ov", "ov_1"] and r.detectors[0].type == "openvino" and r.detectors[0].inference_ms == 42.4
    assert r.detection_fps == 3.0 and r.skipped_fps == 0.0
    assert r.cameras["driveway"].detect == (2304, 1296) and r.cameras["driveway"].camera_fps == 10.0
    assert r.coral_present is False and r.gpu and r.gpu[0].vendor == "amd-vaapi"
    assert r.current_model_key == "yolov9-m-640"
    assert r.lpr.enabled and r.lpr.plate_detect_ms == 9.8 and r.lpr.ocr_ms == 18.2
    assert 0.06 < r.busy_fraction < 0.07  # 3 fps × 42 ms / 1000 / 2 detectors


def test_k8_recommendation_keeps_m640_without_second_detector_nag():
    r = h.HardwareReport.from_frigate(K8_CONFIG, K8_STATS)
    recs = h.recommend(r)
    assert recs[0].kind == "model" and recs[0].model_key == "yolov9-m-640"
    assert "keep" in recs[0].why.lower()
    assert not any(rec.kind == "detectors" for rec in recs)
    assert all(rec.cost_line for rec in recs)


def test_coral_box_gets_basic():
    r = h.HardwareReport.from_frigate(CORAL_CONFIG, CORAL_STATS)
    assert r.coral_present
    recs = h.recommend(r)
    assert recs[0].kind == "basic" and "Coral" in recs[0].why and recs[0].model_key is None


def test_slow_cpu_box_gets_s320_and_second_detector():
    r = h.HardwareReport.from_frigate(SLOW_CONFIG, SLOW_STATS)
    recs = h.recommend(r)
    assert recs[0].kind == "model" and recs[0].model_key == "yolov9-s-320"
    assert any(rec.kind == "detectors" and rec.detector_count == 2 for rec in recs)
    assert "ms" in recs[0].cost_line


def test_projection_scales_from_measured_speed():
    # the box runs s-640 at 90 ms where the table says 16.8 → ratio ~5.4 → m-640 projects ~227 ms
    r = h.HardwareReport.from_frigate(SLOW_CONFIG, SLOW_STATS)
    assert 200 < h.projected_ms(r, "yolov9-m-640") < 260
    assert 25 < h.projected_ms(r, "yolov9-s-320") < 35


def test_render_report_markdown_mentions_key_facts():
    r = h.HardwareReport.from_frigate(K8_CONFIG, K8_STATS)
    text = h.render_report_markdown(r, h.recommend(r))
    for needle in ("0.18.0-abc", "openvino", "42.4", "2304x1296", "plate reader", "9.8", "yolov9-m-640"):
        assert needle in text, needle


def test_report_survives_missing_sections():
    r = h.HardwareReport.from_frigate({}, {})
    assert r.detectors == [] and r.busy_fraction == 0.0 and r.current_model_key is None
    assert h.recommend(r)[0].kind in ("model", "basic")
