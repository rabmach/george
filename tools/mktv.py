#!/usr/bin/env python3
"""Regenerate the CH 57 playlist from archive.org.

Fetches the file list of the Leave It To Beaver complete-series item,
builds direct streaming URLs for every episode MP4, writes an m3u.
Usage: python3 tools/mktv.py [outfile]  (default tv/beaver.m3u)
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ITEM = "leave-it-to-beaver-the-complete-series-1957-1963"
METADATA = f"https://archive.org/metadata/{ITEM}"
BASE = f"https://archive.org/download/{ITEM}/"


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else
               Path(__file__).resolve().parent.parent / "tv" / "beaver.m3u")
    req = urllib.request.Request(METADATA,
                                 headers={"User-Agent": "george-mktv"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    eps = sorted(
        (f["name"] for f in data.get("files", [])
         if f.get("name", "").endswith(".mp4")
         and not f["name"].endswith(".ia.mp4")
         and "/Season" in f["name"]),
        key=str.lower)
    if not eps:
        sys.exit("no episode mp4s found - item layout changed?")
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for name in eps:
        label = name.rsplit("/", 1)[-1].removesuffix(".mp4")
        url = BASE + urllib.parse.quote(name)
        lines += [f"#EXTINF:-1,{label}", url]
    out.write_text("\n".join(lines) + "\n")
    print(f"{len(eps)} episodes -> {out}")


if __name__ == "__main__":
    main()
