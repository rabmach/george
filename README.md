# george

*Built in the open: human-directed, AI-assisted ([opencode](https://github.com/anomalyco/opencode)), human-verified.*

TUI sort of a "control center" for your Debian desktop because why the hell not. One python process (stdlib + urwid only,
~30 MB RAM, ~0% idle CPU) that opens fullscreen at X login and gives you:

george in X
![george X](george-x.jpg)

george in a tty:
![george tty](george-in-tty.jpg)


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
- **SHOWCASE + THIS BOX** lower center — left block is a wicked handy scratch
  space that you can save as a note or send off as an email, and, you may keep
  doing it (`[showcase]` lines in `buttons.toml`) — and the buffer **persists**:
  it is saved as you type and restored at the next start, so a stray `q` (or a
  machine that fell asleep on you) never costs your notes; right block is a
  fastfetch-style **THIS BOX** readout (PC, OS + kernel + uptime, WM + GTK
  theme + package count, init, date, load, processes, memory, partitions) —
  all built from george's own `/proc` + `/sys` + `/etc` reads, **zero
  subprocesses**, same numbers fastfetch would show. The channels themselves
  are plain **launcher buttons** in
  the LAUNCH column: **CH 57** plays random public-domain *Leave It to Beaver*
  episodes, **CH 59** a silent-comedy/cartoons/oddball-docs lineup
  (`[tv]` / `[funny]` in `buttons.toml`; the lineup maps are in
  `tv/*.m3u`, regenerate with `tools/mktv.py` / `tools/update-funny.py`).
  Each chip opens mpv in **its own separate window** (shuffled, looping,
  tiled ~60% top-right so george stays visible behind it — `f` fullscreens
  it if you want) —
  alt-tab back to george whenever, close the mpv window to stop. No docked or
  embedded video, no focus tricks, nothing for george to babysit. Listen, I
  know this is weird, but, random Leave it to Beaver episodes is hilarious so
  it's in and it's staying. The george window itself starts undecorated and
  maximized if you give openbox a rule for it (`<application class="georges">
  <decor>no</decor><maximized>true</maximized></application>` in
  `~/.config/openbox/rc.xml`) — the maximized part also covers a second
  launcher run attaching to the running session (an attached window has no
  george process inside it to place itself).
- **Built-in terminal** — one alacritty window is split by tmux into two
  panes: george on top, a real shell below (`bin/george` does this; Ctrl+t,
  wired as a tmux no-prefix binding, flips focus between them, or click a
  pane). The terminal pane runs your shell in a loop that never exits, so
  Ctrl+D, `exit`, or even a failed command followed by Ctrl+D just gives a
  fresh prompt (a dirty exit adds a 1-second anti-spin pause) — the terminal
  cannot take itself or george down. Enter the terminal with Ctrl+t or a
  click; come back to the dashboard the same way. The **▶ TERM** chip in the
  LAUNCH column (same row style as the radio and channel chips) focuses the
  terminal, respawns it if it somehow died, or creates it if it's gone —
  no keybind to remember; killed panes stay visible (tmux `remain-on-exit`)
  so the chip always has something to bring back. **A console (tty) george
  gets the same terminal and loses the chip**: tmux needs no X — the console
  itself is the terminal — so `bin/george` on a bare tty builds the same
  two-pane session (george 70% top, shell 30% bottom, Ctrl+t flips,
  `remain-on-exit`), under its own session name so an X george on the same
  tmux server is never touched. No ▶ TERM chip there: the pane is built
  in, the shell loop never dies (Ctrl+D just gives a fresh prompt), and
  the only way to kill it is a deliberate `tmux kill-pane`/kill -9 — whose
  recovery is simply quitting and relaunching (the launcher sees the dead
  pane and rebuilds). george quits by killing its own tmux session (clean
  exits only; a hard kill leaves a corpse the next launch rebuilds over).
  `term:` buttons work on the console too — the script runs in george's
  pane region while the terminal pane survives underneath. `gui:` buttons
  still need X.
  If george.py is started WITHOUT the launcher (bare `python3 george.py` on
  a console — no tmux pane exists there), the ▶ TERM chip comes back and
  falls back to a **command line inside the dashboard**: type a shell
  command and enter runs it on a fresh page (exit footer, q closes, Ctrl+c
  kills the command — never george), then the line reopens like a mini
  shell session. A trailing `&` runs the command detached instead: george
  stays on screen and the line closes — e.g. `ttysnap&` screenshots the
  live dashboard (ttysnap names the shot by whatever console is on screen,
  and warns instead of saving when the framebuffer didn't match the console
  — X owns a vt's scanout, so fbcat sees black there). A bare console has
  one screen, so george's render and a command's output can't both be
  visible at once — enter for readable output, `&` for side-effect
  commands.
- **Random Nina chip** (`[nina]` in `buttons.toml`) — one-audio rule: starts
  by freezing the radio (SIGSTOP, so it resumes where it left off), then
  streams the whole shuffled Nina Simone archive pool (208 songs + 2 whole
  albums, looping) until the chip is hit again — not a one-track stop. The
  picker answers `--playlist` with a fresh shuffled playlist; the repo ships
  `cmd = "nina.sh"` as an example. Standalone `nina.sh` (outside george) also
  toggles: run it again to stop, no need to find the window for `q`.
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

Run it from an interactive terminal and the prompt comes straight back —
george spawns detached (its own session; closing the launching terminal
never touches it). Login-hook and menu launches behave as before. If the
tmux build ever fails (stale server, broken socket), the launcher degrades
to a plain george dashboard instead of vanishing, and says so via
notify-send.

Keys: `1-9` quick-launch, `n` nag, `e` event, `f` find&replace, `g` greet,
`r` reload config, `?` help, `esc` hide (alt-tab to raise), `q` quit. CH
57/59 launch mpv in their own window (close it / alt-tab back); the built-in
terminal is the tmux pane below — Ctrl+t or click to enter/leave, Ctrl+D in
it only spawns a fresh prompt.

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

Requires: python3-urwid (Python ≥ 3.11 for stdlib `tomllib`); on X: alacritty,
wmctrl, xdotool, xprop (x11-utils), and `tmux` (for the built-in terminal
panes — Ctrl+t flips focus; already present on most Debian installs). On a
bare console tty only `tmux` is needed — the console itself is the terminal,
and the launcher builds the same two-pane session there. Running
`python3 george.py` bare (no launcher) needs nothing but urwid. The
CH 57/59 buttons additionally need `mpv`. Nothing else — the embedded-video
helper is long gone; channels are plain launcher buttons.
