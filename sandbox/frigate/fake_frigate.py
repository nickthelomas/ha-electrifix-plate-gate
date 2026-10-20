"""Just enough of Frigate 0.18's HTTP API for Plate Gate's sandbox.

Read: /api/version, /api/config, /api/config/raw, /api/stats.
Write: POST /api/config/save?save_option=restart|saveonly (body = YAML text; 400 if it contains
``break_me``), POST /api/restart. A "restart" makes version/stats/config answer 503 for 3 s and resets
uptime, like the real thing. Inference speed follows the configured model file name.
Sandbox-only: POST /sandbox/arm_failure → the NEXT saved config never comes back (proves rollback);
the save after that clears it.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml

INITIAL = """\
# Sandbox Frigate config
mqtt:
  host: mosquitto
  topic_prefix: frigate

detectors:
  ov:
    type: openvino
    device: CPU

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

cameras:
  driveway:
    ffmpeg:
      inputs:
        - path: rtsp://sandbox/driveway
          roles: [detect, record]
    detect:
      width: 2304
      height: 1296
      fps: 10
    zones:
      driveway_approach:   # stops at the kerb
        coordinates: 0.1,0.9,0.9,0.9,0.9,0.5,0.1,0.5
  yard:
    ffmpeg:
      inputs:
        - path: rtsp://sandbox/yard
          roles: [detect]
    detect:
      fps: 5
"""
SPEEDS = {"t-320": 4.5, "s-320": 5.6, "s-640": 16.8, "m-640": 42.4, "c-640": 56.1}
RESTART_S = 3.0


class State:
    raw = INITIAL
    restarting_until = 0.0
    started_at = time.time()
    stay_down = False
    poison_next = False
    saves = 0


def config():
    return yaml.safe_load(State.raw) or {}


def speed():
    path = (config().get("model") or {}).get("path") or ""
    for key, ms in SPEEDS.items():
        if key in path:
            return ms
    return 12.0


def down():
    return State.stay_down or time.time() < State.restarting_until


def restart():
    State.restarting_until = time.time() + RESTART_S
    State.started_at = time.time() + RESTART_S


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/version":
            return self._send(503, "restarting", "text/plain") if down() else self._send(200, "0.18.0-sandbox", "text/plain")
        if path == "/api/config/raw":
            return self._send(200, State.raw, "text/plain")
        if path.startswith("/models/"):
            name = path.split("/models/", 1)[1]
            f = "/models-dist/" + name
            if "/" in name or not name.endswith(".onnx") or not __import__("os").path.isfile(f):
                return self._send(404, {"message": "no such model"})
            data = open(f, "rb").read()
            return self._send(200, data, "application/octet-stream")
        if down():
            return self._send(503, {"message": "restarting"})
        if path == "/api/config":
            return self._send(200, config())
        if path == "/api/stats":
            cfg = config()
            dets = {n: {"inference_speed": speed(), "cpu": 10.0 + speed() / 4, "mem": 1.2, "pid": 100} for n in (cfg.get("detectors") or {"ov": {}})}
            cams = {n: {"camera_fps": 10.0, "process_fps": 10.0, "skipped_fps": 0.0, "detection_fps": 1.5, "detect_cpu": 3.0} for n in (cfg.get("cameras") or {})}
            return self._send(200, {
                "cameras": cams, "detectors": dets, "detection_fps": 1.5 * len(cams),
                "service": {"uptime": int(time.time() - State.started_at), "version": "0.18.0-sandbox"},
                "gpu_usages": {"amd-vaapi": {"gpu": "2%", "mem": "1%"}},
                "embeddings": {"plate_recognition_speed": 18.2, "yolov9_plate_detection_speed": 9.8} if (cfg.get("lpr") or {}).get("enabled") else {},
            })
        self._send(404, {"message": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if path == "/api/config/save":
            if "break_me" in body:
                return self._send(400, {"success": False, "message": "Your configuration is invalid.\nbreak_me: extra field not permitted"})
            try:
                yaml.safe_load(body)
            except yaml.YAMLError as err:
                return self._send(400, {"success": False, "message": f"Your configuration is invalid.\n{err}"})
            State.raw = body
            State.saves += 1
            State.stay_down = State.poison_next
            State.poison_next = False
            if "save_option=restart" in self.path:
                restart()
            return self._send(200, {"success": True, "message": "Config successfully saved."})
        if path == "/api/restart":
            restart()
            return self._send(200, {"success": True, "message": "Restarting (this can take up to one minute)..."})
        if path == "/sandbox/arm_failure":
            State.poison_next = True
            return self._send(200, {"armed": True})
        if path == "/sandbox/reset":
            State.raw = INITIAL
            State.stay_down = State.poison_next = False
            return self._send(200, {"reset": True})
        self._send(404, {"message": "not found"})

    def log_message(self, fmt, *args):
        print(self.address_string(), fmt % args, flush=True)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
