"""MQTT service: retained status (+ LWT), commands on a worker thread, progress + results.

Topics (prefix electrifix_plate_gate/companion/<id>):
  status            retained  {id, version, online, deployment, frigate_config_dir, writable, models, hardware, updated}
  cmd                         {req_id, action, ...}
  result/<req_id>             {req_id, ok, done, message, progress: {stage, pct}, data}
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, field

from . import VERSION, bench, core

_LOGGER = logging.getLogger(__name__)
PREFIX = "electrifix_plate_gate/companion"
HEARTBEAT_S = 600
ACTIONS = ("probe", "list_models", "install_model", "remove_model", "accuracy_bench")
CAMERA_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PAYLOAD = 64 * 1024
REQ_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
QUEUE_SIZE = 16


@dataclass
class Config:
    mqtt_host: str
    config_dir: str
    mqtt_port: int = 1883
    mqtt_user: str | None = None
    mqtt_pass: str | None = None
    companion_id: str = "addon"
    deployment: str = "haos"
    allowed_bases: tuple[str, ...] = field(default_factory=lambda: core.DEFAULT_ALLOWED_BASES)
    media_dir: str = "/media/frigate"

    @classmethod
    def from_env(cls) -> Config:
        extra = tuple(b.strip() for b in os.environ.get("PG_ALLOWED_BASES", "").split(",") if b.strip())
        return cls(
            mqtt_host=os.environ.get("PG_MQTT_HOST", "core-mosquitto"),
            mqtt_port=int(os.environ.get("PG_MQTT_PORT", "1883") or 1883),
            mqtt_user=os.environ.get("PG_MQTT_USER") or None,
            mqtt_pass=os.environ.get("PG_MQTT_PASS") or None,
            config_dir=os.environ.get("PG_FRIGATE_CONFIG_DIR", "/addon_configs/ccab4aaf_frigate"),
            companion_id=os.environ.get("PG_COMPANION_ID") or ("addon" if os.environ.get("PG_DEPLOYMENT", "haos") == "haos" else socket.gethostname()),
            deployment=os.environ.get("PG_DEPLOYMENT", "haos"),
            allowed_bases=core.DEFAULT_ALLOWED_BASES + extra,
            media_dir=os.environ.get("PG_FRIGATE_MEDIA_DIR") or "/media/frigate",
        )


class Companion:
    """One companion instance. ``client`` is a paho Client (v2 callbacks) or a test double."""

    def __init__(self, cfg: Config, client=None, *, sysfs: str = "/sys", proc: str = "/proc") -> None:
        self.cfg = cfg
        self.client = client
        self._sysfs, self._proc = sysfs, proc
        self._busy = threading.Lock()                     # one download at a time
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)  # quick commands, one worker
        self._worker: threading.Thread | None = None
        try:
            core.clean_partials(cfg.config_dir)
        except OSError as err:
            _LOGGER.warning("could not tidy the model folder (%s); the status will say it is not writable", err)

    # ---- topics / payloads ---------------------------------------------------------

    def topic(self, *parts: str) -> str:
        return "/".join((PREFIX, self.cfg.companion_id, *parts))

    def status_payload(self) -> dict:
        hw = core.probe(self.cfg.config_dir, sysfs=self._sysfs, proc=self._proc)
        return {
            "id": self.cfg.companion_id,
            "version": VERSION,
            "online": True,
            "deployment": self.cfg.deployment,
            "frigate_config_dir": self.cfg.config_dir,
            "writable": hw["config_dir_writable"],
            "models": core.list_models(self.cfg.config_dir),
            "hardware": hw,
            "updated": time.time(),
        }

    def _publish(self, topic: str, payload: dict, retain: bool = False) -> None:
        self.client.publish(topic, json.dumps(payload), qos=1, retain=retain)

    def publish_status(self) -> None:
        try:
            self._publish(self.topic("status"), self.status_payload(), retain=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("status failed: %s", err)

    def _result(self, req_id: str, *, ok: bool, done: bool, message: str = "", progress: dict | None = None, data=None) -> None:
        payload = {"req_id": req_id, "ok": ok, "done": done, "message": message}
        if progress is not None:
            payload["progress"] = progress
        if data is not None:
            payload["data"] = data
        self._publish(self.topic("result", req_id), payload)

    # ---- paho wiring -----------------------------------------------------------------

    def start_client(self) -> None:
        if self.client is None:
            import paho.mqtt.client as mqtt

            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"plate-gate-companion-{self.cfg.companion_id}")
            self.client.suppress_exceptions = True  # a bug in a callback must never kill the network thread
            if self.cfg.mqtt_user:
                self.client.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_pass)
        self.client.will_set(self.topic("status"), json.dumps({"id": self.cfg.companion_id, "online": False}), qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="plate-gate-worker", daemon=True)
            self._worker.start()

    def on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        try:
            failed = bool(getattr(reason_code, "is_failure", False)) or (isinstance(reason_code, int) and reason_code != 0)
            if failed:
                _LOGGER.error("MQTT refused the connection: %s (check the MQTT user/password in the add-on options)", reason_code)
                return
            _LOGGER.info("connected to MQTT (%s)", reason_code)
            self.publish_status()
            client.subscribe(self.topic("cmd"), qos=1)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("on_connect failed")

    def on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload or b""
            if len(payload) > MAX_PAYLOAD:
                _LOGGER.warning("ignoring an oversized command (%d bytes)", len(payload))
                return
            try:
                cmd = json.loads(payload)
            except (TypeError, ValueError, RecursionError):
                _LOGGER.warning("ignoring non-JSON command")
                return
            if not isinstance(cmd, dict) or not REQ_ID_RE.match(str(cmd.get("req_id") or "")):
                _LOGGER.warning("ignoring command without a valid req_id")
                return
            if cmd.get("action") in ("install_model", "accuracy_bench"):
                # long jobs get their own thread: the busy lock refuses a second one immediately
                threading.Thread(target=self.handle, args=(cmd,), daemon=True).start()
                return
            try:
                self._queue.put_nowait(cmd)
            except queue.Full:
                self._result(str(cmd["req_id"]), ok=False, done=True, message="The companion is busy (too many requests queued); try again in a moment.")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("on_message failed")

    def _work(self) -> None:
        while True:
            cmd = self._queue.get()
            try:
                self.handle(cmd)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("worker failed")

    # ---- commands ----------------------------------------------------------------------

    def handle(self, cmd: dict) -> None:
        req_id = str(cmd["req_id"])
        action = str(cmd.get("action") or "")
        try:
            if action == "probe":
                self._result(req_id, ok=True, done=True, data=core.probe(self.cfg.config_dir, sysfs=self._sysfs, proc=self._proc))
            elif action == "list_models":
                self._result(req_id, ok=True, done=True, data=core.list_models(self.cfg.config_dir))
            elif action == "remove_model":
                removed = core.remove_model(self.cfg.config_dir, str(cmd.get("filename") or ""))
                self._result(req_id, ok=True, done=True, message="removed" if removed else "not present", data={"removed": removed})
                self.publish_status()
            elif action == "install_model":
                self._install(req_id, cmd)
            elif action == "accuracy_bench":
                self._accuracy(req_id, cmd)
            else:
                self._result(req_id, ok=False, done=True, message=f"Unknown action {action!r}; this companion only does: {', '.join(ACTIONS)}.")
        except core.CompanionError as err:
            self._result(req_id, ok=False, done=True, message=str(err))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("command %s failed", action)
            self._result(req_id, ok=False, done=True, message=f"Unexpected error: {type(err).__name__}: {err}")

    def _install(self, req_id: str, cmd: dict) -> None:
        if not self._busy.acquire(blocking=False):
            self._result(req_id, ok=False, done=True, message="The companion is busy with another download; try again in a minute.")
            return
        try:
            url, filename, sha = cmd.get("url"), cmd.get("filename"), cmd.get("sha256")
            if not isinstance(url, str) or not isinstance(filename, str) or not (sha is None or isinstance(sha, str)):
                raise core.CompanionError("Refused: url and filename must be text and sha256 text or absent.")
            sha = sha or None
            last = {"pct": -1}

            def progress(pct: int, stage: str) -> None:
                if pct != last["pct"] or stage in ("connecting", "verified"):
                    last["pct"] = pct
                    self._result(req_id, ok=True, done=False, message=stage, progress={"stage": stage, "pct": pct})

            info = core.install_model(self.cfg.config_dir, url, filename, sha, self.cfg.allowed_bases, progress_cb=progress)
            self.publish_status()
            self._result(req_id, ok=True, done=True, message=f"{filename} installed ({info['size'] // (1024 * 1024)} MB).", data=info)
        finally:
            self._busy.release()

    def _accuracy(self, req_id: str, cmd: dict) -> None:
        camera, models, max_images = cmd.get("camera"), cmd.get("models"), cmd.get("max_images", 60)
        if not isinstance(camera, str) or not CAMERA_RE.match(camera):
            raise core.CompanionError("Refused: camera must be a plain name.")
        if not isinstance(models, list) or not models or not all(isinstance(m, str) and core.FILENAME_RE.fullmatch(m) for m in models):
            raise core.CompanionError("Refused: models must be a list of plain .onnx names.")
        try:
            max_images = max(1, min(500, int(max_images)))
        except (TypeError, ValueError):
            raise core.CompanionError("Refused: max_images must be a number.") from None
        if not self._busy.acquire(blocking=False):
            self._result(req_id, ok=False, done=True, message="The companion is busy with another job (a download or a benchmark); try again in a minute.")
            return
        try:
            last = {"pct": -1}

            def progress(pct: int, stage: str) -> None:
                if pct != last["pct"]:
                    last["pct"] = pct
                    self._result(req_id, ok=True, done=False, message=stage, progress={"stage": stage, "pct": pct})

            data = bench.run_benchmark(self.cfg.media_dir, self.cfg.config_dir, camera, models, max_images, progress_cb=progress)
            if data.get("error") or not data.get("images_used"):
                self._result(req_id, ok=False, done=True, message=str(data.get("error") or "No snapshots could be read."), data=data)
                return
            self._result(req_id, ok=True, done=True, message=f"Ran {len(models)} model(s) over {data['images_used']} snapshot(s).", data=data)
        finally:
            self._busy.release()

    # ---- run -----------------------------------------------------------------------------

    def run_forever(self) -> None:
        self.start_client()
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))  # clean stop on docker stop
        self.client.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self.client.loop_start()
        try:
            while True:
                time.sleep(HEARTBEAT_S)
                self.publish_status()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self._publish(self.topic("status"), {"id": self.cfg.companion_id, "online": False}, retain=True)
            except Exception:  # noqa: BLE001
                pass
            self.client.loop_stop()
            self.client.disconnect()
