"""The detection models Plate Gate knows: what to write into Frigate and where to fetch the file.

The ONNX files are straight conversions of the YOLOv9 authors' released weights (GPL-3.0), made with
``tools/Dockerfile.yolov9`` (the recipe from Frigate's own docs). Plate Gate only downloads them.
Frigate cannot fetch a model from a URL and Plate Gate cannot write files on the Frigate machine, so
``install_command`` gives the user the one line that puts the file where Frigate expects it.
"""
from __future__ import annotations

import yaml

from .config_writer import DEFAULT_MODEL_DIR, ModelChoice, frigate_model_block

REPO_URL = "https://github.com/nickthelomas/ha-electrifix-plate-gate"
MODELS_BASE_URL = f"{REPO_URL}/releases/download/models-v1"
SOURCE_WEIGHTS_URL = "https://github.com/WongKinYiu/yolov9/releases/tag/v0.1"
RECIPE_URL = f"{REPO_URL}/blob/main/tools/Dockerfile.yolov9"
HAOS_FRIGATE_CONFIG_DIR = "/addon_configs/ccab4aaf_frigate"
# sha256 of the files built with tools/build_models.sh on 2026-09-28 (models-dist/SHA256SUMS)
SHA256 = {
    "yolov9-m-640.onnx": "111464dfb360019e63186c9a92e8147f635e17afaa312125938aaf87ed8dbad0",
    "yolov9-s-320.onnx": "c55248f52c77d77ef3c1b8e8a630f08761f133be5e9daf811a2ae439f546d094",
    "yolov9-s-640.onnx": "ebe245ebf9b2207794411a04dded1fe5b06711f7f1c734806f768047c089e102",
    "yolov9-t-320.onnx": "b25d64a88ede1cfa1c38891e276308c5f614ff603699e113bda52406e52b1583"
}


def _choice(size: str, imgsz: int) -> ModelChoice:
    filename = f"yolov9-{size}-{imgsz}.onnx"
    return ModelChoice(
        key=f"yolov9-{size}-{imgsz}", family="yolov9", size=size, imgsz=imgsz, filename=filename,
        url=f"{MODELS_BASE_URL}/{filename}", sha256=SHA256.get(filename), licence="GPL-3.0",
        source_weights_url=SOURCE_WEIGHTS_URL, recipe_url=RECIPE_URL,
    )


MODEL_CATALOGUE: dict[str, ModelChoice] = {c.key: c for c in (
    _choice("t", 320), _choice("s", 320), _choice("s", 640), _choice("m", 640), _choice("c", 640),
)}

# Measured per-check latency (ms) on ElectriFix's test machines, OpenVINO on the CPU.
TABLE_MS: dict[str, dict[str, float]] = {
    "ryzen7-8845hs": {"yolov9-t-320": 4.5, "yolov9-s-320": 5.6, "yolov9-s-640": 16.8, "yolov9-m-640": 42.4, "yolov9-c-640": 56.1},
    "ryzen5-7640hs": {"yolov9-s-320": 6.9, "yolov9-s-640": 23.7},
}
REFERENCE_MACHINE = "ryzen7-8845hs"
# What each model is good for, in plain words (for the hardware screen).
NOTES: dict[str, str] = {
    "yolov9-t-320": "lightest; fine for close, large cars only",
    "yolov9-s-320": "fast; misses small or far plates",
    "yolov9-s-640": "good all-rounder; found 89% of driveway cars in our test",
    "yolov9-m-640": "best on a CPU: found 96% of driveway cars; ~2.5× the CPU of s-640",
    "yolov9-c-640": "not recommended: barely better than m on plates and worse on general car scenes",
}


def frigate_model_yaml(choice: ModelChoice, model_dir: str = DEFAULT_MODEL_DIR) -> str:
    return yaml.safe_dump({"model": frigate_model_block(choice, model_dir)}, sort_keys=False)


def install_command(choice: ModelChoice, deployment: str, *, host_config_dir: str = "/path/to/frigate/config") -> str:
    """One copy-paste line that puts the file where Frigate (in its container) sees ``DEFAULT_MODEL_DIR``."""
    base = HAOS_FRIGATE_CONFIG_DIR if deployment == "haos" else host_config_dir.rstrip("/")
    target_dir = f"{base}/model_cache/plate_gate"
    cmd = f"mkdir -p {target_dir} && curl -L -o {target_dir}/{choice.filename} {choice.url}"
    if choice.sha256:
        cmd += f" && echo '{choice.sha256}  {target_dir}/{choice.filename}' | sha256sum -c"
    return cmd


def licence_note(choice: ModelChoice) -> str:
    return (
        f"{choice.key}: converted from the YOLOv9 authors' weights ({choice.source_weights_url}) with this recipe "
        f"({choice.recipe_url}). Licence {choice.licence}."
    )
