# HANDOFF — v1.3.1 (truly-offline full variant) + v1.3.0 real-desktop testing

## 0. What changed in v1.3.1 (read this first)

The **full** variant now bundles the model + the 33 gzipped SQL dumps + a
**prebuilt Qdrant snapshot**, and makes **ZERO outbound network requests
after the user downloads the artifact** — not on first launch, not on any
launch, not during index restore, not during search, not on any error path.
The CI acceptance observes **exactly 0 tcpdump packets** (not "almost 0",
not "only DNS" — 0) in clean `--network none` containers on four distros.
The **lean** variant is unchanged (first-run download of model + dataset,
resumable; kept so the ingestion path stays CI-tested).

* The full variant's user persona **never touches the network again**.
* Future model/vector-config changes rebuild the index fully offline from
  the bundled SQL (`--rebuild-index`) — that is the entire point of
  bundling the raw dumps (v1.3.1 spec, non-negotiable).
* Enforcement layers + audit table: `docs/NETWORK-AUDIT.md`.
* Wizard repair + support: `dehkhoda-server bootstrap --step index
  --diagnostics` produces a single `.zip` (OS, glibc, engine chain log,
  qdrant log, backend log, last state, /health JSON, one sample search,
  pins metadata, `index.ready`) — this replaces the "paste this" blocks
  below for v1.3.1 installs (kept for v1.3.0 troubleshooting).

### Full offline test (manual, Windows or any real desktop)

Linux CI already asserts this automatically (Test A on ubuntu:24.04,
debian:12, fedora, arch — `bootstrap/tests/full_offline_acceptance.sh`).
Manual reproduction on a real machine:

```bash
# 1. disconnect completely
nmcli networking off          # or unplug the cable / disable Wi-Fi
# 2. run the full AppImage and complete the wizard
./Dehkhoda.RAG_<v>_x86_64-full.AppImage
# 3. in a root shell, capture EVERYTHING that is not loopback:
sudo tcpdump -i any -nn 'not host 127.0.0.1 and not host ::1' -w /tmp/pcap
#    ... run the whole wizard + one search, then Ctrl-C and read it:
tcpdump -r /tmp/pcap | wc -l        # expectation: EXACTLY 0
tcpdump -r /tmp/pcap 'udp port 53' | wc -l   # DNS: EXACTLY 0
# 4. verify the install
<sidecar> bootstrap --step index --verify-index   # exit 0
curl -s http://127.0.0.1:<port>/health            # "engine":"ready","source":"bundled"
```

Platform-specific caveats (outside our control, documented per spec):

* **Windows SmartScreen/WER**: the app itself makes zero requests, but
  Windows Error Reporting / SmartScreen checks for an unsigned binary are
  OS-level traffic we cannot suppress — they are NOT the app phoning home.
* **WebView2** must already be present (pre-installed on Win10/11). If it
  is missing, the app fails with a clear message rather than downloading
  it; Tauri's WebView2 bootstrapper download is not triggered by us.
* **Manual Windows tcpdump procedure** (deviation ACCEPTED in the v1.3.1
  spec review): install Wireshark/tshark, or use the built-in `pktmon`
  with this exact sequence:

  ```text
  pktmon start --capture --pkt-size 0 -f C:\pktmon.etl
  ... run the app ...
  pktmon stop
  pktmon etl2txt C:\pktmon.etl
  ```

  Then filter `not ip.addr == 127.0.0.1` in the txt output, run the
  wizard, and expect 0 non-loopback packets.

### Rebuild index offline (future model / vector-config changes)

```bash
dehkhoda-server bootstrap --step index --rebuild-index
# uses ONLY the bundled SQL (no network); recomputes chunks + BM25 stats +
# vectors with the CURRENT model/config; verifies record_count against the
# pin; writes a fresh index.ready. CI Test C proves this path on 4 distros
# with --network none (0 packets).
dehkhoda-server bootstrap --step index --verify-index   # metadata + live count
```

Constraints (spec PART 4): the snapshot pin (qdrant_version, model
revision, dataset sha) is checked on every start — any drift marks the
index `stale` and triggers an explicit rebuild-or-download decision, never
a silent mismatch. Never ship a new qdrant binary without regenerating the
snapshot in the same release.

### CI fixes applied during v1.3.1

| Commit | Fix |
|---|---|
| `dee8667` | zero-network closure: HF offline env + `local_files_only`, socket guard with the Persian fail-safe, dataset URL pinned to the immutable commit (was unpinned `master`), @font-face for the bundled Vazirmatn, NETWORK-AUDIT.md |
| `fdd5dff` | prebuilt snapshot restore + offline rebuild + `build-index-snapshot` CI job (production ingestion once per release, pin verify-or-define) + size gates + published-asset parity check |
| `a0fa00f` | three-behavior wizard dictionary step + Test A/Test C offline acceptance (tcpdump == 0 on 4 distros) + `/health engine/source` contract |

*(fill further rows as CI runs; every fix lands with its real commit sha
and a one-line reason — same rule as v1.3.0.)*

### Known limitations in v1.3.1

1. **Windows full-offline tcpdump verification is manual** (no Windows
   containers in CI); the snapshot-restore path itself IS machine-verified
   by the windows runner restore gate, and Linux Test A is automated.
2. Test C runs the rebuild with the **hash embedder** to keep CI inside
   the 6-hour job limit (deviation ACCEPTED in the v1.3.1 spec review).
   Test C's purpose is to prove the offline rebuild works, which is
   embedder-independent — chunking/record counts do not depend on the
   embedder. Real-e5 rebuild quality is validated by the snapshot job
   itself AND by a manual full-offline test on Arch.
3. The `build-index-snapshot` job runs the full 312k-row e5 ingestion on a
   CI runner (hours, once per pins change) — expected, cached, and the
   reason the pin commit must land BEFORE the release tag.
4. v1.3.0 limitations 2–7 remain where still applicable (unsigned Windows
   binaries, `x86_64` asset naming, red non-gating `ci` workflow).

---

# HANDOFF — v1.3.0 real-desktop testing (historical)

Released: tag `v1.3.0` → commit `b302beb` (see «CI fixes applied» — your local
`81d2134` build predates 5 release-blocking fixes).
Release page: https://github.com/montajebii/reverse-DehKhoda/releases/tag/v1.3.0

> v1.3.0 note: limitation 1 below (dataset not bundled in either variant)
> is FIXED for the full variant in v1.3.1 — lean keeps the first-run
> dataset download by design.

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

## 9. v1.3.1 chunker truncation audit (2026-10-29, pre-pin gate)

Full-corpus replay of the exact `chunk_definition` (extracted from the
shipped source via AST, run over all 312,506 usable rows) against the
pinned e5 tokenizer (`tokenizer.json` sha256-verified, `max_seq_length`
512 from the pinned `sentence_bert_config.json`):

* Buggy else-branch (no hard-split when a >1049-char sentence follows a
  non-empty chunk): 310 rows emitted oversized chunks (max 13,130 chars),
  630 of 350,829 chunks (0.180%) exceeded 512 tokens and were silently
  truncated by sentence-transformers (vector = first ~510 tokens; no
  exception, no log). Worst vector covered ~13% of its chunk text.
* After the else-branch hard-split fix: 351,156 chunks, 384 (0.109%)
  still exceed 512 tokens — ALL <=1200 chars, worst 845 tokens, caused by
  vocalized Arabic harakat tokenizing as separate XLM-R tokens (down to
  0.71 chars/token), which the char-based contract cannot bound.
* Mitigation shipped: BM25 sparse + stored payload always cover the full
  chunk text (lexical/RRF retrieval and displayed definitions are
  complete); residual documented as `known_truncations: 384` in
  `bootstrap/pins.json`. Token-aware chunking = v1.3.2.
* Evidence scripts: `scripts/chunker_evidence.py`,
  `scripts/chunker_followup.py`, `scripts/postfix_global.py`
  (work dir `scripts/work/`).

## 10. v1.3.1 known quality limitations

The SAFEGUARD-2 merge probes (3 fixed Persian reverse-dictionary queries,
run against the merged collection with the pinned
`intfloat/multilingual-e5-base` encoder) pass all their hard gates
(≥ 1 hybrid RRF hit, top dense cosine > 0.5, non-empty definition payload),
but they expose a real semantic-quality issue in the dense channel.
Observed in the 2026-10-31 full local dry-run (real shard artifacts, pinned
qdrant `d60f8c31`, pinned model) and to be re-confirmed on the attempt-5
merge job:

| Probe (verbatim) | Top dense hit | Verdict |
| --- | --- | --- |
| «احساس بی‌قراری و اضطراب» (restlessness & anxiety) | «بی دغدغگی» (carefree) | OPPOSITE of the intended sense |
| «حالت ترس و وحشت شدید» (intense fear & terror) | «ترسندگی» (timidity/fearfulness) | correct ✓ |
| «کسی که بسیار سخاوتمند است» (someone very generous) | «افزون خواستن» (greedy) | OPPOSITE of the intended sense |

Reading: this is an **encoder limitation, not a merge-pipeline bug**. The
merge independently verifies counts (351,156), the chunk/truncation sums
(384), per-shard sha256 and snapshot integrity; the probes prove the
collection carries real, non-empty definitions. What e5-base's Persian
space does not reliably separate is antonym pairs (carefree ↔ anxious,
greedy ↔ generous): the query embedding lands nearer the antonym's vector
than the synonym's, and the sparse channel does not rescue the dense top-1.
**Do NOT change the model for v1.3.1** — that would require rebuilding the
snapshot and break the pin contract (`prebuilt_index.snapshot_sha256`).
Production exposure is mitigated by hybrid BM25 + RRF ranking; the dense
top-1 alone is not the user-facing ordering.

## 11. v1.3.2 candidates

* Evaluate `BAAI/bge-m3` on the same 3 SAFEGUARD-2 probes; require 3/3
  sense-correct top hits before considering a re-pin. (Note: pins.json
  currently carries only `intfloat/multilingual-e5-base` — bge-m3 must be
  added via `scripts/gen_pins.py` first.)
* Hybrid weighting tuning — increase the sparse weight in the fusion mix
  (lexical BM25 matching is unaffected by the antonym problem).
* Consider a Persian-specific reranker on top-N results (cross-encoder
  over the RRF top-24, re-rank before display).
* Test with real users on real queries — collect per-query relevance
  feedback on top-3 results before locking the v1.3.2 model choice.

## 12. Why Qdrant snapshot shas are non-deterministic (v1.3.1 release-train root cause)

Qdrant snapshot containers embed a **runtime collection UUID and timestamps**
(observe the snapshot filename itself: `dehkhoda-1262086191253432-2026-10-31-07-01-41-0.snapshot` — the number is per-instance entropy). Therefore:

* **Byte-identity CANNOT be reproduced by re-merging from the same shards.**
  Proof: run 41143446238 (attempt 5) -> `1718d119…` (1,240,174,914 B); run
  41178302834 -> `0ebf98a7…` (1,241,479,202 B). Identical data — same
  record_count (351,156), same known_truncations (384), probe dense top scores
  identical to every printed digit ([0.8556328, 0.8596259, 0.85398567]) —
  different container bytes (+1,304,288 B of UUID/timestamp entropy).
* **The pin's semantic is: "the EXACT BYTES of this specific file we ship"**
  (`prebuilt_index.snapshot_sha256` is a byte-level contract, not a data-level
  one).
* **Convergence requires REUSE, not REBUILD.** The acquire order is:
  actions/cache restore (tag-scope cache) -> download the pinned
  `source_run` artifact (`prebuilt_index.source_run` = 41143446238,
  `source_artifact` = index-snapshot) -> rebuild.
* **Rebuild only happens when the underlying data changes** (model, chunker,
  corpus). That is a **pin-bump event** (new bytes, re-run PIN-FILL, new
  `snapshot_sha256`, bump the cache `r<N>` key) — **not a retry event**.
  Retrying a rebuild against a byte-exact pin can never converge.

Release-train history that lead here (v1.3.1):

| Run | Trigger | Outcome |
| --- | --- | --- |
| 41148366899 (#40) | tag v1.3.1 @ 6e44d70 | 5× ingest-shard cache HIT then died in the cached-shard verify heredoc: `shard_out/` vs `shard-out/` filename typo (release.yml lines 241-242). Fixed in 396ca0e. |
| 41178302834 (#41) | tag v1.3.1 @ 396ca0e | Typo fix validated (5/5 shards cache HIT + verified). merge-index: merge cache MISS (cache scoped to `ci/merge-seed-fix`, invisible to tag runs — key string byte-identical, no drift) -> real merge -> data identical, container bytes `0ebf98a7…` != pin `1718d119…` -> MERGE-FATAL (SAFEGUARD worked as designed). |
| fix (this commit) | — | Option A: `bootstrap/merge_index.py` gains `acquire_snapshot()` (`acquire` CLI: cache-hit -> source_run artifact download -> rebuild signal, with SNAPSHOT-IDENTITY-FAIL data checks: record_count, known_truncations, 3 probe scores ±0.01); release.yml merge-index job gains the "Acquire pinned snapshot" step and re-gates cached/rebuild steps on `steps.acquire.outputs.snapshot_source`; pins.json prebuilt_index gains `source_run`/`source_artifact`/`source_artifact_expires` provenance. |

### v1.3.1 post-release action (do NOT do during the release train)

After v1.3.1 ships, upload the snapshot bytes from run 41143446238 to the
v1.3.1 release page as a permanent asset named
`dehkhoda-snapshot-v1.3.1.snapshot.gz` (sha256 `1718d119…`). The workflow
artifact it currently lives in expires 2026-11-13; the release asset does not.
Future patch releases must not depend on workflow artifact retention.

### Known limitation, not fixed in this release (non-gating)

`ci.yml` Tauri build failures are a separate non-gating workflow. Documented
here as a known limitation; NOT fixed in the v1.3.1 release train (which
touches release.yml only). Does not block v1.3.1 shipping.
