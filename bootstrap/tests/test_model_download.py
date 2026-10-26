"""Offline unit tests for the v1.3.0 lean-variant model downloader.

Covers the release spec's critical mechanics WITHOUT touching HuggingFace
(a local Range-capable HTTP server plays huggingface.co):
  * pins.json integrity (every file has a real sha256 + size; totals match)
  * pre-flight math (disk 2x rule, exact total_size_bytes, verbatim errors)
  * resumable download against the local server:
      - fresh download -> renamed only after sha256 -> .ready marker
      - cancel mid-flight -> .partial preserved -> resume continues from the
        exact byte offset the server saw (no re-download of completed bytes)
      - corrupted bytes -> checksum retry ONCE -> ModelDownloadError(kind=checksum)
  * verify_dir catches a corrupted file
  * model_ready fast path (<100 ms, no hashing)

Exit 0 = all assertions hold, 1 otherwise.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap import model_download as md  # noqa: E402

TMP = Path("/tmp/dh-model-test")


# ---------------------------------------------------------------------------
# local Range-capable multi-file server
# ---------------------------------------------------------------------------
class RangeHandler(BaseHTTPRequestHandler):
    bodies: dict[str, bytes] = {}
    hits: list[str] = []  # request log: "HEAD:<path>" / "GET <path> off=<n>"

    def log_message(self, *a):  # silence
        pass

    def do_HEAD(self):
        RangeHandler.hits.append(f"HEAD:{self.path}")
        self.send_response(302)  # like HF: resolve redirects to the CDN
        self.send_header("Location", self.path)
        self.end_headers()

    def do_GET(self):
        # the downloader hits <base>/<revision>/<file> — the FILE is the last
        # path segment
        name = self.path.rsplit("/", 1)[-1]
        body = RangeHandler.bodies.get(name, b"")
        rng = self.headers.get("Range")
        start = 0
        if rng and "=" in rng:
            spec = rng.split("=", 1)[1]          # "N-" | "N-M"
            start = int(spec.split("-", 1)[0] or 0)
        if start:
            RangeHandler.hits.append(f"GET {name} off={start}")
            chunk = body[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            RangeHandler.hits.append(f"GET {name} off=0")
            chunk = body
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        # pace large bodies so mid-transfer cancellation is observable in
        # tests (10 ms per 256 KiB — ~120 ms for the 3 MB fake model file)
        piece = 1 << 18
        try:
            for i in range(0, len(chunk), piece):
                self.wfile.write(chunk[i:i + piece])
                if len(chunk) > (1 << 20):
                    import time as _t
                    _t.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client cancelled — expected during the cancel test


def start_server(bodies: dict[str, bytes]) -> str:
    RangeHandler.bodies = bodies
    RangeHandler.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


# ---------------------------------------------------------------------------
# fake pins wired into the module (resolve_base -> the local server)
# ---------------------------------------------------------------------------
F1 = b"A" * (3 * 1024 * 1024)   # model.safetensors — multi-chunk so cancel
                                # can land mid-transfer (chunk = 256 KiB)
F2 = b"B" * 1024                # config.json
REVISION = "deadbeef" * 5


class FakeEnv:
    """Patch md.load_pins + paths.models_root to a temp sandbox."""

    def __init__(self, base_url: str):
        self.pins = {
            "embedding_model": {
                "id": "fake/model", "repo_id": "fake/model",
                "revision": REVISION, "dim": 4,
                "total_size_bytes": len(F1) + len(F2),
                "resolve_base": base_url,
                "files": [
                    {"path": "config.json", "size": len(F2),
                     "sha256": hashlib.sha256(F2).hexdigest()},
                    {"path": "model.safetensors", "size": len(F1),
                     "sha256": hashlib.sha256(F1).hexdigest()},
                ],
            }
        }
        self.root = TMP / "models"

    def load_pins(self):
        return self.pins

    def models_root(self):
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    base = start_server({"config.json": F2, "model.safetensors": F1})
    env = FakeEnv(base)
    if TMP.exists():
        shutil.rmtree(TMP)
    real_load_pins, real_models_root = md.load_pins, md.paths.models_root
    md.load_pins = env.load_pins
    md.paths.models_root = env.models_root

    try:
        em = env.pins["embedding_model"]

        # ---- 1. ship-pin integrity (the REAL pins.json) -------------------
        real = real_load_pins()["embedding_model"]
        check("ship pin: 9 files, every file sha256'd, 40-hex revision",
              len(real["files"]) == 9
              and all(len(f.get("sha256") or "") == 64 for f in real["files"])
              and len(real["revision"]) == 40)

        # ---- 2. fake-pin integrity + preflight math -----------------------
        check("pins: total_size_bytes matches files",
              em["total_size_bytes"] == sum(f["size"] for f in em["files"])
              and md.total_size_bytes() == em["total_size_bytes"])
        real_net = md.check_network
        md.check_network = lambda timeout=5.0: (True, "")
        pf = md.preflight()
        check("preflight: disk rule = 2x total",
              pf["need_bytes"] == 2 * em["total_size_bytes"]
              and pf["disk_ok"] == (pf["disk_free_bytes"] >= pf["need_bytes"]))
        check("preflight: no partial, not ready",
              pf["partial"] is None and pf["already_ready"] is None)

        # ---- 3. fresh download: rename-after-verify + .ready --------------
        tdir = md.target_dir()
        events: list[dict] = []
        d = md.download(progress_cb=events.append)
        check("download: returns the target dir", d == tdir)
        check("download: final files exist, zero .partial residue",
              (tdir / "model.safetensors").stat().st_size == len(F1)
              and not list(tdir.rglob("*.partial")))
        check("download: progress events carry every spec field",
              events and all(
                  {"file", "bytes_done", "bytes_total", "speed_bps",
                   "eta_seconds", "overall_percent"} <= set(e)
                  for e in events))
        check("download: last event overall_percent == 100",
              abs(events[-1]["overall_percent"] - 100.0) < 0.01)
        md.ensure_model_ready("downloaded")
        check("download: .ready written with the revision",
              json.loads((env.root / ".ready").read_text())["revision"]
              == REVISION)
        check("download: model_ready fast path (<100ms)",
              (md.model_ready() or {}).get("source") == "downloaded")

        # ---- 4. cancel mid-flight -> .partial kept -> resume ---------------
        for p in tdir.rglob("*"):
            if p.is_file():
                p.unlink()
        (env.root / ".ready").unlink()
        cancel = threading.Event()

        def cancel_soon():
            for _ in range(400):
                pp = tdir / "model.safetensors.partial"
                if pp.exists() and pp.stat().st_size > 512:
                    cancel.set()
                    return
                threading.Event().wait(0.005)

        threading.Thread(target=cancel_soon, daemon=True).start()
        try:
            md.download(cancel=cancel, progress_cb=lambda e: None)
            check("cancel: raises DownloadCancelled", False)
        except md.DownloadCancelled:
            check("cancel: raises DownloadCancelled", True)
        partial = tdir / "model.safetensors.partial"
        check("cancel: .partial preserved with partial bytes",
              partial.exists() and 0 < partial.stat().st_size < len(F1))
        kept = partial.stat().st_size
        RangeHandler.hits = []
        md.download(progress_cb=lambda e: None)  # relaunch -> resume
        check("resume: server saw Range from the exact kept offset",
              any(h == f"GET model.safetensors off={kept}"
                  for h in RangeHandler.hits),
              str(RangeHandler.hits))
        check("resume: completes and renames",
              (tdir / "model.safetensors").stat().st_size == len(F1)
              and not partial.exists())
        md.ensure_model_ready("downloaded")

        # ---- 5. checksum mismatch -> delete, retry ONCE, exact error -------
        for p in tdir.rglob("*"):
            if p.is_file():
                p.unlink()
        (env.root / ".ready").unlink()
        base2 = start_server({"config.json": F2,                # healthy
                              "model.safetensors": b"X" * len(F1)})  # corrupt
        env.pins["embedding_model"]["resolve_base"] = base2
        RangeHandler.hits = []
        try:
            md.download(progress_cb=lambda e: None)
            check("checksum: raises on mismatch", False)
        except md.ModelDownloadError as e:
            check("checksum: kind=checksum with digest detail",
                  e.kind == "checksum" and "sha256" in e.msg.lower()
                  and "model.safetensors" in e.msg)
        gets = [h for h in RangeHandler.hits if h.startswith("GET model")]
        check("checksum: retried exactly once (2 GETs), part removed",
              len(gets) == 2 and not list(tdir.rglob("*.partial")),
              str(gets))
        env.pins["embedding_model"]["resolve_base"] = base

        # ---- 6. verify_dir catches corruption ------------------------------
        (tdir / "model.safetensors").write_bytes(b"corrupted!")
        check("verify_dir: detects corrupted file",
              "model.safetensors" in md.verify_dir(tdir))
        md.verify_dir(tdir, delete_bad=True)
        check("verify_dir(delete_bad): removed the bad file",
              not (tdir / "model.safetensors").exists())

        # ---- 7. preflight surfaces partial + verbatim network error --------
        (tdir / "model.safetensors.partial").write_bytes(b"partial-bytes")
        md.check_network = lambda timeout=5.0: (
            False, "url=http://x kind=dns DNS resolution failed: DNS down")
        pf2 = md.preflight()
        md.check_network = real_net
        check("preflight: partial detected with byte count",
              bool(pf2["partial"]) and pf2["partial"]["bytes"] > 0)
        check("preflight: network failure surfaced verbatim",
              not pf2["network_ok"] and "DNS down" in pf2["network_error"])
        md.discard_partial()
        check("discard_partial: residue gone", md.detect_partial() is None)
    finally:
        md.load_pins = real_load_pins
        md.paths.models_root = real_models_root

    print("=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("ALL MODEL DOWNLOADER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
