"""Timestamped copies of Frigate's config.yml under <HA config>/electrifix_plate_gate/backups/."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from homeassistant.core import HomeAssistant

KEEP = 20


@dataclass
class BackupInfo:
    path: str
    at: float
    summary: list[str]
    kind: str  # "before_write" | "before_restore" | "benchmark"
    restored_at: float | None = None


def backup_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("electrifix_plate_gate", "backups"))


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-") or "frigate"


def _write(directory: Path, slug: str, text: str, summary: list[str], kind: str, url: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    path = directory / f"{slug}--{kind}--{stamp}-{time.time_ns()}.yml"
    path.write_text(text, encoding="utf-8")
    path.with_suffix(".json").write_text(
        json.dumps({"at": now, "summary": summary, "kind": kind, "url": url}), encoding="utf-8"
    )
    for old in sorted(directory.glob(f"{slug}--{kind}--*.yml"))[:-KEEP]:  # prune per kind
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    return path


def _list(directory: Path, slug: str) -> list[BackupInfo]:
    out: list[BackupInfo] = []
    for path in sorted(directory.glob(f"{slug}--*.yml"), key=lambda p: p.name.rsplit("-", 1)[-1], reverse=True):
        meta: dict = {}
        side = path.with_suffix(".json")
        if side.exists():
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        out.append(BackupInfo(
            str(path), float(meta.get("at") or path.stat().st_mtime), list(meta.get("summary") or []),
            str(meta.get("kind") or "before_write"), meta.get("restored_at"),
        ))
    return out


def _mark(path: Path, key: str, value) -> None:
    side = Path(path).with_suffix(".json")
    meta: dict = {}
    if side.exists():
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    meta[key] = value
    side.write_text(json.dumps(meta), encoding="utf-8")


async def async_save_backup(hass: HomeAssistant, url: str, text: str, summary: list[str], kind: str = "before_write") -> Path:
    return await hass.async_add_executor_job(_write, backup_dir(hass), _slug(url), text, list(summary), kind, url)


async def async_list_backups(hass: HomeAssistant, url: str) -> list[BackupInfo]:
    return await hass.async_add_executor_job(_list, backup_dir(hass), _slug(url))


async def async_mark_restored(hass: HomeAssistant, path: str | Path) -> None:
    await hass.async_add_executor_job(_mark, Path(path), "restored_at", time.time())


async def async_read_backup(hass: HomeAssistant, path: str | Path) -> str:
    return await hass.async_add_executor_job(lambda: Path(path).read_text(encoding="utf-8"))
