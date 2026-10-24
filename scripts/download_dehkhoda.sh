#!/usr/bin/env bash
# Download all 33 Dehkhoda-SQL files (mirrors backend/ingestion/dehkhoda_loader.py behavior).
#
# Usage:
#   scripts/download_dehkhoda.sh                 # -> ./data/dehkhoda
#   scripts/download_dehkhoda.sh /some/other/dir
set -euo pipefail
DEST="${1:-./data/dehkhoda}"
BASE="https://raw.githubusercontent.com/nimah79/Dehkhoda-SQL/master"
mkdir -p "$DEST"
FILES=(
01-A 02-Alef 03-Be 04-Pe 05-Te 06-Se 07-Jim 08-Che 09-He 10-Khe 11-Dal 12-Zal
13-Re 14-Ze 15-Zhe 16-Sin 17-Shin 18-Sad 19-Zad 20-Ta 21-Za 22-Ain 23-Qain
24-Fe 25-Qaf 26-Kaf 27-Gaf 28-Lam 29-Mim 30-Noun 31-Vav 32-He 33-Ye
)
for f in "${FILES[@]}"; do
  out="$DEST/$f.sql.gz"
  if [ -s "$out" ]; then echo "SKIP $f"; continue; fi
  curl -fsSL --retry 4 --retry-delay 2 -o "$out" "$BASE/$f.sql.gz" \
    && echo "OK   $f ($(( $(wc -c < "$out") / 1024 )) KB)"
done
echo "---"
ls "$DEST" | wc -l
du -sh "$DEST"
