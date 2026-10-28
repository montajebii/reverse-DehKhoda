#!/usr/bin/env bash
# =============================================================================
# bootstrap/tests/full_offline_acceptance.sh — v1.3.1 Test A + Test C
#
# Runs INSIDE a clean distro container started with `--network none` (the
# tcpdump-capable image is prepared beforehand with network ON — see
# release.yml linux-acceptance). Asserts, against the EXTRACTED FULL
# release AppImage:
#
#   Test A (default mode) — the zero-network guarantee, PART 2/3:
#     0. the bundle manifest declares model+dataset+index; 33 dumps and the
#        pinned snapshot are physically inside the AppImage
#     1. the wizard path (report -> db -> model -> ingest) completes fully
#        offline: the bundled snapshot RESTORES (target <60s; hard limit
#        120s with a slow-filesystem log line)
#     2. tcpdump on all non-loopback traffic for the ENTIRE run captures
#        EXACTLY 0 packets — not "almost 0", not one DNS lookup, 0
#     3. index.ready exists and --verify-index passes (metadata + live count)
#     4. the restored record count equals pins.prebuilt_index.record_count
#     5. one REAL Persian hybrid search returns >= 1 plausible headword
#     6. /health reports engine=ready and source=bundled
#     7. the UI bundle contains no external http(s) URLs (whitelist:
#        w3.org SVG xmlns, loopback, tauri scheme, the display-only
#        release-page URL — see docs/NETWORK-AUDIT.md A9)
#
#   Test C (--test-c) — offline rebuild extensibility, PART 3/4:
#     fresh install (fresh data dir), --rebuild-index with the hash
#     embedder (deviation, documented): rebuild uses ONLY the bundled SQL,
#     record_count matches the pin afterwards, hybrid search returns
#     results, and the whole run is tcpdump-wrapped (0 packets).
#
# Exit 0 = all assertions hold. Any failure prints the exact reason.
# =============================================================================
set -euo pipefail

REPO="${1:-/repo}"
MODE="${2:---test-a}"
cd "$REPO"

fail() { echo "ACCEPTANCE FAIL: $*"; exit 1; }

APPIMAGE=$(ls dist-linux/*-full.AppImage 2>/dev/null | head -1 \
  || ls dist-linux/*.AppImage | head -1)
[ -n "$APPIMAGE" ] || fail "no AppImage found in dist-linux/"
chmod +x "$APPIMAGE"
"$APPIMAGE" --appimage-extract >/dev/null
BIN=$(find squashfs-root -type f -name dehkhoda-server | head -1)
[ -n "$BIN" ] || fail "backend binary not found in AppImage"
chmod +x "$BIN"

export DEHKHODA_DATA_DIR=/tmp/dh-offline
rm -rf "$DEHKHODA_DATA_DIR"

# ---- bash-only HTTP helpers (containers may lack curl/wget) --------------
http_get() { # $1=port $2=path -> body on stdout
  local port="$1" path="$2"
  exec 3<>"/dev/tcp/127.0.0.1/${port}"
  printf 'GET %s HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n' "$path" >&3
  # strip headers (first blank line separates)
  sed '1,/^\r$/d' <&3
  exec 3<&- 3>&-
}
http_post() { # $1=port $2=path $3=body -> body on stdout
  local port="$1" path="$2" body="$3"
  local len=${#body}
  exec 3<>"/dev/tcp/127.0.0.1/${port}"
  printf 'POST %s HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: %s\r\n\r\n%s' \
    "$path" "$len" "$body" >&3
  sed '1,/^\r$/d' <&3
  exec 3<&- 3>&-
}

# ---- 0. bundle manifest + payload presence -------------------------------
echo "=== bundle payload presence (full variant) ==="
MANIFEST=$(find squashfs-root -path "*_internal/bundle.json" -type f | head -1)
[ -n "$MANIFEST" ] || fail "_internal/bundle.json manifest missing"
grep -q '"index": *true' "$MANIFEST" || fail "manifest index != true"
grep -q '"dataset": *true' "$MANIFEST" || fail "manifest dataset != true"
grep -q '"model": *true' "$MANIFEST" || fail "manifest model != true"
echo "manifest: $(tr -d '\n' < "$MANIFEST")"

NDUMPS=$(find squashfs-root -path "*_internal/dumps/*" -name '*.sql.gz' -type f | wc -l)
[ "$NDUMPS" = "33" ] || fail "expected 33 bundled .sql.gz dumps, found $NDUMPS"
SNAP=$(find squashfs-root -path "*_internal/index/*" -name '*.snapshot.gz' -type f | head -1)
[ -n "$SNAP" ] || fail "bundled snapshot missing from AppImage"
echo "dumps=33 OK, snapshot: $SNAP ($(wc -c < "$SNAP") bytes)"
BMODEL=$(find squashfs-root -type f -path "*_internal/models/*" -name "model.safetensors" | head -1)
[ -n "$BMODEL" ] || fail "bundled model.safetensors missing"

PIN_RECORDS=$(grep -o '"record_count": *[0-9]*' \
  "$(find squashfs-root -path '*bootstrap/pins.json' | head -1)" | head -1 | grep -o '[0-9]*')
[ -n "$PIN_RECORDS" ] && [ "$PIN_RECORDS" != "0" ] || \
  fail "pins.prebuilt_index.record_count is unset — the snapshot job must run first"

# ---- tcpdump guard for the WHOLE run -------------------------------------
echo "=== starting tcpdump guard (non-loopback traffic) ==="
rm -f /tmp/pcap
tcpdump -i any -nn 'not host 127.0.0.1 and not host ::1' -w /tmp/pcap \
  2>/tmp/tcpdump.err &
TCPDUMP_PID=$!
sleep 1
kill -0 "$TCPDUMP_PID" 2>/dev/null || fail "tcpdump failed to start: $(cat /tmp/tcpdump.err)"

run_step() { # $1=outfile, rest=argv
  local out="$1"; shift
  set +e
  "$@" >"$out" 2>"${out%.jsonl}.err"
  local rc=$?
  set -e
  if [ -s "${out%.jsonl}.err" ]; then echo "--- stderr ---"; tail -8 "${out%.jsonl}.err"; fi
  return $rc
}

T0=$(date +%s)
if [ "$MODE" = "--test-c" ]; then
  # =================================================================
  # TEST C — rebuild from bundled SQL, fully offline, fresh install
  # =================================================================
  echo "=== TEST C: --rebuild-index (hash embedder, documented deviation) ==="
  run_step /tmp/rebuild.jsonl env \
    DEHKHODA_SKIP_AUTO_RESTORE=1 \
    DEHKHODA_EMBEDDER=hash DEHKHODA_DENSE_DIM=256 \
    "$BIN" bootstrap --step index --rebuild-index \
    || fail "rebuild-index step failed"
  tail -1 /tmp/rebuild.jsonl
  tail -1 /tmp/rebuild.jsonl | grep -q '"ok": *true' || fail "rebuild not ok"
  tail -1 /tmp/rebuild.jsonl | grep -q '"mode": *"rebuilt_from_bundle"' \
    || fail "rebuild mode != rebuilt_from_bundle"
  REBUILT=$(tail -1 /tmp/rebuild.jsonl | grep -o '"record_count": *[0-9]*' | grep -o '[0-9]*$')
  [ "$REBUILT" = "$PIN_RECORDS" ] || \
    fail "rebuilt record_count $REBUILT != pin $PIN_RECORDS"
  echo "rebuild used ONLY the bundled SQL; record_count == pin ($REBUILT)"

  run_step /tmp/verifyc.jsonl env \
    DEHKHODA_EMBEDDER=hash DEHKHODA_DENSE_DIM=256 \
    "$BIN" bootstrap --step index --verify-index || fail "verify-index failed"
  tail -1 /tmp/verifyc.jsonl | grep -q '"ok": *true' || fail "verify-index not ok"
else
  # =================================================================
  # TEST A — zero-packet offline first run of the full variant
  # =================================================================
  echo "=== TEST A: the wizard path (report -> db -> model -> ingest) ==="
  run_step /tmp/report.jsonl "$BIN" bootstrap --step report \
    || fail "report step failed"
  tail -1 /tmp/report.jsonl | grep -q '"ok": *true' || fail "report not ok"

  run_step /tmp/db.jsonl "$BIN" bootstrap --step db || fail "db step failed"
  tail -1 /tmp/db.jsonl | grep -q '"ok": *true' || fail "db step not ok"
  grep -q 'qdrant_native' /tmp/db.jsonl || \
    echo "NOTE: engine chain did not surface qdrant_native (check log)"

  run_step /tmp/model.jsonl "$BIN" bootstrap --step model \
    || fail "model step failed"
  tail -1 /tmp/model.jsonl | grep -q '"ok": *true' || fail "model not ok"
  tail -1 /tmp/model.jsonl | grep -q '"source": *"bundled"' \
    || fail "model source != bundled (model leaked a download path?)"

  run_step /tmp/ingest.jsonl "$BIN" bootstrap --step ingest \
    || fail "ingest (restore) step failed"
  tail -1 /tmp/ingest.jsonl
  tail -1 /tmp/ingest.jsonl | grep -q '"ok": *true' || fail "ingest not ok"
  tail -1 /tmp/ingest.jsonl | grep -q '"mode": *"restored"' \
    || fail "dataset event mode != restored (restore path not taken)"
  RESTORED=$(tail -1 /tmp/ingest.jsonl | grep -o '"record_count": *[0-9]*' | grep -o '[0-9]*$')
  [ "$RESTORED" = "$PIN_RECORDS" ] || \
    fail "restored record_count $RESTORED != pin $PIN_RECORDS"
  echo "restore OK: $RESTORED records == pin"
fi
T1=$(date +%s)
DUR=$((T1 - T0))
echo "wizard duration: ${DUR}s (spec target <90s, hard limit 120s)"
if [ "$DUR" -gt 120 ]; then
  fail "first run took ${DUR}s — exceeds the 120s slow-fs allowance"
fi
[ "$DUR" -gt 90 ] && echo "NOTE: ${DUR}s > 90s target (slow filesystem, logged per spec)"

# ---- tcpdump verdict: EXACTLY 0 packets ----------------------------------
sleep 1
kill "$TCPDUMP_PID" 2>/dev/null || true
wait "$TCPDUMP_PID" 2>/dev/null || true
PACKETS=$(tcpdump -r /tmp/pcap 2>/dev/null | wc -l)
DNS=$(tcpdump -r /tmp/pcap 'udp port 53' 2>/dev/null | wc -l)
echo "tcpdump packets (non-loopback): $PACKETS, DNS/UDP53: $DNS"
[ "$PACKETS" = "0" ] || { tcpdump -r /tmp/pcap 2>/dev/null | head -20; \
  fail "tcpdump observed $PACKETS non-loopback packets — zero-network guarantee violated"; }
[ "$DNS" = "0" ] || fail "DNS lookup observed (UDP/53) — zero-network guarantee violated"

# ---- index.ready + verify ------------------------------------------------
[ -f "$DEHKHODA_DATA_DIR/index.ready" ] || fail "index.ready marker missing"
echo "index.ready: $(tr -d '\n' < "$DEHKHODA_DATA_DIR/index.ready")"
run_step /tmp/verify.jsonl "$BIN" bootstrap --step index --verify-index \
  || fail "verify-index failed"
tail -1 /tmp/verify.jsonl | grep -q '"ok": *true' || fail "verify-index not ok"
grep -q '"record_count"' "$DEHKHODA_DATA_DIR/index.ready" || \
  fail "index.ready missing record_count"

# ---- HTTP sidecar: /health + one real hybrid search ----------------------
echo "=== HTTP sidecar: /health + Persian hybrid search ==="
HTTP_PORT=8399
set +e
if [ "$MODE" = "--test-c" ]; then
  DEHKHODA_EMBEDDER=hash DEHKHODA_DENSE_DIM=256 DEHKHODA_SKIP_AUTO_RESTORE=1 \
    "$BIN" --port "$HTTP_PORT" >/tmp/http.log 2>&1 &
else
  "$BIN" --port "$HTTP_PORT" >/tmp/http.log 2>&1 &
fi
HTTP_PID=$!
set -e
HEALTH_OK=""
for i in $(seq 1 120); do
  BODY=$(http_get "$HTTP_PORT" "/health" 2>/dev/null || true)
  if echo "$BODY" | grep -q '"status"'; then HEALTH_OK=1; break; fi
  sleep 1
done
[ -n "$HEALTH_OK" ] || { tail -20 /tmp/http.log; fail "sidecar /health never answered"; }
echo "$BODY" | tail -1
echo "$BODY" | grep -q '"engine": *"ready"' || fail "/health engine != ready"
if [ "$MODE" != "--test-c" ]; then
  echo "$BODY" | grep -q '"source": *"bundled"' || \
    fail "/health source != bundled (got: $(echo "$BODY" | grep -o '"source": *"[a-z]*"'))"
  echo "$BODY" | grep -q '"index": *"ready"' || fail "/health index != ready"
fi

SEARCH=$(http_post "$HTTP_PORT" "/search" \
  '{"query":"احساس بی‌قراری و اضطراب","top_k":5}')
echo "$SEARCH" | tail -1 | head -c 600; echo
echo "$SEARCH" | grep -q '"word"' || fail "search returned no headwords"
echo "search OK (>=1 plausible headword)"

# ---- UI bundle: no external URLs ----------------------------------------
echo "=== UI bundle external-URL grep ==="
URLS=$(find squashfs-root -type f \( -name '*.js' -o -name '*.css' -o -name '*.html' \) \
  -print0 2>/dev/null | xargs -0 grep -hoE 'https?://[^"'"'"')<> ]+' 2>/dev/null \
  | sort -u | grep -vE '^(https?://(127\.0\.0\.1|localhost|w3\.org|www\.w3\.org|tauri\.localhost|schema\.tauri\.app|github\.com/montajebii/reverse-DehKhoda))' \
  || true)
if [ -n "$URLS" ]; then
  echo "$URLS"
  fail "external URLs found in the UI bundle (see above)"
fi
echo "UI bundle: no unexpected external URLs (whitelist: w3.org xmlns, loopback, tauri, display-only release URL)"

kill "$HTTP_PID" 2>/dev/null || true
echo
if [ "$MODE" = "--test-c" ]; then
  echo "TEST C OK — offline rebuild from bundled SQL verified (0 network packets)"
else
  echo "TEST A OK — 0 tcpdump packets, restore ready, counts verified, /health bundled"
fi
