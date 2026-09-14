#!/usr/bin/env bash
# nina.sh - continuous random Nina Simone from the archive.org pool.
#
# Reads the pools built by update-nina.py (nina.tsv):
#   S = a song,   A = a whole album (the occasional "treat")
#
# Modes:
#   nina.sh (no args)     toggle stream: builds a freshly shuffled, LOOPING
#                         playlist of the whole pool (albums included) and
#                         plays until stopped. A second invocation (e.g. the
#                         same keybind again) quits it over the mpv IPC
#                         socket - no need to find the window to press q.
#   nina.sh --url-only    print "TITLE<TAB>URL" for one random pick, exit
#                         (kept for older seams / testing)
#   nina.sh --playlist    (re)build the shuffled playlist, print
#                         "FIRST_TITLE<TAB>PLIST_PATH" - the george chip
#                         seam, so george's nina keeps playing too (george
#                         owns the mpv process for freeze/thaw).
#   nina.sh --once        print the pick then play ONE track (old behavior;
#                         handy for tests)
#
# Extension knobs: NINA_ALBUM_WEIGHT (was for single picks; playlist mode
# mixes the whole pool evenly), NINA_PLAYER, NINA_MPV_ARGS (extra mpv flags,
# e.g. NINA_MPV_ARGS=--force-window=yes), NINA_TSV, NINA_STATE.
set -euo pipefail

SELF=$(readlink -f "$0")
DIR=$(dirname "$SELF")
TSV="${NINA_TSV:-$DIR/nina.tsv}"
M3U="${NINA_M3U:-$DIR/nina-songs.m3u}"
STATED="${NINA_STATE:-$HOME/.local/state}"
SOCK="$STATED/nina.mpv.sock"
PLIST="$STATED/nina-playlist.m3u"
PLAYER="${NINA_PLAYER:-mpv}"
EXTRA="${NINA_MPV_ARGS:-}"
ALBUM_WEIGHT="${NINA_ALBUM_WEIGHT:-8}"

FIRST_TITLE=""
PICK_TITLE=""
PICK_URL=""
PICK_KIND=""

pick() {
    local r=0 line=""
    r=$(( RANDOM % 100 ))
    if (( r < ALBUM_WEIGHT )); then local kind="A"; else local kind="S"; fi
    line=$(grep -P "^${kind}\t" "$TSV" | shuf -n 1)
    if [[ -z "$line" ]]; then
        if [[ "$kind" == "A" ]]; then
            line=$(grep -P '^S\t' "$TSV" | shuf -n 1)
        else
            line=$(grep -P '^A\t' "$TSV" | shuf -n 1)
        fi
    fi
    if [[ -z "$line" ]]; then
        # tsv missing entirely - fall back to the plain song m3u
        line=$(grep '^https\?://' "$M3U" | shuf -n 1)
    fi
    if [[ -z "$line" ]]; then
        echo "nina: no playlist found (run ~/nina/update-nina.py)" >&2
        exit 1
    fi
    IFS=$'\t' read -r PICK_KIND PICK_TITLE PICK_URL <<< "$line"
}

build_playlist() {
    local lf tmp
    lf="$(mktemp "$PLIST.XXXXXX.lines")"
    tmp="$(mktemp "$PLIST.XXXXXX")"
    if [[ -s "$TSV" ]]; then
        # whole pool, albums + songs together, freshly shuffled;
        # an album coming up plays its whole run (fewer, bigger treats)
        { grep -P '^A\t' "$TSV"; grep -P '^S\t' "$TSV"; } | shuf > "$lf"
        IFS=$'\t' read -r _ FIRST_TITLE _ < "$lf" || FIRST_TITLE="?"
        cut -f3 "$lf" > "$tmp"
    else
        grep '^https\?://' "$M3U" | shuf > "$tmp"
        FIRST_TITLE="(unknown)"
    fi
    rm -f "$lf"
    mv -f "$tmp" "$PLIST"
}

stop_stream() {
    if [[ -S "$SOCK" ]]; then
        # bash can't open() a unix stream socket, so use python3's
        # connect() to tell a live mpv to quit; a dead player leaves a
        # stale socket that fails to connect and gets cleaned up.
        if python3 - "$SOCK" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(b'{"command":["quit"]}\n')
s.close()
PY
        then
            echo "Random Nina: stopped"
            exit 0
        fi
        rm -f "$SOCK"
    fi
}

MODE="${1:-}"
case "$MODE" in
    --url-only)
        pick
        printf '%s\t%s\n' "$PICK_TITLE" "$PICK_URL"
        exit 0
        ;;
    --playlist)
        mkdir -p "$STATED"
        build_playlist
        printf '%s\t%s\n' "$FIRST_TITLE" "$PLIST"
        exit 0
        ;;
    --once)
        pick
        echo "Random Nina: $PICK_TITLE"
        exec "$PLAYER" $EXTRA "$PICK_URL"
        ;;
    "")
        stop_stream
        mkdir -p "$STATED"
        build_playlist
        echo "Random Nina playing: $FIRST_TITLE ... (same key stops it)"
        exec "$PLAYER" --loop-playlist --input-ipc-server="$SOCK" \
            --playlist="$PLIST" $EXTRA
        ;;
    *)
        echo "nina: unknown mode '$MODE'" >&2
        exit 2
        ;;
esac