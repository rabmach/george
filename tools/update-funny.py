#!/usr/bin/env python3
"""Regenerate the CH 59 (Funny Channel) playlist from archive.org.

Curated pool of public-domain silent comedy, early animation and
unintentionally-hilarious industrial/educational films (Prelinger et al.).
Each entry is verified by hand; the script only re-fetches the file lists.

Single items pick their best playable file (skips archive.org's derived
.ia.mp4 copies, low-bitrate _512kb/_256kb previews and .ogv dupes; when an
item holds two encodes of the same film it keeps the first/largest).
Multi items (e.g. the Laurel & Hardy silent collection = 12 shorts) include
every episode, sorted.

Usage: python3 tools/update-funny.py [outdir]  (default: the repo's tv/)
Writes:  funny.tsv  TITLE<TAB>URL      (source of truth)
         funny.m3u  EXTINF + URL lines (what mpv --playlist eats)
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# identifier: (channel label, mode)  mode: "single" | "multi"
ITEMS = {
    # ---- silent comedy -------------------------------------------------
    "TheRink1916CharlieChaplinSkating.WithEdnaPurvianceEricCampbell":
        ("The Rink (1916) Chaplin", "single"),
    "charliechaplintheimmigrant1917hd_201908":
        ("The Immigrant (1917) Chaplin", "single"),
    "easy-street-1917-directed-by-charlie-chaplin":
        ("Easy Street (1917) Chaplin", "single"),
    "cops-buster-keaton":
        ("Cops (1922) Keaton", "single"),
    "buster-keaton-the-scarecrow-1920":
        ("The Scarecrow (1920) Keaton", "single"),
    "one-week-buster-keaton":
        ("One Week (1920) Keaton", "single"),
    "HAUNTEDSPOOKSHaroldLloydSilentAHalRoachComedy":
        ("Haunted Spooks (1920) Lloyd", "single"),
    "safety-last-1923-silent-film-noir-comedy-short":
        ("Safety Last (1923, segment) Lloyd", "single"),
    "01-should-married-men-go-home":
        ("Laurel & Hardy Silent Shorts", "multi"),
    # ---- early animation ----------------------------------------------
    "dizzy-dishes-betty-boop-cartoon":
        ("Dizzy Dishes (1930) Fleischer", "single"),
    "BettyBoopInCrazyTown":
        ("Betty Boop in Crazy Town (1931)", "single"),
    "SnowWhiteWithBettyBoop1933":
        ("Snow White w/ Betty Boop (1933) Cab Calloway", "single"),
    # ---- oddball docs (Prelinger & co) --------------------------------
    "duck-and-cover-1952":
        ("Duck and Cover (1952)", "single"),
    "StoryOfMenstruation1946":
        ("The Story of Menstruation (1946) Disney", "single"),
    "DateWith1950":
        ("A Date With Your Family (1950)", "single"),
    "a-word-to-the-wives...-1955":
        ("A Word to the Wives (1955)", "single"),
    "reefer-madness-1936-by-louis-j.-gasnier":
        ("Reefer Madness (1936)", "single"),
}

EXT = (".mp4", ".m4v", ".webm", ".mov")
BASE = "https://archive.org/download"
UA = "george-update-funny"


def fetch(identifier):
    url = f"https://archive.org/metadata/{identifier}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def pick_files(files, mode):
    cand = []
    for f in files:
        n = f.get("name", "")
        low = n.lower()
        if not low.endswith(EXT):
            continue
        if n.endswith(".ia.mp4"):
            continue
        if low.startswith("__") or "_512kb" in low or "_256kb" in low:
            continue
        if low.endswith("_edit.mp4"):
            continue
        cand.append(f)
    if mode == "multi":
        cand.sort(key=lambda f: f["name"].lower())
        return cand
    # single: dedup encodes of the same film by playback length,
    # keep the first (metadata order already favours the main file)
    seen = {}
    for f in cand:
        ln = f.get("length")
        key = int(float(ln)) if ln else f["name"]
        seen.setdefault(key, f)
    return list(seen.values())[:1]


def dur(f):
    ln = f.get("length")
    try:
        return int(float(ln))
    except (TypeError, ValueError):
        return 0


def main():
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else
                  Path(__file__).resolve().parent.parent / "tv")
    outdir.mkdir(parents=True, exist_ok=True)
    tsv, m3u, total_secs = [], ["#EXTM3U"], 0
    problems = []
    for identifier, (label, mode) in ITEMS.items():
        try:
            data = fetch(identifier)
        except Exception as e:
            problems.append(f"{identifier}: fetch failed ({e})")
            continue
        files = pick_files(data.get("files", []), mode)
        if not files:
            problems.append(f"{identifier}: no playable file")
            continue
        for f in files:
            name = f["name"]
            est = f"{dur(f)//60}m{dur(f)%60:02d}s" if dur(f) else "?"
            total_secs += dur(f)
            title = name.rsplit("/", 1)[-1].removesuffix(".mp4") \
                .removesuffix(".m4v").removesuffix(".webm") \
                .removesuffix(".mov")
            if mode == "single":
                title = label
            url = f"{BASE}/{identifier}/{urllib.parse.quote(name)}"
            tsv.append(f"{label}\t{title}\t{url}")
            m3u += [f"#EXTINF:-1,{title} ({est})", url]
    (outdir / "funny.tsv").write_text(
        "CH\tTITLE\tURL\n" + "\n".join(tsv) + "\n")
    (outdir / "funny.m3u").write_text("\n".join(m3u) + "\n")
    print(f"{len(tsv)} tracks -> {outdir}/funny.m3u "
          f"(runtime ~{total_secs // 3600}h{(total_secs % 3600) // 60:02d}m)")
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  {p}")
    return tsv, problems


if __name__ == "__main__":
    main()