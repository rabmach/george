#!/usr/bin/env bash
set -euo pipefail

SELF=$(readlink -f "$0")
DIR=$(dirname "$SELF")
DEFAULT_PLIST=""
for cand in "$DIR/beaver.m3u" "$DIR/../tv/beaver.m3u"; do
    [[ -r "$cand" ]] && DEFAULT_PLIST="$cand" && break
done
PLAYLIST="${LITB_PLAYLIST:-$DEFAULT_PLIST}"

URL=$(grep -E '^https?://' "$PLAYLIST" | shuf -n 1)

if [[ -z "$URL" ]]; then
    echo "rltb: no playable URL found in $PLAYLIST" >&2
    exit 1
fi

if [[ -n "${LITB_PLAYER:-}" ]]; then
    exec "$LITB_PLAYER" "$URL"
elif command -v mpv >/dev/null 2>&1; then
    exec mpv "$URL"
else
    exec xdg-open "$URL"
fi
