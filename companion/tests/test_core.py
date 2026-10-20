"""Companion core: probe, list, install (with guards), remove. No MQTT, no HA."""
import hashlib
import http.server
import os
import threading
from pathlib import Path

import pytest

from plate_gate_companion import core


# ---- fake machine -------------------------------------------------------------------
def make_sysfs(root: Path, usb=(), pci=()):
    for i, (vid, pid) in enumerate(usb):
        d = root / "bus" / "usb" / "devices" / f"1-{i+1}"
        d.mkdir(parents=True)
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        (d / "product").write_text("Some Device\n")
    for i, (vid, did) in enumerate(pci):
        d = root / "bus" / "pci" / "devices" / f"0000:0{i}:00.0"
        d.mkdir(parents=True)
        (d / "vendor").write_text("0x" + vid + "\n")
        (d / "device").write_text("0x" + did + "\n")
    (root / "class" / "drm").mkdir(parents=True)
    (root / "class" / "drm" / "renderD128").mkdir()


def make_proc(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "cpuinfo").write_text("processor\t: 0\nmodel name\t: AMD Ryzen 7 8845HS w/ Radeon 780M Graphics\nprocessor\t: 1\nmodel name\t: AMD Ryzen 7 8845HS w/ Radeon 780M Graphics\n")
    (root / "meminfo").write_text("MemTotal:       65432100 kB\nMemFree:        123 kB\n")


def test_probe_finds_coral_usb_and_hailo_pci(tmp_path):
    sysfs, proc, cfg = tmp_path / "sys", tmp_path / "proc", tmp_path / "cfg"
    make_sysfs(sysfs, usb=[("1a6e", "089a"), ("046d", "c52b")], pci=[("1e60", "2864"), ("1002", "15bf")])
    make_proc(proc)
    p = core.probe(str(cfg), sysfs=str(sysfs), proc=str(proc))
    assert p["cpu_model"].startswith("AMD Ryzen 7 8845HS") and p["cores"] == 2
    assert p["mem_total_mb"] == 63898
    assert p["coral_usb"] is True and p["coral_pci"] is False and p["hailo"] is True and p["memryx"] is False
    assert {"vendor": "1a6e", "product": "089a", "name": "Coral USB (unflashed)"} in p["usb"]
    assert any(d["name"] == "Hailo-8" for d in p["pci"])
    assert p["render_nodes"] == ["renderD128"]
    assert p["config_dir_writable"] is True and p["disk_free_mb"] > 0
    assert (cfg / "model_cache" / "plate_gate").is_dir()


def test_probe_with_nothing_special(tmp_path):
    sysfs, proc, cfg = tmp_path / "sys", tmp_path / "proc", tmp_path / "cfg"
    make_sysfs(sysfs)
    make_proc(proc)
    p = core.probe(str(cfg), sysfs=str(sysfs), proc=str(proc))
    assert not (p["coral_usb"] or p["coral_pci"] or p["hailo"] or p["memryx"]) and p["usb"] == [] and p["pci"] == []


def test_probe_survives_missing_sysfs(tmp_path):
    p = core.probe(str(tmp_path / "cfg"), sysfs=str(tmp_path / "nope"), proc=str(tmp_path / "nope"))
    assert p["cpu_model"] == "unknown" and p["usb"] == [] and p["mem_total_mb"] is None


# ---- a tiny HTTP server for downloads -------------------------------------------
@pytest.fixture
def served(tmp_path):
    src = tmp_path / "srv"
    src.mkdir()
    data = os.urandom(3 * 1024 * 1024 + 17)
    (src / "yolov9-s-320.onnx").write_bytes(data)
    (src / "huge.onnx").write_bytes(b"x")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(src), **kw)

        def send_head(self):
            if self.path.endswith("huge.onnx"):
                self.send_response(200)
                self.send_header("Content-Length", str(core.MAX_BYTES + 1))
                self.end_headers()
                return open(src / "huge.onnx", "rb")
            return super().send_head()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}/"
    yield base, hashlib.sha256(data).hexdigest(), len(data)
    srv.shutdown()


def test_install_happy_path_with_progress(tmp_path, served):
    base, sha, size = served
    cfg = tmp_path / "cfg"
    seen = []
    info = core.install_model(str(cfg), base + "yolov9-s-320.onnx", "yolov9-s-320.onnx", sha, (base,), progress_cb=lambda pct, stage: seen.append((pct, stage)))
    assert info == {"filename": "yolov9-s-320.onnx", "size": size, "sha256": sha}
    assert (cfg / "model_cache" / "plate_gate" / "yolov9-s-320.onnx").stat().st_size == size
    assert not list((cfg / "model_cache" / "plate_gate").glob("*.part"))
    assert seen[-1][0] == 100 and seen[-1][1] == "verified" and any(s == "downloading" for _, s in seen)
    assert core.list_models(str(cfg)) == [info]


def test_install_sha_mismatch_leaves_nothing(tmp_path, served):
    base, sha, size = served
    cfg = tmp_path / "cfg"
    with pytest.raises(core.CompanionError, match="checksum"):
        core.install_model(str(cfg), base + "yolov9-s-320.onnx", "yolov9-s-320.onnx", "0" * 64, (base,))
    assert list((cfg / "model_cache" / "plate_gate").iterdir()) == []


def test_install_without_sha_is_allowed_but_reported(tmp_path, served):
    base, sha, size = served
    info = core.install_model(str(tmp_path / "cfg"), base + "yolov9-s-320.onnx", "yolov9-s-320.onnx", None, (base,))
    assert info["sha256"] == sha


@pytest.mark.parametrize("url,filename,msg", [
    ("http://evil.example/yolov9-s-320.onnx", "yolov9-s-320.onnx", "allowed"),
    ("{base}yolov9-s-320.onnx", "../../config.yml", "filename"),
    ("{base}yolov9-s-320.onnx", "model.bin", "filename"),
    ("{base}yolov9-s-320.onnx", "a/b.onnx", "filename"),
])
def test_install_refuses_bad_requests(tmp_path, served, url, filename, msg):
    base, sha, size = served
    cfg = tmp_path / "cfg"
    with pytest.raises(core.CompanionError, match=msg):
        core.install_model(str(cfg), url.format(base=base), filename, sha, (base,))
    assert not (cfg / "model_cache" / "plate_gate").exists() or list((cfg / "model_cache" / "plate_gate").iterdir()) == []


def test_install_refuses_oversize(tmp_path, served):
    base, sha, size = served
    with pytest.raises(core.CompanionError, match="too large"):
        core.install_model(str(tmp_path / "cfg"), base + "huge.onnx", "huge.onnx", None, (base,))


def test_list_ignores_partials_and_clean_partials(tmp_path):
    cfg = tmp_path / "cfg"
    d = core.model_dir(str(cfg))
    (d / "yolov9-m-640.onnx").write_bytes(b"abc")
    (d / "yolov9-s-640.onnx.part").write_bytes(b"half")
    (d / "notes.txt").write_text("x")
    assert [m["filename"] for m in core.list_models(str(cfg))] == ["yolov9-m-640.onnx"]
    assert core.clean_partials(str(cfg)) == 1 and not (d / "yolov9-s-640.onnx.part").exists()


def test_remove_only_inside_the_folder(tmp_path):
    cfg = tmp_path / "cfg"
    d = core.model_dir(str(cfg))
    (d / "yolov9-m-640.onnx").write_bytes(b"abc")
    (cfg / "config.yml").write_text("secret")
    assert core.remove_model(str(cfg), "yolov9-m-640.onnx") is True
    assert core.remove_model(str(cfg), "yolov9-m-640.onnx") is False
    with pytest.raises(core.CompanionError):
        core.remove_model(str(cfg), "../config.yml")
    assert (cfg / "config.yml").exists()
