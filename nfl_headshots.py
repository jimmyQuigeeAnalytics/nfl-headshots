#!/usr/bin/env python3
"""
nfl_headshots.py — fetch transparent-background NFL player headshot PNGs.

Resolves player names against the public nflverse roster data, then pulls the
official cutout headshot from the NFL.com Cloudinary CDN and/or ESPN's CDN.
Both serve pre-cut, transparent-background images, so the output drops straight
into a thumbnail template with a stroke/glow applied.

Examples
--------
  # basic
  python3 nfl_headshots.py "Ja'Marr Chase" "Bijan Robinson" "Jahmyr Gibbs"

  # from a text file, one name per line, upscaled and tightly cropped
  python3 nfl_headshots.py --from-file players.txt --width 1400 --trim

  # whole position group for a team
  python3 nfl_headshots.py --team BAL --position WR

  # disambiguate a shared name
  python3 nfl_headshots.py "Josh Allen, BUF" "Byron Young, LA"

  # see what would happen without downloading
  python3 nfl_headshots.py --from-file players.txt --dry-run

  # just search the roster
  python3 nfl_headshots.py --list "harrison"
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

ROSTER_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{season}.csv"
)
ESPN_HEADSHOT = "https://a.espncdn.com/i/headshots/nfl/players/full/{espn_id}.png"
SDIO_HEADSHOTS = "https://api.sportsdata.io/v3/nfl/headshots/json/Headshots?key={key}"
SDIO_CACHE_HOURS = 24
CACHE_DIR = Path(os.environ.get("NFL_HEADSHOT_CACHE", Path.home() / ".cache" / "nfl-headshots"))
CACHE_MAX_AGE_DAYS = 7
USER_AGENT = "nfl-headshots/1.0 (+personal thumbnail tooling)"

# roster statuses worth preferring when a name collides
STATUS_RANK = {"ACT": 0, "RES": 1, "DEV": 2, "CUT": 3, "RET": 4}

# fantasy-relevant positions, used by --skill
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K", "FB"}

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
TEAM_CODES = {
    "ARI", "AZ", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "JAC", "KC", "LA", "LAR", "LAC", "LV",
    "OAK", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SD", "SEA",
    "SF", "TB", "TEN", "WAS", "WSH",
}

# nflverse switches abbreviations between seasons (ARI/AZ, LA/LAR, WAS/WSH...),
# so treat each of these clusters as interchangeable when the user types one.
TEAM_ALIASES = [
    {"ARI", "AZ", "ARZ"},
    {"LA", "LAR", "STL"},
    {"LAC", "SD"},
    {"LV", "OAK"},
    {"WAS", "WSH", "WFT"},
    {"JAX", "JAC"},
]


def team_variants(code: str) -> set[str]:
    code = code.upper()
    for group in TEAM_ALIASES:
        if code in group:
            return set(group)
    return {code}
POSITIONS = {"QB", "RB", "WR", "TE", "K", "FB", "OL", "DL", "LB", "DB", "LS", "P"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def norm(name: str) -> str:
    """Normalize a player name for matching: fold accents, drop punctuation and suffixes."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)          # Ja'Marr -> jamarr, T.J. -> tj
    parts = [p for p in s.split() if p and p not in NAME_SUFFIXES]
    return " ".join(parts)


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("'", "").replace("’", "").replace(".", "")  # Ja'Marr -> jamarr
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "player"


def http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - want the message, not the class
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def nfl_url(base: str, width: int | None) -> str:
    """Rewrite an nflverse NFL.com Cloudinary URL to force PNG (alpha) at a chosen width.

    Input looks like:
      https://static.www.nfl.com/image/upload/f_auto,q_auto/league/<hash>
      https://static.www.nfl.com/image/private/f_auto,q_auto/league/<hash>
    """
    transform = "f_png,q_100"
    if width:
        transform += f",w_{width},c_limit"
    parts = base.split("/")
    for i, seg in enumerate(parts):
        if seg in ("upload", "private"):
            nxt = i + 1
            # the segment after upload/private is the transformation if it looks like one
            if nxt < len(parts) and re.match(r"^[a-z]{1,3}_[^/]*$", parts[nxt]):
                parts[nxt] = transform
            else:
                parts.insert(nxt, transform)
            return "/".join(parts)
    # unexpected shape; hand it back untouched
    return base


# --------------------------------------------------------------------------
# roster
# --------------------------------------------------------------------------

@dataclass
class Player:
    name: str
    team: str
    position: str
    status: str
    espn_id: str
    nfl_headshot: str
    jersey: str = ""
    sdio_url: str = ""
    sdio_field: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.team} {self.position})"


def current_season() -> int:
    now = datetime.now(timezone.utc)
    # NFL league year rolls in March; before then the prior season file is the fresh one
    return now.year if now.month >= 3 else now.year - 1


def load_roster(season: int | None, refresh: bool, verbose: bool = True) -> tuple[list[Player], int]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [season] if season else [current_season(), current_season() - 1]

    for s in candidates:
        path = CACHE_DIR / f"roster_{s}.csv"
        fresh = (
            path.exists()
            and not refresh
            and (time.time() - path.stat().st_mtime) < CACHE_MAX_AGE_DAYS * 86400
        )
        if not fresh:
            try:
                if verbose:
                    print(f"  fetching {s} roster from nflverse ...", file=sys.stderr)
                data = http_get(ROSTER_URL.format(season=s))
                path.write_bytes(data)
            except Exception as exc:  # noqa: BLE001
                if path.exists():
                    if verbose:
                        print(f"  download failed ({exc}); using cached {path.name}", file=sys.stderr)
                else:
                    if verbose:
                        print(f"  no roster for {s} ({exc})", file=sys.stderr)
                    continue

        players: list[Player] = []
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("full_name") or "").strip()
                if not name:
                    continue
                players.append(
                    Player(
                        name=name,
                        team=(row.get("team") or "").strip(),
                        position=(row.get("position") or "").strip(),
                        status=(row.get("status") or "").strip(),
                        espn_id=(row.get("espn_id") or "").strip(),
                        nfl_headshot=(row.get("headshot_url") or "").strip(),
                        jersey=(row.get("jersey_number") or "").strip(),
                    )
                )
        if players:
            return players, s

    raise SystemExit("Could not load any nflverse roster file. Check your network connection.")


# --------------------------------------------------------------------------
# sportsdata.io (licensed IMAGN action headshots, incl. background-removed)
# --------------------------------------------------------------------------

def _nk(key: str) -> str:
    """Normalize a JSON field name for fuzzy matching: HostedHeadshotNoBackgroundUrl -> hostedheadshotnobackgroundurl"""
    return re.sub(r"[^a-z0-9]", "", key.lower())


# preference order per variant, matched as substrings of the normalized field name
SDIO_VARIANT_PREFS = {
    "nobg": ["nobackground", "transparent", "preferred", "withbackground", "headshot", "url"],
    "preferred": ["preferred", "nobackground", "withbackground", "headshot", "url"],
    "withbg": ["withbackground", "preferred", "nobackground", "headshot", "url"],
}


def pick_sdio_url(rec: dict, variant: str) -> tuple[str, str] | None:
    """Find the best image URL in a SportsDataIO Headshot record without hardcoding field names.

    The public docs don't publish the exact schema, so rather than guess we scan every
    string field that looks like a URL and rank them by what the field is called.
    """
    urls = [
        (k, v) for k, v in rec.items()
        if isinstance(v, str) and v.lower().startswith("http")
    ]
    if not urls:
        return None
    for needle in SDIO_VARIANT_PREFS.get(variant, SDIO_VARIANT_PREFS["nobg"]):
        for k, v in urls:
            if needle in _nk(k):
                return k, v
    return urls[0]


def _rec_get(rec: dict, *needles: str) -> str:
    """Pull a field like Name/Team/Position without assuming exact casing."""
    for needle in needles:
        for k, v in rec.items():
            if _nk(k) == needle and isinstance(v, (str, int)):
                return str(v).strip()
    for needle in needles:
        for k, v in rec.items():
            if needle in _nk(k) and isinstance(v, (str, int)):
                return str(v).strip()
    return ""


def load_sdio(key: str, refresh: bool, cache_hours: int = SDIO_CACHE_HOURS,
              verbose: bool = True) -> list[dict]:
    import json

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / "sportsdata_headshots.json"
    fresh = (
        path.exists()
        and not refresh
        and (time.time() - path.stat().st_mtime) < cache_hours * 3600
    )
    if not fresh:
        try:
            if verbose:
                print("  fetching SportsDataIO headshots feed ...", file=sys.stderr)
            data = http_get(SDIO_HEADSHOTS.format(key=key))
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            if path.exists():
                if verbose:
                    print(f"  SportsDataIO fetch failed ({exc}); using cached feed", file=sys.stderr)
            else:
                if verbose:
                    print(f"  SportsDataIO fetch failed: {exc}", file=sys.stderr)
                return []
    try:
        recs = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  could not parse SportsDataIO feed: {exc}", file=sys.stderr)
        return []
    if isinstance(recs, dict):  # error payloads come back as an object
        if verbose:
            print(f"  SportsDataIO returned an object, not a list: {str(recs)[:200]}", file=sys.stderr)
        return []
    out = [r for r in recs if isinstance(r, dict)]
    if verbose and out:
        real, scrambled, _empty = _sdio_url_stats(out)
        if scrambled and not real:
            print(
                "  SportsDataIO: feed is live but every image URL is 'Scrambled' "
                "(trial key / Headshots not unlocked). Falling back to NFL.com / ESPN.",
                file=sys.stderr,
            )
    return out


def attach_sdio(players: list[Player], recs: list[dict], variant: str,
                verbose: bool = True) -> int:
    """Match feed records onto roster players by normalized name (team breaks ties)."""
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for rec in recs:
        name = _rec_get(rec, "name", "playername", "shortname")
        if not name:
            first = _rec_get(rec, "firstname")
            last = _rec_get(rec, "lastname")
            name = f"{first} {last}".strip()
        if not name:
            continue
        found = pick_sdio_url(rec, variant)
        if not found:
            continue
        field, url = found
        by_name.setdefault(norm(name), []).append(
            (_rec_get(rec, "team", "teamabbr").upper(), field, url)
        )

    hits = 0
    for p in players:
        options = by_name.get(norm(p.name))
        if not options:
            continue
        chosen = None
        if len(options) > 1:
            want = team_variants(p.team)
            chosen = next((o for o in options if o[0] in want), None)
        chosen = chosen or options[0]
        p.sdio_url, p.sdio_field = chosen[2], chosen[1]
        hits += 1

    if verbose:
        print(f"  SportsDataIO: {len(recs)} records, {hits} matched to roster "
              f"(field: {next((p.sdio_field for p in players if p.sdio_field), 'n/a')})",
              file=sys.stderr)
    return hits


def _sdio_url_stats(recs: list[dict]) -> tuple[int, int, int]:
    """Count records with real http URLs vs SportsDataIO 'Scrambled' trial placeholders."""
    real = scrambled = empty = 0
    for rec in recs:
        vals = [v for v in rec.values() if isinstance(v, str)]
        if any(v.lower().startswith("http") for v in vals):
            real += 1
        elif any(v == "Scrambled" for v in vals):
            scrambled += 1
        else:
            empty += 1
    return real, scrambled, empty


def sdio_inspect(recs: list[dict], variant: str) -> int:
    import json
    if not recs:
        print("Feed returned no records. If it's preseason, the IMAGN converted "
              "headshots may not be published yet — the docs say they land shortly "
              "after the season starts.")
        return 1
    real, scrambled, empty = _sdio_url_stats(recs)
    print(f"{len(recs)} records.  "
          f"(real image URLs: {real}, Scrambled: {scrambled}, no URL fields: {empty})\n")
    if scrambled and not real:
        print("WARNING: every image URL in this feed is the literal string 'Scrambled'.")
        print("That is SportsDataIO trial/sample-key behavior — the schema is real,")
        print("but image URLs stay locked until the Headshots product is on a paid plan.")
        print("Until then, --source auto will fall through to NFL.com / ESPN.\n")
    print("Fields on the first record:")
    for k, v in recs[0].items():
        kind = type(v).__name__
        preview = str(v)
        if len(preview) > 78:
            preview = preview[:75] + "..."
        marker = "  <-- URL" if isinstance(v, str) and v.lower().startswith("http") else ""
        print(f"  {k:<42} {kind:<6} {preview}{marker}")
    found = pick_sdio_url(recs[0], variant)
    print(f"\nvariant '{variant}' would use field: {found[0] if found else 'NONE FOUND'}")
    print("\nFull first record:")
    print(json.dumps(recs[0], indent=2)[:2000])
    return 0 if real else 2


def build_index(players: list[Player]) -> dict[str, list[Player]]:
    idx: dict[str, list[Player]] = {}
    for p in players:
        idx.setdefault(norm(p.name), []).append(p)
    for group in idx.values():
        group.sort(key=lambda p: (STATUS_RANK.get(p.status, 9), not bool(p.nfl_headshot)))
    return idx


# --------------------------------------------------------------------------
# name resolution
# --------------------------------------------------------------------------

@dataclass
class Query:
    raw: str
    name: str
    team: str | None = None
    position: str | None = None


def parse_query(raw: str) -> Query:
    """Accept 'Josh Allen', 'Josh Allen, BUF', 'Josh Allen QB', 'Josh Allen (BUF QB)'."""
    s = raw.strip()
    team = pos = None
    s = re.sub(r"[()]", " ", s)
    if "," in s:
        head, tail = s.split(",", 1)
        for tok in tail.replace("/", " ").split():
            t = tok.strip().upper()
            if t in TEAM_CODES:
                team = t
            elif t in POSITIONS:
                pos = t
        s = head
    tokens = s.split()
    while tokens:
        t = tokens[-1].upper()
        if t in TEAM_CODES and team is None:
            team = t
            tokens.pop()
        elif t in POSITIONS and pos is None:
            pos = t
            tokens.pop()
        else:
            break
    return Query(raw=raw.strip(), name=" ".join(tokens).strip(), team=team, position=pos)


@dataclass
class Resolution:
    query: Query
    player: Player | None = None
    reason: str = ""
    candidates: list[Player] = field(default_factory=list)


def resolve(q: Query, index: dict[str, list[Player]], all_names: list[str]) -> Resolution:
    key = norm(q.name)
    if not key:
        return Resolution(q, None, "empty name")

    matches = list(index.get(key, []))

    # startswith fallback: "Marvin Harrison" -> "Marvin Harrison Jr."
    if not matches:
        pref = [n for n in all_names if n.startswith(key + " ") or n == key]
        for n in pref:
            matches.extend(index[n])

    # fuzzy last resort
    if not matches:
        close = difflib.get_close_matches(key, all_names, n=4, cutoff=0.82)
        for n in close:
            matches.extend(index[n])
        if matches:
            filtered = _filter(matches, q)
            if len(filtered) == 1:
                return Resolution(q, filtered[0], f"fuzzy -> {filtered[0].label}")
            if filtered:
                return Resolution(q, filtered[0], f"fuzzy -> {filtered[0].label}", filtered)
        return Resolution(q, None, "no match", [])

    filtered = _filter(matches, q)
    if not filtered:
        return Resolution(q, None, f"name found but no {q.team or ''} {q.position or ''}".strip(), matches)
    if len(filtered) == 1:
        return Resolution(q, filtered[0], "exact")

    # multiple: prefer skill positions, then status, then the one with a headshot
    filtered.sort(
        key=lambda p: (
            p.position not in SKILL_POSITIONS,
            STATUS_RANK.get(p.status, 9),
            not bool(p.nfl_headshot),
        )
    )
    if filtered[0].position in SKILL_POSITIONS and filtered[1].position not in SKILL_POSITIONS:
        return Resolution(q, filtered[0], f"auto-picked {filtered[0].label}")
    return Resolution(q, None, "ambiguous", filtered)


def _filter(players: list[Player], q: Query) -> list[Player]:
    out = players
    if q.team:
        want = team_variants(q.team)
        out = [p for p in out if p.team.upper() in want]
    if q.position:
        out = [p for p in out if p.position == q.position]
    return out


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------

@dataclass
class Result:
    query: str
    player: Player | None
    ok: bool = False
    source: str = ""
    path: str = ""
    alpha: bool | None = None
    size: tuple[int, int] | None = None
    bytes_: int = 0
    note: str = ""


def has_alpha(img: "Image.Image") -> bool:
    if img.mode in ("RGBA", "LA"):
        extrema = img.getchannel("A").getextrema()
        return extrema[0] < 255
    return img.mode == "P" and "transparency" in img.info


def post_process(data: bytes, width: int | None, trim: bool) -> tuple[bytes, bool, tuple[int, int]]:
    if Image is None:
        return data, False, (0, 0)
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode not in ("RGBA", "LA"):
        img = img.convert("RGBA")
    alpha = has_alpha(img)
    if trim and alpha:
        bbox = img.getchannel("A").getbbox()
        if bbox:
            img = img.crop(bbox)
    if width and img.width != width:
        h = max(1, round(img.height * width / img.width))
        img = img.resize((width, h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), alpha, img.size


def fetch_one(res: Resolution, args) -> Result:
    q = res.query.raw
    p = res.player
    if p is None:
        note = res.reason
        if res.candidates:
            opts = "; ".join(f"{c.name} [{c.team} {c.position}]" for c in res.candidates[:5])
            note = f"{res.reason} -> try: {opts}"
        return Result(query=q, player=None, note=note)

    order: list[tuple[str, str]] = []
    if args.source in ("auto", "sdio", "both") and p.sdio_url:
        order.append(("sportsdata", p.sdio_url))
    if args.source in ("auto", "nfl", "both") and p.nfl_headshot:
        order.append(("nfl.com", nfl_url(p.nfl_headshot, args.width)))
    if args.source in ("auto", "espn", "both") and p.espn_id:
        order.append(("espn", ESPN_HEADSHOT.format(espn_id=p.espn_id)))
    if args.source in ("espn", "nfl", "sdio"):
        want = {"espn": "espn", "nfl": "nfl.com", "sdio": "sportsdata"}[args.source]
        order = [o for o in order if o[0] == want]
    if not order:
        if args.source == "sdio":
            return Result(query=q, player=p, note="not in the SportsDataIO headshots feed")
        return Result(query=q, player=p, note="no headshot URL or espn_id in roster data")

    if args.dry_run:
        src, url = order[0]
        return Result(query=q, player=p, ok=True, source=src, path=url, note="dry-run")

    best: Result | None = None
    best_data: bytes | None = None
    errors = []
    for src, url in order:
        try:
            raw = http_get(url, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{src}: {exc}")
            continue
        try:
            data, alpha, size = post_process(raw, args.width, args.trim)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{src}: decode failed ({exc})")
            continue

        fname = f"{slug(p.name)}.png" if args.naming == "slug" else f"{p.name}.png"
        out = Path(args.out) / fname
        cand = Result(
            query=q, player=p, ok=True, source=src, path=str(out),
            alpha=alpha, size=size, bytes_=len(data),
        )
        if not alpha:
            cand.note = "opaque - no transparency in source"
        # in auto mode a transparent image wins, so keep looking if this one is opaque
        if args.source == "auto" and not alpha and len(order) > 1 and best is None:
            best, best_data = cand, data
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return cand

    if best is not None and best_data is not None:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        Path(best.path).write_bytes(best_data)
        best.note = "no transparent version found; saved opaque"
        return best

    return Result(query=q, player=p, note="; ".join(errors) or "download failed")


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download transparent NFL player headshot PNGs by name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples\n--------\n")[-1],
    )
    ap.add_argument("names", nargs="*", help='player names, e.g. "Ja\'Marr Chase" "Josh Allen, BUF"')
    ap.add_argument("--from-file", help="text file with one player name per line (# comments ok)")
    ap.add_argument("--team", help="add every player on this team (e.g. BAL)")
    ap.add_argument("--position", help="filter --team by position (e.g. WR)")
    ap.add_argument("--skill", action="store_true", help="with --team, limit to QB/RB/WR/TE/K")
    ap.add_argument("--out", default="headshots", help="output directory (default: ./headshots)")
    ap.add_argument("--width", type=int, default=1000, help="output width in px, 0 = native (default: 1000)")
    ap.add_argument("--trim", action="store_true", help="crop away transparent margins")
    ap.add_argument("--source", choices=["auto", "sdio", "nfl", "espn", "both"], default="auto",
                    help="auto = SportsDataIO (if a key is set) -> NFL.com -> ESPN (default)")
    ap.add_argument("--sdio-key", default=os.environ.get("SPORTSDATA_API_KEY"),
                    help="SportsDataIO API key (better: set SPORTSDATA_API_KEY in your env)")
    ap.add_argument("--sdio-variant", choices=["nobg", "preferred", "withbg"], default="nobg",
                    help="which SportsDataIO image to prefer (default: nobg = background removed)")
    ap.add_argument("--sdio-inspect", action="store_true",
                    help="print the feed's record count and field names, then exit")
    ap.add_argument("--naming", choices=["slug", "name"], default="slug",
                    help="output filenames: jamarr-chase.png or Ja'Marr Chase.png")
    ap.add_argument("--season", type=int, help="roster season (default: current)")
    ap.add_argument("--refresh", action="store_true", help="force re-download of roster data")
    ap.add_argument("--workers", type=int, default=8, help="parallel downloads (default: 8)")
    ap.add_argument("--timeout", type=int, default=30, help="per-request timeout seconds")
    ap.add_argument("--dry-run", action="store_true", help="resolve names and print URLs, download nothing")
    ap.add_argument("--list", metavar="TEXT", help="search the roster for TEXT and exit")
    ap.add_argument("--manifest", default="manifest.csv", help="manifest filename inside --out")
    args = ap.parse_args()

    if args.width == 0:
        args.width = None

    if args.sdio_inspect:
        if not args.sdio_key:
            ap.error("--sdio-inspect needs --sdio-key or SPORTSDATA_API_KEY")
        return sdio_inspect(load_sdio(args.sdio_key, args.refresh), args.sdio_variant)

    print("Loading nflverse roster ...", file=sys.stderr)
    players, season = load_roster(args.season, args.refresh)
    print(f"  {len(players)} players ({season} season)", file=sys.stderr)

    if args.sdio_key and args.source in ("auto", "sdio", "both"):
        recs = load_sdio(args.sdio_key, args.refresh)
        if recs:
            attach_sdio(players, recs, args.sdio_variant)
        elif args.source == "sdio":
            print("  SportsDataIO feed is empty or unreachable; nothing to download.",
                  file=sys.stderr)
    elif args.source == "sdio":
        ap.error("--source sdio needs --sdio-key or SPORTSDATA_API_KEY")

    index = build_index(players)
    all_names = sorted(index.keys())
    print(file=sys.stderr)

    if args.list:
        needle = norm(args.list)
        hits = [p for p in players if needle in norm(p.name)]
        hits.sort(key=lambda p: (STATUS_RANK.get(p.status, 9), p.name))
        for p in hits[:60]:
            mark = "" if p.nfl_headshot else "  (no headshot)"
            print(f"{p.name:<28} {p.team:<4} {p.position:<3} {p.status}{mark}")
        print(f"\n{len(hits)} match(es).")
        return 0

    # assemble the request list
    requested: list[str] = list(args.names)
    if args.from_file:
        for line in Path(args.from_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                requested.append(line)
    if args.team:
        want = team_variants(args.team)
        pool = [p for p in players if p.team.upper() in want]
        if args.position:
            pool = [p for p in pool if p.position == args.position.upper()]
        elif args.skill:
            pool = [p for p in pool if p.position in SKILL_POSITIONS]
        pool.sort(key=lambda p: (STATUS_RANK.get(p.status, 9), p.position, p.name))
        requested.extend(f"{p.name}, {p.team} {p.position}" for p in pool)

    if not requested:
        ap.error("give at least one name, --from-file, --team, or --list")

    # dedupe, preserve order
    seen, ordered = set(), []
    for r in requested:
        if r.lower() not in seen:
            seen.add(r.lower())
            ordered.append(r)

    resolutions = [resolve(parse_query(r), index, all_names) for r in ordered]

    if not args.dry_run:
        Path(args.out).mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda r: fetch_one(r, args), resolutions))

    # report
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    width = max((len(r.query) for r in results), default=10)

    print()
    for r in results:
        if r.ok and args.dry_run:
            print(f"  {r.query:<{width}}  {r.player.label:<32} {r.source:<8} {r.path}")
        elif r.ok:
            dims = f"{r.size[0]}x{r.size[1]}" if r.size else "?"
            flag = "alpha" if r.alpha else "OPAQUE"
            extra = f"  [{r.note}]" if r.note else ""
            print(f"  ok    {r.query:<{width}}  {r.player.label:<32} {r.source:<8} {dims:<10} {flag}{extra}")
        else:
            who = r.player.label if r.player else "-"
            print(f"  FAIL  {r.query:<{width}}  {who:<32} {r.note}")

    if not args.dry_run and ok:
        man = Path(args.out) / args.manifest
        with man.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["query", "player", "team", "position", "source", "file", "width", "height", "transparent"])
            for r in ok:
                w.writerow([
                    r.query, r.player.name, r.player.team, r.player.position, r.source,
                    Path(r.path).name,
                    r.size[0] if r.size else "", r.size[1] if r.size else "",
                    "yes" if r.alpha else "no",
                ])
        print(f"\nManifest: {man}")

    print(f"\n{len(ok)} downloaded, {len(bad)} failed -> {args.out}")
    if bad:
        print("Tip: disambiguate with a team or position, e.g. \"Josh Allen, BUF\", "
              "or run --list <partial name> to find the exact spelling.")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
