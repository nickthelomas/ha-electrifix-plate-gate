"""What the Frigate box is doing, and which model Plate Gate would recommend. Pure."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .frigate_api import version_note
from .models import MODEL_CATALOGUE, NOTES, REFERENCE_MACHINE, TABLE_MS, install_command

CPU_TYPES = ("openvino", "onnx", "cpu")
TARGET_BUSY = 0.6      # keep each detector under 60% busy
TARGET_BUSY_WHEN_SKIPPING = 0.3
SECOND_DETECTOR_AT = 0.5
CLASS_RATIO = {"openvino": 1.0, "onnx": 1.0, "cpu": 3.0}  # vs the reference machine when we have no measurement


@dataclass
class Detector:
    name: str
    type: str
    device: str
    inference_ms: float | None
    cpu_pct: float | None


@dataclass
class CameraStat:
    name: str
    detect: tuple[int, int] | None
    camera_fps: float
    detection_fps: float
    skipped_fps: float
    detect_cpu: float | None


@dataclass
class Gpu:
    vendor: str
    gpu: str
    mem: str


@dataclass
class LprStat:
    enabled: bool
    plate_detect_ms: float | None
    ocr_ms: float | None


@dataclass
class HardwareReport:
    frigate_version: str
    detectors: list[Detector]
    detection_fps: float
    skipped_fps: float
    cameras: dict[str, CameraStat]
    gpu: list[Gpu]
    coral_present: bool
    current_model_path: str | None
    current_model_size: tuple[int, int] | None
    lpr: LprStat = field(default_factory=lambda: LprStat(False, None, None))

    @classmethod
    def from_frigate(cls, config: dict, stats: dict) -> HardwareReport:
        config = config or {}
        stats = stats or {}
        det_cfg = config.get("detectors") or {}
        det_stats = stats.get("detectors") or {}
        detectors = []
        for name in list(det_cfg) + [n for n in det_stats if n not in det_cfg]:
            c, s = det_cfg.get(name) or {}, det_stats.get(name) or {}
            detectors.append(Detector(name, str(c.get("type") or "?"), str(c.get("device") or ""), _f(s.get("inference_speed")), _f(s.get("cpu"))))
        cameras = {}
        cam_stats = stats.get("cameras") or {}
        for name in list((config.get("cameras") or {})) + [n for n in cam_stats if n not in (config.get("cameras") or {})]:
            c, s = (config.get("cameras") or {}).get(name) or {}, cam_stats.get(name) or {}
            d = c.get("detect") or {}
            detect = (int(d["width"]), int(d["height"])) if d.get("width") and d.get("height") else None
            cameras[name] = CameraStat(name, detect, _f(s.get("camera_fps")) or 0.0, _f(s.get("detection_fps")) or 0.0, _f(s.get("skipped_fps")) or 0.0, _f(s.get("detect_cpu")))
        gpus = [Gpu(v, str(g.get("gpu", "")), str(g.get("mem", ""))) for v, g in (stats.get("gpu_usages") or {}).items() if isinstance(g, dict)]
        model = config.get("model") or {}
        size = (int(model["width"]), int(model["height"])) if model.get("width") and model.get("height") else None
        emb = stats.get("embeddings") or {}
        lpr = LprStat(bool((config.get("lpr") or {}).get("enabled")), _f(emb.get("yolov9_plate_detection_speed")), _f(emb.get("plate_recognition_speed")))
        return cls(
            frigate_version=str((stats.get("service") or {}).get("version") or "?"),
            detectors=detectors,
            detection_fps=_f(stats.get("detection_fps")) or 0.0,
            skipped_fps=sum(c.skipped_fps for c in cameras.values()),
            cameras=cameras, gpu=gpus,
            coral_present=any(d.type == "edgetpu" for d in detectors),
            current_model_path=str(model.get("path")) if model.get("path") else None,
            current_model_size=size, lpr=lpr,
        )

    @property
    def cpu_detectors(self) -> list[Detector]:
        return [d for d in self.detectors if d.type in CPU_TYPES]

    @property
    def current_model_key(self) -> str | None:
        if not self.current_model_path:
            return None
        for key, choice in MODEL_CATALOGUE.items():
            if self.current_model_path.endswith(choice.filename):
                return key
        return None

    @property
    def mean_inference_ms(self) -> float | None:
        vals = [d.inference_ms for d in self.cpu_detectors if d.inference_ms]
        return sum(vals) / len(vals) if vals else None

    @property
    def busy_fraction(self) -> float:
        ms = self.mean_inference_ms
        n = len(self.cpu_detectors)
        if not ms or not n:
            return 0.0
        return self.detection_fps * ms / 1000.0 / n


@dataclass
class Recommendation:
    kind: str  # "model" | "detectors" | "basic"
    why: str
    cost_line: str
    model_key: str | None = None
    detector_count: int | None = None
    install: str | None = None


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def speed_ratio(report: HardwareReport) -> float:
    """How this box compares to the reference machine (1.0 = same)."""
    key = report.current_model_key
    measured = report.mean_inference_ms
    table = TABLE_MS[REFERENCE_MACHINE]
    if key in table and measured:
        return measured / table[key]
    types = {d.type for d in report.cpu_detectors}
    return max((CLASS_RATIO.get(t, 1.0) for t in types), default=1.0)


def projected_ms(report: HardwareReport, model_key: str) -> float:
    return round(TABLE_MS[REFERENCE_MACHINE][model_key] * speed_ratio(report), 1)


def recommend(report: HardwareReport) -> list[Recommendation]:
    recs: list[Recommendation] = []
    if report.coral_present and not report.cpu_detectors:
        recs.append(Recommendation(
            "basic",
            "You have a Coral. Keep it: it runs Frigate's default model quickly. A Coral cannot run YOLOv9, "
            "which is what finds more plates; running YOLOv9 on the CPU next to the Coral is a Phase 3 job.",
            "Coral: ~8 ms per check, near-zero CPU. Best for the cost; YOLOv9 on a CPU finds more plates for more CPU.",
        ))
        return recs
    fps = max(report.detection_fps, 1.0)
    n = max(len(report.cpu_detectors), 1)
    # frames already being skipped means the measured detection rate understates the real demand
    target = TARGET_BUSY_WHEN_SKIPPING if report.skipped_fps > 0.5 else TARGET_BUSY
    order = ["yolov9-m-640", "yolov9-s-640", "yolov9-s-320", "yolov9-t-320"]
    pick = None
    for key in order:
        ms = projected_ms(report, key)
        if fps * ms / 1000.0 / n <= target:
            pick = key
            break
    pick = pick or "yolov9-t-320"
    current = report.current_model_key
    ms = projected_ms(report, pick)
    if current == pick:
        why = f"Keep {pick}: {NOTES[pick]}."
    else:
        why = f"Use {pick}: {NOTES[pick]}."
    idx = order.index(pick)
    if idx > 0:
        up = order[idx - 1]
        cost = f"{pick} ≈ {ms} ms per check here. Best for the cost; {up} is better at small/far plates for about {projected_ms(report, up)} ms per check."
    else:
        cost = f"{pick} ≈ {ms} ms per check here. This is the top model we recommend on a CPU."
    recs.append(Recommendation("model", why, cost, model_key=pick, install=install_command(MODEL_CATALOGUE[pick], "docker")))
    n = len(report.cpu_detectors)
    if report.busy_fraction > SECOND_DETECTOR_AT and n < 2:
        recs.append(Recommendation(
            "detectors",
            f"Your detector is {report.busy_fraction:.0%} busy. A second detector is a pool, not per camera, and gives headroom for the plate reader.",
            "Costs one more CPU core while detecting; no extra hardware.",
            detector_count=2,
        ))
    if report.skipped_fps > 0.5:
        recs.append(Recommendation(
            "detectors",
            f"Frigate is skipping {report.skipped_fps:.1f} frames/s: the detector cannot keep up.",
            "Add a detector or drop to a lighter model before anything else.",
            detector_count=max(2, n + 1),
        ))
    return recs


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _host_of(url: str) -> str:
    if "://" not in url:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port else host


def stream_host(config: dict, camera: str) -> str | None:
    """Where the camera's first input comes from, credentials stripped. A go2rtc restream
    (rtsp://127.0.0.1:8554/<name>) is resolved to that stream's real source."""
    try:
        path = str(((config.get("cameras") or {})[camera].get("ffmpeg") or {}).get("inputs")[0]["path"])
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if "://" not in path:
        return path
    parts = urlsplit(path)
    if (parts.hostname or "") in LOOPBACK:
        name = parts.path.rstrip("/").rsplit("/", 1)[-1]
        streams = (config.get("go2rtc") or {}).get("streams") or {}
        src = streams.get(name)
        if isinstance(src, list):
            src = next((x for x in src if isinstance(x, str) and "://" in x and not x.startswith("ffmpeg:")), None)
        if isinstance(src, str) and "://" in src:
            return f"{_host_of(src)} (via go2rtc)"
        return f"{_host_of(path)} (a local restream)"
    return _host_of(path)


def render_companion_markdown(status, report: HardwareReport) -> list[str]:
    hw = status.hardware or {}
    lines = ["", f"**Companion** ({status.deployment or '?'}, v{status.version}) sees the machine directly:"]
    if hw.get("cpu_model"):
        lines.append(f"CPU {hw['cpu_model']}, {hw.get('cores', '?')} threads, {round((hw.get('mem_total_mb') or 0) / 1024)} GB RAM.")
    found = []
    if hw.get("coral_usb"):
        found.append("Coral on USB" + ("" if report.coral_present else " (found, but not used by Frigate)"))
    if hw.get("coral_pci"):
        found.append("Coral on M.2/PCIe" + ("" if report.coral_present else " (found, but not used by Frigate)"))
    if hw.get("hailo"):
        found.append("Hailo-8")
    if hw.get("memryx"):
        found.append("MemryX")
    lines.append("Accelerators: " + (", ".join(found) if found else "none found") + ".")
    files = [m.get("filename") for m in status.models]
    lines.append("Model files on the Frigate machine: " + (", ".join(files) if files else "none yet") + f" (folder {status.frigate_config_dir}/model_cache/plate_gate, {'writable' if status.writable else 'NOT writable'}).")
    return lines


def render_report_markdown(report: HardwareReport, recs: list[Recommendation], companion=None, camera_stream: str | None = None) -> str:
    lines = [f"**Frigate {report.frigate_version}**", ""]
    if (note := version_note(report.frigate_version)):
        lines += [note, ""]
    lines.append("| Detector | Type | Speed | CPU |")
    lines.append("|---|---|---|---|")
    for d in report.detectors:
        lines.append(f"| {d.name} | {d.type} {d.device} | {d.inference_ms if d.inference_ms is not None else '?'} ms | {d.cpu_pct if d.cpu_pct is not None else '?'}% |")
    lines.append("")
    lines.append(f"Detections: {report.detection_fps:.1f}/s, skipped {report.skipped_fps:.1f}/s, detectors {report.busy_fraction:.0%} busy.")
    if report.current_model_path:
        size = f" ({report.current_model_size[0]}x{report.current_model_size[1]})" if report.current_model_size else ""
        lines.append(f"Current model: `{report.current_model_path}`{size}.")
    for c in report.cameras.values():
        detect = f"{c.detect[0]}x{c.detect[1]}" if c.detect else "native"
        lines.append(f"Camera {c.name}: detect {detect}, {c.camera_fps:.0f} fps.")
    if camera_stream:
        lines.append(f"Camera stream comes from `{camera_stream}` (if you swapped cameras, this is where to look).")
    if companion is not None:
        lines += render_companion_markdown(companion, report)
    if report.gpu:
        lines.append("GPU: " + ", ".join(f"{g.vendor} {g.gpu}" for g in report.gpu))
    lines.append(f"The plate reader is {'on' if report.lpr.enabled else 'off'}" + (f" (plate find {report.lpr.plate_detect_ms} ms, read {report.lpr.ocr_ms} ms)" if report.lpr.plate_detect_ms else "") + ".")
    lines.append("")
    lines.append("**Recommendation**")
    for r in recs:
        lines.append("")
        lines.append(f"• {r.why}")
        lines.append(f"  {r.cost_line}")
        if r.model_key:
            filename = MODEL_CATALOGUE[r.model_key].filename
            if companion is not None and any(m.get("filename") == filename for m in companion.models):
                lines.append(f"  {filename} is already on the Frigate machine.")
            elif companion is not None and companion.online:
                lines.append(f"  Get the file with the Companion: Configure → **Install model** → {r.model_key}.")
            elif r.install:
                lines.append(f"  Put the file on the Frigate machine: `{r.install}`")
    return "\n".join(lines)


def render_accuracy_markdown(data: dict | None) -> str:
    if not data:
        return "No result."
    used, skipped = data.get("images_used", 0), data.get("skipped", 0)
    lines = [f"Ran over the {used} most recent event snapshots for **{data.get('camera')}**" + (f" ({skipped} skipped: unreadable)" if skipped else "") + ".", "",
             "| Model | Frames with a vehicle | Vehicles counted | Mean confidence | Per frame* |", "|---|---|---|---|---|"]
    for r in data.get("rows") or []:
        if r.get("ok"):
            lines.append(f"| {r['filename']} | {r.get('images_with_vehicle')} / {used} | {r.get('vehicles_total')} | {r.get('mean_top_score')} | {r.get('mean_ms')} ms |")
        else:
            lines.append(f"| {r.get('filename')} | failed | – | – | – |")
    failed = [r for r in (data.get("rows") or []) if not r.get("ok")]
    for r in failed:
        lines.append(f"\n• {r.get('filename')}: {r.get('note')}")
    lines += ["", "*Per-frame time is on the Companion's CPU with onnxruntime, not Frigate's detector: use the speed benchmark for that.",
              "These snapshots are frames Frigate already flagged, so the comparison favours the model that raised them; whole frames are letterboxed to the model size (harder than Frigate's region crops, which favours the 640 models a little); it is not ground truth."]
    return "\n".join(lines)
