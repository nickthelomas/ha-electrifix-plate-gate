"""Pure functions: probe the machine, list/install/remove model files. No MQTT here.

Guard rails (this runs on a stranger's NVR on the strength of an MQTT message):
- downloads only from allow-listed bases, only into <config>/model_cache/plate_gate/,
- filenames restricted to a safe pattern, size capped, sha256 verified when given,
- written to `.part` first, then atomically renamed.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import urllib.request
from collections.abc import Callable
from pathlib import Path

MODEL_SUBDIR = "model_cache/plate_gate"
FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.onnx$")
MAX_BYTES = 512 * 1024 * 1024
CHUNK = 1024 * 1024
DEFAULT_ALLOWED_BASES = ("https://github.com/nickthelomas/ha-electrifix-plate-gate/releases/download/",)
KNOWN_USB = {("1a6e", "089a"): "Coral USB (unflashed)", ("18d1", "9302"): "Coral USB"}
KNOWN_PCI = {("1ac1", "089a"): "Coral PCIe/M.2", ("1e60", "2864"): "Hailo-8"}
MEMRYX_VENDOR = "1fe9"


class CompanionError(Exception):
    """A refused or failed request; the message is meant for the user."""


def model_dir(config_dir: str) -> Path:
    d = Path(config_dir) / MODEL_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- probe ------------------------------------------------------------------------

def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def _usb(sysfs: Path) -> list[dict]:
    out = []
    base = sysfs / "bus" / "usb" / "devices"
    if not base.is_dir():
        return out
    for dev in sorted(base.iterdir()):
        vid, pid = _read(dev / "idVendor"), _read(dev / "idProduct")
        if not vid or not pid:
            continue
        vid, pid = vid.lower(), pid.lower()
        name = KNOWN_USB.get((vid, pid)) or _read(dev / "product") or ""
        out.append({"vendor": vid, "product": pid, "name": name})
    return out


def _pci(sysfs: Path) -> list[dict]:
    out = []
    base = sysfs / "bus" / "pci" / "devices"
    if not base.is_dir():
        return out
    for dev in sorted(base.iterdir()):
        vid, did = _read(dev / "vendor"), _read(dev / "device")
        if not vid or not did:
            continue
        vid, did = vid.lower().replace("0x", ""), did.lower().replace("0x", "")
        name = KNOWN_PCI.get((vid, did)) or ("MemryX" if vid == MEMRYX_VENDOR else "")
        out.append({"vendor": vid, "device": did, "name": name})
    return out


def probe(config_dir: str, *, sysfs: str = "/sys", proc: str = "/proc") -> dict:
    """What this machine has, as far as a container can see."""
    sysfs_p, proc_p = Path(sysfs), Path(proc)
    cpu_model, cores = "unknown", 0
    cpuinfo = _read(proc_p / "cpuinfo")
    if cpuinfo:
        for line in cpuinfo.splitlines():
            if line.startswith("processor"):
                cores += 1
            elif line.startswith("model name") and cpu_model == "unknown":
                cpu_model = line.split(":", 1)[1].strip()
    mem_total_mb = None
    meminfo = _read(proc_p / "meminfo")
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                mem_total_mb = int(line.split()[1]) // 1024
    usb, pci = _usb(sysfs_p), _pci(sysfs_p)
    drm = sysfs_p / "class" / "drm"
    render_nodes = sorted(p.name for p in drm.iterdir() if p.name.startswith("renderD")) if drm.is_dir() else []
    try:
        d = model_dir(config_dir)
        writable = os.access(d, os.W_OK)
        disk_free_mb = shutil.disk_usage(d).free // (1024 * 1024)
    except OSError:
        writable, disk_free_mb = False, 0
    return {
        "cpu_model": cpu_model,
        "cores": cores,
        "mem_total_mb": mem_total_mb,
        "usb": usb,
        "pci": pci,
        "coral_usb": any((u["vendor"], u["product"]) in KNOWN_USB for u in usb),
        "coral_pci": any((p["vendor"], p["device"]) == ("1ac1", "089a") for p in pci),
        "hailo": any((p["vendor"], p["device"]) == ("1e60", "2864") for p in pci),
        "memryx": any(p["vendor"] == MEMRYX_VENDOR for p in pci),
        "render_nodes": render_nodes,
        "config_dir": str(config_dir),
        "config_dir_writable": writable,
        "disk_free_mb": disk_free_mb,
    }


# ---- models -----------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


_SHA_CACHE: dict[str, tuple[int, int, str]] = {}  # path -> (size, mtime_ns, sha256)


def _sha256_cached(path: Path) -> str:
    st = path.stat()
    key = str(path)
    hit = _SHA_CACHE.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    digest = _sha256_file(path)
    _SHA_CACHE[key] = (st.st_size, st.st_mtime_ns, digest)
    return digest


def list_models(config_dir: str) -> list[dict]:
    try:
        d = model_dir(config_dir)
    except OSError:
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and not p.is_symlink() and FILENAME_RE.fullmatch(p.name):
            out.append({"filename": p.name, "size": p.stat().st_size, "sha256": _sha256_cached(p)})
    return out


def clean_partials(config_dir: str) -> int:
    n = 0
    try:
        d = model_dir(config_dir)
    except OSError:
        return 0
    for p in d.glob("*.part"):
        p.unlink(missing_ok=True)
        n += 1
    return n


def validate_install(url: str, filename: str, allowed_bases: tuple[str, ...]) -> None:
    if not any(url.startswith(b) for b in allowed_bases):
        raise CompanionError(f"Download refused: {url} is not from an allowed source ({', '.join(allowed_bases)}).")
    if not FILENAME_RE.fullmatch(filename or "") or "/" in filename or "\\" in filename or filename.startswith("."):
        raise CompanionError(f"Download refused: filename {filename!r} is not a plain .onnx name.")


def install_model(
    config_dir: str, url: str, filename: str, sha256: str | None, allowed_bases: tuple[str, ...],
    progress_cb: Callable[[int, str], None] | None = None, opener: Callable | None = None,
) -> dict:
    """Download ``url`` into the model folder as ``filename``; verify; atomic rename."""
    validate_install(url, filename, allowed_bases)
    d = model_dir(config_dir)
    final, part = d / filename, d / (filename + ".part")
    report = progress_cb or (lambda pct, stage: None)
    opener = opener or urllib.request.urlopen
    h = hashlib.sha256()
    done = 0
    try:
        report(0, "connecting")
        with opener(url, timeout=60) as resp:
            length = resp.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            if total is not None and total > MAX_BYTES:
                raise CompanionError(f"Download refused: the file is too large ({total // (1024 * 1024)} MB).")
            with part.open("wb") as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_BYTES:
                        raise CompanionError("Download aborted: the file is too large.")
                    f.write(chunk)
                    h.update(chunk)
                    report(int(done * 100 / total) if total else 0, "downloading")
        digest = h.hexdigest()
        if sha256 and digest.lower() != sha256.lower():
            raise CompanionError("Download failed the checksum check; the file was discarded. Try again, and tell us if it keeps happening.")
        os.replace(part, final)
        _SHA_CACHE[str(final)] = (final.stat().st_size, final.stat().st_mtime_ns, digest)
        report(100, "verified")
        return {"filename": filename, "size": done, "sha256": digest}
    except CompanionError:
        part.unlink(missing_ok=True)
        raise
    except Exception as err:  # noqa: BLE001 - network/IO: report plainly
        part.unlink(missing_ok=True)
        raise CompanionError(f"Download failed: {type(err).__name__}: {err}") from err


def remove_model(config_dir: str, filename: str) -> bool:
    if not FILENAME_RE.fullmatch(filename or "") or "/" in filename or "\\" in filename or filename.startswith("."):
        raise CompanionError(f"Refused: {filename!r} is not a plain .onnx name.")
    p = model_dir(config_dir) / filename
    if not p.is_file():
        return False
    p.unlink()
    return True
