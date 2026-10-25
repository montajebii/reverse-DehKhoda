"""Backend launcher — decision-table executor for the vector store.

Paths (locked decision table):
  milvus         : docker engine (already running) + compose -> `up -d`,
                   then wait for TCP 19530 / pymilvus ping.
  milvus_lite    : embedded library; a real capability smoke proves the
                   wheel (dense+sparse schema, hybrid_search with RRF).
  qdrant_native  : bundled or pinned-download qdrant binary spawned with env
                   config on a free 127.0.0.1 port, waited on /readyz.

Socket-permission strategy on Linux: if `docker info` fails for the current
user but the daemon is active, compose runs once via
`pkexec env DEHKHODA_VOLUME_DIR=… docker compose up -d` (the single polkit
dialog). The app itself talks plain TCP to Milvus afterwards — no group
membership, no re-login, no terminal.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import model_fetcher, paths, state
from .detector import CREATE_NO_WINDOW
from .events import Emitter

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
# milvus (docker compose)
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
# milvus lite
# =========================================================================
def smoke_milvus_lite(em: Emitter) -> bool:
    """Prove the Milvus Lite wheel really supports our hybrid schema."""
    try:
        import tempfile

        import pymilvus  # noqa: F401
        from pymilvus import DataType, MilvusClient, RRFRanker

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
            res = client.hybrid_search(
                cols,
                reqs=[{"data": [[1.0] * 8], "anns_field": "dense",
                       "param": {"metric_type": "COSINE"}, "limit": 2},
                      {"data": [{3: 0.5, 7: 1.0}], "anns_field": "sparse",
                       "param": {"metric_type": "IP"}, "limit": 2}],
                ranker=RRFRanker(k=60), limit=2, output_fields=["id"],
            )
            client.drop_collection(cols)
            em.progress(log=f"milvus_lite smoke ok: {len(res[0]) if res else 0} hits")
            return True
    except Exception as exc:
        em.progress(log=f"milvus_lite smoke failed: {exc}")
        return False


# =========================================================================
# qdrant native
# =========================================================================
def qdrant_binary() -> Optional[Path]:
    cands = [
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


def launch_qdrant(em: Emitter, fetch_if_missing: bool = True) -> dict[str, Any]:
    binp = qdrant_binary()
    if binp is None and fetch_if_missing:
        em.progress(task_fa="دریافت موتور جایگزین (Qdrant)…", indet=True)
        binp = model_fetcher.fetch_qdrant(
            progress_cb=lambda d, t, m: em.progress(log=f"qdrant: {m}"))
    if binp is None:
        raise RuntimeError("qdrant binary missing and fetch disabled")

    # reuse an already-running local instance
    for port in (6333,):
        if http_get(f"http://127.0.0.1:{port}/readyz", 2.0) in (200, 204):
            em.progress(task_fa="Qdrant از قبل فعال است", log=f"reusing port {port}")
            return {"backend": "qdrant_native", "url": f"http://127.0.0.1:{port}",
                    "port": port, "reused": True}

    port = free_port()
    storage = paths.data_dir() / "qdrant"
    storage.mkdir(parents=True, exist_ok=True)
    env = {
        "QDRANT__SERVICE__HTTP_PORT": str(port),
        "QDRANT__SERVICE__HOST": "127.0.0.1",
        "QDRANT__STORAGE__STORAGE_PATH": str(storage),
    }
    kwargs: dict[str, Any] = {"env": {**os.environ, **env}}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(binp)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kwargs)
    em.progress(task_fa="در انتظار آماده‌شدن Qdrant…", indet=True,
                log=f"qdrant pid={proc.pid} port={port}")
    deadline = time.time() + 90
    while time.time() < deadline:
        if http_get(f"http://127.0.0.1:{port}/readyz", 2.0) in (200, 204):
            break
        if proc.poll() is not None:
            raise RuntimeError(f"qdrant exited rc={proc.returncode}")
        time.sleep(0.5)
    else:
        raise RuntimeError("qdrant /readyz timeout")
    return {"backend": "qdrant_native", "url": f"http://127.0.0.1:{port}",
            "port": port, "pid": proc.pid, "reused": False}


# =========================================================================
# dispatcher
# =========================================================================
def launch(em: Emitter) -> dict[str, Any]:
    """Launch the store chosen by the decision table (from setup-state)."""
    st = state.load()
    path = st.get("backend")
    if not path:
        raise RuntimeError("decision table not run yet (no backend in state)")
    if path == "milvus":
        data = launch_milvus(em)
    elif path == "milvus_lite":
        data = {"backend": "milvus_lite",
                "ok": smoke_milvus_lite(em),
                "uri": str(paths.data_dir() / "milvus_lite.db")}
        if not data["ok"]:
            raise RuntimeError("Milvus Lite capability smoke failed")
    elif path == "qdrant_native":
        data = launch_qdrant(em)
    else:
        raise RuntimeError(f"unsupported backend path: {path}")

    st = state.set_value("runtime", data)
    em.progress(task_fa="موتور برداری آماده است")
    return data


def health_check(em: Emitter) -> dict[str, Any]:
    st = state.load()
    runtime = st.get("runtime") or {}
    backend = st.get("backend") or runtime.get("backend")
    if backend == "milvus":
        ok = tcp_open(MILVUS_PORT)
    elif backend == "qdrant_native":
        url = runtime.get("url", "http://127.0.0.1:6333")
        ok = http_get(f"{url}/readyz", 3.0) in (200, 204)
    elif backend == "milvus_lite":
        ok = (paths.data_dir() / "milvus_lite.db").exists()
    else:
        ok = False
    return {"backend": backend, "healthy": bool(ok)}


if __name__ == "__main__":
    em = Emitter("db")
    print(__doc__)
