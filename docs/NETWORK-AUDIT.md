# NETWORK-AUDIT.md — v1.3.1 zero-network audit (full variant)

Scope: the **full** variant must make **ZERO outbound network requests**
after the user downloads the artifact — not on first launch, not on any
subsequent launch, not during index restore, not during search, not on any
error path. The CI acceptance observes packets with tcpdump and asserts an
exact count of **0** (Test A, four distros, `--network none`).

This audit (spec PART 7 Step 1a) lists **every** source of network activity
found in the codebase and its disposition. Audit date: 2026-10-28 (v1.3.1
work cycle, base commit 476c98e).

## Summary

| # | Source | Location | Disposition | Verified by |
|---|--------|----------|-------------|-------------|
| A1 | HuggingFace hub client (model load) | `backend/app/embeddings/provider.py:51` `SentenceTransformer(...)` | **CLOSED (code)** — `apply_hf_offline_env()` sets all six switches before the import; `local_files_only=True` whenever the model id is a local dir (always true in bundled full installs) | `bootstrap/tests/test_net_guard.py` (socket-level monkeypatch: model env + connect-block), CI unit test `test_offline_guard.py` (bundled model load with `socket.connect` raising on non-loopback), Test A tcpdump=0 |
| A2 | HuggingFace offline switches missing process-wide | `bootstrap/cli.py` `main()`, `backend/scripts/entry.py` `main()` | **CLOSED (code)** — both entrypoints call `net_guard.activate()` before any step / app import; Rust shell sets the same switches in `spawn_cmd` (`desktop/src-tauri/src/main.rs`) | `test_net_guard.py`, Test A tcpdump=0 |
| A3 | Lean model download (huggingface.co) | `bootstrap/model_download.py` (HEAD probe + Range download) | **KEEP (lean only)** — the resumable pinned downloader is the lean variant's entire purpose; never reachable in full (guard raises + wizard step is short-circuited by the bundled-model fast path) | `test_model_download.py` (fake local HF server), Test B online acceptance |
| A4 | Dataset dumps download (raw.githubusercontent.com) | `bootstrap/model_fetcher.py:fetch_dumps`, `backend/app/ingestion/dehkhoda_loader.py:download_dumps` | **CLOSED (code)** — `dehkhoda_loader.BASE_URL` was **unpinned `master`** → now pinned to the immutable commit sha in pins.json; in the full variant `ensure_dumps()` receives the bundled dumps dir, so no fetch fires at all; the socket guard hard-fails any residual attempt | `bootstrap/tests/test_pins_sync.py`, Test A tcpdump=0, Test C |
| A5 | Qdrant / docker-compose fallback downloads (github.com releases) | `bootstrap/model_fetcher.py:fetch_qdrant/fetch_compose_binary`, called from `backend_launcher.launch_qdrant` / `compose_invocation` | **KEEP (fallback rungs, unreachable in full)** — full ships the qdrant sidecar (gzip + sha256) so `_qdrant_from_bundled_gz` always resolves; compose fetch is only on the opt-in Milvus path; guard raises if either is ever attempted in full | acceptance 2 (sidecar self-extract offline), Test A tcpdump=0 |
| A6 | Tauri auto-updater | none found — `tauri.conf.json` has **no** `plugins.updater`; `Cargo.toml` has no `tauri-plugin-updater`; zero `updater` hits in Rust/JS | **NONE (verified)** — nothing to remove; do not add one for full | `rg -i "updater" desktop/` → 0 hits; Test A tcpdump=0 |
| A7 | Telemetry / analytics / crash reporting (sentry, posthog, mixpanel, segment, amplitude, plausible, umami, backtrace) | whole-repo grep → only `QDRANT__TELEMETRY_DISABLED=true` (disables qdrant's own telemetry, `backend_launcher.py:535`) and the English word "plausible" in HANDOFF prose | **NONE (verified)** — no SDK, no endpoint, nothing to remove | whole-repo grep in CI (`release.yml` packaged-AppImage grep), Test A |
| A8 | Web fonts / icons from CDN (fonts.googleapis.com, gstatic, jsdelivr, unpkg, fontawesome) | `desktop/ui/styles.css` had a `font-family: "Vazirmatn"` rule but **no @font-face** (font file shipped but unreferenced — system Tahoma fallback was actually used) | **CLOSED (code)** — `@font-face` added with a relative `fonts/Vazirmatn-Regular.woff2` src; icons are inline SVG | CI grep of the extracted AppImage UI bundle for `http(s)://` (Test A step 9) |
| A9 | External URLs in the UI bundle | `wizard.js:534` `MODEL_RELEASE_URL` (displayed text + explicit user click `window.open`), `app.js:355` `http://127.0.0.1:<port>` (loopback sidecar), `tauri.conf.json` `$schema` (build-time IDE hint, not runtime) | **KEEP (display-only / loopback / build-time)** — none execute a network request from the app itself; the wizard never opens the URL without a user click, and only on the lean no-network error card | CI URL grep whitelist (tauri://localhost, relative paths, loopback) |
| A10 | Version check / health ping / license check to a public server | none found — all HTTP from the shell/launcher is loopback (`/readyz` on 127.0.0.1, Milvus 127.0.0.1:19530) | **NONE (verified)** | Test A tcpdump=0 |
| A11 | DNS lookups | none executed by the app in full (no external hostnames are resolved; `net_guard` blocks `getaddrinfo` for non-loopback names pre-packet) | **CLOSED (guard)** | Test A asserts no UDP/53 packets |
| A12 | Windows OS-level phone-home (Windows Error Reporting, SmartScreen checks) | outside the app process — unsigned-binary SmartScreen / WER traffic is the OS acting on our binary, not the app making requests | **DOCUMENTED** in HANDOFF.md — the app itself makes zero requests; WebView2 bootstrapper download is NOT triggered by the app (WebView2 must already be present; the app fails with a clear message otherwise) | manual (documented procedure in HANDOFF.md) |

## Enforcement layers (defense in depth)

1. **Build time** — the full variant stages model + dumps + snapshot and
   writes `_internal/bundle.json` (`{"variant":"full","model":true,
   "dataset":true,"index":true}`). Lean stages none of them.
2. **Shell (Rust)** — `spawn_cmd` reads the manifest and sets
   `DEHKHODA_NO_NETWORK=1` + the six HF switches on every sidecar spawn
   (HTTP server and wizard steps alike).
3. **Python entrypoints** — `bootstrap.cli.main()` and
   `backend/scripts/entry.py` call `net_guard.activate()` before any heavy
   import (idempotent; lean builds are untouched).
4. **Library level** — `provider.apply_hf_offline_env()` +
   `SentenceTransformer(..., local_files_only=True)` as the last line of
   defense right before the import.
5. **Runtime guard** — `net_guard.install()` monkeypatches
   `socket.socket.connect` / `socket.create_connection` /
   `socket.getaddrinfo`; any non-loopback target raises
   `NetworkAttemptError` with the Persian fail-safe message
   «این نسخه نباید به اینترنت متصل شود…» before a packet can be sent.
6. **CI** — Test A (four distros, `--network none`): tcpdump asserts 0
   non-loopback packets, 0 UDP/53 DNS packets; extracted-AppImage UI
   bundle grepped for external URLs; `/health` must report
   `source: bundled`; socket-level unit test loads the bundled model with
   `connect()` monkeypatched to raise.

## Verification matrix

| Layer | Local | CI |
|---|---|---|
| env switches | `test_net_guard.py` | windows-bootstrap + linux gates |
| socket guard raise/allow | `test_net_guard.py` | same |
| model load with zero connect() | `test_offline_guard.py` (needs local model dir; skipped when absent) | release build gate with the bundled tarball |
| packaged-AppImage URL grep | — | Test A step 9 (all four distros) |
| tcpdump 0 packets | — | Test A (all four distros) |
| `/health source: bundled` | — | Test A step 8 |

## Deliberate non-closures (lean variant only, by design)

* `model_download.py` / `model_fetcher.py` download paths — the lean
  variant's first-run downloader. In the full variant these code paths are
  unreachable: the wizard short-circuits on the bundled model/index and the
  socket guard raises before any byte is sent. CI covers them with a fake
  local HF server (never live HuggingFace).
* The pinned dataset/qdrant/compose URLs in `model_fetcher.py` — used by
  the CI snapshot job and lean first-run; never by the full variant.
