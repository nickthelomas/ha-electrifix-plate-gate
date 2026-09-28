"""Pure Frigate config editor: minimal changes, comments preserved, diff produced.

No Home Assistant imports. Uses ruamel.yaml's round-trip mode so the user's comments,
ordering and quoting survive. Only these keys are ever touched:
``lpr.enabled / known_plates / match_distance / debug_save_plates``,
``cameras.<camera>.detect.width/height`` (removed = native resolution, Frigate 0.18),
the root ``model:`` block and ``detectors:`` of type openvino/onnx/cpu.
"""
from __future__ import annotations

import copy
import difflib
import io
import re
from dataclasses import dataclass, field

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from .plates import Person, patterns_for

DEFAULT_MODEL_DIR = "/config/model_cache/plate_gate"
CPU_DETECTOR_TYPES = ("openvino", "onnx", "cpu")


class WriterError(Exception):
    """The config text could not be parsed as YAML."""


@dataclass(frozen=True)
class ModelChoice:
    """A detection model we know how to configure (and, when hosted, where to fetch it)."""

    key: str
    family: str
    size: str
    imgsz: int
    filename: str
    url: str
    sha256: str | None
    licence: str
    source_weights_url: str
    recipe_url: str


@dataclass
class Desired:
    """What the user asked for."""

    people: list[Person]
    match_distance: int = 0
    debug_save_plates: bool = False
    camera: str = ""
    detect_native: bool = False
    model: ModelChoice | None = None
    detector_count: int | None = None
    model_dir: str = DEFAULT_MODEL_DIR
    touch_lpr: bool = True  # False = benchmark: leave the plate-reader settings alone


@dataclass
class Change:
    """Old and new text, a unified diff, and human summaries."""

    old_yaml: str
    new_yaml: str
    diff: str
    summary: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.old_yaml == self.new_yaml


def sniff_indent(text: str) -> tuple[int, int, int]:
    """(mapping, sequence, offset) as the user's file is written, so a small edit stays small.

    ``- item`` under its key (offset 0) is the most common Frigate style; ruamel's default would
    re-indent every list in the file."""
    mapping, sequence, offset = 2, 2, 0
    lines = text.splitlines()
    parent_indent = None
    found_map = found_seq = False
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.lstrip(" ")
        prev = next((l for l in reversed(lines[:i]) if l.strip() and not l.lstrip().startswith("#")), None)
        prev_indent = len(prev) - len(prev.lstrip(" ")) if prev is not None else 0
        if stripped.startswith("- ") and prev is not None and prev.rstrip().endswith(":") and not found_seq:
            offset = max(0, indent - prev_indent)
            sequence = offset + 2
            found_seq = True
        elif not stripped.startswith("- ") and prev is not None and prev.rstrip().endswith(":") and indent > prev_indent and not found_map:
            mapping = indent - prev_indent
            found_map = True
        if found_map and found_seq:
            break
    return mapping, sequence, offset


def _yaml(indent: tuple[int, int, int] = (2, 2, 0)) -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    mapping, sequence, offset = indent
    y.indent(mapping=mapping, sequence=sequence, offset=offset)
    return y


def load_yaml(text: str):
    """Round-trip load (also handy in tests)."""
    try:
        data = _yaml().load(text)
    except YAMLError as err:
        raise WriterError(f"Frigate config is not valid YAML: {err}") from err
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise WriterError("Frigate config must be a mapping at the top level")
    return data


def dump_yaml(data, indent: tuple[int, int, int] = (2, 2, 0)) -> str:
    buf = io.StringIO()
    _yaml(indent).dump(data, buf)
    return buf.getvalue()


def frigate_model_block(choice: ModelChoice, model_dir: str = DEFAULT_MODEL_DIR) -> dict:
    """The root ``model:`` block Frigate needs for one of our YOLOv9 ONNX files."""
    return {
        "path": f"{model_dir.rstrip('/')}/{choice.filename}",
        "width": choice.imgsz,
        "height": choice.imgsz,
        "input_tensor": "nchw",
        "input_dtype": "float",
        "model_type": "yolo-generic",
        "labelmap_path": "/labelmap/coco-80.txt",
    }


def _ensure_map(parent: CommentedMap, key: str) -> CommentedMap:
    node = parent.get(key)
    if not isinstance(node, CommentedMap):
        node = CommentedMap()
        parent[key] = node
    return node


def _apply_lpr(root: CommentedMap, desired: Desired, summary: list[str]) -> None:
    lpr = _ensure_map(root, "lpr")
    if lpr.get("enabled") is not True:
        lpr["enabled"] = True
        summary.append("Turn the plate reader (lpr) on")
    if lpr.get("match_distance") != desired.match_distance:
        lpr["match_distance"] = desired.match_distance
        summary.append(f"Set match_distance to {desired.match_distance} (Plate Gate does the tolerant matching itself)")
    known = _ensure_map(lpr, "known_plates")
    for person in desired.people:
        patterns = [pat for _, pat in patterns_for(person)]
        current = list(known.get(person.name) or [])
        if current != patterns:
            seq = CommentedSeq(patterns)
            known[person.name] = seq
            summary.append(f"Known plates for {person.name}: {', '.join(person.plates)}")
    if desired.debug_save_plates:
        if lpr.get("debug_save_plates") is not True:
            lpr["debug_save_plates"] = True
            summary.append("Save plate crops for diagnosis (debug_save_plates)")
    elif "debug_save_plates" in lpr:
        del lpr["debug_save_plates"]
        summary.append("Stop saving plate crops (debug_save_plates removed)")
    cameras = root.get("cameras")
    if desired.camera and isinstance(cameras, CommentedMap) and isinstance(cameras.get(desired.camera), CommentedMap):
        cam_lpr = cameras[desired.camera].get("lpr")
        if isinstance(cam_lpr, CommentedMap) and cam_lpr.get("enabled") is False:
            cam_lpr["enabled"] = True
            summary.append(f"Turn the plate reader on for {desired.camera} (it was switched off at camera level)")


def _own_keys(node: CommentedMap) -> set:
    """Keys written directly in this mapping (not pulled in through a ``<<`` merge)."""
    try:
        return {k for k, _ in node.non_merged_items()}
    except AttributeError:
        return set(node.keys())


def _apply_detect_native(root: CommentedMap, desired: Desired, summary: list[str], warnings: list[str] | None = None) -> None:
    cameras = root.get("cameras")
    if not isinstance(cameras, CommentedMap) or not isinstance(cameras.get(desired.camera), CommentedMap):
        return
    cam = cameras[desired.camera]
    detect = cam.get("detect")
    if not isinstance(detect, CommentedMap):
        return
    anchor = getattr(getattr(detect, "anchor", None), "value", None)
    if "detect" not in _own_keys(cam) or anchor:
        if warnings is not None:
            warnings.append(
                f"The detect block for {desired.camera} is shared with other cameras (a YAML anchor or merge), "
                "so Plate Gate left it alone. Give this camera its own detect block to use the native resolution."
            )
        return
    removed = False
    for key in ("width", "height"):
        if key in detect:
            del detect[key]
            removed = True
    if removed:
        summary.append(f"Let Frigate detect {desired.camera} at the camera's native resolution (detect width/height removed)")


def _apply_model(root: CommentedMap, desired: Desired, summary: list[str], warnings: list[str]) -> None:
    if desired.model is not None:
        block = frigate_model_block(desired.model, desired.model_dir)
        current = root.get("model")
        if not isinstance(current, CommentedMap) or dict(current) != block:
            model = _ensure_map(root, "model")
            for key in list(model.keys()):
                if key not in block:
                    del model[key]
            for key, value in block.items():
                model[key] = value
            summary.append(f"Detection model: {desired.model.key} (file {block['path']})")
        detectors_now = root.get("detectors")
        if isinstance(detectors_now, CommentedMap):
            for name, det in detectors_now.items():
                if isinstance(det, CommentedMap) and det.get("type") in CPU_DETECTOR_TYPES and "model" in det:
                    del det["model"]
                    summary.append(f"Removed the model block nested under detector {name}: Frigate needs it at the root, and nesting it crashes at runtime")
    if desired.detector_count is not None:
        detectors = _ensure_map(root, "detectors")
        cpu_names = [n for n, d in detectors.items() if isinstance(d, CommentedMap) and d.get("type") in CPU_DETECTOR_TYPES]
        others = [n for n in detectors if n not in cpu_names]
        if others:
            warnings.append(
                f"Leaving detector(s) {', '.join(others)} in place. A Coral cannot run YOLOv9; "
                "Plate Gate only manages the CPU/OpenVINO detectors."
            )
        template = CommentedMap({"type": "openvino", "device": "CPU"})
        if cpu_names and isinstance(detectors.get(cpu_names[0]), CommentedMap):
            template = copy.deepcopy(detectors[cpu_names[0]])
            template.pop("model", None)
        base = cpu_names[0] if cpu_names else "ov"
        wanted = [base] + [f"{base}_{i}" for i in range(1, desired.detector_count)]
        if cpu_names != wanted:
            for name in cpu_names:
                if name not in wanted:
                    del detectors[name]
            for name in wanted:
                if name not in detectors:
                    detectors[name] = copy.deepcopy(template)
            summary.append(f"{desired.detector_count} detector(s): {', '.join(wanted)} ({template.get('type')} {template.get('device', '')})".rstrip())


def _plain(node):
    """Plain python copy (merged keys resolved) for before/after comparison."""
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_plain(v) for v in node]
    return node


def _outside_intended(before: dict, after: dict, desired: Desired) -> str | None:
    """Name of the first thing that changed outside the keys we are allowed to touch, else None."""
    b, a = copy.deepcopy(before), copy.deepcopy(after)
    for d in (b, a):
        d.pop("lpr", None)
        d.pop("model", None)
        d.pop("detectors", None)
        cam = (d.get("cameras") or {}).get(desired.camera) if isinstance(d.get("cameras"), dict) else None
        if isinstance(cam, dict):
            cam.pop("lpr", None)
            det = cam.get("detect")
            if isinstance(det, dict):
                det.pop("width", None)
                det.pop("height", None)
    if b == a:
        return None
    for key in sorted(set(b) | set(a)):
        if b.get(key) != a.get(key):
            if key == "cameras" and isinstance(b.get(key), dict) and isinstance(a.get(key), dict):
                for cam in sorted(set(b[key]) | set(a[key])):
                    if b[key].get(cam) != a[key].get(cam):
                        return f"cameras.{cam}"
            return key
    return "?"


def plan_change(raw_yaml: str, desired: Desired) -> Change:
    """Build the minimal edit and describe it. Never writes anything."""
    indent = sniff_indent(raw_yaml)
    root = load_yaml(raw_yaml)
    before = _plain(load_yaml(raw_yaml))
    summary: list[str] = []
    warnings: list[str] = []
    if desired.touch_lpr:
        _apply_lpr(root, desired, summary)
    if desired.detect_native:
        _apply_detect_native(root, desired, summary, warnings)
    _apply_model(root, desired, summary, warnings)
    new_yaml = dump_yaml(root, indent)
    changed_elsewhere = _outside_intended(before, _plain(load_yaml(new_yaml)), desired)
    if changed_elsewhere:
        raise WriterError(
            f"Refusing to write: the edit would also change '{changed_elsewhere}', which Plate Gate must not touch "
            "(usually a YAML anchor or merge key shared between sections). Nothing was written."
        )
    if new_yaml == raw_yaml or load_yaml(new_yaml) == load_yaml(raw_yaml) and not summary:
        return Change(raw_yaml, raw_yaml, "", [], warnings)
    diff = "".join(
        difflib.unified_diff(
            raw_yaml.splitlines(keepends=True), new_yaml.splitlines(keepends=True),
            fromfile="config.yml (now)", tofile="config.yml (after)", n=3,
        )
    )
    return Change(raw_yaml, new_yaml, diff, summary, warnings)
