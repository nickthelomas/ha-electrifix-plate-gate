"""Companion MQTT service against a fake paho client: status, LWT, commands, progress, busy."""
import json
import threading
import time

import pytest

from plate_gate_companion import core
from plate_gate_companion.service import Companion, Config


class FakeClient:
    """Records what the service publishes; lets the test inject messages."""

    def __init__(self):
        self.published = []      # (topic, payload dict, retain)
        self.subscribed = []
        self.will = None
        self.on_connect = None
        self.on_message = None

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, json.loads(payload), retain)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def inject(self, topic, payload: dict):
        class Msg:
            pass
        m = Msg()
        m.topic, m.payload = topic, json.dumps(payload).encode()
        self.on_message(self, None, m)


@pytest.fixture
def comp(tmp_path):
    cfg = Config(mqtt_host="broker", config_dir=str(tmp_path / "cfg"), companion_id="test", deployment="docker", allowed_bases=("http://127.0.0.1/",))
    client = FakeClient()
    c = Companion(cfg, client=client, sysfs=str(tmp_path / "nosys"), proc=str(tmp_path / "noproc"))
    c.start_client()  # wires callbacks + LWT, does not connect
    return c, client


def _results(client, req_id):
    return [p for t, p, r in client.published if t.endswith(f"/result/{req_id}")]


def _wait(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_connect_publishes_retained_status_and_lwt(comp):
    c, client = comp
    assert client.will == ("electrifix_plate_gate/companion/test/status", {"id": "test", "online": False}, True)
    client.on_connect(client, None, {}, 0, None)
    topic, status, retain = client.published[0]
    assert topic == "electrifix_plate_gate/companion/test/status" and retain is True
    assert status["online"] is True and status["id"] == "test" and status["deployment"] == "docker"
    from plate_gate_companion import VERSION
    assert status["version"] == VERSION and status["models"] == [] and "cpu_model" in status["hardware"]
    assert status["frigate_config_dir"].endswith("cfg") and status["writable"] is True
    assert "electrifix_plate_gate/companion/test/cmd" in client.subscribed


def test_probe_command_returns_data(comp):
    c, client = comp
    client.inject(c.topic("cmd"), {"req_id": "r1", "action": "probe"})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "r1")))
    final = _results(client, "r1")[-1]
    assert final["ok"] is True and final["req_id"] == "r1" and "cores" in final["data"]


def test_unknown_action_and_bad_payloads(comp):
    c, client = comp
    client.inject(c.topic("cmd"), {"req_id": "r2", "action": "format_disk"})
    assert _wait(lambda: _results(client, "r2"))
    assert _results(client, "r2")[-1]["ok"] is False and "unknown" in _results(client, "r2")[-1]["message"].lower()
    client.inject(c.topic("cmd"), {"action": "probe"})  # no req_id → ignored, no crash
    client.on_message(client, None, type("M", (), {"topic": c.topic("cmd"), "payload": b"not json"})())
    assert len([1 for t, p, r in client.published if "/result/" in t and p.get("req_id") is None]) == 0


def test_install_publishes_progress_then_done_and_refreshes_status(comp, tmp_path, monkeypatch):
    c, client = comp
    calls = {}

    def fake_install(config_dir, url, filename, sha256, allowed_bases, progress_cb=None, opener=None):
        calls["args"] = (url, filename, sha256, allowed_bases)
        progress_cb(0, "connecting")
        progress_cb(50, "downloading")
        core.model_dir(config_dir).joinpath(filename).write_bytes(b"model")
        progress_cb(100, "verified")
        return {"filename": filename, "size": 5, "sha256": "abc"}

    monkeypatch.setattr("plate_gate_companion.service.core.install_model", fake_install)
    client.inject(c.topic("cmd"), {"req_id": "r3", "action": "install_model", "url": "http://127.0.0.1/yolov9-s-320.onnx", "filename": "yolov9-s-320.onnx", "sha256": "abc"})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "r3")))
    msgs = _results(client, "r3")
    assert [m["progress"]["pct"] for m in msgs if not m.get("done")] == [0, 50, 100]
    assert msgs[-1]["ok"] and msgs[-1]["data"]["filename"] == "yolov9-s-320.onnx"
    assert calls["args"] == ("http://127.0.0.1/yolov9-s-320.onnx", "yolov9-s-320.onnx", "abc", ("http://127.0.0.1/",))
    statuses = [p for t, p, r in client.published if t.endswith("/status")]
    assert statuses[-1]["models"][0]["filename"] == "yolov9-s-320.onnx"


def test_install_refusal_is_reported(comp):
    c, client = comp
    client.inject(c.topic("cmd"), {"req_id": "r4", "action": "install_model", "url": "http://evil/x.onnx", "filename": "x.onnx"})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "r4")))
    assert _results(client, "r4")[-1]["ok"] is False and "allowed" in _results(client, "r4")[-1]["message"]


def test_second_install_while_busy_is_refused(comp, monkeypatch):
    c, client = comp
    gate = threading.Event()

    def slow_install(config_dir, url, filename, sha256, allowed_bases, progress_cb=None, opener=None):
        gate.wait(5)
        return {"filename": filename, "size": 1, "sha256": "x"}

    monkeypatch.setattr("plate_gate_companion.service.core.install_model", slow_install)
    cmd = {"action": "install_model", "url": "http://127.0.0.1/a.onnx", "filename": "a.onnx"}
    client.inject(c.topic("cmd"), {"req_id": "r5", **cmd})
    time.sleep(0.05)
    client.inject(c.topic("cmd"), {"req_id": "r6", **cmd})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "r6")))
    assert _results(client, "r6")[-1]["ok"] is False and "busy" in _results(client, "r6")[-1]["message"].lower()
    gate.set()
    assert _wait(lambda: any(p.get("done") for p in _results(client, "r5")))
    assert _results(client, "r5")[-1]["ok"] is True


def test_remove_and_list(comp, tmp_path):
    c, client = comp
    core.model_dir(c.cfg.config_dir).joinpath("yolov9-m-640.onnx").write_bytes(b"m")
    client.inject(c.topic("cmd"), {"req_id": "r7", "action": "list_models"})
    assert _wait(lambda: _results(client, "r7"))
    assert _results(client, "r7")[-1]["data"][0]["filename"] == "yolov9-m-640.onnx"
    client.inject(c.topic("cmd"), {"req_id": "r8", "action": "remove_model", "filename": "yolov9-m-640.onnx"})
    assert _wait(lambda: _results(client, "r8"))
    assert _results(client, "r8")[-1]["ok"] is True and core.list_models(c.cfg.config_dir) == []


def test_partials_cleaned_on_start(tmp_path):
    cfg = Config(mqtt_host="broker", config_dir=str(tmp_path / "cfg"), companion_id="t", deployment="haos", allowed_bases=())
    core.model_dir(cfg.config_dir).joinpath("x.onnx.part").write_bytes(b"half")
    Companion(cfg, client=FakeClient(), sysfs=str(tmp_path / "n"), proc=str(tmp_path / "n"))
    assert not core.model_dir(cfg.config_dir).joinpath("x.onnx.part").exists()


def test_config_from_env(monkeypatch):
    from plate_gate_companion.service import Config
    monkeypatch.setenv("PG_MQTT_HOST", "mosq")
    monkeypatch.setenv("PG_MQTT_PORT", "1884")
    monkeypatch.setenv("PG_FRIGATE_CONFIG_DIR", "/frigate_config")
    monkeypatch.setenv("PG_ALLOWED_BASES", "http://a/,http://b/")
    cfg = Config.from_env()
    assert cfg.mqtt_host == "mosq" and cfg.mqtt_port == 1884 and cfg.config_dir == "/frigate_config"
    assert cfg.allowed_bases == ("https://github.com/nickthelomas/ha-electrifix-plate-gate/releases/download/", "http://a/", "http://b/")


# ---- review hardening ---------------------------------------------------------------
class Reason:
    def __init__(self, failure):
        self.is_failure = failure

    def __str__(self):
        return "Not authorized" if self.is_failure else "Success"


def test_garbage_and_oversized_messages_do_not_kill_the_client(comp):
    c, client = comp

    class M:
        pass

    m = M()
    m.topic, m.payload = c.topic("cmd"), b"[" * 100000  # RecursionError in json, not ValueError
    client.on_message(client, None, m)
    m2 = M()
    m2.topic, m2.payload = c.topic("cmd"), b'{"req_id":"big","action":"probe","pad":"' + b"x" * (70 * 1024) + b'"}'
    client.on_message(client, None, m2)
    m3 = M()
    m3.topic, m3.payload = c.topic("cmd"), b'{"req_id":"a+b#","action":"probe"}'  # wildcard chars in a topic segment
    client.on_message(client, None, m3)
    assert not any("/result/" in t for t, p, r in client.published)
    client.inject(c.topic("cmd"), {"req_id": "ok1", "action": "probe"})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "ok1")))


def test_on_connect_failure_publishes_nothing(comp):
    c, client = comp
    client.on_connect(client, None, {}, Reason(True), None)
    assert client.published == [] and client.subscribed == []
    client.on_connect(client, None, {}, Reason(False), None)
    assert client.published and client.subscribed


def test_quick_commands_run_on_one_worker_with_a_bounded_queue(comp, monkeypatch):
    c, client = comp
    monkeypatch.setattr("plate_gate_companion.service.core.probe", lambda *a, **k: (time.sleep(0.03), {"slow": True})[1])
    before = threading.active_count()
    for i in range(40):
        client.inject(c.topic("cmd"), {"req_id": f"q{i}", "action": "probe"})
    assert threading.active_count() <= before + 2  # no thread per message
    assert _wait(lambda: sum(1 for t, p, r in client.published if "/result/q" in t and p.get("done")) == 40, timeout=10)
    refused = [p for t, p, r in client.published if "/result/q" in t and p.get("done") and not p["ok"]]
    assert refused and all("busy" in p["message"].lower() or "queue" in p["message"].lower() for p in refused)


def test_sha_is_cached_per_file(comp, monkeypatch):
    c, client = comp
    d = core.model_dir(c.cfg.config_dir)
    (d / "yolov9-m-640.onnx").write_bytes(b"m" * 1000)
    calls = {"n": 0}
    real = core._sha256_file

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(core, "_sha256_file", counting)
    core.list_models(c.cfg.config_dir)
    core.list_models(c.cfg.config_dir)
    assert calls["n"] == 1
    (d / "yolov9-m-640.onnx").write_bytes(b"n" * 1001)
    core.list_models(c.cfg.config_dir)
    assert calls["n"] == 2


def test_install_validates_types_before_downloading(comp):
    c, client = comp
    client.inject(c.topic("cmd"), {"req_id": "t1", "action": "install_model", "url": "http://127.0.0.1/a.onnx", "filename": "a.onnx", "sha256": 123})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "t1")))
    assert _results(client, "t1")[-1]["ok"] is False and "sha256" in _results(client, "t1")[-1]["message"]


def test_unwritable_config_dir_does_not_crash_startup(tmp_path):
    cfg = Config(mqtt_host="b", config_dir="/proc/definitely/not/writable", companion_id="t", deployment="haos", allowed_bases=())
    c = Companion(cfg, client=FakeClient(), sysfs=str(tmp_path), proc=str(tmp_path))
    assert c.status_payload()["writable"] is False


# ---- accuracy benchmark action --------------------------------------------------------
def test_accuracy_bench_action_runs_with_progress(comp, monkeypatch):
    c, client = comp
    calls = {}

    def fake_run(media_dir, config_dir, camera, filenames, max_images, progress_cb=None, session_factory=None):
        calls["args"] = (media_dir, camera, filenames, max_images)
        progress_cb(50, "a.onnx: 1/2")
        progress_cb(100, "done")
        return {"camera": camera, "images_used": 2, "skipped": 0, "rows": [{"filename": "a.onnx", "ok": True, "images_with_vehicle": 2}]}

    monkeypatch.setattr("plate_gate_companion.service.bench.run_benchmark", fake_run)
    client.inject(c.topic("cmd"), {"req_id": "b1", "action": "accuracy_bench", "camera": "driveway", "models": ["a.onnx"], "max_images": 25})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "b1")))
    msgs = _results(client, "b1")
    assert [m["progress"]["pct"] for m in msgs if not m.get("done")] == [50, 100]
    assert msgs[-1]["ok"] and msgs[-1]["data"]["images_used"] == 2 and msgs[-1]["data"]["rows"][0]["filename"] == "a.onnx"
    assert calls["args"] == (c.cfg.media_dir, "driveway", ["a.onnx"], 25)


def test_accuracy_bench_refused_while_install_running(comp, monkeypatch):
    c, client = comp
    gate = threading.Event()
    monkeypatch.setattr("plate_gate_companion.service.core.install_model", lambda *a, **k: (gate.wait(5), {"filename": "a.onnx", "size": 1, "sha256": "x"})[1])
    client.inject(c.topic("cmd"), {"req_id": "i1", "action": "install_model", "url": "http://127.0.0.1/a.onnx", "filename": "a.onnx"})
    assert _wait(lambda: c._busy.locked())
    client.inject(c.topic("cmd"), {"req_id": "b2", "action": "accuracy_bench", "camera": "driveway", "models": ["a.onnx"], "max_images": 5})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "b2")))
    assert _results(client, "b2")[-1]["ok"] is False and "busy" in _results(client, "b2")[-1]["message"].lower()
    gate.set()
    assert _wait(lambda: any(p.get("done") for p in _results(client, "i1")))


def test_accuracy_bench_validates_payload(comp):
    c, client = comp
    client.inject(c.topic("cmd"), {"req_id": "b3", "action": "accuracy_bench", "camera": "../x", "models": "a.onnx", "max_images": 5})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "b3")))
    assert _results(client, "b3")[-1]["ok"] is False


def test_media_dir_from_env(monkeypatch):
    monkeypatch.setenv("PG_FRIGATE_MEDIA_DIR", "/mnt/media")
    assert Config.from_env().media_dir == "/mnt/media"
    monkeypatch.delenv("PG_FRIGATE_MEDIA_DIR")
    assert Config.from_env().media_dir == "/media/frigate"


def test_accuracy_bench_zero_images_reports_failure(comp, monkeypatch):
    c, client = comp
    monkeypatch.setattr("plate_gate_companion.service.bench.run_benchmark",
                        lambda *a, **k: {"camera": "driveway", "images_used": 0, "skipped": 0, "rows": [], "error": "No snapshots found in /media/frigate/clips for driveway. Are snapshots enabled in Frigate, and is the media folder mapped?"})
    client.inject(c.topic("cmd"), {"req_id": "z1", "action": "accuracy_bench", "camera": "driveway", "models": ["a.onnx"], "max_images": 5})
    assert _wait(lambda: any(p.get("done") for p in _results(client, "z1")))
    last = _results(client, "z1")[-1]
    assert last["ok"] is False and "snapshots" in last["message"].lower()
