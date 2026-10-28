# HANDOFF — v1.3.0 real-desktop testing

Released: tag `v1.3.0` → commit `b302beb` (see «CI fixes applied» — your local
`81d2134` build predates 5 release-blocking fixes).
Release page: https://github.com/montajebii/reverse-DehKhoda/releases/tag/v1.3.0

## 1. Direct downloads

| File | Size | sha256 |
|---|---|---|
| [Dehkhoda.RAG_1.3.0_x86_64-full.AppImage](https://github.com/montajebii/reverse-DehKhoda/releases/download/v1.3.0/Dehkhoda.RAG_1.3.0_x86_64-full.AppImage) | 1210 MB | `b80717e6c99a4ee87fd70ee1d2f7476a15166545ca1c1de977cb90ea29b20bfc` |
| [Dehkhoda.RAG_1.3.0_x86_64-lean.AppImage](https://github.com/montajebii/reverse-DehKhoda/releases/download/v1.3.0/Dehkhoda.RAG_1.3.0_x86_64-lean.AppImage) | 557 MB | `30c08127a87ebf147ae881bd72fa8c747534b41dab2d2f9327ffc73b2fc1f32e` |
| [dehkhoda-setup-win-1.3.0-full.exe](https://github.com/montajebii/reverse-DehKhoda/releases/download/v1.3.0/dehkhoda-setup-win-1.3.0-full.exe) | 751 MB | `fe844aaf9a8d51934c966cf0a0bfed50cf2a88ca19d94169602412789c55f463` |
| [dehkhoda-setup-win-1.3.0-lean.exe](https://github.com/montajebii/reverse-DehKhoda/releases/download/v1.3.0/dehkhoda-setup-win-1.3.0-lean.exe) | 238 MB | `67715416dad56778ae0987aa287d3582dd1fa3546ba32113a3843f2d1aebda43` |
| [SHA256SUMS.all](https://github.com/montajebii/reverse-DehKhoda/releases/download/v1.3.0/SHA256SUMS.all) | 625 B | — |

Also on the page (since v1.2.x, unchanged scope): `Dehkhoda.RAG_1.3.0_amd64.deb`
(full, offline) and `dehkhoda-rag-1.3.0-1-x86_64.pkg.tar.zst` (Arch).
Verify with `sha256sum -c SHA256SUMS.all` — verified 6/6 OK on the freshly
published assets.

## 2. Test scenarios

### A. Lean resume (Linux, network ON)
1. `chmod +x Dehkhoda.RAG_1.3.0_x86_64-lean.AppImage && ./Dehkhoda.RAG_1.3.0_x86_64-lean.AppImage`
2. Wizard: accept defaults until the «مدل زبانی» step; start the download.
3. At 30–50 %, kill the app hard: `pkill -f dekhkhoda` (or close + `pkill -f dehkhoda-server`).
4. Inspect partials: `ls -la ~/.local/share/dehkhoda/models/<revision>/` — expect `*.partial` files with sizes > 0.
5. Relaunch the AppImage, reach the model step again → expect «دانلود مدل ناقص است. ادامه می‌دهید؟» → confirm.
6. PASS criteria: progress resumes from ~the same percent (no restart from 0);
   the log/progress shows the resumed byte offset; on completion the file is
   renamed (no `.partial` left) and the wizard advances to «آماده‌سازی دهخدا».

### B. Full offline (Linux, network DISABLED)
1. Download + install the **full** AppImage while still online (dataset is NOT
   bundled — see limitations!).
2. Disconnect: `nmcli networking off` (or unplug).
3. Run the full AppImage. Wizard must show «مدل زبانی همراه بسته نصب شد.» and
   NOT offer a download step.
4. Zero-outbound check: `sudo tcpdump -i any -n host huggingface.co` in a
   root shell while the wizard runs → expect 0 packets (qdrant binds
   127.0.0.1 only).
5. Complete the wizard (dataset still needs the network — do the ingest step
   BEFORE disconnecting, then re-test model/offline steps with network off).
6. PASS criteria: wizard completes, `جست‌وجو` returns hybrid results.

### C. Windows 11 — no console window
1. Install `dehkhoda-setup-win-1.3.0-{full,lean}.exe` (SmartScreen will warn —
   see limitations).
2. Launch from the Start menu. PASS criteria: **no console/terminal window
   appears at any point** — launch, wizard steps, engine start, search.
3. Check Task Manager → only `Dehkhoda RAG.exe` + `dehkhoda-server.exe`
   (+ `qdrant.exe`), no `cmd.exe`/`conhost.exe` spike tied to the app.

### D. Search quality (real e5, not the CI hash embedder)
1. On any variant with the model ready, run 5–10 Persian headword queries
   (e.g. «آب», «حکمت», «دیوان») in the search box.
2. PASS criteria: top hits are plausible Dehkhoda headwords; the hybrid
   (dense+sparse RRF) ordering is stable across re-runs; no «نتیجه‌ای یافت
   نشد» on common words.

## 3. Logs

| Platform | Path |
|---|---|
| Linux | `~/.local/share/dehkhoda/logs/` — `backend.log` (sidecar stdout/stderr), `qdrant.log` |
| Windows | `%LOCALAPPDATA%\Dehkhoda\logs\` — same two files (note the capital «D») |

There is **no** `wizard.log` / `model-download.log` file: wizard + download
progress are streamed to the wizard UI as JSONL events (a file sink is a
v1.3.1 candidate).

## 4. If this fails, paste this

- **"engine not available"**
  ```bash
  tail -n 100 ~/.local/share/dehkhoda/logs/backend.log ~/.local/share/dehkhoda/logs/qdrant.log
  ```
  (Windows: `Get-Content "$env:LOCALAPPDATA\Dehkhoda\logs\backend.log" -Tail 100`)
- **"download stuck"**
  ```bash
  ls -la ~/.local/share/dehkhoda/models/*/ ; du -sh ~/.local/share/dehkhoda/models/*/
  ```
  (progress lives in the wizard UI; there is no model-download.log — include a
  screenshot of the «مدل زبانی» step's error card + «جزئیات فنی» panel)
- **"search returns nothing"**
  paste the wizard's «جزئیات فنی» panel content, plus:
  ```bash
  curl -s http://127.0.0.1:8000/health   # port as shown in backend.log
  ```
- **"AppImage won't run"**
  ```bash
  uname -a; cat /etc/os-release | head -2; ldd --version | head -1; ls -la /dev/fuse
  ```
  (glibc ≥ 2.36 required — binaries are built in debian:12; FUSE is not needed:
  the AppImage self-extracts)

## 5. Re-run the wizard (repair)

The wizard opens automatically when `setup-state.json` has `"complete": false`.
Force it:
```bash
dehkhoda-server bootstrap --step state --set complete=false   # then relaunch the app
```
Completed steps are skipped (idempotent); only failed/incomplete ones re-run.
Full CLI is also available: `dehkhoda-server bootstrap --step report` (dry
system report, no side effects).

## 6. CI fixes applied during the v1.3.0 release

| Commit | Fix |
|---|---|
| `4dacaa2` | restore `npx tauri build --no-bundle` lost in the build-windows step split (NSIS prelude died: no `dehkhoda-rag.exe`) |
| `98de2ec` | `state_env.resolve_model_dir`: `NameError: Path` (missing import) crashed all model-verify gates once `setup-state.json` carried `model.dir` + regression test + de-blinded gate stderr |
| `c011bac` | `events.emit` → `ensure_ascii` JSONL: Persian `task_fa` over a piped Windows stdout (cp1252) raised `UnicodeEncodeError` — latent real-app bug on en-US Windows; + ascii-safety test |
| `007c1d6` | lean model-step results now report `"verified": true` (acceptance contract; verification itself always ran) |
| `0d8557a` | `.deb` renamed `space→dot` before hashing — GitHub asset names can't contain spaces, so the published SHA256SUMS.all referenced a name that didn't exist on the release page |
| `b302beb` | that rename step needs `shell: bash` (`${var// /./}` is bash-only; job default is sh/dash) |

**The released binaries are built from `b302beb`** — not `81d2134`. All fixes
are CI/packaging or crash-fixes; none change user-facing variant behavior
(the e5 model bytes, dataset pins, engine chain and UI theme are untouched).

## 7. Known limitations in v1.3.0

1. **Neither variant bundles the Dehkhoda dataset** (~51 MB gzipped; both
   download it on first run). If your network is restricted, the wizard's
   «آماده‌سازی دهخدا» step WILL fail in both variants — only the *model* is
   offline-guaranteed in full.
2. CI's `container-tests` job uses a hash embedder (no model); the
   `linux-acceptance` job DOES run the real e5 dense hybrid probe offline in
   4 distros × 2 variants — but only your manual tests validate real-world
   semantic quality (scenario D).
3. Windows binaries are **unsigned** — SmartScreen/Defender will warn
   («More info → Run anyway»).
4. Asset naming: AppImages are `x86_64` (not the `amd64` used in the .deb
   name / earlier planning); the .deb ships dot-normalized since `0d8557a`.
5. Windows installer sizes (751/238 MB) sit below the original 900–1100 /
   400–600 MB estimates because NSIS LZMA compresses the 1.13 GB model to
   ~513 MB inside the full installer — the full−lean delta (513 MB exe,
   654 MB AppImage) confirms the model is bundled in full and absent in lean.
6. Persian JSONL events are ASCII-escaped on the wire (cp1252-safe since
   `c011bac`); the wizard UI renders them identically.
7. The non-gating `ci` workflow (unit tests / PyInstaller / Tauri smoke) is
   red at its `tauri` jobs: the build script expects
   `resources/sidecars/qdrant/qdrant.gz`, which only the release workflow's
   prep steps stage (`resource path ... doesn't exist`). v1.2.1 shipped with
   `ci` red as well (it died earlier in the chain). Not release-gating;
   v1.3.1 candidate.

## 8. v1.3.1 candidates (not implemented, by scope)

- Persist wizard JSONL (incl. model-download progress) to
  `logs/wizard.log` / `logs/model-download.log` for post-mortems.
- Align AppImage asset naming to `amd64` (matches the .deb and the spec).
- `paths-ignore` for doc-only pushes (harmless today: the release workflow is
  tag-triggered only).
- Consider publishing per-variant SHA256SUMS alongside SHA256SUMS.all.
