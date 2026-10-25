"""Checksummed, resumable downloads for the bootstrap wizard.

Covers every artifact the first-run flow fetches:
  * embedding-model weights (pinned HF revision, per-file sha256),
  * the pinned standalone docker-compose binary,
  * the pinned Qdrant fallback binary (tar.gz / zip),
  * the 33 Dehkhoda .sql.gz dumps (pinned immutable commit SHA).

Design:
  * HTTP Range resume via `<name>.part` files — a killed download continues,
    never restarts.
  * Every byte is verified: sha256 when the pin declares one, else the pinned
    size; the e5 directory additionally gets a `.complete-<revision>` marker.
  * progress_cb(done_bytes, total_bytes, message) is called with coarse
    granularity (>= 512 KB deltas) to keep the event stream light.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import paths

ProgressCb = Callable[[int, int, str], None]


def load_pins() -> dict:
    for cand in (paths.bundled("bootstrap/pins.json"),
                 Path(__file__).resolve().parent / "pins.json"):
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    raise FileNotFoundError("pins.json not found in bundle")


def _noop(done: int, total: int, msg: str) -> None:
    pass


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _urlopen_range(url: str, offset: int, timeout: float = 60.0):
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": "dehkhoda-bootstrap/1.0",
        **({"Range": f"bytes={offset}-"} if offset else {}),
    })
    return urllib.request.urlopen(req, timeout=timeout)


def download_file(
    url: str,
    dest: Path,
    sha256: Optional[str] = None,
    expected_size: Optional[int] = None,
    progress_cb: ProgressCb = _noop,
    resume: bool = True,
) -> Path:
    """Stream url -> dest with .part resume + pin verification."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.exists():
        if (sha256 and _sha256(dest) == sha256) or \
           (not sha256 and expected_size and dest.stat().st_size == expected_size) or \
           (not sha256 and not expected_size):
            progress_cb(dest.stat().st_size, dest.stat().st_size or expected_size or 0, "cached")
            return dest
        dest.unlink()  # corrupted artifact: refetch

    done = part.stat().st_size if (resume and part.exists()) else 0
    if done and expected_size and done > expected_size:
        part.unlink()
        done = 0

    for attempt in range(4):
        try:
            resp = _urlopen_range(url, done)
            total = None
            if expected_size:
                total = expected_size
            else:
                cr = resp.headers.get("Content-Range")
                cl = resp.headers.get("Content-Length")
                if cr and "/" in cr:
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                elif cl:
                    total = int(cl) + (done if resp.status == 206 else 0)
            mode = "ab" if done else "wb"
            last = time.time()
            with open(part, mode) as fh, resp:
                while True:
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last >= 0.4:
                        last = now
                        progress_cb(done, total or 0, "downloading")
            if expected_size and done != expected_size:
                raise IOError(f"size mismatch for {dest.name}: {done} != {expected_size}")
            if sha256:
                got = _sha256(part)
                if got != sha256:
                    raise IOError(f"sha256 mismatch for {dest.name}: {got}")
            part.replace(dest)
            progress_cb(done, total or done, "done")
            return dest
        except Exception:
            done = part.stat().st_size if part.exists() else 0
            if attempt == 3:
                raise
            time.sleep(2.0 * (attempt + 1))
    raise IOError(f"download failed: {url}")  # pragma: no cover


# =========================================================================
# model weights
# =========================================================================
def model_dir() -> Path:
    return paths.models_dir()


def model_complete(revision: str) -> bool:
    d = model_dir()
    return (d / f".complete-{revision}").exists() and \
        all((d / f["path"]).exists() for f in load_pins()["embedding_model"]["files"])


def fetch_model(progress_cb: ProgressCb = _noop) -> Path:
    """Download the pinned e5 weights into the per-user models dir."""
    pins = load_pins()["embedding_model"]
    dest = model_dir()
    total = sum(f["size"] for f in pins["files"])
    done_all = 0

    def cb(done: int, tot: int, msg: str) -> None:
        progress_cb(done_all + done, total, f"{pins['id']}:{msg}")

    for f in pins["files"]:
        out = dest / f["path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        url = f"{pins['resolve_base']}/{pins['revision']}/{f['path']}"
        download_file(url, out, sha256=f.get("sha256"),
                      expected_size=f["size"], progress_cb=cb)
        done_all += f["size"]
        progress_cb(done_all, total, f"{pins['id']}:{f['path']} ok")
    (dest / f".complete-{pins['revision']}").write_text(
        json.dumps({"revision": pins["revision"], "dim": pins["dim"]}),
        encoding="utf-8",
    )
    return dest


# =========================================================================
# helper binaries
# =========================================================================
def _arch_key() -> str:
    m = (os.uname().machine if hasattr(os, "uname") else "")
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m or "unknown"


def fetch_compose_binary(progress_cb: ProgressCb = _noop) -> Path:
    """Pinned standalone compose binary -> bin_dir/docker-compose (Linux)."""
    pins = load_pins()["docker_compose"]
    key = _arch_key()
    f = pins["files"][key]
    out = paths.bin_dir() / "docker-compose"
    url = f"{pins['base_url']}/{f['name']}"
    download_file(url, out, sha256=f["sha256"],
                  expected_size=None, progress_cb=progress_cb)
    out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return out


def fetch_qdrant(progress_cb: ProgressCb = _noop) -> Path:
    """Pinned Qdrant binary -> bin_dir/qdrant(.exe), extracted + verified."""
    pins = load_pins()["qdrant"]
    if sys.platform.startswith("win"):
        key, exe_name = "windows-x86_64", "qdrant.exe"
    else:
        key = f"linux-{_arch_key()}"
        exe_name = "qdrant"
    f = pins["files"][key]
    out = paths.bin_dir() / exe_name
    if out.exists() and out.stat().st_size > 1_000_000:
        return out  # already extracted (checksum verified at download time)

    with tempfile.TemporaryDirectory(prefix="dh-qdrant-") as td:
        tdp = Path(td)
        arch = tdp / f["name"]
        url = f"{pins['base_url']}/{f['name']}"
        download_file(url, arch, sha256=f["sha256"], progress_cb=progress_cb)
        progress_cb(0, 0, f"extracting {f['name']}")
        if f["name"].endswith(".zip"):
            with zipfile.ZipFile(arch) as zf:
                zf.extractall(tdp / "x")
        else:
            with tarfile.open(arch, "r:gz") as tf:
                tf.extractall(tdp / "x", filter="data")
        root = tdp / "x"
        strip = int(f.get("strip_components", 0))
        for _ in range(strip):
            subs = [p for p in root.iterdir() if p.is_dir()]
            if not subs:
                break
            root = subs[0]
        src = next((p for p in root.rglob(exe_name) if p.is_file()), None)
        if src is None:
            raise FileNotFoundError(f"{exe_name} not found in {f['name']}")
        paths.bin_dir().mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        if not sys.platform.startswith("win"):
            out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return out


# =========================================================================
# Dehkhoda dumps
# =========================================================================
def dumps_dir() -> Path:
    return paths.dumps_dir()


def dump_pin_list() -> list[dict]:
    return load_pins()["dehkhoda_dataset"]["files"]


def dump_url(name: str) -> str:
    pins = load_pins()["dehkhoda_dataset"]
    return f"{pins['raw_base']}/{name}.sql.gz"


def fetch_dumps(progress_cb: ProgressCb = _noop,
                per_file: Optional[Callable[[str, int, int], None]] = None) -> list[Path]:
    """Download all pinned dumps with per-file resume. Returns final paths."""
    files = dump_pin_list()
    total = sum(f["size"] for f in files)
    done_all = 0
    out: list[Path] = []
    ddir = dumps_dir()
    for f in files:
        dest = ddir / f"{f['name']}.sql.gz"

        def cb(done: int, tot: int, msg: str, _n=f["name"]) -> None:
            progress_cb(done_all + done, total, f"{_n}.sql.gz {msg}")
            if per_file:
                per_file(_n, done, f["size"])

        download_file(dump_url(f["name"]), dest,
                      expected_size=f["size"], progress_cb=cb)
        done_all += f["size"]
        out.append(dest)
    return out


def selftest() -> dict:
    """Offline selftest: pins integrity + URL construction (no network)."""
    pins = load_pins()
    em = pins["embedding_model"]
    total = sum(f["size"] for f in em["files"])
    ok = (
        total == em["total_bytes"]
        and pins["dehkhoda_dataset"]["total_bytes"] ==
        sum(f["size"] for f in pins["dehkhoda_dataset"]["files"])
        and len(pins["dehkhoda_dataset"]["files"]) == 33
        and all(len(f["sha256"]) == 64 for f in pins["qdrant"]["files"].values())
        and all(len(f["sha256"]) == 64 for f in pins["docker_compose"]["files"].values())
    )
    return {
        "pins_ok": ok,
        "model_files": len(em["files"]),
        "model_total_mb": round(total / 1048576, 1),
        "dumps": len(pins["dehkhoda_dataset"]["files"]),
        "compose_version": pins["docker_compose"]["version"],
        "qdrant_version": pins["qdrant"]["version"],
        "sample_dump_url": dump_url("01-A"),
        "sample_model_url": f"{em['resolve_base']}/{em['revision']}/model.safetensors",
    }


if __name__ == "__main__":
    r = selftest()
    print(json.dumps(r, indent=1))
    raise SystemExit(0 if r["pins_ok"] else 1)
