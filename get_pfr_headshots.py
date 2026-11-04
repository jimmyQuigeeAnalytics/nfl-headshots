#!/usr/bin/env python3
"""Download transparent headshots from Pro-Football-Reference player URLs.

The site blocks scripted page fetches, and the photo on a player page is a
small JPEG. The id in the URL (BigsTa00 in
.../players/B/BigsTa00.htm) is enough: the public nflverse roster maps that
id to an ESPN id, and ESPN serves the transparent PNG.

    python3 get_pfr_headshots.py \\
        https://www.pro-football-reference.com/players/B/BigsTa00.htm

Also accepts bare ids (BigsTa00) and a text file of URLs via --from-file.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.request
from pathlib import Path

ROSTER_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{season}.csv"
)
ESPN_HEADSHOT = "https://a.espncdn.com/i/headshots/nfl/players/full/{espn_id}.png"
CACHE_DIR = Path.home() / ".cache" / "nfl-headshots"
USER_AGENT = "nfl-headshots/1.0 (+personal thumbnail tooling)"
PFR_ID = re.compile(r"([A-Za-z]{6}\d{2})")


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def current_season() -> int:
    now = time.gmtime()
    return now.tm_year if now.tm_mon >= 3 else now.tm_year - 1


def parse_pfr_id(text: str) -> str:
    match = PFR_ID.search(text.strip())
    if not match:
        raise ValueError(f"no Pro-Football-Reference player id in {text!r}")
    return match.group(1)


def load_roster(season: int) -> list[dict[str, str]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"roster_{season}.csv"
    if not path.exists() or (time.time() - path.stat().st_mtime) > 7 * 86400:
        print(f"Fetching {season} roster ...", file=sys.stderr)
        path.write_bytes(http_get(ROSTER_URL.format(season=season)))
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def filename_for(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") + ".png"


def download(urls: list[str], out_dir: Path, season: int) -> int:
    roster = load_roster(season)
    by_pfr: dict[str, dict[str, str]] = {}
    for row in roster:
        pfr_id = (row.get("pfr_id") or "").strip()
        if pfr_id and pfr_id not in by_pfr:
            by_pfr[pfr_id] = row

    out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    for raw in urls:
        try:
            pfr_id = parse_pfr_id(raw)
        except ValueError as exc:
            print(f"FAIL  {raw}  {exc}")
            failed += 1
            continue

        row = by_pfr.get(pfr_id)
        if row is None:
            print(f"FAIL  {pfr_id}  not in the {season} roster")
            failed += 1
            continue

        name = (row.get("full_name") or pfr_id).strip()
        team = (row.get("team") or "").strip()
        position = (row.get("position") or "").strip()
        espn_id = (row.get("espn_id") or "").strip()
        dest = out_dir / filename_for(name)
        label = f"{name} ({team} {position})".strip()

        if not espn_id:
            print(f"FAIL  {label}  roster row has no espn_id")
            failed += 1
            continue
        if dest.exists() and dest.stat().st_size > 0:
            print(f"skip  {label}  {dest.name} already exists")
            continue

        url = ESPN_HEADSHOT.format(espn_id=espn_id)
        try:
            data = http_get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {label}  {exc}")
            failed += 1
            continue
        if not data.startswith(b"\x89PNG"):
            print(f"FAIL  {label}  ESPN did not return a PNG for id {espn_id}")
            failed += 1
            continue
        dest.write_bytes(data)
        print(f"ok    {label}  {dest.name}  {len(data)} bytes  pfr={pfr_id}")

    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="PFR player page URLs or ids, e.g. BigsTa00")
    ap.add_argument("--from-file", help="text file with one URL or id per line")
    ap.add_argument("--out", default="nfl_headshots", help="output directory (default: ./nfl_headshots)")
    ap.add_argument("--season", type=int, default=current_season(), help="roster season")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.from_file:
        for line in Path(args.from_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                urls.append(line)
    if not urls:
        ap.error("give at least one PFR URL, id, or --from-file")

    failed = download(urls, Path(args.out), args.season)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
