# george

Jetson-grade TUI control center. One python process (stdlib + urwid only,
~30 MB RAM, ~0% idle CPU) that opens fullscreen at X login and gives you:

![george screenshot](27aug-george.jpg)

- **LAUNCH** column — config-driven buttons that launch whatever is on your
  `$PATH` (your own scripts, apps, system tools). Configure in `buttons.toml`
  (a portable demo ships; copy it and point `GEORGE_CONFIG` at your own copy
  to keep your personal button set out of the repo). Press `r` to reload.
- **SYSTEM & STATUS** center — live CPU / MEM / LOAD / NET / DISK / BATT /
  TEMP panes read straight from `/proc` + sysfs every 2 s (left of the
  block) beside the scrolling status log (right). Arrow onto a pane and
  press enter to open the matching tool (iotop, iftop, ncdu, btop...) in a
  new terminal; mapping is in `[click]` of `buttons.toml`. Keyboard-first:
  mouse input is disabled, arrows + enter drive everything.
- **SHOWCASE + TV** lower center — left block is free display space
  (`[showcase]` lines in `buttons.toml`); right block is CH 57: george
  docks a borderless mpv window exactly over it (I actually drag it to the right a bit), shuffling public-domain
  *Leave It to Beaver* episodes streamed straight from archive.org
  (`[tv]` in `buttons.toml`; regenerate lineup with `tools/mktv.py`).
  The player runs with mpv default key bindings disabled
  (`--no-input-default-bindings`) so it can't be paused or quit on its own —
  it only dies with george (although it can be paused by the TV 57 channel switch in the left column). It stays
  pinned over george's body while george is focused, then drops below any
  other window you alt-tab to (so it never floats over your actual work),
  and re-pins the moment you return to george. It also hides under george's
  dialogs, follows resizes, and dies with george.
- **Top bar** — clock plus a chip per running/minimized window (via wmctrl).
  Display-only since going keyboard-first; alt-tab / your WM's keys manage
  windows as always.
- **Right column** — month calendar with today boxed (`<`/`>` page months),
  `nag 15m` button (your `nag` script), event form (light-surface modal,
  focus starts on the date field) writing to
  `~/.local/share/george/events.txt`; due events fire a critical
  notify-send plus a terminal bell, then move to `events.fired.log`;
  upcoming list, and up to 12 RSS/link slots (stdlib parser, cached under
  `~/.cache/george/`, refreshed every 15 min).
- **find & replace** (`f`) — literal or regex replacement across a directory
  tree with dry-run preview, hit counts, glob filter, binary/git skip, and
  `*.bak-fr` originals kept.

## Run

    bin/george            # opens its own terminal, class "georges"
    # or, once installed on your PATH:
    george

Keys: `1-9` quick-launch, `n` nag, `e` event, `f` find&replace, `g` greet,
`r` reload config, `?` help, `esc` hide (alt-tab to raise), `q` quit.

Term buttons hold their window open after the command exits (exit code +
"enter closes"). Per-item `hold = false` in `buttons.toml` restores
close-on-exit.

## Configuration

Config resolution, in order of preference:

1. `$GEORGE_CONFIG` — if set, used verbatim.
2. `~/.config/george/buttons.toml` — your personal copy, if it exists.
3. `buttons.toml` in this repo — the portable demo with common on-PATH
   commands, used only when you have no personal config yet.

So the demo ships and works out of the box, but as soon as you create a
personal config at `~/.config/george/buttons.toml` george uses that instead
(keeping your button set out of any public fork). To make your own:

    mkdir -p ~/.config/george
    cp buttons.toml ~/.config/george/buttons.toml

Press `r` inside george to reload after editing either file.

## Install at login (WM-agnostic)

Add to your X-session startup (e.g. `~/.xinitrc`), adapting the path to
wherever you cloned george:

    if ! pgrep -u "$USER" -f '/george\.py' >/dev/null 2>&1; then
        (sleep 5 && /path/to/george/bin/george) &
    fi

george maximizes itself at startup via wmctrl (EWMH), so no window-manager
config is touched. Works under any EWMH-compliant WM.

## Self test

    python3 george.py --self-test

Covers cpu/net math, wmctrl parsing, calendar grid, RSS+Atom parsing, the
full find&replace engine against a scratch tree, event round-trips.

Requires: python3-urwid (Python ≥ 3.11 for stdlib `tomllib`), alacritty,
wmctrl, xdotool, xprop (x11-utils). TV block additionally needs `mpv`.

Layout note: the CH 57 block docks *over* what is currently open in the
lower-right of george — the demo config traces a block so its exact size
depends on terminal rows; tune `[tv] x/y/w/h` in config if the dock doesn't
line up with your window size.
