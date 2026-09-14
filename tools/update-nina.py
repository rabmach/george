#!/usr/bin/env python3
"""update-nina.py - build the Random Nina Simone playlist from archive.org.

Curated set of archive.org items that hold Nina Simone music. Each item is
classified automatically:

    multi-file item        -> every *.mp3 becomes a song (per-track uploads)
    single short file      -> the track becomes a song (a lone single)
    single long file       -> the whole thing becomes an *album* entry

The album pool is what the weighted picker occasionally draws from (a whole
record, the "treat" slot). Songs are deduplicated across items by normalized
title (casefold, parenthetical suffixes stripped) so "Mood Indigo" floating
around four uploads collapses to a single entry - the remastered/studio cut
wins over plain, and plain over live.

Writes (all local):
    nina.tsv             kind<TAB>title<TAB>url   - what nina.sh reads
    nina-songs.m3u       convenience mpv playlist (songs only)
    nina-albums.m3u      convenience mpv playlist (whole-album entries)
    random-nina.js       the site picker data, written into ~/madcarters
                         (overwritten every run so the page can't drift)

Stdlib only (urllib/json). Usage:
    update-nina.py [--site PATH] [--check] [--timeout N]
"""

import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
SITE_DIR = os.path.expanduser("~/madcarters")

# candidate archive.org items (kept extendable; classified automatically)
ITEMS = [
    "pleasebringithome",                       # 175 individual songs on one item
    "nina-simone-the-great-nina-simone",       # 18-track compilation
    "nina-simone-at-town-hall-1959",
    "the-amazing-nina-simone-1959",
    "nina-simone-1965-pastel-blues",
    "AlbumDeFamilia091",                       # High Priestess of Soul 1967
    "8-dd-nina-simone",                        # At Carnegie Hall
    "nina_20210202",
    "feelingGood",
    "NinaSimone-HouseOfTheRisingSun",
    "AintGotNoIGotLifeNinaSimone",
    "nina-simone-dont-let-me-be-misunderstood-remastered",
    "NinaSimone-GoodBait",
    "nina-simone-ne-me-quitte-pas",
    "SinnermanMix14Mins",
    "sinnerman_239",
]

# nicer labels for the whole-file (album pool) items; falls back to the
# archive.org item title otherwise
ALBUM_LABELS = {
    "AlbumDeFamilia091": "High Priestess of Soul (1967)",
    "8-dd-nina-simone": "At Carnegie Hall",
    "nina-simone-at-town-hall-1959": "At Town Hall (1959)",
    "the-amazing-nina-simone-1959": "The Amazing Nina Simone (1959)",
    "nina-simone-1965-pastel-blues": "Pastel Blues (1965)",
    "i-put-a-spell-on-you_nina-simone": "I Put A Spell On You (recording)",
    "SinnermanMix14Mins": "Sinnerman - 14-minute mix",
    "nina_20210202": "Nina",
}

ALBUM_SECS = 600  # a lone file longer than this counts as a whole-album entry

LIVE_RE = re.compile(r"\blive\b", re.IGNORECASE)
REMST_RE = re.compile(r"\bremaster", re.IGNORECASE)
PAREN_RE = re.compile(r"[\(\[][^)\]]*[\)\]]")
KB_RE = re.compile(r"_\d+kb$", re.IGNORECASE)

# nicer display titles for the lone single-track uploads (their file names
# are uploader junk); falls back to the cleaned file name otherwise
SINGLE_TITLES = {
    "NinaSimone-HouseOfTheRisingSun": "House of the Rising Sun (Live at the Bitter End)",
    "feelingGood": "Feeling Good",
    "AintGotNoIGotLifeNinaSimone": "Ain't Got No, I Got Life",
    "NinaSimone-GoodBait": "Good Bait",
    "nina-simone-ne-me-quitte-pas": "Ne me quitte pas",
    "sinnerman_239": "Sinnerman",
    "nina-simone-dont-let-me-be-misunderstood-remastered": "Don't Let Me Be Misunderstood",
}


def fetch_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "RandomNina/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def download_url(item, name):
    return "https://archive.org/download/{}/{}".format(
        urllib.parse.quote(item), urllib.parse.quote(name, safe="/"))


LEAD_TRACK_RE = re.compile(r"^\s*(?:\d{2,}|\d+[.\-_:])(?:[\s.\-_:])*")
LEAD_ARTIST_RE = re.compile(r"^\s*nina\s*simone[\s\-_:/.]*", re.IGNORECASE)
PART_RE = re.compile(r"\bpart\s*[ivxi1-9]\b", re.IGNORECASE)


def norm_key(name):
    base = os.path.splitext(os.path.basename(name))[0]
    base = KB_RE.sub(" ", base)
    base = PAREN_RE.sub(" ", base)
    base = LEAD_TRACK_RE.sub("", base)     # "01. Nina Simone - Be My Husband"
    base = LEAD_ARTIST_RE.sub("", base)    # -> "Be My Husband"
    s = base.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def song_score(name):
    # lower is better: remastered studio < plain studio < live
    n = name.lower()
    score = 0
    if REMST_RE.search(n):
        score += 0
    else:
        score += 5
    if LIVE_RE.search(n):
        score += 100
    if PART_RE.search(n):
        score += 3  # prefer the complete take over a numbered "part"
    if KB_RE.search(n):
        score += 40  # low-bitrate preview copies lose ties aggressively
    return score


def clean_title(name):
    base = os.path.splitext(os.path.basename(name))[0].strip()
    base = KB_RE.sub("", base)
    base = LEAD_TRACK_RE.sub("", base)     # "01. Nina Simone - Be My Husband"
    base = LEAD_ARTIST_RE.sub("", base).strip()
    return base


def classify_item(item, timeout):
    """Return (songs, albums) for one item. songs is a list of
    (display_title, url, sort_key), albums a list of (label, url)."""
    data = fetch_json("https://archive.org/metadata/" + urllib.parse.quote(item),
                      timeout)
    files = data.get("files", [])
    mp3s = []
    for f in files:
        name = f.get("name", "")
        if name.lower().endswith((".mp3", ".m4a")):
            mp3s.append(f)
    if not mp3s:
        return [], []

    songs, albums = [], []
    if len(mp3s) > 1:
        for f in mp3s:
            name = f["name"]
            title = clean_title(name)
            songs.append((title, download_url(item, name),
                          song_score(name), norm_key(title)))
    else:
        name = mp3s[0]["name"]
        length = 0.0
        try:
            length = float(mp3s[0].get("length", 0) or 0)
        except (TypeError, ValueError):
            pass
        if length > ALBUM_SECS:
            label = ALBUM_LABELS.get(item) or (data.get("metadata", {}) or {}).get("title", item)
            albums.append((label, download_url(item, name)))
        else:
            title = SINGLE_TITLES.get(item) or clean_title(name)
            songs.append((title, download_url(item, name),
                          song_score(name), norm_key(title)))
    return songs, albums


def choose_song(members):
    """From [(title,url,score)], return the best (lowest-score) version."""
    members = sorted(members, key=lambda m: (m[2], m[0].lower()))
    return members[0][0], members[0][1]


def build():
    timeout = 30
    args = sys.argv[1:]
    check = False
    site_dir = SITE_DIR
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--site" and i + 1 < len(args):
            site_dir, i = args[i + 1], i + 2
        elif a == "--check":
            check, i = True, i + 1
        elif a == "--timeout" and i + 1 < len(args):
            timeout, i = int(args[i + 1]), i + 2
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print("update-nina: unknown arg: %s" % a, file=sys.stderr)
            return 2

    song_by_key = {}
    album_pool = []
    failures = []
    print("fetching %d archive.org items..." % len(ITEMS))
    for item in ITEMS:
        try:
            songs, albums = classify_item(item, timeout)
        except Exception as e:
            failures.append((item, str(e)))
            print("  !! %-46s %s" % (item, e))
            continue
        for title, url, score, key in songs:
            song_by_key.setdefault(key, []).append((title, url, score))
        album_pool.extend(albums)
        print("  %-46s %3d songs, %2d albums" % (item, len(songs), len(albums)))
    if failures:
        print("WARNING: %d item(s) unreachable (playlist may be thin): %s"
              % (len(failures), ", ".join(x[0] for x in failures)))
        if check:
            return 1

    songs = sorted(
        (choose_song(v) for v in song_by_key.values()),
        key=lambda t: t[0].lower())
    albums = sorted(album_pool, key=lambda a: a[0].lower())

    if not songs and not albums:
        print("update-nina: no music found - did archive.org change?", file=sys.stderr)
        return 1
    if check:
        print("update-nina: %d unique songs, %d album entries" % (len(songs), len(albums)))
        return 0

    print("\nresult: %d unique songs, %d album entries" % (len(songs), len(albums)))

    # nina.tsv - the file nina.sh actually reads
    tsv_path = os.path.join(HERE, "nina.tsv")
    with open(tsv_path, "w", encoding="utf-8") as fh:
        for title, url in songs:
            fh.write("S\t%s\t%s\n" % (sanitize(title), url))
        for label, url in albums:
            fh.write("A\t%s\t%s\n" % (sanitize(label + " - full album"), url))
    print("  wrote %s (%d lines)" % (tsv_path, len(songs) + len(albums)))

    # convenience m3u playlists (songs only / albums only)
    for out_name, pool, tag in (
            ("nina-songs.m3u", songs, "Nina Simone"),
            ("nina-albums.m3u", albums, "Nina Simone (album)")):
        path = os.path.join(HERE, out_name)
        lines = ["#EXTM3U"]
        for title, url in pool:
            lines.append("#EXTINF:-1,%s - %s" % (tag, title))
            lines.append(url)
        write_atomic(path, "\n".join(lines).rstrip("\n") + "\n")
        print("  wrote %s" % path)

    # the site picker data (overwrites -> page can't drift out of sync)
    if site_dir:
        js_path = os.path.join(site_dir, "random-nina.js")
        rows = []
        for title, url in songs:
            rows.append("  [%s,%s,0]," % (jq(title), jq(url)))
        for label, url in albums:
            rows.append("  [%s,%s,1]," % (jq(label + " - full album"), jq(url)))
        js = ("// generated by update-nina.py - do not hand-edit, run\n"
              "//   ~/nina/update-nina.py\n"
              "// entries: [title, url, is_album]\n"
              "var NINA = [\n%s\n];\n" % "\n".join(rows))
        write_atomic(js_path, js)
        print("  wrote %s (%d entries)" % (js_path, len(rows)))
    return 0


def sanitize(s):
    return s.replace("\t", " ").replace("\n", " ").strip()


def jq(s):
    return json.dumps(s).replace("<", "\\u003c")


def write_atomic(path, text, prefix=""):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=(prefix or "." + os.path.basename(path)) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    sys.exit(build())