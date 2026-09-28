"""Thin, version-tolerant client for the Frigate HTTP API.

Phase 1 only reads: ``/api/version`` and ``/api/config``. Works against the
unauthenticated port (5000) and the authenticated one (8971) when a
username/password are given (``POST /api/login`` sets a cookie on the session).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=10)
TESTED_MINOR = "0.18"  # the Frigate line Plate Gate was written against and tested with


def version_note(version: str | None) -> str:
    """Empty when Frigate is on the tested line; otherwise a plain warning (never a block)."""
    plain = (version or "").split("-")[0].strip()
    if plain.startswith(TESTED_MINOR + ".") or plain == TESTED_MINOR:
        return ""
    shown = plain or "unknown"
    return (
        f"⚠️ Frigate {shown}: Plate Gate was built and tested with Frigate {TESTED_MINOR}. "
        "It may well work here, but check every diff before you tick the box, and tell us if something looks wrong."
    )


class FrigateError(Exception):
    """Base error."""


class FrigateAuthRequired(FrigateError):
    """Frigate answered 401/403 and we have no (working) credentials."""


class FrigateCannotConnect(FrigateError):
    """Network error, timeout, or a reply that is not Frigate's."""


class FrigateAdminRequired(FrigateAuthRequired):
    """The config/restart routes need Frigate's admin role (port 8971 login)."""


class FrigateConfigInvalid(FrigateError):
    """Frigate rejected the config we tried to save; ``message`` has its reasons."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class CameraInfo:
    name: str
    detect_width: int
    detect_height: int
    zones: list[str]
    lpr_enabled: bool


@dataclass
class FrigateInfo:
    version: str
    topic_prefix: str
    lpr_enabled: bool
    cameras: dict[str, CameraInfo]


class FrigateClient:
    """Read-only Frigate client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._session = session
        self.url = url.rstrip("/")
        self._username = username or None
        self._password = password or None
        self._logged_in = False

    async def _request(self, method: str, path: str, **kwargs) -> aiohttp.ClientResponse:
        try:
            resp = await self._session.request(method, f"{self.url}{path}", timeout=TIMEOUT, **kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise FrigateCannotConnect(f"{type(err).__name__}: {err}".rstrip(": ")) from err
        if resp.status in (401, 403):
            raise FrigateAuthRequired(f"{path} -> {resp.status}")
        return resp

    async def _get(self, path: str) -> aiohttp.ClientResponse:
        """GET; on 401/403 log in once when we have credentials (port 8971) and retry."""
        try:
            resp = await self._request("GET", path)
        except FrigateAuthRequired:
            if not (self._username and self._password):
                raise
            await self._login()
            resp = await self._request("GET", path)
        if resp.status >= 400:
            raise FrigateCannotConnect(f"{path} -> HTTP {resp.status}")
        return resp

    async def _admin(self, method: str, path: str, **kwargs) -> aiohttp.ClientResponse:
        """Admin-only routes: on 401/403 log in once (if we can) and retry, else FrigateAdminRequired."""
        try:
            return await self._request(method, path, **kwargs)
        except FrigateAuthRequired:
            try:
                await self._login()
            except FrigateAuthRequired as err:
                raise FrigateAdminRequired(f"{path} needs Frigate's admin login") from err
            try:
                return await self._request(method, path, **kwargs)
            except FrigateAuthRequired as err:
                raise FrigateAdminRequired(f"{path}: this user is not a Frigate admin") from err

    # ---- Phase 2: config + stats ----------------------------------------------------

    async def async_raw_config(self) -> str:
        """The config.yml text exactly as Frigate has it."""
        resp = await self._admin("GET", "/api/config/raw")
        if resp.status >= 400:
            raise FrigateCannotConnect(f"/api/config/raw -> HTTP {resp.status}")
        return await resp.text()

    async def async_save_config(self, yaml_text: str, *, restart: bool) -> None:
        """Save config.yml; Frigate validates it first. 400 -> FrigateConfigInvalid."""
        option = "restart" if restart else "saveonly"
        resp = await self._admin(
            "POST", f"/api/config/save?save_option={option}", data=yaml_text,
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        if resp.status == 400:
            try:
                body = await resp.json(content_type=None)
                message = str(body.get("message") or body)
            except (aiohttp.ContentTypeError, ValueError, AttributeError):
                message = await resp.text()
            raise FrigateConfigInvalid(message)
        if resp.status >= 400:
            raise FrigateCannotConnect(f"/api/config/save -> HTTP {resp.status}")

    async def async_restart(self) -> None:
        resp = await self._admin("POST", "/api/restart")
        if resp.status >= 400:
            raise FrigateCannotConnect(f"/api/restart -> HTTP {resp.status}")

    async def async_stats(self) -> dict:
        resp = await self._get("/api/stats")
        try:
            return await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as err:
            raise FrigateCannotConnect("stats is not JSON") from err

    async def async_config(self) -> dict:
        resp = await self._get("/api/config")
        try:
            return await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as err:
            raise FrigateCannotConnect("config is not JSON") from err

    async def _login(self) -> None:
        if not (self._username and self._password):
            raise FrigateAuthRequired("credentials required")
        try:
            resp = await self._session.post(
                f"{self.url}/api/login",
                json={"user": self._username, "password": self._password},
                timeout=TIMEOUT,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise FrigateCannotConnect(str(err)) from err
        if resp.status in (401, 403):
            raise FrigateAuthRequired("login rejected")
        if resp.status >= 400:
            raise FrigateCannotConnect(f"/api/login -> HTTP {resp.status}")
        self._logged_in = True

    async def async_info(self) -> FrigateInfo:
        """Version, MQTT topic prefix, LPR state and the cameras."""
        version = "unknown"
        try:
            resp = await self._get("/api/version")
            version = (await resp.text()).strip() or "unknown"
        except FrigateAuthRequired:
            if not self._logged_in:
                raise  # no credentials, or the login was rejected
            # logged in, but /api/version still refused: carry on, /api/config will tell us more
        resp = await self._get("/api/config")
        try:
            config = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as err:
            raise FrigateCannotConnect("config is not JSON; is this Frigate?") from err
        if not isinstance(config, dict) or not isinstance(config.get("cameras"), dict):
            raise FrigateCannotConnect("config has no cameras; is this Frigate?")
        lpr_enabled = bool((config.get("lpr") or {}).get("enabled", False))
        cameras: dict[str, CameraInfo] = {}
        for name, cam in config["cameras"].items():
            cam = cam or {}
            detect = cam.get("detect") or {}
            cameras[name] = CameraInfo(
                name=name,
                detect_width=int(detect.get("width") or 0),
                detect_height=int(detect.get("height") or 0),
                zones=list((cam.get("zones") or {}).keys()),
                lpr_enabled=bool((cam.get("lpr") or {}).get("enabled", lpr_enabled)),
            )
        return FrigateInfo(
            version=version,
            topic_prefix=str((config.get("mqtt") or {}).get("topic_prefix") or "frigate"),
            lpr_enabled=lpr_enabled,
            cameras=cameras,
        )
