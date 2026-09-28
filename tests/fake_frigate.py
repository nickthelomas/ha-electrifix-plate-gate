"""A scripted Frigate for aioclient_mock: raw/save/restart/stats/config/version with a simulated restart."""
from __future__ import annotations

import yaml
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse


class FakeFrigate:
    def __init__(self, raw: str, camera: str = "driveway", url: str = "http://f:5000") -> None:
        self.raw = raw
        self.camera = camera
        self.url = url
        self.saves: list[tuple[str, str]] = []  # (save_option, body)
        self.restarts = 0
        self.uptime = 1000
        self.down_remaining = 0          # polls that answer 503 after a restart
        self.stay_down = False           # a broken config: never comes back
        self.reject_containing: str | None = None  # save returns 400 if body contains this
        self.inference_ms = {"default": 20.0}
        self.on_save = None            # callback(body) after a successful save
        self.restart_on_save = True    # False = a stale Frigate that saves but never restarts
        self.camera_fps = 5.0

    # -- helpers --------------------------------------------------------------
    def _restart(self) -> None:
        self.restarts += 1
        self.down_remaining = 2
        self.uptime = 0

    def _healthy(self) -> bool:
        if self.stay_down:
            return False
        if self.down_remaining > 0:
            self.down_remaining -= 1
            return False
        return True

    def config_dict(self) -> dict:
        return yaml.safe_load(self.raw) or {}

    def _speed(self) -> float:
        path = ((self.config_dict().get("model") or {}).get("path")) or "default"
        for key, ms in self.inference_ms.items():
            if key in path:
                return ms
        return self.inference_ms.get("default", 20.0)

    # -- handlers -------------------------------------------------------------
    async def raw_get(self, method, url, data):
        return AiohttpClientMockResponse(method, url, text=self.raw)

    async def save_post(self, method, url, data):
        option = "restart" if "save_option=restart" in str(url) else "saveonly"
        body = data if isinstance(data, str) else (data or b"").decode()
        if self.reject_containing and self.reject_containing in body:
            return AiohttpClientMockResponse(method, url, status=400, json={"success": False, "message": "Your configuration is invalid.\nbroken"})
        self.saves.append((option, body))
        self.raw = body
        if option == "restart" and self.restart_on_save:
            self._restart()
        if self.on_save:
            self.on_save(body)
        return AiohttpClientMockResponse(method, url, json={"success": True, "message": "saved"})

    async def restart_post(self, method, url, data):
        self._restart()
        return AiohttpClientMockResponse(method, url, json={"success": True, "message": "Restarting"})

    async def stats_get(self, method, url, data):
        if not self._healthy():
            return AiohttpClientMockResponse(method, url, status=503)
        self.uptime += 5
        cfg = self.config_dict()
        detectors = {n: {"inference_speed": self._speed(), "cpu": 12.0, "mem": 1.0} for n in (cfg.get("detectors") or {"ov": {}})}
        return AiohttpClientMockResponse(method, url, json={
            "cameras": {self.camera: {"camera_fps": self.camera_fps, "process_fps": 5.0, "skipped_fps": 0.0, "detection_fps": 1.2, "detect_cpu": 3.0}},
            "detectors": detectors,
            "detection_fps": 1.2,
            "service": {"uptime": self.uptime, "version": "0.18.0-fake"},
        })

    async def config_get(self, method, url, data):
        if not self._healthy_peek():
            return AiohttpClientMockResponse(method, url, status=503)
        return AiohttpClientMockResponse(method, url, json=self.config_dict())

    def _healthy_peek(self) -> bool:
        return not self.stay_down and self.down_remaining <= 0

    async def version_get(self, method, url, data):
        if not self._healthy_peek():
            return AiohttpClientMockResponse(method, url, status=503)
        return AiohttpClientMockResponse(method, url, text="0.18.0-fake")

    def install(self, aioclient_mock) -> None:
        aioclient_mock.get(f"{self.url}/api/config/raw", side_effect=self.raw_get)
        aioclient_mock.post(f"{self.url}/api/config/save?save_option=restart", side_effect=self.save_post)
        aioclient_mock.post(f"{self.url}/api/config/save?save_option=saveonly", side_effect=self.save_post)
        aioclient_mock.post(f"{self.url}/api/restart", side_effect=self.restart_post)
        aioclient_mock.get(f"{self.url}/api/stats", side_effect=self.stats_get)
        aioclient_mock.get(f"{self.url}/api/config", side_effect=self.config_get)
        aioclient_mock.get(f"{self.url}/api/version", side_effect=self.version_get)
