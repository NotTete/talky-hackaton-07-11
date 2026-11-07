#!/usr/bin/env bash
# Validate a LIVE build, push it, and log the score.  Usage: scripts/live_push.sh <file.csv.gz> [label]
# Run from the recon2 worktree.  LIVE needs no typed confirmation (owner decision 2026-11-07).
set -euo pipefail
f="$1"; label="${2:-}"
data=/home/tete/Documents/Talky/talky-hackaton-07-11/data
log=out/recon2/live_scores.tsv
PYTHONPATH=src uv run python -m bankrec validate --data "$data" "$f" --phase live | tail -2
sha=$(gzip -dc "$f" | sha256sum | cut -c1-16)
out=$(PYTHONPATH=src uv run python -m bankrec push --data "$data" LIVE "$f" 2>&1 || true)
score=$(grep -o 'score=[0-9.]*' <<<"$out" | head -1 | cut -d= -f2)
echo "$out" | grep -E "status=|error|Error|HTTP" | head -3
[ -f "$log" ] || printf 'time\tcommit\tsha\tscore\tlabel\tfile\n' > "$log"
printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date +%H:%M)" "$(git rev-parse --short HEAD)" "$sha" "${score:-?}" "$label" "$f" >> "$log"
echo "LIVE score: ${score:-?}  (logged to $log)"
