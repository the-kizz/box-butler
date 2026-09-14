#!/usr/bin/env bash
# Fetch Fira Sans / Fira Code woff2 (OFL) into boxbutler/web/static/fonts.
# Run once; the resulting files are committed to the repo so the app never
# depends on the internet at runtime (spec §5: self-hosted fonts, no CDN).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/boxbutler/web/static/fonts"
mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Fetching Fira Code v6.2 (tonsky/FiraCode, OFL)..."
curl -sL -o "$TMP/FiraCode.zip" \
  "https://github.com/tonsky/FiraCode/releases/download/6.2/Fira_Code_v6.2.zip"
unzip -p "$TMP/FiraCode.zip" woff2/FiraCode-Regular.woff2 > "$OUT/FiraCode-Regular.woff2"
curl -sL -o "$OUT/OFL-FiraCode.txt" \
  "https://raw.githubusercontent.com/tonsky/FiraCode/master/LICENSE"

echo "Fetching Fira Sans (mozilla/Fira via Google Fonts static woff2, OFL)..."
curl -sL -o "$OUT/FiraSans-Regular.woff2" \
  "https://fonts.gstatic.com/s/firasans/v18/va9E4kDNxMZdWfMOD5Vvl4jL.woff2"
curl -sL -o "$OUT/FiraSans-Medium.woff2" \
  "https://fonts.gstatic.com/s/firasans/v18/va9B4kDNxMZdWfMOD5VnZKveRhf6.woff2"
curl -sL -o "$OUT/OFL-FiraSans.txt" \
  "https://raw.githubusercontent.com/mozilla/Fira/master/LICENSE"

echo "Done. Fonts in $OUT"
