#!/usr/bin/env bash
# Download the hackathon data once and extract it into data/raw/.
#
#   ./scripts/download_data.sh              # docs + sample (fast, ~10 MB)
#   ./scripts/download_data.sh --train-lite # + TRAIN lite (~460 MB)
#   ./scripts/download_data.sh --all        # + TRAIN full and LIVE/FINAL (~GBs)
#
# The archives are served from the event site (no auth for the data files).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/data/raw"
BASE="${BANKREC_BASE_URL:-https://hackathon.usetalky.com/data/v1}"
PY="${PYTHON:-python3}"

mkdir -p "$DEST"

files=("bankrec-docs-and-masters.zip")
case "${1:-}" in
  --train-lite) files+=("bankrec-train-lite.tar.gz") ;;
  --all)        files+=("bankrec-train.tar.gz" "bankrec-live.tar.gz" "bankrec-final.tar.gz") ;;
  "" ) ;;
  *) echo "usage: $0 [--train-lite|--all]" >&2; exit 2 ;;
esac

for f in "${files[@]}"; do
  if [[ -f "$DEST/$f" ]]; then
    echo "skip  $f (already downloaded)"
  else
    echo "fetch $BASE/$f"
    curl -fL --retry 3 -o "$DEST/$f" "$BASE/$f"
  fi
done

# Extract with Python: no unzip/tar assumptions on the host.
"$PY" - "$DEST" <<'PY'
import sys, tarfile, zipfile
from pathlib import Path

dest = Path(sys.argv[1])
for archive in sorted(dest.iterdir()):
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        print("unzipped", archive.name)
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")
        print("untarred", archive.name)
PY

# The docs archive also carries the official README/POLICIES/FORMAT + starter.
mkdir -p "$ROOT/docs/official-starter"
for f in README.md README_EN.md POLICIES.md FORMAT.md DATA_DICTIONARY.md; do
  [[ -f "$DEST/$f" ]] && mv "$DEST/$f" "$ROOT/docs/$f"
done
if [[ -d "$DEST/starter" ]]; then
  cp -r "$DEST/starter/." "$ROOT/docs/official-starter/"
  rm -rf "$DEST/starter"
fi

echo
echo "data root: $DEST"
ls "$DEST"
