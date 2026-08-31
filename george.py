#!/usr/bin/env python3
"""george -- jetson-grade control center.

A clickable TUI dashboard for an openbox desktop: system info panes,
config-driven launch buttons for ~/bin, calendar + nag + events,
window chips for running/minimized apps, RSS/link slots, and an
in-app recursive find&replace. Runs under X (mouse) or bare tty (keys).

stdlib + python3-urwid only. Config: buttons.toml beside this file.
"""

import calendar as cal_mod
import fnmatch
import json
import os
import queue
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from collections import deque
from datetime import date, datetime
from pathlib import Path
from urllib.request import Request, urlopen

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("george needs python3.11+ (tomllib missing)")

try:
    import urwid
except ModuleNotFoundError:
    sys.exit("george needs urwid: sudo apt install python3-urwid")

APP = "george"
HERE = Path(__file__).resolve().parent
_PERSONAL_CFG = Path("~/.config/george/buttons.toml").expanduser()
_env_cfg = os.environ.get("GEORGE_CONFIG") or ""
if _env_cfg:
    CONFIG_PATH = Path(_env_cfg)
else:
    CONFIG_PATH = _PERSONAL_CFG if _PERSONAL_CFG.is_file() else HERE / "buttons.toml"
DATA_DIR = Path("~/.local/share/george").expanduser()
CACHE_DIR = Path("~/.cache/george").expanduser()
EVENTS_FILE = DATA_DIR / "events.txt"
FIRED_LOG = DATA_DIR / "events.fired.log"

FR_BACKUP = ".bak-fr"
FR_SKIP_DIRS = {".git", ".cache", ".venv", "__pycache__", "node_modules",
                ".cargo", ".rustup", ".mozilla"}
FR_MAX_FILE = 8 * 1024 * 1024

WM_EXCLUDE_CLASSES = {"georges", "feh", "xscreensaver", "tint2", "plank"}
CHIP_W = 14
STAT_EVERY = 2
CLOCK_EVERY = 1
EVENT_EVERY = 30
FEED_EVERY = 900
FEED_SHOW = 6
LINK_SLOTS = 12


ICONS = {
    "cpu": "\uf2db", "mem": "\uefc1", "net": "\uf1eb", "disk": "\uf0a0",
    "bat": "\uf240", "temp": "\uf2c8", "cal": "\uf073", "tv": "\uf26c",
    "rss": "\uf09e", "music": "\uf001", "term": "\uf489", "clock": "\uf017",
    "gear": "\uf013", "search": "\uf002", "bolt": "\uf0e7",
    "rocket": "\uf135", "radio": "\uf1eb",
    "host": "\uf109", "os": "\uf17c", "kern": "\uf0c2",
    "pkg": "\uf1ad", "gpu": "\uf108",
}
_ASCII = os.environ.get("GEORGE_ASCII") == "1"


def ico(k):
    return "" if _ASCII else ICONS.get(k, "")


def sec_icon(title):
    t = str(title).strip().lower()
    for pre, ic in (("music", "music"), ("media", "music"),
                    ("sys", "gear"), ("script", "term"), ("app", "rocket"),
                    ("tool", "search"), ("net", "net"), ("disk", "disk")):
        if t.startswith(pre):
            return ico(ic)
    return ico("bolt")


_SPARK = "▁▂▃▄▅▆▇█"


def spark(vals, width=None):
    if not vals:
        return ""
    vals = list(vals)
    if width and len(vals) > width:
        vals = vals[-width:]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    return "".join(_SPARK[min(7, int((v - lo) * 8 / span))] for v in vals)


def gauge(frac, width=10):
    frac = max(0.0, min(1.0, frac))
    n = round(frac * width)
    return "▰" * n + "▱" * (width - n)


def human_gb(num_bytes):
    return num_bytes / (1024 ** 3)


def fmt_rate(bps):
    if bps >= 1024 * 1024:
        return f"{bps / 1048576:.1f}M/s"
    return f"{bps / 1024:.0f}K/s"


def parse_proc_stat(text):
    return [int(x) for x in text.splitlines()[0].split()[1:]]


def cpu_pct(prev, cur):
    didle = (cur[3] + cur[4]) - (prev[3] + prev[4])
    dtotal = sum(cur) - sum(prev)
    if dtotal <= 0:
        return 0.0
    return max(0.0, min(100.0, (dtotal - didle) / dtotal * 100.0))


def parse_route(text):
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) > 7 and f[1] == "00000000" and f[7] == "00000000":
            return f[0]
    return None


def parse_cores_stat(text):
    cores = {}
    for line in text.splitlines()[1:]:
        if not line.startswith("cpu") or line.startswith("cpuid"):
            break
        f = line.split()
        key = f[0][3:]
        if not key.isdigit():
            continue
        try:
            cores[int(key)] = [int(x) for x in f[1:]]
        except ValueError:
            continue
    return cores


def core_pct(prev, cur):
    out = {}
    for i, c in cur.items():
        p = prev.get(i)
        if not p:
            continue
        didle = (c[3] + c[4]) - (p[3] + p[4])
        dtotal = sum(c) - sum(p)
        if dtotal > 0:
            out[i] = max(0.0, min(100.0, (dtotal - didle) / dtotal * 100.0))
    return out


def proc_sample():
    out = {}
    me = os.getpid()
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if not n.isdigit() or int(n) == me:
            continue
        try:
            t = Path("/proc", n, "stat").read_text()
            rp = t.rfind(")")
            name = t[:rp].split(None, 1)[1].strip("(")[:12]
            rest = t[rp + 2:].split()
            out[int(n)] = (name, int(rest[11]) + int(rest[12]))
        except (OSError, IndexError, ValueError):
            continue
    return out


def proc_top(prev, cur, dt):
    CLK = os.sysconf("SC_CLK_TCK")
    usage = []
    for pid, (nm, tks) in cur.items():
        p = prev.get(pid)
        if p and tks >= p[1]:
            pct = (tks - p[1]) / CLK / dt * 100.0
            if pct >= 0.5:
                usage.append((pct, pid, nm))
    usage.sort(reverse=True)
    return usage[:4]


def cpu_temp():
    best = None
    try:
        for z in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                if (z / "name").read_text().strip() != "coretemp":
                    continue
            except OSError:
                continue
            for t in z.glob("temp*_input"):
                try:
                    v = int(t.read_text().strip()) / 1000.0
                    best = v if best is None else max(best, v)
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return best


def disk_used_pct(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return 0.0
    return (total - free) / total * 100.0


def net_bytes(iface):
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == iface:
                f = rest.split()
                return int(f[0]), int(f[8])
    except (OSError, IndexError, ValueError):
        pass
    return None


def disk_free(path):
    st = os.statvfs(path)
    return human_gb(st.f_bavail * st.f_frsize)


def mem_info():
    d = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = int(v.strip().split()[0])
    total = d.get("MemTotal", 0)
    avail = d.get("MemAvailable", 0)
    used = total - avail
    pct = used / total * 100 if total else 0.0
    return pct, used / 1024 / 1024


def mem_total_gb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, IndexError, ValueError):
        pass
    return 0.0


def battery():
    base = Path("/sys/class/power_supply")
    try:
        for b in sorted(base.glob("BAT*")):
            cap = b / "capacity"
            if cap.exists():
                pct = int(cap.read_text().strip())
                status = ""
                sf = b / "status"
                if sf.exists():
                    status = sf.read_text().strip()
                return pct, status
    except OSError:
        pass
    return None, ""


def max_temp():
    best = None
    try:
        for z in Path("/sys/class/thermal").glob("thermal_zone*"):
            t = z / "temp"
            if t.exists():
                v = int(t.read_text().strip()) / 1000.0
                best = v if best is None else max(best, v)
    except (OSError, ValueError):
        pass
    return best


def uptime_secs():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return 0.0


def fmt_uptime(secs):
    m = int(secs // 60)
    d, rem = divmod(m, 1440)
    h, mi = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {mi}m"


def os_pretty():
    try:
        with open("/etc/os-release") as f:
            for ln in f:
                if ln.startswith("PRETTY_NAME="):
                    return ln.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "Linux"


def kernel_version():
    try:
        v = Path("/proc/sys/kernel/osrelease").read_text().strip()
    except OSError:
        v = "?"
    return v


def cpu_model():
    try:
        for ln in Path("/proc/cpuinfo").read_text().splitlines():
            if ln.startswith("model name"):
                return ln.split(":", 1)[1].strip()
    except OSError:
        pass
    return "?"


def cpu_cores():
    try:
        return sum(1 for ln in Path("/proc/cpuinfo").read_text().splitlines()
                   if ln.startswith("processor"))
    except OSError:
        return os.cpu_count() or 1


def dmi_product():
    try:
        base = Path("/sys/class/dmi/id")
        pn = (base / "product_name").read_text().strip()
        pv = (base / "product_version").read_text().strip()
        return pn, pv
    except OSError:
        return "", ""


def gtk_theme():
    for p, tag in ((Path.home() / ".config/gtk-3.0/settings.ini", "GTK3"),
                   (Path.home() / ".config/gtk-4.0/settings.ini", "GTK4")):
        try:
            for ln in p.read_text().splitlines():
                if ln.strip().startswith("gtk-theme-name"):
                    return ln.split("=", 1)[1].strip(), tag
        except OSError:
            continue
    return "", ""


KNOWN_WMS = ("openbox", "mutter", "gnome-shell", "kwin_x11", "kwin_wayland",
             "xfwm4", "marco", "compiz", "fluxbox", "i3", "sway", "bspwm",
             "awesome", "dwm", "windowmaker", "icewm", "jwm")


def detect_wm():
    if os.environ.get("WAYLAND_DISPLAY"):
        sess = "Wayland"
    elif os.environ.get("DISPLAY"):
        sess = "X11"
    else:
        sess = "?"
    try:
        for n in os.listdir("/proc"):
            if not n.isdigit():
                continue
            try:
                comm = Path("/proc", n, "comm").read_text().strip()
            except OSError:
                continue
            if comm in KNOWN_WMS:
                return comm, sess
    except OSError:
        pass
    return "", sess


def dpkg_stats():
    """(installed_count, systemd_version) straight from the dpkg database —
    no dpkg/apt subprocess. One big read: call once and cache."""
    count = 0
    sysd = ""
    pkg, installed, ver = "", False, ""

    def close_stanza():
        nonlocal count, sysd, pkg, installed, ver
        if installed:
            if pkg == "systemd" and not sysd:
                sysd = ver
        pkg, installed, ver = "", False, ""

    try:
        for ln in Path("/var/lib/dpkg/status").read_text(errors="replace").splitlines():
            if not ln:
                close_stanza()
            elif ln.startswith("Package: ") and pkg == "":
                pkg = ln[9:].strip()
            elif ln.startswith("Status: ") and "install ok installed" in ln:
                installed = True
                count += 1
            elif ln.startswith("Version: ") and ver == "":
                ver = ln[9:].strip()
        close_stanza()
    except OSError:
        pass
    return count, sysd


def fstype_for(path):
    try:
        best, ftype = "", ""
        for ln in Path("/proc/mounts").read_text().splitlines():
            f = ln.split(None, 3)
            if len(f) < 3:
                continue
            mnt = f[1]
            if path == mnt or path.startswith(mnt + "/"):
                if len(mnt) > len(best):
                    best, ftype = mnt, f[2]
        return ftype
    except OSError:
        return ""


def disk_stat(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    pct = used / total * 100.0 if total else 0.0
    return used / 1024 ** 3, total / 1024 ** 3, pct


def proc_count():
    try:
        return sum(1 for n in os.listdir("/proc") if n.isdigit())
    except OSError:
        return 0


def parse_wm(text):
    wins = []
    for line in text.splitlines():
        f = line.split(None, 3)
        if len(f) < 4:
            continue
        if re.fullmatch(r"[\w.+-]+\.[\w.+-]+", f[2]):
            wins.append((f[0], f[3], f[2]))
            continue
        t = f[3].split(None, 1)
        if len(t) < 2:
            continue
        wins.append((f[0], t[1], t[0]))
    return wins


def wm_raw():
    try:
        return subprocess.run(["wmctrl", "-lx"], capture_output=True,
                              text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def self_win_id():
    txt = wm_raw()
    if not txt:
        return None
    for wid, _title, cls in parse_wm(txt):
        if cls.lower().startswith(APP + "s"):
            return wid
    return None


def wm_cmd(wid, action):
    flag = {"raise": "-ia", "min": "-i -b add,hidden", "close": "-ic"}[action]
    try:
        subprocess.Popen(f"wmctrl {flag} {wid}", shell=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def fr_scan(root, find, pattern="*", use_regex=False):
    hits = []
    rx = None
    if use_regex:
        try:
            rx = re.compile(find)
        except re.error:
            return None
    base = Path(root).expanduser()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in FR_SKIP_DIRS)
        for name in filenames:
            if not fnmatch.fnmatch(name, pattern):
                continue
            p = Path(dirpath) / name
            try:
                if p.stat().st_size > FR_MAX_FILE:
                    continue
                text = p.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if rx is not None:
                n = len(rx.findall(text))
            elif find:
                n = text.count(find)
            else:
                n = 0
            if n:
                hits.append((str(p), n))
    return hits


def fr_apply(paths, find, repl, use_regex=False):
    files = total = 0
    for sp in paths:
        p = Path(sp)
        try:
            text = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        bak = p.with_name(p.name + FR_BACKUP)
        if not bak.exists():
            shutil.copy2(p, bak)
        if use_regex:
            new, n = re.subn(find, repl, text)
        else:
            n = text.count(find)
            new = text.replace(find, repl)
        if n:
            try:
                p.write_bytes(new.encode("utf-8"))
            except OSError:
                continue
            files += 1
            total += n
    return files, total


def parse_feed(data):
    titles = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    atom = "{http://www.w3.org/2005/Atom}"
    for item in root.iter("item"):
        t = item.findtext("title")
        l = item.findtext("link")
        d = item.findtext("pubDate") or ""
        if t:
            titles.append((t.strip(), (l or "").strip(), d.strip()))
    for entry in root.iter(atom + "entry"):
        t = entry.findtext(atom + "title")
        lel = entry.find(atom + "link")
        l = lel.get("href") if lel is not None else ""
        d = entry.findtext(atom + "published") or entry.findtext(atom + "updated") or ""
        if t:
            titles.append((t.strip(), (l or "").strip(), d.strip()))
    return titles[:FEED_SHOW]


def load_events():
    out = []
    if EVENTS_FILE.exists():
        for line in EVENTS_FILE.read_text().splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                try:
                    dt = datetime.strptime(parts[0] + " " + parts[1],
                                           "%Y-%m-%d %H:%M")
                    out.append((dt, parts[2]))
                except ValueError:
                    continue
    return sorted(out)


def append_event(dt, note):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a") as f:
        f.write(f"{dt:%Y-%m-%d}\t{dt:%H:%M}\t{note}\n")


def remove_event(dt, note):
    keep = []
    for d, n in load_events():
        if d == dt and n == note:
            continue
        keep.append(f"{d:%Y-%m-%d}\t{d:%H:%M}\t{n}\n")
    EVENTS_FILE.write_text("".join(keep))


def cal_markup(year, month, today):
    name = cal_mod.month_name[month]
    lines = [[(None, "Mo Tu We Th Fr Sa Su")]]
    for week in cal_mod.monthcalendar(year, month):
        parts = []
        for d in week:
            s = f"{d:>2} " if d else " . "
            if d == today.day and year == today.year and month == today.month:
                parts.append(("today", s))
            else:
                parts.append((None, s))
        lines.append(parts)
    while len(lines) < 7:
        lines.append([(None, "")])
    return name, lines


PALETTE = [
    ("bar",      "black",        "dark cyan"),
    ("bar_hi",   "white,bold",   "dark cyan"),
    ("sect",     "dark cyan,bold", ""),
    ("btn",      "white",        ""),
    ("btn_f",    "black",        "light cyan"),
    ("ok",       "light green",  ""),
    ("warn",     "yellow",       ""),
    ("crit",     "light red",    ""),
    ("dim",      "dark gray",    ""),
    ("accent",   "light blue", ""),
    ("info",     "light blue",   ""),
    ("today",    "black",        "brown"),
    ("cal_hdr",  "dark cyan",    ""),
    ("chip",     "yellow",      ""),
    ("chip_f",   "black",        "yellow"),
    ("log",      "light gray",   ""),
    ("frame_f",  "light cyan",   ""),
    ("modal",    "black",        "light gray"),
    ("mfield",   "dark blue",    "light gray"),
    ("mdim",     "dark gray",    "light gray"),
    ("msect",    "black,bold",   "light gray"),
    ("mwarn",    "dark red",     "light gray"),
]


def build_term_argv(cmd, label=None):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label or cmd)[:24]
    script = (f"{cmd}\n"
              "rc=$?\n"
              "echo\n"
              'echo "--- george: exit $rc | enter closes ---"\n'
              "read -r _\n")
    return ["alacritty", "--title", f"george:{safe}",
            "-e", "bash", "-c", script]


class Click(urwid.WidgetWrap):
    def __init__(self, label, cb, attr="btn", fa="btn_f", align="left"):
        self._cb = cb
        self._extra = {}
        if isinstance(label, urwid.Widget):
            self._t = label
            inner = label
            if attr is None:
                attr = ""
        else:
            self._t = urwid.Text(label, align=align)
            inner = self._t
        super().__init__(urwid.AttrMap(inner, attr, fa))

    def selectable(self):
        return self._cb is not None

    def keypress(self, size, key):
        if self._cb and key == "enter":
            self._cb()
            return None
        return key

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and self._cb:
            cb = self._cb
            if button == 1:
                cb()
            elif button in (2, 3) and button in self._extra:
                self._extra[button]()
        else:
            return super().mouse_event(size, event, button, col, row, focus)


class Bar(urwid.WidgetWrap):
    def __init__(self, app):
        self._app = app
        self._ranges = []
        self._t = urwid.Text("")
        super().__init__(urwid.AttrMap(self._t, "bar"))

    def update(self, width):
        wins = []
        txt = wm_raw()
        right = f"{datetime.now():%a %d %b  %H:%M:%S} "
        if txt is None:
            self._ranges = []
            if not self._wm_dead:
                self._wm_dead = True
                self._app.log("wmctrl unavailable: window chips disabled", "warn")
            pad = max(1, width - 9 - len(right))
            self._t.set_text(f" GEORGE {' ' * pad}{right}")
            return
        seen = set()
        for wid, title, cls in parse_wm(txt):
            cl = cls.lower()
            if any(cl.startswith(x) for x in WM_EXCLUDE_CLASSES):
                continue
            if title == "Desktop" or not title.strip():
                continue
            if wid in seen:
                continue
            seen.add(wid)
            wins.append((wid, title))
        left = " GEORGE "
        right = f"{datetime.now():%a %d %b  %H:%M:%S} "
        budget = width - len(left) - len(right) - 4
        ranges = []
        segs = [left]
        pos = len(left)
        for wid, title in wins:
            label = f" {title[:CHIP_W - 3]} "
            if pos + len(label) + 3 > width - len(right):
                break
            segs.append(label)
            ranges.append((pos, pos + len(label), wid))
            pos += len(label)
            segs.append(" | ")
            pos += 3
        if ranges:
            segs.pop()
        pad = max(1, width - pos - len(right))
        segs.append(" " * pad)
        segs.append(right)
        self._ranges = ranges
        self._t.set_text("".join(segs))

    def mouse_event(self, size, event, button, col, row, focus):
        if event != "mouse press":
            return
        for lo, hi, wid in self._ranges:
            if lo <= col < hi:
                wm_cmd(wid, {1: "raise", 2: "min", 3: "close"}.get(button, "raise"))
                return


class CalendarWidget:
    def __init__(self, app):
        self._app = app
        t = date.today()
        self.year, self.month = t.year, t.month
        self.title = urwid.Text("", align="center")
        self.grid = urwid.Text("")
        nav = urwid.Columns([
            ("pack", Click(" < ", lambda: self.page(-1), "cal_hdr", "btn_f")),
            ("fixed", 22, self.title),
            ("pack", Click(" > ", lambda: self.page(1), "cal_hdr", "btn_f")),
        ])
        self.pile = urwid.Pile([nav, self.grid])
        self.redraw()

    def page(self, n):
        m = self.month + n
        if m < 1:
            m, self.year = 12, self.year - 1
        elif m > 12:
            m, self.year = 1, self.year + 1
        self.month = m
        self.redraw()

    def redraw(self):
        t = date.today()
        name, lines = cal_markup(self.year, self.month, t)
        self.title.set_text(("cal_hdr", f"{name} {self.year} "))
        rendered = []
        for i, ln in enumerate(lines):
            if i:
                rendered.append("\n")
            if len(ln) == 1 and ln[0][0] is None:
                rendered.append(ln[0][1])
            else:
                rendered.append(list(ln))
        self.grid.set_text(rendered)


HELP_TEXT = [
    ("sect", " GEORGE KEYBINDINGS\n"),
    ("log", " mouse      click buttons/chips/panes; mid-click chip minimizes; right closes\n"),
    ("log", " 1-9        quick-launch the first nine left-column buttons\n"),
    ("log", " n          nag in 15 minutes            e   new event\n"),
    ("log", " f          find & replace               g   greet\n"),
    ("log", " m          radio on/off                 r   reload buttons.toml\n"),
    ("log", " CH 57/59   chips open their own mpv window       ?   this help\n"),
    ("sect", " BUILT-IN TERMINAL (tmux pane below)\n"),
    ("log", " Ctrl+t      flip focus to the terminal pane / back to george\n"),
    ("log", " click       click the terminal pane to enter it; click george to\n"),
    ("log", "             return. In the terminal, Ctrl+D just gives a fresh\n"),
    ("log", "             prompt - it can never close george. Ctrl+t is the\n"),
    ("log", "             always-works way back to the dashboard.\n"),
    ("log", " esc        hide george (keeps running; click its chip to raise)\n"),
    ("log", " q          quit                         arrows/tab move focus\n"),
]


class Dialog(urwid.Frame):
    def __init__(self, body, title, hint="esc cancel"):
        head = urwid.Text(("sect", f" {title} "))
        foot = urwid.Text(("dim", f" {hint} "))
        super().__init__(body, header=head, footer=foot)


class App:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.q = queue.Queue()
        self.loglines = deque(maxlen=300)
        self.cfg = {"sections": [], "clicks": {}, "links": [],
                    "tv": {}, "showcase": {}, "radio": {}, "nina": {}}
        self.load_cfg(first=True)
        self._cpu_prev = None
        self._net_prev = None
        self._cores_prev = {}
        self._proc_prev = None
        self._proc_dt = None
        self._hist_cpu = deque(maxlen=60)
        self._hist_rx = deque(maxlen=48)
        self._hist_tx = deque(maxlen=48)
        self._hist_t = deque(maxlen=60)
        self._iface = None
        self._wm_dead = False
        self._radio_proc = None
        self._last_width = None
        self._radio_paused = False
        self._nina_proc = None
        self._nina_paused = False
        self.modal = None
        self.mode = "list"
        self.fr_hits = None
        self.quick = []

        self.bar = Bar(self)
        self.status_walker = urwid.SimpleFocusListWalker([])
        self.left_walker = urwid.SimpleFocusListWalker([])
        self.cal = CalendarWidget(self)
        self.upcoming_walker = urwid.SimpleFocusListWalker([])
        self.feed_walker = urwid.SimpleFocusListWalker([])

        self.build_left()
        self.left_box = urwid.AttrMap(
            urwid.LineBox(urwid.ListBox(self.left_walker), title=f" {ico('rocket')} LAUNCH "),
            "", {"focus": "frame_f"})
        self.center_box = self.build_center()
        self.right_box = self.build_right()
        body = urwid.Columns([
            ("fixed", 26, self.left_box),
            ("weight", 1, self.center_box),
            ("fixed", 34, self.right_box),
        ], dividechars=1)
        footer = urwid.Text(("dim",
            " q quit | esc hide/back | n nag | e event | f find&replace | g greet "
            "| m radio | r reload | → scratch · tab actions | 1-9 quick | ? help "))
        self.root = urwid.AttrMap(urwid.Frame(body, header=self.bar,
                                              footer=footer), "")
        self.loop = urwid.MainLoop(self.root, PALETTE,
                                   unhandled_input=self.hotkey,
                                   handle_mouse=False)

    def load_cfg(self, first=False):
        try:
            with CONFIG_PATH.open("rb") as f:
                raw = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            if first:
                sys.exit(f"cannot read {CONFIG_PATH}: {e}")
            self.log(f"config error: {e}", "crit")
            return
        self.cfg["sections"] = raw.get("section", [])
        self.cfg["clicks"] = raw.get("click", {})
        self.cfg["links"] = raw.get("link", [])
        self.cfg["tv"] = raw.get("tv", {})
        self.cfg["funny"] = raw.get("funny", {})
        self.cfg["showcase"] = raw.get("showcase", {})
        self.cfg["radio"] = raw.get("radio", {})
        self.cfg["nina"] = raw.get("nina", {})
        if not first:
            self.rebuild_dynamic()

    def log(self, msg, attr="log"):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.loglines.append(urwid.Text((attr, f"[{stamp}] {msg}")))
        if hasattr(self, "status_walker"):
            del self.status_walker[:]
            self.status_walker.extend(self.loglines)
            self.status_walker.set_focus(len(self.status_walker) - 1)

    def spawn(self, spec, label=None):
        cmd = spec.get("cmd", "").strip()
        if not cmd:
            return
        term = spec.get("term", False)
        if spec.get("gui") and not os.environ.get("DISPLAY"):
            self.log(f"gui button needs X: {cmd}", "warn")
            return
        if term and not os.environ.get("DISPLAY"):
            # tty mode: george already owns the terminal - drop the UI,
            # run the script in the foreground right here, resume after.
            self._tty_run(spec, label)
            return
        argv = shlex.split(cmd)
        if term:
            if spec.get("hold", True):
                argv = build_term_argv(cmd, label)
            else:
                safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label or cmd)[:24]
                argv = ["alacritty", "--title", f"george:{safe}",
                        "-e"] + argv
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            self.log(f"launched: {label or cmd}", "ok")
        except OSError as e:
            self.log(f"launch failed: {cmd}: {e}", "crit")

    def _tty_run(self, spec, label=None):
        """Console-tty terminal launch: suspend the urwid screen, run the
        script in the foreground on this tty (with the same hold wrapper as
        X mode), then bring george back. Ctrl+c kills the script, not
        george (SIG_DFL for the child, SIG_IGN restored after)."""
        cmd = spec.get("cmd", "")
        script = cmd
        if spec.get("hold", True):
            script = (f"{cmd}\n"
                      "rc=$?\n"
                      "echo\n"
                      'echo "--- george: exit $rc | enter closes ---"\n'
                      "read -r _\n")
        self.log(f"launched in tty: {label or cmd}", "ok")
        self.loop.screen.stop()
        rc = None
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            rc = subprocess.run(["bash", "-c", script]).returncode
        except OSError as e:
            print(f"george: launch failed: {cmd}: {e}")
            try:
                input("enter to continue...")
            except EOFError:
                pass
        finally:
            try:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
            self.loop.screen.start()
            self.loop.draw_screen()
        if rc not in (0, None):
            self.log(f"exit {rc}: {label or cmd}", "warn")

    def build_left(self):
        items = []
        self.quick = []
        first = True
        for sec in self.cfg["sections"]:
            if not first:
                items.append(urwid.Divider(" "))
            first = False
            items.append(urwid.Text(
                ("sect", f" {sec_icon(sec.get('title','?'))} "
                         f"{sec.get('title','?').upper()} ")))
            for it in sec.get("items", []):
                if it.get("cmd") == "__fr__":
                    btn = Click(f" {it.get('label', 'find & replace')} ",
                                self.fr_dialog)
                    items.append(btn)
                    continue
                spec = {"cmd": it.get("cmd", ""), "term": it.get("term", False)}
                btn = Click(f" {it.get('label', it.get('cmd','?'))} ",
                            lambda s=spec, l=it.get("label"): self.spawn(s, l))
                items.append(btn)
                if len(self.quick) < 9:
                    self.quick.append((spec, it.get("label")))
        rcfg = self.cfg.get("radio", {})
        if rcfg.get("url"):
            items.append(urwid.Divider(" "))
            lbl = rcfg.get("label", "radio")
            self.radio_btn = Click(f" ▶ RADIO {lbl} ", self.do_radio)
            items.append(self.radio_btn)
        ncfg = self.cfg.get("nina", {})
        if ncfg.get("cmd") or ncfg.get("url"):
            items.append(urwid.Divider(" "))
            nlbl = ncfg.get("label", "NINA")
            self.nina_btn = Click(f" ▶ {nlbl.upper()} ", self.do_nina)
            items.append(self.nina_btn)
        tvcfg = self.cfg.get("tv", {})
        if tvcfg.get("enable"):
            items.append(urwid.Divider(" "))
            tlbl = (tvcfg.get("title", "CH 57") or "CH 57").split(" - ")[0]
            self.tv_btn = Click(f" ▶ {tlbl} ", self.do_tv)
            items.append(self.tv_btn)
        fcfg = self.cfg.get("funny", {})
        if fcfg.get("enable"):
            items.append(urwid.Divider(" "))
            flbl = (fcfg.get("title", "CH 59") or "CH 59").split(" - ")[0]
            self.funny_btn = Click(f" ▶ {flbl} ", self.do_funny)
            items.append(self.funny_btn)
        del self.left_walker[:]
        self.left_walker.extend(items)

    def build_center(self):
        self.p_cpu = urwid.Text("")
        self.p_mem = urwid.Text("")
        self.p_load = urwid.Text("")
        self.p_net = urwid.Text("")
        self.p_disk = urwid.Text("")
        self.p_misc = urwid.Text("")
        self.p_top = urwid.Text("")

        def pane(widget, key):
            spec = self.cfg["clicks"].get(key)
            if spec:
                return urwid.AttrMap(urwid.Filler(
                    Click(widget, lambda s=spec: self.spawn(s), None, "btn_f"),
                    "top"), "", {})
            return urwid.AttrMap(urwid.Filler(widget, "top"), "")

        def mkrow(cells):
            return urwid.Columns([("weight", 1, pane(w, k))
                                  for w, k in cells], dividechars=2)

        rows = [
            mkrow([(self.p_cpu, "cpu"), (self.p_mem, "mem"),
                   (self.p_load, None)]),
            mkrow([(self.p_net, "net"), (self.p_disk, "disk"),
                   (self.p_misc, None)]),
            urwid.Filler(self.p_top, "top"),
        ]
        sysgrid = urwid.Pile([("weight", 1, r) for r in rows])
        merged = urwid.Columns([
            ("fixed", 58, sysgrid),
            ("weight", 1, urwid.ListBox(self.status_walker)),
        ], dividechars=2)
        merged_box = urwid.AttrMap(urwid.LineBox(merged, title=f" {ico('gear')} SYSTEM & STATUS "),
                                   "", {"focus": "frame_f"})

        # ---- SCRATCH: capture pad + dispatch row ------------------------
        self.pad = urwid.Edit(multiline=True)
        self.pad_hint = urwid.Text(("dim",
            " arrow in: stamped + type · enter newline · tab actions "))
        self.scratch_list = urwid.ListBox(
            urwid.SimpleFocusListWalker([self.pad]))
        self.b_email = Click(" email ", self.dispatch_email, None, "btn_f")
        self.b_note = Click(" note ", self.dispatch_note, None, "btn_f")
        self.b_cancel = Click(" esc ", self.dispatch_cancel, None, "btn_f")
        self.scratch_btnrow = urwid.Columns(
            [self.b_email, self.b_note, self.b_cancel], dividechars=2)
        scratchpile = urwid.Pile([
            ("flow", self.pad_hint),
            ("weight", 1, self.scratch_list),
            ("flow", self.scratch_btnrow),
        ])
        self.scratch_pile = scratchpile
        scratchbox = urwid.AttrMap(urwid.LineBox(
            scratchpile, title=f" {ico('music')} SCRATCH "),
            "", {"focus": "frame_f"})
        # Right slot: a static "billboard" of the box's identity — same
        # /proc + sysfs data george already samples, laid out readably.
        self.p_billboard = urwid.Text("")
        billbox = urwid.LineBox(urwid.Filler(
            urwid.Padding(self.p_billboard, left=1, right=1), "top"),
            title=f" {ico('host')} THIS BOX ")
        bottom = urwid.Columns([
            ("weight", 1, scratchbox),
            ("fixed", max(40, int((self._last_width or 190) * 0.38)), billbox),
        ], dividechars=1)
        center = urwid.Pile([
            ("weight", 2, merged_box),
            ("flow", urwid.Divider(" ")),
            ("weight", 3, bottom),
        ])
        self.center_pile = center
        return urwid.AttrMap(center, "", {})

    def build_right(self):
        calbox = urwid.LineBox(urwid.Filler(self.cal.pile, "top"),
                               title=f" {ico('cal')} CALENDAR ")
        nag_btn = Click(" nag 15m ", self.do_nag)
        evt_btn = Click(" event ", self.event_dialog)
        btnrow = urwid.Columns([nag_btn, evt_btn], dividechars=1)
        upbox = urwid.LineBox(urwid.ListBox(self.upcoming_walker), title=f" {ico('bolt')} EVENTS ")
        feedbox = urwid.LineBox(urwid.ListBox(self.feed_walker),
                                title=f" {ico('rss')} FEEDS & LINKS ")
        pile = urwid.Pile([
            ("fixed", 10, calbox),
            ("flow", btnrow),
            ("fixed", 8, upbox),
            ("weight", 2, feedbox),
        ])
        return pile

    def rebuild_dynamic(self):
        self.build_left()
        self.refresh_upcoming()
        self.render_feeds()
        self.log("config reloaded", "ok")

    def render_billboard(self):
        try:
            if getattr(self, "_bb_static", None) is None:
                pn, pv = dmi_product()
                wm, sess = detect_wm()
                theme, tag = gtk_theme()
                pkgs, sysd = dpkg_stats()
                pc = f"{pn} ({pv})".strip() if pn else "?"
                self._bb_static = {
                    "pc": pc,
                    "os": os_pretty(),
                    "kern": kernel_version(),
                    "wm": f"{wm} ({sess})" if wm else sess,
                    "theme": f"{theme} [{tag}]" if theme else "",
                    "pkgs": pkgs, "sysd": sysd,
                    "fsr": fstype_for("/"), "fsh": fstype_for("/home"),
                }
            S = self._bb_static

            la = os.getloadavg()
            cores = os.cpu_count() or 1
            la1 = ("crit" if la[0] > cores * 1.5
                   else "warn" if la[0] > cores * 0.7 else "ok")
            mp, gu = mem_info()
            matt = "crit" if mp > 90 else "warn" if mp > 75 else "ok"
            ur_, tr_, up_ = disk_stat("/")
            hr_, ht_, hp_ = disk_stat("/home")
            da = "crit" if up_ > 90 else "warn" if up_ > 75 else "ok"
            dh = "crit" if hp_ > 90 else "warn" if hp_ > 75 else "ok"

            def K(k):
                return ("accent", f" {k:<4}")

            AR = ("dim", " -> ")
            NL = ("", "\n")

            def T():
                return ("dim", " \u251c\u2500 ")

            def E():
                return ("dim", " \u2514\u2500 ")

            L = []
            L += [K("PC"), AR, ("info", S["pc"]), NL]
            L += [K("OS"), AR, ("info", S["os"]), NL]
            L += [T(), ("dim", "kernel "), ("info", S["kern"]), NL]
            L += [E(), ("dim", "up "), ("info", fmt_uptime(uptime_secs())), NL]
            L += [K("WM"), AR, ("info", S["wm"]), NL]
            if S["theme"]:
                L += [T(), ("dim", "theme "), ("info", S["theme"]), NL]
            L += [E(), ("dim", f"{S['pkgs']} pkgs "),
                  ("dim", "(dpkg)"), NL]
            init = "systemd" + (f" {S['sysd']}" if S["sysd"] else "")
            L += [K("INIT"), AR, ("info", init), NL]
            L += [K("DATE"), AR, ("info",
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")), NL]
            L += [K("LOAD"), AR, (la1, f"{la[0]:.2f} "),
                  ("dim", f"{la[1]:.2f} {la[2]:.2f}"), NL]
            L += [K("PROC"), AR, ("info", str(proc_count())), NL]
            L += [K("MEM"), AR, (matt, f"{gu:.1f}G / "
                                       f"{mem_total_gb():.1f}G "),
                  ("dim", f"({mp:.0f}%)"), NL]
            L += [K("DISK"), AR, ("dim", "/ "),
                  (da, f"{ur_:.1f}/{tr_:.0f}G "),
                  ("dim", f"({up_:.0f}%) {S['fsr']}"), NL]
            L += [("dim", " " * 6 + "\u2514\u2500 "), ("dim", "~ "),
                  (dh, f"{hr_:.1f}/{ht_:.0f}G "),
                  ("dim", f"({hp_:.0f}%) {S['fsh']}"), NL]
            self.p_billboard.set_text(L)
        except Exception as e:
            self.p_billboard.set_text(("dim", f" billboard error: {e}"))

    def _screen_size(self):
        try:
            out = subprocess.run(["xdotool", "getdisplaygeometry"],
                                 capture_output=True, text=True,
                                 timeout=3).stdout.split()
            return int(out[0]), int(out[1])
        except (OSError, subprocess.SubprocessError, IndexError,
                ValueError):
            return None

    def _freeze(self, proc):
        try:
            os.kill(proc.pid, signal.SIGSTOP)
            return True
        except OSError:
            return False

    def _thaw(self, proc):
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except OSError:
            pass

    def _chan_start(self, name):
        cfg = self.cfg.get(name, {})
        if name == "tv":
            disp = cfg.get("title") or "CHANNEL 57"
        else:
            disp = cfg.get("title") or "CHANNEL 59"
        if not (cfg.get("enable") and os.environ.get("DISPLAY")):
            return
        if not shutil.which("mpv"):
            self.log("tv: mpv not installed", "warn")
            return
        pl = Path(cfg.get("playlist", "") or "").expanduser()
        if not pl.is_absolute():
            pl = (HERE / pl).resolve()
        else:
            pl = pl.resolve()
        if not pl.is_file():
            self.log(f"tv playlist missing: {pl}", "warn")
            return
        try:
            args = ["mpv", "--loop-playlist", "--shuffle", "--force-window",
                    "--keepaspect=yes", f"--playlist={pl}"]
            g = self._chan_geometry()
            if g:
                args += [f"--geometry={g}"]
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.log(f"{disp} opened in its own window", "accent")
        except OSError as e:
            self.log(f"tv failed: {e}", "crit")

    def _chan_geometry(self):
        """Tiled ~60% window in the top-right so george stays visible behind
        it (alt-tab back is demonstrable); falls back to None (WM default)."""
        s = self._screen_size()
        if not s:
            return None
        sw, sh = s
        w = int(sw * 0.6)
        h = int(sh * 0.6)
        margin = 8
        x = sw - w - margin
        y = margin
        return f"{w}x{h}+{x}+{y}"


    def radio_start(self):
        rcfg = self.cfg.get("radio", {})
        url = rcfg.get("url")
        if not url:
            self.log("radio: no [radio] url configured", "warn")
            return
        if self._nina_proc and self._nina_proc.poll() is None:
            if self._freeze(self._nina_proc):
                self._nina_paused = True
                self.log("nina paused for radio", "info")
        try:
            tvlog = Path("~/.local/state").expanduser() / "george-tv.log"
            lf = tvlog.open("a")
            self._radio_proc = subprocess.Popen(
                ["mpv", "--no-video", "--force-window=no",
                 "--audio-display=no", "--really-quiet", url],
                stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                start_new_session=True)
            lbl = rcfg.get("label", "radio")
            self.log(f"radio on: {lbl}", "accent")
            if getattr(self, "radio_btn", None):
                self.radio_btn._t.set_text(f" ■ RADIO {lbl} ON AIR ")
        except OSError as e:
            self.log(f"radio failed: {e}", "crit")

    def radio_stop(self, why="radio off", silent=False):
        proc, self._radio_proc = self._radio_proc, None
        was_paused = self._radio_paused
        self._radio_paused = False
        if proc is not None and proc.poll() is None:
            if was_paused:
                self._thaw(proc)
            try:
                proc.terminate()
            except OSError:
                pass
        if not silent:
            self.log(why, "ok")
        rcfg = self.cfg.get("radio", {})
        lbl = rcfg.get("label", "radio")
        if getattr(self, "radio_btn", None):
            self.radio_btn._t.set_text(f" ▶ RADIO {lbl} ")
        if self._nina_paused:
            np_, self._nina_paused = self._nina_proc, False
            if np_ is not None and np_.poll() is None:
                self._thaw(np_)
                if not silent:
                    self.log("nina resumed", "accent")

    def nina_start(self):
        ncfg = self.cfg.get("nina", {})
        url = ncfg.get("url")
        cmd = (ncfg.get("cmd") or "nina.sh").strip()
        dynamic = not url
        if dynamic:
            nina_cmd = None
            for cand in (cmd, str(Path.home() / "bin" / cmd),
                         str(Path.home() / "nina" / cmd)):
                if shutil.which(cand) or Path(cand).expanduser().is_file():
                    nina_cmd = cand
                    break
            if not nina_cmd:
                self.log("nina: no cmd or url configured", "warn")
                return
            try:
                out = subprocess.run([nina_cmd, "--playlist"],
                                     capture_output=True, text=True,
                                     timeout=15,
                                     stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                self.log(f"nina pick failed: {e}", "warn")
                return
            if out.returncode != 0:
                self.log("nina: picker errored: " +
                         (out.stderr.strip().splitlines() or
                          ["?"])[-1][:80], "warn")
                return
            parts = out.stdout.strip().split("\t", 1)
            if len(parts) != 2 or not parts[1]:
                self.log("nina: picker returned no playlist", "warn")
                return
            title, url = parts[0], parts[1]
        else:
            title = ncfg.get("label", "NINA")
        if self._radio_proc and self._radio_proc.poll() is None:
            if self._freeze(self._radio_proc):
                self._radio_paused = True
                self.log("radio paused for nina", "info")
        try:
            tvlog = Path("~/.local/state").expanduser() / "george-tv.log"
            lf = tvlog.open("a")
            args = ["mpv", "--no-video", "--force-window=no",
                    "--audio-display=no", "--really-quiet",
                    "--title=Random Nina"]
            if dynamic:
                # continuous: nina.sh handed us a fresh shuffled playlist
                args += ["--loop-playlist", "--playlist=" + url]
            else:
                args += [url]
            self._nina_proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                start_new_session=True)
            nlbl = ncfg.get("label", "NINA")
            self.log(f"nina on: {title}", "accent")
            if getattr(self, "nina_btn", None):
                self.nina_btn._t.set_text(f" ■ {nlbl.upper()} ON AIR ")
        except OSError as e:
            self.log(f"nina failed: {e}", "crit")

    def nina_stop(self, why="nina off", silent=False):
        proc, self._nina_proc = self._nina_proc, None
        was_paused = self._nina_paused
        self._nina_paused = False
        if proc is not None and proc.poll() is None:
            if was_paused:
                self._thaw(proc)
            try:
                proc.terminate()
            except OSError:
                pass
        if not silent:
            self.log(why, "ok")
            ncfg = self.cfg.get("nina", {})
            nlbl = ncfg.get("label", "NINA")
            if getattr(self, "nina_btn", None):
                self.nina_btn._t.set_text(f" ▶ {nlbl.upper()} ")
        if self._radio_paused:
            rp, self._radio_paused = self._radio_proc, False
            if rp is not None and rp.poll() is None:
                self._thaw(rp)
                if not silent:
                    self.log("radio resumed", "accent")

    def do_nina(self):
        if self._nina_proc and self._nina_proc.poll() is None:
            self.nina_stop()
        else:
            self.nina_start()

    def do_radio(self):
        if self._radio_proc and self._radio_proc.poll() is None:
            self.radio_stop()
        else:
            self.radio_start()

    def do_tv(self):
        self._chan_start("tv")

    def do_funny(self):
        self._chan_start("funny")

    # ---- SCRATCH capture pad ----------------------------------------
    def _stamp_pad(self):
        import re as _re
        txt = self.pad.edit_text
        stamp = f"[{datetime.now():%Y-%m-%d %H:%M}]"
        if _re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*$", txt.rstrip()):
            return
        if txt and not txt.endswith("\n"):
            txt += "\n"
        self.pad.set_edit_text(txt + stamp + "\n")
        self.pad.set_edit_pos(len(txt) + len(stamp) + 1)

    def _enter_pad(self):
        frame = self.loop.widget.base_widget
        if frame.focus_position != "body":
            return
        frame.body.focus_position = 1
        self.center_pile.focus_position = 2
        self.scratch_pile.focus_position = 1
        self._stamp_pad()
        self.mode = "pad"

    def _to_list(self):
        frame = self.loop.widget.base_widget
        if frame.focus_position == "body":
            frame.body.focus_position = 0
        self.mode = "list"

    def _pad_text(self):
        import re as _re
        stamp_re = _re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*$")
        return "\n".join(
            ln for ln in self.pad.edit_text.splitlines()
            if ln.strip() and not stamp_re.match(ln.strip()))

    def _scratch_empty(self):
        return not self.pad.edit_text.strip().rstrip("] ").strip("[]0123456789-: ")

    def dispatch_email(self):
        body = self._pad_text()
        if not body:
            self.log("scratch empty - nothing to send", "warn")
            return
        f = Path("/tmp") / f"george-scratch-{time.time_ns()}.txt"
        f.write_text(f"To: \nSubject: scratch "
                     f"[{datetime.now():%Y-%m-%d %H:%M}]\n\n{body}\n")
        subprocess.Popen(["claws-mail", "--compose-from-file", str(f)],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
        self.log("scratch handed to claws-mail compose", "accent")
        self._pad_reset()

    def dispatch_note(self):
        text = self.pad.edit_text.strip()
        if not text:
            self.log("scratch empty - nothing to note", "warn")
            return
        import re as _re
        subject = ""
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not _re.match(r"^\[\d{4}-", ln):
                subject = ln
                break
        slug = _re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40]
        now = datetime.now()
        fname = f"{now:%Y%m%d-%H%M%S}" + (f"-{slug}" if slug else "") + ".txt"
        d = Path("~/notes").expanduser()
        d.mkdir(parents=True, exist_ok=True)
        p = d / fname
        p.write_text(self.pad.edit_text + "\n")
        self.log(f"noted: {p.name}", "accent")
        self._pad_reset()

    def dispatch_cancel(self):
        self.scratch_pile.focus_position = 1
        self.mode = "pad"

    def _pad_reset(self):
        self.pad.set_edit_text("")
        self.scratch_pile.focus_position = 1
        self.mode = "pad"
        self._stamp_pad()

    def do_nag(self):
        self.spawn({"cmd": "nag in 15"}, "nag set for 15 minutes")

    def do_greet(self):
        try:
            subprocess.Popen([str(Path("~/bin/greet.sh").expanduser())],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.log("greet.sh fired: WOOHOO", "accent")
        except OSError as e:
            self.log(f"greet failed: {e}", "warn")

    def refresh_upcoming(self):
        del self.upcoming_walker[:]
        now = datetime.now()
        evts = [(d, n) for d, n in load_events() if d >= now][:8]
        if not evts:
            self.upcoming_walker.append(urwid.Text(("dim", " nothing scheduled")))
        for d, n in evts:
            self.upcoming_walker.append(urwid.Text(
                [("info", f" {d:%m-%d %H:%M} "), ("log", n[:22])]))

    def render_feeds(self):
        del self.feed_walker[:]
        links = self.cfg["links"]
        shown = 0
        for i in range(LINK_SLOTS):
            if i < len(links):
                ent = links[i]
                name = ent.get("name", "?")
                url = ent.get("url", "")
                kind = ent.get("kind", "rss")
            else:
                name, url, kind = f"(empty slot {i + 1})", "", "empty"
            if kind == "empty":
                self.feed_walker.append(
                    urwid.Text(("dim", f" {name}")))
                continue
            shown += 1
            self.feed_walker.append(urwid.Text(("sect", f" [{name}] ")))
            cache = CACHE_DIR / f"feed_{i}.json"
            if cache.exists():
                try:
                    data = json.loads(cache.read_text())
                    for t, l, d in data.get("items", []):
                        tt = t if len(t) <= 58 else t[:57] + "~"
                        self.feed_walker.append(
                            Click(f"  {tt} ",
                                  (lambda u=l: self.open_url(u)),
                                  "log", "btn_f"))
                except (json.JSONDecodeError, OSError):
                    self.feed_walker.append(urwid.Text(("warn", "  (cache bad)")))
            elif kind == "link":
                self.feed_walker.append(
                    Click(f"  {url[:56]} ", (lambda u=url: self.open_url(u)),
                          "info", "btn_f"))
            else:
                self.feed_walker.append(urwid.Text(("dim", "  (fetching...)")))
        if not shown:
            self.feed_walker.append(urwid.Text(
                ("dim", " edit buttons.toml [[link]] entries")))

    def open_url(self, url):
        if not url:
            return
        br = os.environ.get("BROWSER")
        try:
            if br:
                subprocess.Popen(shlex.split(br) + [url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            else:
                webbrowser.open(url)
            self.log(f"opened {url[:60]}", "info")
        except OSError as e:
            self.log(f"browser failed: {e}", "crit")

    def feed_worker(self):
        first = True
        while True:
            for i in range(LINK_SLOTS):
                if i >= len(self.cfg["links"]):
                    break
                ent = self.cfg["links"][i]
                if ent.get("kind", "rss") != "rss" or not ent.get("url"):
                    continue
                cache = CACHE_DIR / f"feed_{i}.json"
                if first and cache.exists():
                    continue
                try:
                    req = Request(ent["url"], headers={"User-Agent": APP})
                    with urlopen(req, timeout=6) as r:
                        titles = parse_feed(r.read())
                    if titles is not None:
                        data = {"name": ent.get("name"), "items": titles}
                        tmp = cache.with_suffix(".tmp")
                        tmp.write_text(json.dumps(data))
                        tmp.replace(cache)
                        self.q.put(("feeds", i))
                except Exception:
                    pass
            first = False
            for _ in range(FEED_EVERY):
                time.sleep(1)

    def snapshot(self):
        now = time.monotonic()
        t = Path("/proc/stat").read_text()
        cur = parse_proc_stat(t)
        pct = None
        if self._cpu_prev:
            pct = cpu_pct(self._cpu_prev, cur)
        self._cpu_prev = cur

        cur_cores = parse_cores_stat(t)
        cpcts = core_pct(self._cores_prev, cur_cores)
        if self._cores_prev and cpcts:
            self._hist_cpu.append(sum(cpcts.values()) / len(cpcts))
        self._cores_prev = cur_cores

        tp = cpu_temp()
        if tp is not None:
            self._hist_t.append(tp)

        iface = parse_route(Path("/proc/net/route").read_text())
        self._iface = iface
        rx = tx = None
        if iface:
            nb = net_bytes(iface)
            if nb and self._net_prev and self._net_prev[0] == iface:
                dt = now - self._net_prev[3]
                if dt > 0:
                    rx = (nb[0] - self._net_prev[1]) / dt
                    tx = (nb[1] - self._net_prev[2]) / dt
            if nb:
                self._net_prev = (iface, nb[0], nb[1], now)
        if rx is not None:
            self._hist_rx.append(min(rx, 1 << 22))
            self._hist_tx.append(min(tx, 1 << 22))
        return pct, rx, tx, cpcts

    def tick_stats(self, loop=None, data=None):
        if not getattr(self, "_placed", False):
            self.place_self()
        try:
            pct, rx, tx, cpcts = self.snapshot()
            if pct is not None:
                cattr = ("crit" if pct > 85
                         else "warn" if pct > 60 else "ok")
                cpu_seg = [("sect", f" {ico('cpu')} CPU "),
                           (cattr, f"{pct:4.1f}%")]
                if self._hist_cpu:
                    cpu_seg.append(("info", " " + spark(self._hist_cpu, 14)))
                for i in sorted(cpcts or {}):
                    c = cpcts[i]
                    a = "crit" if c > 85 else "warn" if c > 60 else "ok"
                    cpu_seg.append(
                        (a, f"\n{i}{_SPARK[min(7, int(c / 12.5))]}"))
                self.p_cpu.set_text(cpu_seg)
            else:
                self.p_cpu.set_text(("dim", " CPU --"))

            mp, gu = mem_info()
            matt = "crit" if mp > 90 else "warn" if mp > 75 else "ok"
            self.p_mem.set_text([("sect", f" {ico('mem')} MEM "),
                                 (matt, gauge(mp / 100.0, 8) + " "),
                                 ("dim", f"{gu:.1f}G")])

            la = os.getloadavg()[0]
            cores = os.cpu_count() or 1
            latt = "crit" if la > cores * 1.5 else "warn" if la > cores * 0.7 else "ok"
            self.p_load.set_text([("sect", f" {ico('clock')} LOAD "), (latt, f"{la:.2f}"),
                                  ("dim", f" up {fmt_uptime(uptime_secs())}")])

            if rx is not None:
                net_seg = [("sect", f" {ico('net')} NET "),
                           ("ok", f"v{fmt_rate(rx)} "),
                           ("info", f"^{fmt_rate(tx)}"),
                           ("dim", f" {self._iface or ''}"),
                           ("info", "\n v" + spark(self._hist_rx, 18)),
                           ("accent", " ^" + spark(self._hist_tx, 18))]
                self.p_net.set_text(net_seg)
            else:
                self.p_net.set_text(("dim", " NET down"))

            fr = disk_free("/")
            fh = disk_free("/home")
            du = disk_used_pct("/")
            du = disk_used_pct("/")
            if du is not None:
                datt = "crit" if du > 90 else "warn" if du > 75 else "ok"
                self.p_disk.set_text([("sect", f" {ico('disk')} DISK "),
                                      (datt, gauge(du / 100.0, 6) + " "),
                                      ("dim", f"/{fr:.0f}G ~{fh:.0f}G")])
            else:
                self.p_disk.set_text(("dim", " DISK --"))

            bp, bs = battery()
            tp = cpu_temp() or max_temp()
            misc = [("sect", f" {ico('bat')} ")]
            if bp is not None:
                batt = "crit" if bp < 15 else "warn" if bp < 35 else "ok"
                chg = {"Charging": ("ok", "+"), "Full": ("ok", "="),
                       "Discharging": (batt, "")}.get(bs, (batt, ""))
                misc += [(chg[0], f"{chg[1]}{bp}% "), ("dim", f"{bs[:6]} ")]
            else:
                misc += [("dim", "-- ")]
            misc.append(("sect", f"{ico('temp')} "))
            if tp is not None:
                tatt = "crit" if tp > 85 else "warn" if tp > 65 else "ok"
                misc.append((tatt, f"{tp:.0f}C"))
                if tatt != "dim":
                    misc.append(("dim", " " + spark(list(self._hist_t)[-20:], 10)))
            else:
                misc.append(("dim", "--"))
            self.p_misc.set_text(misc)

            now = time.monotonic()
            samp = proc_sample()
            if self._proc_prev and self._proc_dt:
                dt = now - self._proc_dt
                top = proc_top(self._proc_prev, samp, dt)
                seg = [("sect", f" {ico('term')} TOP ")]
                if top:
                    for p_, pid, nm in top:
                        a = "crit" if p_ > 80 else "warn" if p_ > 40 else "ok"
                        seg.append((a, f"{p_:3.0f}% "))
                        seg.append(("dim", nm + "  "))
                else:
                    seg.append(("dim", "idle"))
                self.p_top.set_text(seg)
            self._proc_prev = samp
            self._proc_dt = now
            self.render_billboard()

            width = self.loop.screen.get_cols_rows()[0]
            self._last_width = width
            self.bar.update(width)
        except Exception as e:
            self.log(f"tick error: {e}", "crit")
        self.loop.set_alarm_in(STAT_EVERY, self.tick_stats)

    def tick_clock(self, loop=None, data=None):
        self.drain_queue()
        self.loop.set_alarm_in(CLOCK_EVERY, self.tick_clock)

    def tick_events(self, loop=None, data=None):
        now = datetime.now()
        fired = False
        for d, n in load_events():
            if d <= now:
                try:
                    subprocess.run(["notify-send", "-a", APP, "-u", "critical",
                                    f"GEORGE {d:%H:%M}", n], timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
                sys.stdout.write("\a")
                sys.stdout.flush()
                with FIRED_LOG.open("a") as f:
                    f.write(f"{d:%Y-%m-%d %H:%M}\t{n}\n")
                remove_event(d, n)
                self.log(f"event fired: {n}", "accent")
                fired = True
        if fired:
            self.refresh_upcoming()
        self.loop.set_alarm_in(EVENT_EVERY, self.tick_events)

    def drain_queue(self):
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                return
            if msg[0] == "feeds":
                self.render_feeds()
            elif msg[0] == "fr_scan":
                self.fr_hits = msg[1]
                self.fr_show_hits(msg[2])
            elif msg[0] == "fr_done":
                files, total = msg[1]
                self.log(f"find&replace: {total} hits in {files} files "
                         f"(backups *{FR_BACKUP})", "ok")
                if self.fr_result_text:
                    self.fr_result_text.set_text(
                        ("ok", f" replaced {total} hits in {files} files.\n"
                               f" originals saved as *{FR_BACKUP}"))

    def fr_worker_scan(self, root, find, pattern, use_regex):
        hits = fr_scan(root, find, pattern, use_regex)
        if hits is None:
            self.q.put(("fr_scan", None, "badregex"))
        else:
            self.q.put(("fr_scan", hits, None))

    def fr_show_hits(self, err):
        if not self.fr_result_text:
            return
        if err == "badregex":
            self.fr_result_text.set_text(("crit", " invalid regex"))
            return
        if self.fr_hits is None:
            return
        if not self.fr_hits:
            self.fr_result_text.set_text(("dim", " no matches"))
            return
        lines = []
        tfiles = len(self.fr_hits)
        thits = sum(n for _, n in self.fr_hits)
        lines.append(("sect", f" {thits} hits in {tfiles} files:\n"))
        for p, n in self.fr_hits[:120]:
            attr = "crit" if n > 40 else "warn" if n > 10 else "log"
            lines.append((attr, f" {n:>4}x {p}\n"))
        if tfiles > 120:
            lines.append(("dim", f" ...and {tfiles - 120} more files\n"))
        self.fr_result_text.set_text(lines)

    def open_modal(self, dlg):
        self.modal = dlg
        wrapped = urwid.AttrMap(dlg, {
            None: "modal", "btn": "mfield", "dim": "mdim",
            "sect": "msect", "log": "mfield", "info": "mfield",
            "warn": "mwarn",
        })
        ov = urwid.Overlay(wrapped, self.root, align="center",
                           width=("relative", 72), valign="middle",
                           height=("relative", 80), min_width=48,
                           min_height=12)
        self.loop.widget = ov
        try:
            body = dlg.body
            if isinstance(body, urwid.Filler):
                body = body.original_widget
            if hasattr(body, "set_focus"):
                body.set_focus(0)
        except Exception:
            pass

    def close_modal(self):
        self.modal = None
        self.loop.widget = self.root

    def event_dialog(self):
        d_edit = urwid.Edit(("btn", " date (YYYY-MM-DD): "), date.today().isoformat())
        t_edit = urwid.Edit(("btn", " time (HH:MM):       "), "12:00")
        n_edit = urwid.Edit(("btn", " note:               "), "")

        def save(_btn=None):
            try:
                dd = date.fromisoformat(d_edit.get_edit_text().strip())
                tt = datetime.strptime(t_edit.get_edit_text().strip(), "%H:%M").time()
            except ValueError:
                self.log("event: bad date/time", "warn")
                return
            note = n_edit.get_edit_text().strip() or "(untitled)"
            dt = datetime.combine(dd, tt)
            append_event(dt, note)
            self.log(f"event saved: {dt:%m-%d %H:%M} {note}", "ok")
            self.close_modal()
            self.refresh_upcoming()

        rows = [("flow", d_edit), ("flow", t_edit), ("flow", n_edit),
                ("flow", urwid.Divider(" ")),
                ("flow", urwid.Columns([Click(" save ", save),
                                        Click(" cancel ", self.close_modal)],
                                       dividechars=2))]
        self.open_modal(Dialog(urwid.Filler(urwid.Pile(rows), "top"),
                               "NEW EVENT"))

    def fr_dialog(self):
        f_edit = urwid.Edit(("btn", " find:        "))
        r_edit = urwid.Edit(("btn", " replace:     "))
        d_edit = urwid.Edit(("btn", " directory:   "), "~")
        g_edit = urwid.Edit(("btn", " file glob:   "), "*")
        self.fr_result_text = urwid.Text(("dim", " scan first, then apply."))
        state = {"regex": False}
        mode_btn_ref = {}

        def toggle(_btn=None):
            state["regex"] = not state["regex"]
            btn = mode_btn_ref.get("w")
            if btn:
                btn._t.set_text(" mode: regex " if state["regex"]
                                else " mode: literal ")

        mode_btn_ref["w"] = Click(" mode: literal ", toggle)

        def scan(_btn=None):
            find = f_edit.get_edit_text()
            if not find:
                self.fr_result_text.set_text(("warn", " nothing to find"))
                return
            self.fr_hits = None
            self.fr_result_text.set_text(("dim", " scanning..."))
            threading.Thread(target=self.fr_worker_scan, daemon=True, args=(
                d_edit.get_edit_text() or "~", find,
                g_edit.get_edit_text() or "*", state["regex"],
            )).start()

        def apply(_btn=None):
            if not self.fr_hits:
                self.fr_result_text.set_text(("warn", " scan first"))
                return
            find = f_edit.get_edit_text()
            repl = r_edit.get_edit_text()
            paths = [p for p, _ in self.fr_hits]
            self.fr_result_text.set_text(("dim", " applying..."))
            threading.Thread(target=lambda: self.q.put(
                ("fr_done", fr_apply(paths, find, repl, state["regex"]))
            ), daemon=True).start()

        rows = [("flow", f_edit), ("flow", r_edit), ("flow", d_edit),
                ("flow", g_edit),
                ("flow", urwid.Columns([mode_btn_ref["w"],
                                        Click(" scan ", scan),
                                        Click(" APPLY ", apply, "warn", "btn_f"),
                                        Click(" close ", self.close_modal)],
                                       dividechars=1)),
                ("flow", urwid.Divider("-"))]
        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(
            [self.fr_result_text]))
        pile = urwid.Pile(rows + [("weight", 1, listbox)])
        self.open_modal(Dialog(pile, "FIND & REPLACE",
                               hint="literal mode = plain text; regex mode = python re"))

    def help_dialog(self):
        rows = [urwid.Text(t if isinstance(t, tuple) else t) for t in HELP_TEXT]
        rows.append(urwid.Divider(" "))
        rows.append(Click(" close ", self.close_modal))
        self.open_modal(Dialog(urwid.ListBox(urwid.SimpleFocusListWalker(rows)),
                               "HELP"))

    def hotkey(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        for k in keys:
            if not isinstance(k, str):
                continue
            if k == "esc":
                if self.modal:
                    self.close_modal()
                else:
                    wid = self.win_id_quiet()
                    if wid:
                        wm_cmd(wid, "min")
                continue
            if self.modal:
                continue
            if k == "q":
                raise urwid.ExitMainLoop
            elif k == "?":
                self.help_dialog()
            elif k == "n":
                self.do_nag()
            elif k == "e":
                self.event_dialog()
            elif k == "f":
                self.fr_dialog()
            elif k == "g":
                self.do_greet()
            elif k == "m":
                self.do_radio()
            elif k == "right" and self.mode == "list":
                self._enter_pad()
            elif k == "tab" and self.mode == "pad":
                self.scratch_pile.focus_position = 2
                self.mode = "dispatch"
            elif k == "tab" and self.mode == "dispatch":
                self.scratch_pile.focus_position = 1
                self.mode = "pad"
            elif k == "esc" and self.mode == "dispatch":
                self.dispatch_cancel()
            elif k == "esc" and self.mode == "pad":
                self._to_list()
            elif k == "r":
                self.load_cfg()
            elif k.isdigit() and 1 <= int(k) <= 9:
                idx = int(k) - 1
                if idx < len(self.quick):
                    spec, label = self.quick[idx]
                    self.spawn(spec, label)

    def win_id_quiet(self):
        return self_win_id()

    def place_self(self, tries=240):
        if not os.environ.get("DISPLAY"):
            return
        if getattr(self, "_placed", False) or getattr(self, "_placing", False):
            return

        def diag(msg):
            try:
                with open(os.path.expanduser(
                        "~/.local/state/george-place.log"), "a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            except OSError:
                pass

        def attempt(loop=None, data=None):
            try:
                diag(f"attempt {data} enter")
                wid = self_win_id()
                if not wid:
                    diag(f"attempt {data}: no own window yet")
                    if data < tries:
                        loop.set_alarm_in(0.5, attempt, data + 1)
                    else:
                        self._placing = False
                        diag("exhausted retries")
                        if not getattr(self, "_place_warned", False):
                            self._place_warned = True
                            self.log("placement: own window never appeared",
                                     "warn")
                    return
                subprocess.run(["wmctrl", "-i", "-r", wid, "-b",
                                "add,maximized_vert,maximized_horz"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3)
                subprocess.run(["xprop", "-id", wid,
                                "-f", "_MOTIF_WM_HINTS", "32c",
                                "-set", "_MOTIF_WM_HINTS",
                                "0x2, 0x0, 0x0, 0x0, 0x0"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3)
                out = subprocess.run(
                    ["xprop", "-id", wid, "_NET_WM_STATE"],
                    capture_output=True, text=True, timeout=3).stdout
                if "MAXIMIZED_VERT" in out and "MAXIMIZED_HORZ" in out:
                    self._placing = False
                    self._placed = True
                    diag(f"placed wid={wid}")
                else:
                    diag(f"attempt {data}: wid={wid} maximize did not stick")
                    if data < tries:
                        loop.set_alarm_in(0.5, attempt, data + 1)
                    else:
                        self._placing = False
            except Exception as e:
                diag(f"attempt {data} error: {e!r}")
                self._placing = False
                if data < tries:
                    try:
                        loop.set_alarm_in(0.5, attempt, data + 1)
                    except Exception:
                        pass

        self._placing = True
        diag("--- place_self chain start ---")
        self.loop.set_alarm_in(0.2, attempt, 0)

    def run(self):
        # Ctrl+c (SIGINT) in the terminal is the instinctive "leave this"
        # key, but here it would hit george and quit it. Neutralise it so an
        # accidental Ctrl+c never takes the dashboard down; the way out of
        # the built-in terminal is Ctrl+t (flip to the other tmux pane) or a
        # click, and q quits george by intent.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        self.log(f"george online. config: {CONFIG_PATH}", "sect")
        self.log("welcome aboard. all systems nominal.", "accent")
        self.do_greet()
        self.refresh_upcoming()
        self.render_feeds()
        threading.Thread(target=self.feed_worker, daemon=True).start()
        self.tick_stats()
        self.tick_clock()
        self.tick_events()
        self.place_self()
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.radio_stop(silent=True)
            self.nina_stop(silent=True)
            # Teardown of the tmux session (so quitting george closes the
            # whole alacritty window back to the desktop) is owned by the
            # launcher wrapper in ~/bin/george, which runs `tmux kill-session`
            # after this process exits - cleaner than guessing the session
            # id from $TMUX here.


def selftest():
    fails = []

    def chk(name, cond):
        if not cond:
            fails.append(name)

    chk("cpu_pct zero delta", cpu_pct([0] * 8, [0] * 8) == 0.0)
    chk("cpu_pct half busy", abs(cpu_pct([0, 0, 0, 90, 10, 0, 0, 0],
                                         [5, 0, 0, 92, 13, 0, 0, 0]) - 50.0) < 0.01)
    chk("cpu_pct clamp", cpu_pct([0, 0, 0, 0, 0, 0, 0, 0],
                                 [10, 0, 0, 0, 0, 0, 0, 0]) == 100.0)
    chk("parse_route", parse_route(
        "Iface\tDest\tGW\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "enp0s31f6\t00000000\tC0A80101\t0003\t0\t0\t100\t00000000\n"
        "wlp3s0\t00A801C0\t00000000\t0001\t0\t0\t600\t007FFFFF\n") == "enp0s31f6")
    chk("parse_proc_stat", parse_proc_stat(
        "cpu  1 2 3 4 5 6 7 8\n") == [1, 2, 3, 4, 5, 6, 7, 8])
    chk("parse_wm", parse_wm(
        "0x01 0 host Alacritty.georges my title\n"
        "0x02 1 host KeePassxc.keepassxc vault\n")[1] ==
        ("0x02", "vault", "KeePassxc.keepassxc"))
    chk("fmt_rate K", fmt_rate(5120) == "5K/s")
    chk("fmt_rate M", fmt_rate(1572864) == "1.5M/s")
    chk("fmt_uptime d", fmt_uptime(90061) == "1d 1h")
    chk("fmt_uptime hm", fmt_uptime(3730) == "1h 2m")

    nm, lines = cal_markup(2026, 8, date(2026, 8, 24))
    chk("cal name", nm == "August")
    chk("cal rows", len(lines) == 7)
    chk("cal today marked", any(
        a == "today" and t.strip() == "24"
        for ln in lines for a, t in ln))

    chk("spark flat", spark([5, 5, 5]) == "▁▁▁")
    chk("spark span", spark([0, 4, 8])[2] == "█" and spark([0, 4, 8])[1] == "▅")
    chk("spark width", len(spark(list(range(30)), width=12)) == 12)
    chk("spark deque", len(spark(deque(range(30), maxlen=60),
                                  width=12)) == 12)
    chk("gauge half", gauge(0.5) == "▰▰▰▰▰▱▱▱▱▱")
    chk("gauge clamp", gauge(2.0).endswith("▰" * 10))

    rss = (b"<rss><channel><item><title>A</title><link>http://a</link>"
           b"<pubDate>Mon, 24 Aug 2026</pubDate></item>"
           b"<item><title>B</title><link>http://b</link></item></channel></rss>")
    got = parse_feed(rss)
    chk("rss parse", got and got[0][0] == "A" and got[0][1] == "http://a")
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            b"<title>C</title>"
            b'<link href="http://c"/></entry></feed>')
    got = parse_feed(atom)
    chk("atom parse", got and got[0][0] == "C" and got[0][1] == "http://c")
    chk("rss garbage", parse_feed(b"<not-xml") is None)

    tmp = Path("/tmp/opencode/fr-test")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "sub").mkdir(parents=True)
    (tmp / "a.txt").write_text("foo bar foo\nbaz foo\n")
    (tmp / "sub" / "b.txt").write_text("just foo here\n")
    (tmp / "skipme.bin").write_bytes(b"\xff\xfe\x00foo")
    (tmp / ".git").mkdir()
    (tmp / ".git" / "c.txt").write_text("foo in git\n")
    hits = fr_scan(tmp, "foo")
    chk("fr finds", hits and dict(hits)[str(tmp / "a.txt")] == 3
        and dict(hits)[str(tmp / "sub" / "b.txt")] == 1)
    chk("fr skips git", all(".git" not in p for p, _ in hits))
    chk("fr skips binary", all("bin" not in p for p, _ in hits))
    chk("fr glob", fr_scan(tmp, "foo", "*.txt") and
        not fr_scan(tmp, "foo", "*.md"))
    chk("fr regex", dict(fr_scan(tmp, "f.o", "*", True))[str(tmp / "a.txt")] == 3)
    chk("fr badregex", fr_scan(tmp, "[", "*", True) is None)
    files, total = fr_apply([str(tmp / "a.txt"), str(tmp / "sub" / "b.txt")],
                            "foo", "quux")
    chk("fr apply counts", files == 2 and total == 4)
    chk("fr applied", "quux" in (tmp / "a.txt").read_text())
    chk("fr backup", (tmp / "a.txt.bak-fr").exists()
        and "foo" in (tmp / "a.txt.bak-fr").read_text())
    shutil.rmtree(tmp)

    if EVENTS_FILE.exists():
        backup_txt = EVENTS_FILE.read_text()
    else:
        backup_txt = None
    dt = datetime(2030, 1, 2, 3, 4)
    append_event(dt, "test alpha")
    evs = load_events()
    chk("event roundtrip", (dt, "test alpha") in evs)
    remove_event(dt, "test alpha")
    chk("event removed", (dt, "test alpha") not in load_events())
    if backup_txt is None:
        if not EVENTS_FILE.exists():
            pass
        elif EVENTS_FILE.read_text() == "":
            EVENTS_FILE.unlink()
    else:
        EVENTS_FILE.write_text(backup_txt)

    chk("toml loads", CONFIG_PATH.exists())

    app = App()
    for opener, name in ((app.event_dialog, "event"), (app.fr_dialog, "fr"),
                         (app.help_dialog, "help")):
        app.close_modal()
        opener()
        try:
            app.loop.widget.render((120, 40), True)
            chk(f"dialog renders {name}", True)
        except Exception as e:
            print(f"  dialog {name}: {type(e).__name__}: {e}")
            chk(f"dialog renders {name}", False)
    app.close_modal()

    av = build_term_argv("guts", "guts (inxi)")
    chk("term argv shape", av[0] == "alacritty" and av[1] == "--title"
        and av[3:5] == ["-e", "bash"] and av[5] == "-c")
    chk("term title sanitized", av[2] == "george:guts_inxi_")
    chk("term holds", "read -r _" in av[6] and "rc=$?" in av[6]
        and "guts" in av[6])

    print(f"selftest: {len(fails)} failed")
    for f in fails:
        print(f"  FAIL: {f}")
    return 1 if fails else 0


_GEORGE_STDIN_KEEPALIVE = None


def install_key_logger():
    if os.environ.get("GEORGE_KEYS", "1") == "0":
        return
    klog = Path("~/.local/state").expanduser() / "george-keys.log"
    klog.parent.mkdir(parents=True, exist_ok=True)
    kf = klog.open("a")

    def stamp(msg):
        kf.write(f"{datetime.now():%H:%M:%S.%f} {msg}\n")
        kf.flush()

    orig_pi = urwid.MainLoop.process_input

    def pi(self, keys):
        stamp(f"IN {keys!r}")
        return orig_pi(self, keys)

    urwid.MainLoop.process_input = pi

    try:
        _Screen = urwid.raw_display.Screen
        _orig_parse = _Screen.parse_input

        def parse_input(scr, loop, callback, raw_codes, *a, **k):
            stamp(f"RAW {bytes(raw_codes)!r}")
            return _orig_parse(scr, loop, callback, raw_codes, *a, **k)

        _Screen.parse_input = parse_input
    except Exception:
        pass
    orig_kp = Click.keypress

    def kp(selfw, size, key):
        r = orig_kp(selfw, size, key)
        if r is not key:
            lbl = str(getattr(selfw._t, "text", "?"))[:24]
            stamp(f"FIRE {key!r} -> {lbl!r}")
        return r

    Click.keypress = kp

    import traceback
    from io import StringIO

    orig_hk = App.hotkey

    def hk(selfw, keys, *a, **k):
        stamp(f"HOTKEY-IN {keys!r}")
        return orig_hk(selfw, keys, *a, **k)

    App.hotkey = hk

    orig_dn = App.do_nag

    def dn(selfw, *a, **k):
        sio = StringIO()
        traceback.print_stack(file=sio)
        stamp(f"DONAG CALLER:\n{sio.getvalue()}")
        return orig_dn(selfw, *a, **k)

    App.do_nag = dn

    orig_sp = App.spawn

    def sp(selfw, spec, label=None):
        sio = StringIO()
        traceback.print_stack(file=sio)
        stamp(f"SPAWN {spec!r} {label!r}\n{sio.getvalue()}")
        return orig_sp(selfw, spec, label)

    App.spawn = sp

    if os.environ.get("GEORGE_TRACE") == "1":
        import inspect

        def wrap_cls(cls):
            if "keypress" not in vars(cls):
                return
            origk = cls.keypress

            def tk(selfw, size, key):
                r = origk(selfw, size, key)
                if isinstance(key, str) and key[0].islower() and \
                        str(r) != key and isinstance(r, str):
                    stamp(f"XFORM {cls.__name__} "
                          f"{str(selfw)[:30]!r} {key!r}->{r!r}")
                return r

            cls.keypress = tk

        for modname in ("urwid", "george"):
            mod = sys.modules.get(modname)
            if not mod:
                continue
            for name, obj in vars(mod).items():
                if inspect.isclass(obj) and hasattr(obj, "keypress"):
                    try:
                        wrap_cls(obj)
                    except Exception:
                        pass

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(selftest())
    install_key_logger()
    try:
        if not sys.stdin.isatty():
            r, w = os.pipe()
            globals()["_GEORGE_STDIN_KEEPALIVE"] = w
            os.dup2(r, 0)
            os.close(r)
            sys.stdin = open(0, encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    try:
        App().run()
    except BaseException:
        import traceback
        LOGDIR = Path("~/.local/state").expanduser()
        LOGDIR.mkdir(parents=True, exist_ok=True)
        with (LOGDIR / "george-crash.log").open("a") as f:
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
            traceback.print_exc(file=f)
        raise
