"""Tests for the pure Frigate config writer (ruamel round-trip; comments must survive)."""
import pytest

from custom_components.electrifix_plate_gate import config_writer as w
from custom_components.electrifix_plate_gate.plates import parse_people, variant_pattern

SAMPLE = """\
# Example Frigate config (sample for tests)
mqtt:
  host: 192.0.2.112   # the HA box
  topic_prefix: frigate

detectors:
  ov:
    type: openvino
    device: CPU
  coral:
    type: edgetpu
    device: usb

model:
  path: /config/model_cache/yolov9-s-320.onnx
  width: 320
  height: 320
  input_tensor: nchw
  input_dtype: float
  model_type: yolo-generic
  labelmap_path: /labelmap/coco-80.txt

lpr:
  enabled: false
  known_plates:
    Neighbour:
      - ABC123   # keep me

cameras:
  driveway:
    ffmpeg:
      inputs:
        - path: rtsp://user:pw@192.0.2.40/stream1
          roles: [detect, record]
    detect:
      width: 1920
      height: 1080
      fps: 10
    zones:
      driveway_approach:   # yellow zone, stops at the kerb
        coordinates: 0.1,0.9,0.9,0.9,0.9,0.5,0.1,0.5
    motion:
      mask:
        - 0,0,0.3,0,0.3,0.1,0,0.1
  yard:
    ffmpeg:
      inputs:
        - path: rtsp://user:pw@192.0.2.2/yard
          roles: [detect]
    detect:
      fps: 5
"""

PEOPLE = parse_people("Bonnie: NO860")
M640 = w.ModelChoice(
    key="yolov9-m-640", family="yolov9", size="m", imgsz=640, filename="yolov9-m-640.onnx",
    url="https://example.invalid/yolov9-m-640.onnx", sha256=None, licence="GPL-3.0",
    source_weights_url="https://github.com/WongKinYiu/yolov9/releases/tag/v0.1", recipe_url="https://example.invalid/recipe",
)


def _plan(**kw):
    return w.plan_change(SAMPLE, w.Desired(people=PEOPLE, camera="driveway", **kw))


def test_lpr_block_added_and_other_people_kept():
    ch = _plan(match_distance=0)
    assert not ch.is_noop
    new = w.load_yaml(ch.new_yaml)
    assert new["lpr"]["enabled"] is True
    assert new["lpr"]["match_distance"] == 0
    assert new["lpr"]["known_plates"]["Bonnie"] == [variant_pattern("NO860")]
    assert new["lpr"]["known_plates"]["Neighbour"] == ["ABC123"]
    assert "debug_save_plates" not in new["lpr"]
    assert "+  enabled: true" in ch.diff and "-  enabled: false" in ch.diff
    assert any("plate reader" in s.lower() or "lpr" in s.lower() for s in ch.summary)


def test_comments_and_untouched_sections_survive():
    ch = _plan()
    for text in (
        "# Example Frigate config (sample for tests)",
        "host: 192.0.2.112   # the HA box",
        "- ABC123   # keep me",
        "driveway_approach:   # yellow zone, stops at the kerb",
        "coordinates: 0.1,0.9,0.9,0.9,0.9,0.5,0.1,0.5",
        "- 0,0,0.3,0,0.3,0.1,0,0.1",
        "roles: [detect, record]",
    ):
        assert text in ch.new_yaml, text


def test_debug_save_plates_set_and_removed():
    ch = _plan(debug_save_plates=True)
    assert w.load_yaml(ch.new_yaml)["lpr"]["debug_save_plates"] is True
    again = w.plan_change(ch.new_yaml, w.Desired(people=PEOPLE, camera="driveway", debug_save_plates=False))
    assert "debug_save_plates" not in w.load_yaml(again.new_yaml)["lpr"]


def test_detect_native_removes_both_keys_only_on_that_camera():
    ch = _plan(detect_native=True)
    new = w.load_yaml(ch.new_yaml)
    assert "width" not in new["cameras"]["driveway"]["detect"] and "height" not in new["cameras"]["driveway"]["detect"]
    assert new["cameras"]["driveway"]["detect"]["fps"] == 10
    assert any("native" in s for s in ch.summary)
    yard = w.plan_change(SAMPLE, w.Desired(people=PEOPLE, camera="yard", detect_native=True))
    assert not any("native" in s for s in yard.summary)


def test_model_and_detectors_replace_only_openvino_keep_coral_with_warning():
    ch = _plan(model=M640, detector_count=2)
    new = w.load_yaml(ch.new_yaml)
    assert new["model"] == {
        "path": "/config/model_cache/plate_gate/yolov9-m-640.onnx", "width": 640, "height": 640,
        "input_tensor": "nchw", "input_dtype": "float", "model_type": "yolo-generic", "labelmap_path": "/labelmap/coco-80.txt",
    }
    assert new["detectors"]["ov"] == {"type": "openvino", "device": "CPU"}
    assert new["detectors"]["ov_1"] == {"type": "openvino", "device": "CPU"}
    assert new["detectors"]["coral"] == {"type": "edgetpu", "device": "usb"}
    assert any("Coral" in wn for wn in ch.warnings)


def test_detector_count_alone_keeps_model():
    ch = _plan(detector_count=1)
    new = w.load_yaml(ch.new_yaml)
    assert new["model"]["path"] == "/config/model_cache/yolov9-s-320.onnx"
    assert list(k for k, v in new["detectors"].items() if v["type"] == "openvino") == ["ov"]


def test_noop_when_nothing_changes():
    first = _plan()
    second = w.plan_change(first.new_yaml, w.Desired(people=PEOPLE, camera="driveway"))
    assert second.is_noop and second.diff == "" and second.summary == []


def test_config_without_lpr_or_detectors_sections():
    raw = "mqtt:\n  host: x\ncameras:\n  driveway:\n    ffmpeg:\n      inputs: []\n"
    ch = w.plan_change(raw, w.Desired(people=PEOPLE, camera="driveway", model=M640, detector_count=2))
    new = w.load_yaml(ch.new_yaml)
    assert new["lpr"]["enabled"] is True and new["detectors"]["ov"]["type"] == "openvino" and new["model"]["width"] == 640


def test_bad_yaml_raises():
    with pytest.raises(w.WriterError):
        w.plan_change("cameras: [\n  : : :", w.Desired(people=PEOPLE, camera="driveway"))


def test_never_touches_other_sections():
    ch = _plan(model=M640, detector_count=2, detect_native=True, debug_save_plates=True)
    old, new = w.load_yaml(SAMPLE), w.load_yaml(ch.new_yaml)
    for key in ("mqtt",):
        assert old[key] == new[key]
    assert old["cameras"]["driveway"]["zones"] == new["cameras"]["driveway"]["zones"]
    assert old["cameras"]["driveway"]["motion"] == new["cameras"]["driveway"]["motion"]
    assert old["cameras"]["yard"] == new["cameras"]["yard"]
