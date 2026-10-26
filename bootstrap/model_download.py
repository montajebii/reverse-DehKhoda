"""Lean-variant model downloader — resumable, checksummed, cancellable.

Downloads the PINNED embedding model (bootstrap/pins.json → embedding_model)
into ``<data_dir>/models/<revision>/`` on first run.

Transport note: files are fetched from the immutable HuggingFace resolve
endpoint ``<resolve_base>/<revision>/<path>`` — exactly the URL
``huggingface_hub.hf_hub_download(revision=<sha>)`` hits. We stream it with
our own Range-resume reader instead of hf_hub_download because the wizard
needs (a) per-chunk progress (file, bytes_done, speed, ETA) streamed into the
JSONL event protocol — hf_hub_download's internal tqdm cannot emit that —
(b) cooperative cancellation that PRESERVES partial bytes, and (c) per-file
sha256 verification against pins.json with a single retry. The revision pin
plus per-file sha256 makes the source immutable and byte-verified either way.

Guarantees (spec v1.3.0):
  * every file downloads to ``<final>.partial`` and is renamed ONLY after
    its sha256 matches pins.json (a `.partial` marker therefore means
    "incomplete, resumable");
  * a killed download resumes from the exact byte offset (HTTP Range);
  * checksum mismatch → delete the file, retry ONCE, then raise
    ModelDownloadError with the exact file/digests (the CLI turns this into
    a Persian error card with a Retry button);
  * on success writes ``<dir>/.complete-<revision>`` and
    ``models/.ready`` = {"revision": ..., "source": "downloaded"} so later
    runs skip the whole step in well under 100 ms.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from . import paths
from .model_fetcher import load_pins

CHUNK = 1 << 18          # 256 KiB read size (responsive cancel checks)
PROGRESS_EVERY = 0.3     # seconds between progress callbacks
DISK_FACTOR = 2          # need 2x the model size free (spec)
HEAD_TIMEOUT = 5.0       # seconds for the reachability probe (spec)


class ModelDownloadError(Exception):
    """Download/verification failure carrying exact technical detail."""

    def __init__(self, msg: str, *, url: str = "", http_status: int = 0,
                 kind: str = "download") -> None:
        super().__init__(msg)
        self.msg = msg
        self.url = url
        self.http_status = int(http_status or 0)
        self.kind = kind  # dns | timeout | http | disk | checksum | download

    def technical(self) -> str:
        parts = []
        if self.url:
            parts.append(f"url={self.url}")
        if self.http_status:
            parts.append(f"http_status={self.http_status}")
        parts.append(f"kind={self.kind}")
        return f"{self.msg} ({' '.join(parts)})"


class DownloadCancelled(Exception):
    """Cooperative cancel (wizard Cancel button / app close). Partials stay."""


def model_pin() -> dict:
    return load_pins()["embedding_model"]


def revision() -> str:
    return str(model_pin()["revision"])


def target_dir() -> Path:
    """models/<revision>/ — the lean download destination."""
    return paths.models_root() / revision()


def total_size_bytes() -> int:
    em = model_pin()
    v = em.get("total_size_bytes") or sum(f["size"] for f in em["files"])
    return int(v)


# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------
def free_disk_bytes(p: Path) -> int:
    while p and not p.exists():
        p = p.parent
    try:
        return shutil.disk_usage(p or Path("/")).free
    except OSError:
        return 0


def _head(url: str, timeout: float) -> None:
    """HEAD the pinned resolve URL; raise ModelDownloadError on failure."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "dehkhoda-bootstrap/1.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.close()
    except urllib.error.HTTPError as e:
        # a 302/307 redirect or even a 4xx from the CDN still PROVES that
        # DNS + TCP + TLS to huggingface.co work — only transport-level
        # failures mean "no network".
        if e.code in (301, 302, 303, 307, 308):
            return
        raise ModelDownloadError(
            f"huggingface.co answered HTTP {e.code}", url=url,
            http_status=e.code, kind="http") from e
    except socket.gaierror as e:
        raise ModelDownloadError(
            f"DNS resolution failed for huggingface.co: {e}", url=url,
            kind="dns") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        msg = f"cannot reach huggingface.co: {reason}"
        if isinstance(reason, socket.gaierror):
            kind = "dns"
            msg = f"DNS resolution failed for huggingface.co: {reason}"
        elif isinstance(reason, TimeoutError) or "timed out" in str(reason):
            kind = "timeout"
            msg = f"connection to huggingface.co timed out after {timeout:g}s"
        else:
            kind = "download"
        raise ModelDownloadError(msg, url=url, kind=kind) from e


def check_network(timeout: float = HEAD_TIMEOUT) -> tuple[bool, str]:
    """(ok, technical_error) — reachability probe for the pinned model URL."""
    em = model_pin()
    url = f"{em['resolve_base']}/{em['revision']}/{em['files'][0]['path']}"
    try:
        _head(url, timeout)
        return True, ""
    except ModelDownloadError as e:
        return False, e.technical()


def preflight() -> dict:
    """Everything the wizard needs BEFORE starting a download (no side effects).

    disk rule: need DISK_FACTOR x total_size_bytes free (spec) next to the
    data dir; network rule: HEAD probe with a 5 s timeout (spec).
    """
    em = model_pin()
    tdir = target_dir()
    need = DISK_FACTOR * total_size_bytes()
    free = free_disk_bytes(paths.data_dir())
    net_ok, net_err = check_network()
    partial = detect_partial()
    return {
        "repo_id": em.get("repo_id") or em["id"],
        "revision": em["revision"],
        "total_size_bytes": total_size_bytes(),
        "need_bytes": need,
        "disk_free_bytes": free,
        "disk_ok": free >= need,
        "network_ok": net_ok,
        "network_error": net_err,
        "target_dir": str(tdir),
        "files": [{"path": f["path"], "size": f["size"]} for f in em["files"]],
        "partial": partial,      # None | {"bytes": n, "files": {...}}
        "already_ready": model_ready(),
    }


# ---------------------------------------------------------------------------
# state probes
# ---------------------------------------------------------------------------
def model_ready() -> dict | None:
    """The .ready fast path: {"revision","source"} when the step can be
    skipped entirely (<100 ms — no file hashing)."""
    ready = paths.read_ready()
    if ready.get("revision") != revision():
        return None
    if valid_dir(target_dir()) or valid_dir(paths.models_dir()):
        return ready
    return None


def valid_dir(d: Path) -> bool:
    files = model_pin()["files"]
    return (d / "config.json").exists() and all(
        (d / f["path"]).exists() for f in files
        if f["path"] in ("config.json", "model.safetensors", "tokenizer.json",
                         "sentencepiece.bpe.model"))


def detect_partial() -> Optional[dict]:
    """Scan the target dir for ``*.partial`` residue from a killed download."""
    tdir = target_dir()
    if not tdir.is_dir():
        return None
    found = {}
    for p in sorted(tdir.rglob("*.partial")):
        found[str(p.relative_to(tdir))] = p.stat().st_size
    # legacy v1.2.1 residue
    legacy = paths.models_dir()
    for p in sorted(legacy.rglob("*.part")):
        found["(legacy)" + str(p.relative_to(legacy))] = p.stat().st_size
    if not found:
        return None
    return {"files": found, "bytes": sum(found.values())}


def discard_partial() -> None:
    """'Start over': remove every .partial / legacy .part and stale temp."""
    tdir = target_dir()
    if tdir.is_dir():
        for p in tdir.rglob("*.partial"):
            p.unlink(missing_ok=True)
    legacy = paths.models_dir()
    for pat in ("*.part", "*.partial"):
        for p in legacy.rglob(pat):
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def sha256_of(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_dir(d: Path, *, delete_bad: bool = False) -> list[str]:
    """Full sha256 verification of every pinned file in ``d``.

    Returns the list of mismatching/missing relative paths (empty = OK).
    With delete_bad=True the mismatching files are removed (retry path).
    """
    em = model_pin()
    bad: list[str] = []
    for f in em["files"]:
        p = d / f["path"]
        if not p.exists():
            bad.append(f["path"])
            continue
        if p.stat().st_size != f["size"] or sha256_of(p) != f["sha256"]:
            bad.append(f["path"])
            if delete_bad:
                p.unlink(missing_ok=True)
    return bad


def mark_ready(source: str) -> None:
    paths.model_ready_path().write_text(
        json.dumps({"revision": revision(), "source": source},
                   ensure_ascii=False),
        encoding="utf-8")


def write_complete_marker(d: Path) -> None:
    (d / f".complete-{revision()}").write_text(
        json.dumps({"revision": revision(),
                    "dim": model_pin().get("dim", 768)}),
        encoding="utf-8")


# ---------------------------------------------------------------------------
# the downloader
# ---------------------------------------------------------------------------
def _open_range(url: str, offset: int, timeout: float = 30.0):
    req = urllib.request.Request(
        url, headers={"User-Agent": "dehkhoda-bootstrap/1.3",
                      **({"Range": f"bytes={offset}-"} if offset else {})})
    return urllib.request.urlopen(req, timeout=timeout)


def _classify_exc(exc: Exception, url: str) -> ModelDownloadError:
    if isinstance(exc, urllib.error.HTTPError):
        return ModelDownloadError(
            f"HTTP {exc.code} from huggingface.co for {url.rsplit('/', 1)[-1]}",
            url=url, http_status=exc.code, kind="http")
    if isinstance(exc, socket.gaierror):
        return ModelDownloadError(
            f"DNS resolution failed for huggingface.co: {exc}", url=url,
            kind="dns")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ModelDownloadError(
            f"connection to huggingface.co timed out ({exc})", url=url,
            kind="timeout")
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        kind = "dns" if isinstance(reason, socket.gaierror) else "download"
        return ModelDownloadError(f"cannot reach huggingface.co: {reason}",
                                  url=url, kind=kind)
    return ModelDownloadError(str(exc), url=url, kind="download")


def _download_one(url: str, part: Path, expected_size: int,
                  sha256: str, cancel: Optional[threading.Event],
                  on_chunk: Callable[[int, bool], None]) -> None:
    """Stream url → part with Range resume + sha256 gate on EVERY path.

    Renames are the caller's job; the part is only left on disk when it is
    byte-verified (so a crash between verify and rename still resumes at
    full size and re-verifies).
    """
    done = part.stat().st_size if part.exists() else 0
    if done and expected_size and done > expected_size:
        part.unlink(missing_ok=True)
        done = 0
    checksum_retry_used = False
    for attempt in range(4):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        try:
            if not (expected_size and done == expected_size):
                resp = _open_range(url, done)
                # a server that ignores Range answers 200 with the FULL body;
                # appending that to `done` bytes would corrupt the part.
                if done and resp.status != 206:
                    done = 0
                with open(part, "ab" if done else "wb") as fh, resp:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise DownloadCancelled()
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        on_chunk(done, False)
                if expected_size and done != expected_size:
                    raise ModelDownloadError(
                        f"truncated transfer: got {done} of "
                        f"{expected_size} bytes", url=url, kind="download")
            # sha256 gate — runs on every path (fresh, resumed, or already
            # at full size), never skipped
            got = sha256_of(part)
            if got != sha256:
                if checksum_retry_used:
                    part.unlink(missing_ok=True)  # never keep verified-bad bytes
                    raise ModelDownloadError(
                        f"sha256 mismatch for {part.name}: got {got}, "
                        f"expected {sha256}", url=url, kind="checksum")
                # spec: delete the file, retry ONCE, then fail with detail
                part.unlink(missing_ok=True)
                done = 0
                checksum_retry_used = True
                on_chunk(0, True)
                continue
            return  # verified
        except (DownloadCancelled, ModelDownloadError):
            raise
        except Exception as exc:  # transport-level: resume & retry
            if attempt == 3:
                raise _classify_exc(exc, url) from exc
            done = part.stat().st_size if part.exists() else 0
            time.sleep(1.5 * (attempt + 1))
    raise ModelDownloadError("download failed after retries", url=url,
                             kind="download")


ProgressFn = Callable[[dict], None]
"""progress payload: {file, bytes_done, bytes_total, speed_bps, eta_seconds,
overall_percent, phase}"""


def download(cancel: Optional[threading.Event] = None,
             progress_cb: ProgressFn = lambda p: None) -> Path:
    """Download every pinned file into models/<revision>/ (resumable).

    Raises DownloadCancelled (partials preserved) or ModelDownloadError.
    Returns the target dir; caller then marks ready via the CLI.
    """
    em = model_pin()
    dest = target_dir()
    dest.mkdir(parents=True, exist_ok=True)
    grand_total = total_size_bytes()
    base_offset = 0
    window: list[tuple[float, int]] = []  # (time, cumulative bytes) samples

    for f in em["files"]:
        final = dest / f["path"]
        final.parent.mkdir(parents=True, exist_ok=True)  # nested pins paths
        part = final.with_name(final.name + ".partial")
        url = f"{em['resolve_base']}/{em['revision']}/{f['path']}"

        if final.exists():
            if sha256_of(final) == f["sha256"]:
                base_offset += f["size"]
                progress_cb({"file": f["path"], "bytes_done": f["size"],
                             "bytes_total": f["size"], "speed_bps": 0,
                             "eta_seconds": 0,
                             "overall_percent": round(
                                 100 * base_offset / grand_total, 1),
                             "phase": "verified"})
                continue
            final.unlink()  # corrupted artifact from an older run

        # migrate a v1.2.1 legacy `<final>.part` so killed pre-upgrade
        # downloads resume instead of restarting from zero
        if not part.exists():
            legacy = (paths.models_dir() / f["path"]).with_name(
                final.name + ".part")
            if legacy.exists() and legacy.stat().st_size <= f["size"]:
                part.parent.mkdir(parents=True, exist_ok=True)
                legacy.replace(part)

        window.clear()

        def on_chunk(done: int, force: bool, _f=f, _part=part) -> None:
            now = time.time()
            window.append((now, done))
            cutoff = now - 4.0
            while window and window[0][0] < cutoff:
                window.pop(0)
            if not force and (now - window[0][0]) < PROGRESS_EVERY and \
                    done < _f["size"]:
                return
            span = now - window[0][0]
            speed = (done - window[0][1]) / span if span > 0.2 else 0.0
            eta = (_f["size"] - done) / speed if speed > 1 else 0
            overall = 100 * (base_offset + done) / grand_total
            progress_cb({"file": _f["path"], "bytes_done": done,
                         "bytes_total": _f["size"], "speed_bps": int(speed),
                         "eta_seconds": int(eta),
                         "overall_percent": round(overall, 1),
                         "phase": "downloading"})

        try:
            _download_one(url, part, f["size"], f["sha256"], cancel, on_chunk)
        except DownloadCancelled:
            progress_cb({"file": f["path"],
                         "bytes_done": part.stat().st_size if part.exists() else 0,
                         "bytes_total": f["size"], "speed_bps": 0,
                         "eta_seconds": 0,
                         "overall_percent": round(
                             100 * (base_offset + (part.stat().st_size
                                 if part.exists() else 0)) / grand_total, 1),
                         "phase": "cancelled"})
            raise
        # final rename ONLY after sha256 matched (done inside _download_one)
        part.replace(final)
        on_chunk(f["size"], True)
        base_offset += f["size"]

    write_complete_marker(dest)
    return dest


def ensure_model_ready(source: str = "downloaded") -> dict:
    """Post-download bookkeeping: .complete marker + .ready + legacy mirror.

    Returns the .ready payload.
    """
    d = target_dir()
    if not (d / f".complete-{revision()}").exists():
        write_complete_marker(d)
    ready = paths.read_ready()
    if ready.get("revision") != revision():
        mark_ready(source)
    return paths.read_ready() or {"revision": revision(), "source": source}
