"""Model catalogue, Frigate blocks and install commands."""
import yaml

from custom_components.electrifix_plate_gate import models as m
from custom_components.electrifix_plate_gate.config_writer import frigate_model_block


def test_catalogue_has_the_sizes_we_ship():
    assert set(m.MODEL_CATALOGUE) >= {"yolov9-t-320", "yolov9-s-320", "yolov9-s-640", "yolov9-m-640"}
    c = m.MODEL_CATALOGUE["yolov9-m-640"]
    assert c.filename == "yolov9-m-640.onnx" and c.imgsz == 640 and c.licence == "GPL-3.0"
    assert c.url == f"{m.MODELS_BASE_URL}/yolov9-m-640.onnx"
    assert "WongKinYiu/yolov9" in c.source_weights_url and c.recipe_url


def test_frigate_block_matches_the_recipe_facts():
    block = frigate_model_block(m.MODEL_CATALOGUE["yolov9-s-320"])
    assert block == {
        "path": "/config/model_cache/plate_gate/yolov9-s-320.onnx", "width": 320, "height": 320,
        "input_tensor": "nchw", "input_dtype": "float", "model_type": "yolo-generic", "labelmap_path": "/labelmap/coco-80.txt",
    }
    yaml.safe_load(m.frigate_model_yaml(m.MODEL_CATALOGUE["yolov9-s-320"]))  # renders valid YAML


def test_install_commands():
    c = m.MODEL_CATALOGUE["yolov9-m-640"]
    docker = m.install_command(c, "docker", host_config_dir="/opt/frigate/config")
    assert "mkdir -p /opt/frigate/config/model_cache/plate_gate" in docker and c.url in docker and "curl -L" in docker
    haos = m.install_command(c, "haos")
    assert "/addon_configs/ccab4aaf_frigate/model_cache/plate_gate" in haos and c.url in haos
    assert "sha256" in m.install_command(c, "docker").lower() or c.sha256 is None


def test_table_ms_seed():
    assert m.TABLE_MS["ryzen7-8845hs"]["yolov9-m-640"] == 42.4
    assert m.TABLE_MS["ryzen7-8845hs"]["yolov9-s-320"] == 5.6
    assert m.TABLE_MS["ryzen5-7640hs"]["yolov9-s-640"] == 23.7
