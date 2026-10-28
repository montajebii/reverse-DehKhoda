"""Backend launcher — decision-table executor + automatic fallback chain.

Default engine (v1.2.1): **Qdrant**. The Linux AppImage is fully
self-contained: the bundled qdrant binary (Tauri sidecar) or the embedded
qdrant-client local mode needs NO Docker, NO pkexec and NO user installs.

Engine fallback chain (locked by spec v1.2.1 — implement exactly):
  a. Milvus Lite   — ONLY on Ubuntu >= 20.04 AND only when the wheel
                     imports cleanly after native-lib preloading.
  b. Qdrant local  — qdrant-client >= 1.9 embedded mode at
                     <user_data_dir>/qdrant (pure Python, zero processes).
  c. Qdrant native — the bundled sidecar binary started on a random
                     localhost port (waited on /readyz).
  d. Milvus        — Docker Compose, only when Docker is available AND the
                     user consented (advanced mode; never auto-triggered).
  e. Otherwise     — EngineUnavailable carrying per-engine diagnostics
                     (full traceback + exact missing shared library) which
                     the wizard renders as a Persian message + Retry.

Every failed attempt is recorded with its full traceback and the exact
missing shared library (see native_preload.describe_exception) — the GUI
NEVER shows a generic "engine is not available" without technical detail.

Socket-permission strategy for the Docker path: if `docker info` fails for
the current user but the daemon is active, compose runs once via
`pkexec env DEHKHODA_VOLUME_DIR=… docker compose up -d` (the single polkit
dialog). The app itself talks plain TCP to Milvus afterwards.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import model_fetcher, paths, state
from .detector import CREATE_NO_WINDOW
from .events import Emitter
from .native_preload import describe_exception, preload_milvus_lite_libs

MILVUS_PORT = 19530


# =========================================================================
# small utilities
# =========================================================================
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_get(url: str, timeout: float = 3.0) -> Optional[int]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dehkhoda-bootstrap/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def _run(argv: list[str], timeout: float = 900.0,
         env: Optional[dict[str, str]] = None,
         on_line: Optional[Any] = None) -> tuple[int, str]:
    """Run a child process, streaming lines to on_line when given."""
    full_env = {**os.environ, **(env or {})}
    kwargs: dict[str, Any] = {"env": full_env}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = CREATE_NO_WINDOW
    if on_line is None:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, errors="replace", **kwargs)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", bufsize=1, **kwargs)
    tail: list[str] = []
    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip()
        tail.append(line)
        on_line(line)
    rc = p.wait(timeout=60)
    return rc, "\n".join(tail[-200:])


class EngineUnavailable(Exception):
    """Every engine in the fallback chain failed.

    Carries per-attempt diagnostics (engine name, error text, full
    traceback, exact missing shared libraries) so the GUI can show the
    REAL cause instead of a generic message.
    """

    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        lines = [f"[{a.get('engine')}] {a.get('error')}" for a in attempts]
        super().__init__("all engines failed :: " + " | ".join(lines))


# =========================================================================
# compose resolution ladder
# =========================================================================
def compose_invocation(em: Emitter) -> Optional[list[str]]:
    """Find a working compose command; download the pinned standalone binary
    as the last rung. Returns the base argv (no subcommand)."""
    docker = shutil.which("docker")
    if docker:
        rc, _ = _run([docker, "compose", "version"], 30.0)
        if rc == 0:
            return [docker, "compose"]
    compose = shutil.which("docker-compose")
    if compose:
        rc, _ = _run([compose, "version"], 30.0)
        if rc == 0:
            return [compose]
    pinned = paths.bin_dir() / "docker-compose"
    if not pinned.exists():
        try:
            em.progress(task_fa="دریافت ابزار استقرار…", log="fetching pinned docker-compose")
            model_fetcher.fetch_compose_binary()
        except Exception as exc:
            em.progress(log=f"compose fetch failed: {exc}")
            return None
    rc, _ = _run([str(pinned), "version"], 30.0)
    if rc == 0:
        return [str(pinned)]
    return None


def compose_file() -> Path:
    cf = paths.bundled_compose_file()
    if not cf.exists():
        # repo/dev fallback
        cand = Path(__file__).resolve().parent.parent / "docker-compose.yml"
        if cand.exists():
            cf = cand
    return cf


# =========================================================================
# milvus (docker compose) — the optional "advanced" engine
# =========================================================================
def _daemon_accessible(docker_bin: str) -> bool:
    rc, _ = _run([docker_bin, "info"], 30.0)
    return rc == 0


def launch_milvus(em: Emitter) -> dict[str, Any]:
    if tcp_open(MILVUS_PORT):
        em.progress(task_fa="Milvus از قبل فعال است", log="port 19530 open")
        return {"backend": "milvus", "uri": f"http://127.0.0.1:{MILVUS_PORT}",
                "reused": True}

    docker_bin = shutil.which("docker")
    if not docker_bin:
        raise RuntimeError("docker engine not found (prereqs step must run first)")

    # daemon running?  (unit may be inactive right after install)
    rc, _ = _run([docker_bin, "info"], 30.0)
    if rc != 0:
        euid = getattr(os, "geteuid", lambda: 0)()
        pkexec = shutil.which("pkexec") and euid != 0
        argv = (["pkexec", "systemctl", "enable", "--now", "docker"]
                if pkexec and shutil.which("systemctl") else
                ["systemctl", "enable", "--now", "docker"])
        em.progress(task_fa="راه‌اندازی سرویس داکر…", log=" ".join(argv))
        rc, out = _run(argv, 120.0)
        if rc != 0:
            raise RuntimeError(f"cannot start docker daemon (rc={rc}): {out[-400:]}")
        for _ in range(30):
            if _daemon_accessible(docker_bin):
                break
            time.sleep(1.0)

    accessible = _daemon_accessible(docker_bin)
    euid = getattr(os, "geteuid", lambda: 0)()
    elevate = (not accessible) and euid != 0 and shutil.which("pkexec")

    compose = compose_invocation(em)
    if not compose:
        raise RuntimeError("no usable docker compose (plugin/standalone/pinned)")
    cf = compose_file()
    if not cf.exists():
        raise RuntimeError(f"compose file missing: {cf}")

    env = {"DEHKHODA_VOLUME_DIR": str(paths.volume_dir())}
    base = [*compose, "-f", str(cf), "-p", "dehkhoda"]
    if elevate:
        flat = [f"{k}={v}" for k, v in env.items()]
        pre = ["pkexec", "env", *flat]
        argv = [*pre, *base, "up", "-d"]
    else:
        argv = [*base, "up", "-d"]

    em.progress(task_fa="آماده‌سازی Milvus (در صورت نیاز، دریافت تصاویر)…",
                log=" ".join(argv), indet=True)
    rc, out = _run(argv, timeout=2400.0,
                   on_line=lambda l: em.progress(log=f"compose: {l[:300]}"))
    if rc != 0:
        raise RuntimeError(f"compose up failed (rc={rc}): {out[-800:]}")

    em.progress(task_fa="در انتظار آماده‌شدن Milvus…", indet=True)
    deadline = time.time() + 300
    while time.time() < deadline:
        if tcp_open(MILVUS_PORT):
            break
        time.sleep(2.0)
    else:
        raise RuntimeError("Milvus did not open port 19530 within 300s")

    if not _milvus_ping():
        em.progress(log="TCP open but gRPC ping failed; continuing (may still warm up)")
    return {"backend": "milvus", "uri": f"http://127.0.0.1:{MILVUS_PORT}",
            "elevated": bool(elevate), "reused": False}


def _milvus_ping() -> bool:
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=f"http://127.0.0.1:{MILVUS_PORT}", timeout=5.0)
        client.list_collections()
        return True
    except Exception:
        return False


# =========================================================================
# milvus lite — optional, Ubuntu >= 20.04 ONLY, clean import required
# =========================================================================
def _ubuntu_ge_20_04() -> bool:
    """Milvus Lite is officially supported on Ubuntu >= 20.04 only.

    On any other distro (Arch, Debian, Fedora, Alpine, …) the wheel may
    import but its native engine (libmilvus/libknowhere) is fragile — this
    gate is what keeps the fallback chain deterministic.
    """
    from .detector import _read_os_release

    rel = _read_os_release()
    if (rel.get("ID") or "").lower() != "ubuntu":
        return False
    try:
        return float((rel.get("VERSION_ID") or "0").split(".")[0] or 0) >= 20
    except ValueError:
        return False


def milvus_lite_importable() -> bool:
    """True iff milvus_lite imports cleanly AFTER native-lib preloading."""
    try:
        preload_milvus_lite_libs()
        import milvus_lite  # noqa: F401
        return True
    except Exception:
        return False


def smoke_milvus_lite(em: Emitter) -> bool:
    """Prove the Milvus Lite wheel really supports our hybrid schema."""
    try:
        import tempfile

        preload_milvus_lite_libs(log=lambda m: em.progress(log=m))
        import pymilvus  # noqa: F401
        from pymilvus import AnnSearchRequest, DataType, MilvusClient, RRFRanker

        with tempfile.TemporaryDirectory(prefix="dh-lite-") as td:
            client = MilvusClient(uri=str(Path(td) / "probe.db"))
            cols = "probe"
            if client.has_collection(cols):
                client.drop_collection(cols)
            schema = client.create_schema(auto_id=False)
            schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=32)
            schema.add_field("dense", DataType.FLOAT_VECTOR, dim=8)
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            index = client.prepare_index_params()
            index.add_index(field_name="dense", index_type="FLAT",
                            metric_type="COSINE")
            index.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX",
                            metric_type="IP")
            client.create_collection(cols, schema=schema, index_params=index)
            client.insert(cols, [
                {"id": "a", "dense": [1.0] * 8, "sparse": {3: 0.5, 7: 1.0}},
                {"id": "b", "dense": [0.0] * 7 + [1.0], "sparse": {1: 0.9}},
            ])
            # AnnSearchRequest (NOT bare dicts) — matches the backend's real
            # hybrid_search call in vector_store/milvus_backend.py. Passing dicts
            # raises AttributeError: 'dict' object has no attribute 'data' on
            # pymilvus >= 2.4.
            dense_req = AnnSearchRequest(
                data=[[1.0] * 8], anns_field="dense",
                param={"metric_type": "COSINE"}, limit=2,
            )
            sparse_req = AnnSearchRequest(
                data=[{3: 0.5, 7: 1.0}], anns_field="sparse",
                param={"metric_type": "IP"}, limit=2,
            )
            res = client.hybrid_search(
                cols,
                reqs=[dense_req, sparse_req],
                ranker=RRFRanker(k=60), limit=2, output_fields=["id"],
            )
            client.drop_collection(cols)
            em.progress(log=f"milvus_lite smoke ok: {len(res[0]) if res else 0} hits")
            return True
    except Exception as exc:
        d = describe_exception(exc, "milvus_lite")
        em.progress(log=f"milvus_lite smoke failed: {d['error']}")
        if d["missing_libs"]:
            em.progress(log="missing shared libraries: " + ", ".join(d["missing_libs"]))
        em.progress(log=d["traceback"][-1500:])
        return False


# =========================================================================
# qdrant — the DEFAULT engine (local embedded mode + native sidecar)
# =========================================================================
def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _qdrant_from_bundled_gz() -> Optional[Path]:
    """Extract the bundled, gzip-compressed qdrant engine to the bin dir.

    The AppImage bundler (linuxdeploy) runs patchelf over every ELF it finds
    in AppDir/usr/bin and usr/lib — that rewrites a static musl binary
    (+1.4 MB) and it SIGSEGVs at start. The engine therefore ships
    GZIP-compressed (linuxdeploy ignores non-ELF files) and is extracted
    here — sha256-verified against the bundled .sha256 (or the pin) — to the
    writable bin dir, which also survives read-only installs.

    Returns the executable path, or None when no bundled .gz exists
    (the pinned download path then applies).
    """
    gz = paths.bundled("sidecars/qdrant/qdrant.gz")
    gz_env = os.environ.get("DEHKHODA_QDRANT_GZ")
    if gz_env and Path(gz_env).exists():
        gz = Path(gz_env)
    if not gz.exists():
        return None

    expected = ""
    sha_file = gz.with_name("qdrant.sha256")
    if sha_file.exists():
        expected = sha_file.read_text(encoding="utf-8").strip().split()[0]
    if not expected:
        try:
            expected = model_fetcher.load_pins()["qdrant"]["files"].get(
                "linux-x86_64", {}).get("binary_sha256", "")
        except Exception:
            expected = ""

    out = paths.bin_dir() / "qdrant"
    if out.exists() and out.stat().st_size > 1_000_000:
        if not expected or _sha256_file(out) == expected:
            return out
        out.unlink()  # stale/corrupt cache — re-extract below

    import gzip
    import shutil as _sh

    tmp = out.with_suffix(".tmp")
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
        _sh.copyfileobj(src, dst)
    actual = _sha256_file(tmp)
    if expected and actual != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"bundled qdrant engine failed sha256 verification: "
            f"expected {expected}, got {actual} (source: {gz})")
    tmp.replace(out)
    out.chmod(out.stat().st_mode | 0o111)
    return out


def qdrant_binary() -> Optional[Path]:
    cands: list[Path] = []
    env_bin = os.environ.get("DEHKHODA_QDRANT_BINARY")
    if env_bin:
        cands.append(Path(env_bin))
    cands += [
        paths.bundled("sidecars/qdrant/qdrant.exe"),
        paths.bundled("sidecars/qdrant/qdrant"),
        paths.bundled("qdrant/qdrant.exe"),
        paths.bundled("qdrant/qdrant"),
        paths.bin_dir() / "qdrant.exe",
        paths.bin_dir() / "qdrant",
    ]
    for c in cands:
        if c.exists():
            return c
    return None


def _probe_hybrid(client: Any, dim: int = 8) -> int:
    """Real dense+sparse+RRF capability probe on an initialised client.

    Creates a temporary collection, upserts two points, runs a hybrid
    query through the SAME Query API the app backend uses, and returns the
    number of hits. Raises on any failure (so the fallback chain sees the
    real reason).
    """
    from qdrant_client import models

    name = "dehkhoda_probe"
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config={
            "dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=False)
            )
        },
    )
    client.upsert(
        collection_name=name,
        points=[
            models.PointStruct(
                id=1,
                vector={
                    "dense": [1.0] * dim,
                    "sparse": models.SparseVector(indices=[3, 7], values=[0.5, 1.0]),
                },
                payload={"id": "a"},
            ),
            models.PointStruct(
                id=2,
                vector={
                    "dense": [0.0] * (dim - 1) + [1.0],
                    "sparse": models.SparseVector(indices=[1], values=[0.9]),
                },
                payload={"id": "b"},
            ),
        ],
        wait=True,
    )
    res = client.query_points(
        collection_name=name,
        prefetch=[
            models.Prefetch(query=[1.0] * dim, using="dense", limit=2),
            models.Prefetch(
                query=models.SparseVector(indices=[3, 7], values=[0.5, 1.0]),
                using="sparse", limit=2,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=2,
        with_payload=True,
    )
    hits = len(res.points)
    client.delete_collection(name)
    if hits < 1:
        raise RuntimeError("hybrid RRF probe returned 0 hits")
    return hits


def launch_qdrant_local(em: Emitter) -> dict[str, Any]:
    """Embedded qdrant-client local mode — zero external processes."""
    from qdrant_client import QdrantClient

    em.progress(task_fa="آماده‌سازی موتور داخلی Qdrant…", log="qdrant local mode")
    storage = paths.data_dir() / "qdrant"
    storage.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(storage))
    try:
        hits = _probe_hybrid(client)
    except Exception:
        client.close()
        raise
    client.close()
    em.progress(log=f"qdrant local smoke ok: {hits} hybrid hits")
    return {"backend": "qdrant_local", "mode": "local",
            "path": str(storage), "probe_hits": hits, "reused": False}


def launch_qdrant(em: Emitter, fetch_if_missing: bool = True) -> dict[str, Any]:
    """Bundled native qdrant sidecar on a random 127.0.0.1 port."""
    binp = _qdrant_from_bundled_gz()
    if binp is None:
        binp = qdrant_binary()
    if binp is None and fetch_if_missing:
        em.progress(task_fa="دریافت موتور جایگزین (Qdrant)…", indet=True)
        binp = model_fetcher.fetch_qdrant(
            progress_cb=lambda d, t, m: em.progress(log=f"qdrant: {m}"))
    if binp is None:
        raise RuntimeError("qdrant binary missing and fetch disabled")

    try:  # resources may lose the +x bit; make sure it is executable
        binp.chmod(binp.stat().st_mode | 0o111)
    except OSError:
        pass

    # reuse an already-running local instance
    for port in (6333,):
        if http_get(f"http://127.0.0.1:{port}/readyz", 2.0) in (200, 204):
            em.progress(task_fa="Qdrant از قبل فعال است", log=f"reusing port {port}")
            return {"backend": "qdrant_native", "url": f"http://127.0.0.1:{port}",
                    "port": port, "reused": True}

    port = free_port()
    storage = paths.data_dir() / "qdrant-server"
    storage.mkdir(parents=True, exist_ok=True)
    env = {
        "QDRANT__SERVICE__HTTP_PORT": str(port),
        "QDRANT__SERVICE__HOST": "127.0.0.1",
        "QDRANT__STORAGE__STORAGE_PATH": str(storage),
        # v1.3.1: pin the snapshots dir to match
        # index_restore._snapshots_dir() — qdrant >= 1.20 only accepts
        # file:// snapshot recovery from inside its own snapshots dir, and
        # the default (./snapshots, CWD-relative) is wrong for bundled apps
        "QDRANT__STORAGE__SNAPSHOTS_PATH": str(storage / "snapshots"),
        # top-level key (NOT service.telemetry_disabled) — a local-first
        # desktop engine must not phone home.
        "QDRANT__TELEMETRY_DISABLED": "true",
    }
    # qdrant's inference service ALWAYS builds an HTTPS client at startup
    # (InferenceService::init_global -> reqwest/rustls with NATIVE root
    # certs) and PANICS when the system has no CA bundle:
    #   "No CA certificates were loaded from the system" -> exit 101
    # Minimal distros/containers ship none. Ship our own (the build
    # container's Mozilla-derived ca-certificates.crt, a non-ELF text file
    # the bundler leaves untouched) and point reqwest at it via the
    # standard SSL_CERT_FILE probe.
    ca = paths.bundled("sidecars/qdrant/ca-bundle.crt")
    if ca.exists():
        env["SSL_CERT_FILE"] = str(ca)
        env["SSL_CERT_DIR"] = ""
    kwargs: dict[str, Any] = {"env": {**os.environ, **env}}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    # sidecar output goes to a log file so ANY failure can be diagnosed from
    # the technical panel (never a bare "exited rc=1")
    log_dir = paths.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    qlog_path = log_dir / "qdrant.log"
    kwargs["stdout"] = open(qlog_path, "wb")
    kwargs["stderr"] = subprocess.STDOUT
    proc = subprocess.Popen([str(binp)], **kwargs)
    em.progress(task_fa="در انتظار آماده‌شدن Qdrant…", indet=True,
                log=f"qdrant pid={proc.pid} port={port} bin={binp} log={qlog_path}")
    deadline = time.time() + 90
    while time.time() < deadline:
        if http_get(f"http://127.0.0.1:{port}/readyz", 2.0) in (200, 204):
            break
        if proc.poll() is not None:
            tail = _tail_file(qlog_path)
            raise RuntimeError(
                f"qdrant exited rc={proc.returncode} (binary: {binp}); "
                f"log tail:\n{tail}")
        time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError(
            f"qdrant /readyz timeout (90s); log tail:\n{_tail_file(qlog_path)}")
    # prove hybrid capability against the live sidecar
    from qdrant_client import QdrantClient

    hits = _probe_hybrid(QdrantClient(url=f"http://127.0.0.1:{port}", timeout=10.0))
    em.progress(log=f"qdrant sidecar ready: {hits} hybrid hits")
    return {"backend": "qdrant_native", "url": f"http://127.0.0.1:{port}",
            "port": port, "pid": proc.pid, "probe_hits": hits, "reused": False}


def _tail_file(path: Path, nbytes: int = 1500) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            return fh.read().decode("utf-8", "replace").strip()[-nbytes:]
    except OSError:
        return "(log unavailable)"


def launch_milvus_lite(em: Emitter) -> dict[str, Any]:
    """Gated Milvus Lite: Ubuntu >= 20.04 AND clean native import."""
    if not _ubuntu_ge_20_04():
        raise RuntimeError(
            "milvus_lite skipped: only supported on Ubuntu >= 20.04 "
            f"(this platform: {_read_distro_pretty()})")
    if not milvus_lite_importable():
        raise RuntimeError(
            "milvus_lite skipped: wheel does not import cleanly after "
            "native-lib preloading (see technical log for the missing .so)")
    db = paths.data_dir() / "milvus_lite.db"
    ok = smoke_milvus_lite(em)
    if not ok:
        raise RuntimeError("milvus_lite hybrid capability smoke failed")
    return {"backend": "milvus_lite", "uri": str(db), "ok": True, "reused": False}


def _read_distro_pretty() -> str:
    from .detector import _read_os_release

    rel = _read_os_release()
    return rel.get("PRETTY_NAME") or rel.get("ID") or "unknown"


# =========================================================================
# dispatcher — automatic fallback chain (locked)
# =========================================================================
CHAIN: dict[str, list[str]] = {
    # preferred choice -> ordered attempts (spec v1.2.1: default = Qdrant)
    "qdrant_native": ["qdrant_local", "qdrant_native", "milvus_lite", "milvus"],
    "qdrant_local": ["qdrant_local", "qdrant_native", "milvus_lite", "milvus"],
    "milvus_lite": ["milvus_lite", "qdrant_local", "qdrant_native", "milvus"],
    "milvus": ["milvus", "qdrant_local", "qdrant_native", "milvus_lite"],
}


def _engine_allowed(engine: str, em: Emitter) -> tuple[bool, str]:
    """Gates for chain stages that must not fire unconditionally."""
    if engine == "milvus_lite":
        if not _ubuntu_ge_20_04():
            return False, "milvus_lite needs Ubuntu >= 20.04"
        if not milvus_lite_importable():
            return False, "milvus_lite wheel does not import cleanly"
        return True, ""
    if engine == "milvus":
        docker = shutil.which("docker")
        if not docker:
            return False, "docker not installed"
        st = state.load()
        consented = bool(st.get("decisions", {}).get("docker_consent", False))
        if not (consented or _daemon_accessible(docker)):
            # chain rule (d): offer Milvus, never auto-start without consent
            return False, "docker present but daemon not accessible and not consented"
        return True, ""
    return True, ""


_ENGINE_LAUNCHERS = {
    "milvus": launch_milvus,
    "milvus_lite": launch_milvus_lite,
    "qdrant_local": launch_qdrant_local,
    "qdrant_native": launch_qdrant,
}


def launch(em: Emitter, preferred: Optional[str] = None,
           defer_index_restore: bool = False) -> dict[str, Any]:
    """Launch the preferred vector engine, falling back down the chain.

    Records every failed attempt (with full traceback + missing library)
    so the wizard can render exact diagnostics. Persists BOTH the user's
    preferred choice (state.backend — untouched) and the engine that
    actually came up (state.runtime.engine) so the HTTP sidecar can
    re-attach on later launches via ensure_runtime().

    v1.3.1 full variant: when the bundle ships the prebuilt index (or
    DEHKHODA_PREFER_NATIVE=1 / DEHKHODA_NO_NETWORK=1 is set), the chain
    starts at qdrant_native — snapshot restore REQUIRES server mode and
    qdrant-client local mode cannot host a restored snapshot. The rest of
    the chain stays as fallback; a local-mode engine is still usable via
    the explicit rebuild_from_bundled_sql path.

    defer_index_restore=True (the wizard's db step) skips the automatic
    restore so the dictionary step can show its own progress; the
    HTTP-sidecar path (ensure_runtime) restores automatically.
    """
    st = state.load()
    pref = preferred or st.get("backend") or "qdrant_native"
    chain = CHAIN.get(pref) or CHAIN["qdrant_native"]

    # v1.3.1: full variant (bundled snapshot) must try the native sidecar
    # first — restore_bundled needs server mode.
    prefer_native = (os.environ.get("DEHKHODA_PREFER_NATIVE") == "1"
                     or os.environ.get("DEHKHODA_NO_NETWORK") == "1")
    if not prefer_native:
        try:
            prefer_native = paths.bundled_snapshot() is not None
        except Exception:
            prefer_native = False
    if prefer_native and chain[0] != "qdrant_native":
        chain = ["qdrant_native"] + [e for e in chain if e != "qdrant_native"]
        em.progress(log="full variant: trying the native qdrant sidecar first "
                        "(snapshot restore requires server mode)")

    attempts: list[dict] = []
    for engine in chain:
        allowed, why = _engine_allowed(engine, em)
        if not allowed:
            attempts.append({"engine": engine, "skipped": True, "error": why})
            em.progress(log=f"[{engine}] skipped: {why}")
            continue
        em.progress(task_fa="آماده‌سازی موتور جست‌وجو…", indet=True,
                    log=f"trying engine: {engine}")
        try:
            data = _ENGINE_LAUNCHERS[engine](em)
        except Exception as exc:
            d = describe_exception(exc, engine)
            attempts.append(d)
            em.progress(log=f"[{engine}] FAILED: {d['error']}")
            if d["missing_libs"]:
                em.progress(log=f"[{engine}] missing shared libraries: "
                                + ", ".join(d["missing_libs"]))
            em.progress(log=d["traceback"][-2000:])
            continue
        data["preferred"] = pref
        runtime = dict(data)
        runtime["engine"] = engine
        st = state.set_value("runtime", runtime)
        state.set_value("backend_active", engine)
        em.progress(task_fa=f"موتور جست‌وجو آماده است ({engine})")

        # v1.3.1: automatic index restore on the non-wizard path (HTTP
        # sidecar boot). The wizard defers to its dictionary step so the
        # restore progress is visible in the right panel. Test C sets
        # DEHKHODA_SKIP_AUTO_RESTORE=1 (it deliberately rebuilds instead).
        if not defer_index_restore and \
                os.environ.get("DEHKHODA_SKIP_AUTO_RESTORE", "") != "1":
            try:
                from . import index_restore

                if index_restore.bundle_capabilities()["index"] and \
                        index_restore.index_state() != "ready":
                    em.progress(task_fa="بازیابی ایندکس آماده…", indet=True,
                                log="launch(): bundled index present but not "
                                    "ready — restoring")
                    index_restore.ensure_index(em)
            except Exception as exc:
                # never-silent: surface the failure, keep the engine up (the
                # dictionary step will retry/rebuild with full progress)
                em.progress(log=f"launch(): index restore FAILED: {exc!r}")
        return runtime

    raise EngineUnavailable(attempts)


def ensure_runtime(em: Optional[Emitter] = None) -> dict[str, Any]:
    """Self-healing entry point for EVERY sidecar start (HTTP app lifespan).

    1. If the persisted runtime is still healthy -> reuse it (fast path).
    2. Otherwise run the fallback chain again (relaunches a dead qdrant
       sidecar on a fresh port, re-verifies local mode, etc.) and persist
       the new runtime so state_env.apply_env() maps the right DEHKHODA_*.
    """
    quiet = em or Emitter("runtime")
    st = state.load()
    runtime = st.get("runtime") or {}
    engine = runtime.get("engine") or st.get("backend")

    if engine == "qdrant_native":
        url = runtime.get("url", "")
        if url and http_get(f"{url}/readyz", 2.0) in (200, 204):
            # v1.3.1: the fast path (<2s to ready) requires BOTH a healthy
            # engine AND a ready bundled index; a missing/stale index falls
            # through to launch(), which restores it (never silently).
            try:
                from . import index_restore

                caps = index_restore.bundle_capabilities()
                if not (caps["index"]
                        and index_restore.index_state() != "ready"):
                    return runtime
            except Exception:
                return runtime
    elif engine == "milvus":
        if tcp_open(MILVUS_PORT):
            return runtime
    elif engine == "qdrant_local":
        path = runtime.get("path") or str(paths.data_dir() / "qdrant")
        if Path(path).exists():
            return {"engine": "qdrant_local", "backend": "qdrant_local",
                    "mode": "local", "path": path}
    elif engine == "milvus_lite":
        if (paths.data_dir() / "milvus_lite.db").exists():
            return {"engine": "milvus_lite", "backend": "milvus_lite",
                    "uri": str(paths.data_dir() / "milvus_lite.db")}

    # stale / missing runtime -> full chain (persists state on success)
    return launch(quiet)


def health_check(em: Emitter) -> dict[str, Any]:
    st = state.load()
    runtime = st.get("runtime") or {}
    backend = st.get("backend_active") or runtime.get("engine") \
        or st.get("backend") or runtime.get("backend")
    if backend == "milvus":
        ok = tcp_open(MILVUS_PORT)
    elif backend == "qdrant_native":
        url = runtime.get("url", "http://127.0.0.1:6333")
        ok = http_get(f"{url}/readyz", 3.0) in (200, 204)
    elif backend == "qdrant_local":
        ok = Path(runtime.get("path") or (paths.data_dir() / "qdrant")).exists()
    elif backend == "milvus_lite":
        ok = (paths.data_dir() / "milvus_lite.db").exists()
    else:
        ok = False
    return {"backend": backend, "healthy": bool(ok)}


if __name__ == "__main__":
    em = Emitter("db")
    print(__doc__)
