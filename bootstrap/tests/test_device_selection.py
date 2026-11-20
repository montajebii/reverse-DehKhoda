"""v1.3.2: DEHKHODA_DEVICE / USE_GPU selection + accelerator fallback.

Run: python3 -m pytest bootstrap/tests/test_device_selection.py -v
No GPU required — torch's detection calls are monkeypatched.
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, "backend")
sys.path.insert(0, ".")

from app.config import Settings  # noqa: E402
from app.embeddings import provider  # noqa: E402


def _settings(device: str = "") -> Settings:
    return Settings(device=device, _env_file=None)  # type: ignore[call-arg]


def _torch(cuda: bool, mps: bool = False):
    """A minimal fake torch module mirroring the API resolve_device uses."""
    t = types.SimpleNamespace()
    t.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    t.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: mps))
    return t


class ResolveDeviceTests(unittest.TestCase):
    def test_default_autodetect_cpu_when_no_torch(self):
        with mock.patch.dict(sys.modules, {"torch": None}):
            self.assertEqual(provider.resolve_device(_settings()), "cpu")

    def test_autodetect_cuda(self):
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=True)}):
            self.assertEqual(provider.resolve_device(_settings()), "cuda")

    def test_autodetect_mps_on_mac(self):
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=False, mps=True)}):
            with mock.patch.object(sys, "platform", "darwin"):
                self.assertEqual(provider.resolve_device(_settings()), "mps")

    def test_autodetect_cpu_when_no_accelerator(self):
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=False)}):
            self.assertEqual(provider.resolve_device(_settings()), "cpu")

    def test_pinned_cpu_overrides_available_cuda(self):
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=True)}):
            self.assertEqual(provider.resolve_device(_settings("cpu")), "cpu")

    def test_pinned_cuda_used_verbatim(self):
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=False)}):
            self.assertEqual(provider.resolve_device(_settings("cuda")), "cuda")

    def test_use_gpu_alias_selects_cuda(self):
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=True)}):
            with mock.patch.dict(os.environ, {"DEHKHODA_USE_GPU": "1"}):
                self.assertEqual(provider.resolve_device(_settings()), "cuda")

    def test_use_gpu_alias_falls_back_to_cpu_without_accelerator(self):
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=False)}):
            with mock.patch.dict(os.environ, {"DEHKHODA_USE_GPU": "1"}):
                self.assertEqual(provider.resolve_device(_settings()), "cpu")

    def test_broken_torch_falls_back_to_cpu(self):
        broken = types.ModuleType("torch")  # raises on attribute access setup
        def boom(*a, **k):
            raise RuntimeError("broken driver")
        broken.cuda = types.SimpleNamespace(is_available=boom)
        with mock.patch.dict(sys.modules, {"torch": broken}):
            self.assertEqual(provider.resolve_device(_settings()), "cpu")


class GetEmbedderSignatureTests(unittest.TestCase):
    def test_get_embedder_default_resolves_device(self):
        """No explicit device -> resolve_device decides (CPU torch = cpu)."""
        with mock.patch.dict(sys.modules, {"torch": _torch(cuda=False)}):
            emb = provider.get_embedder(_settings())
            self.assertEqual(emb.device, "cpu")

    def test_explicit_cpu_keeps_historical_behaviour(self):
        emb = provider.get_embedder(_settings(), device="cpu")
        self.assertEqual(emb.device, "cpu")


class ChainOrderTests(unittest.TestCase):
    """v1.3.1-hotfix4: bundled snapshot present -> qdrant_native tried first,
    regardless of a persisted milvus preference; and launch_qdrant_local
    refuses to silently start when a snapshot is bundled."""

    def test_local_mode_refuses_when_snapshot_bundled(self):
        from bootstrap import backend_launcher, paths

        fake_snap = "/tmp/fake-dehkhoda.snapshot.gz"
        em = backend_launcher.Emitter("db")
        with mock.patch.object(paths, "bundled_snapshot",
                               return_value=fake_snap):
            with mock.patch.dict(os.environ,
                                 {"DEHKHODA_ALLOW_LOCAL_WITH_SNAPSHOT": ""}):
                with self.assertRaises(RuntimeError) as ctx:
                    backend_launcher.launch_qdrant_local(em)
        self.assertIn("native qdrant", str(ctx.exception))

    def test_local_mode_allowed_with_escape_hatch(self):
        from bootstrap import backend_launcher, paths

        em = backend_launcher.Emitter("db")
        with mock.patch.object(paths, "bundled_snapshot", return_value=None):
            with mock.patch.object(
                    backend_launcher, "_probe_hybrid", return_value=2):
                with mock.patch("qdrant_client.QdrantClient") as qc:
                    qc.return_value.close = lambda: None
                    data = backend_launcher.launch_qdrant_local(em)
        self.assertEqual(data["backend"], "qdrant_local")


if __name__ == "__main__":
    unittest.main()
