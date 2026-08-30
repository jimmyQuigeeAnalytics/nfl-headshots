# nfl_headshots.py

Pulls transparent-background NFL player headshot PNGs by name — the cutout assets
that go under a white stroke in a thumbnail template.

## Setup

```bash
pip install pillow          # only dependency
python3 nfl_headshots.py --help
```

Python 3.9+. No API key, no account, nothing to sign up for.

## How it works

1. Downloads the public **nflverse** roster file (~3,000 players) and caches it in
   `~/.cache/nfl-headshots/` for 7 days. This is the name-matching backbone.
2. Matches your typed names against it — handles missing suffixes, apostrophes,
   accents, and misspellings.
3. Pulls each player's image, in this order:
   - **SportsDataIO** background-removed IMAGN action headshots, if you set a key
   - **NFL.com CDN**, with the URL rewritten to force `f_png` so transparency survives
   - **ESPN CDN** as a fallback if the NFL version comes back opaque
4. Verifies the alpha channel on every file and tells you which ones actually
   came back transparent.

## SportsDataIO (use this — it's the good one)

Your account's Player Headshots feed is **licensed IMAGN action photography cropped
to headshot dimensions, with a background-removed version**. That's a materially
better asset than the free CDNs: in-uniform action crops rather than flat studio
portraits, and licensed rather than fair-use. It's the look the bigger fantasy
channels use.

Set the key once in your shell profile:

```bash
export SPORTSDATA_API_KEY=your_key_here
```

Then it's used automatically:

```bash
python3 nfl_headshots.py --from-file players.txt --trim --width 1400
```

**First thing to run** — confirm the schema and that the feed is populated:

```bash
python3 nfl_headshots.py --sdio-inspect
```

That prints the record count, every field on the first record, which fields are
URLs, and which one the current variant would use. I couldn't call the API while
building this (the sandbox blocks it), so rather than hardcode field names from
guesswork, the script **discovers the URL fields at runtime** by scanning for
whichever ones look like image URLs and ranking them by name. It handles
`HostedHeadshotNoBackgroundUrl` / `PreferredHostedHeadshotUrl` /
`HostedHeadshotWithBackgroundUrl` and anything similarly named.

Pick which image you want with `--sdio-variant`:

| Variant | Gets |
|---|---|
| `nobg` (default) | background removed — what you want for thumbnails |
| `withbg` | original action photo, background intact |
| `preferred` | whatever SportsDataIO flags as preferred |

`--source sdio` restricts to the licensed feed only and fails loudly on players
that aren't in it, instead of quietly falling back to a free CDN. Worth using when
you care that every asset is licensed.

**Trial-key caveat:** if `--sdio-inspect` shows every URL as the literal string
`Scrambled`, the key works but Headshots image URLs are locked (SportsDataIO
trial / sample behavior). Schema is real; downloads aren't until that product is
on a paid plan. `--source auto` falls through to NFL.com / ESPN in that case.

**Timing caveat:** SportsDataIO's docs say the converted IMAGN headshots are
"delivered shortly after the season starts." The feed may also be empty or
partial early in the year — `--sdio-inspect` will tell you immediately.

The feed is cached for 24 hours; `--refresh` forces a re-pull.

## Usage

```bash
# a few players
python3 nfl_headshots.py "Ja'Marr Chase" "Bijan Robinson" "Jahmyr Gibbs"

# a list, upscaled and tightly cropped (what you want for thumbnails)
python3 nfl_headshots.py --from-file players.txt --width 1400 --trim --out ./headshots

# a whole team's skill players
python3 nfl_headshots.py --team DET --skill

# one position group
python3 nfl_headshots.py --team BAL --position WR

# find the exact spelling / check who exists
python3 nfl_headshots.py --list "harrison"

# resolve names and print URLs without downloading
python3 nfl_headshots.py --from-file players.txt --dry-run
```

`players.txt` is one name per line; `#` starts a comment.

## Flags worth knowing

| Flag | Does |
|---|---|
| `--trim` | Crops the transparent margin so the player fills the frame. Use this — the raw files have a lot of empty space. |
| `--width N` | Output width in px (default 1000). `--width 0` keeps native size. |
| `--source` | `auto` (default: SportsDataIO → NFL.com → ESPN), or force `sdio` / `nfl` / `espn`. |
| `--sdio-variant` | `nobg` (default) / `withbg` / `preferred`. |
| `--sdio-inspect` | Dump the SportsDataIO feed's schema and record count, then exit. |
| `--naming` | `slug` → `jamarr-chase.png` (default), or `name` → `Ja'Marr Chase.png`. |
| `--refresh` | Force a fresh roster pull (do this after roster moves or the draft). |
| `--season` | Pin a season, e.g. `--season 2025`. |

## Duplicate names

Two players share a name? Add a team or position and it resolves:

```bash
python3 nfl_headshots.py "Josh Allen, BUF" "Byron Young, LA" "Jordan Phillips DL"
```

Old and new abbreviations both work (`ARI`/`AZ`, `LAR`/`LA`, `WAS`/`WSH`, `JAX`/`JAC`).
If it still can't decide, it fails that one name and prints the candidates instead
of guessing.

## Output

Files land in `--out` (default `./headshots`) plus a `manifest.csv` listing every
player, source, dimensions, and whether the file is actually transparent. The
console summary flags anything that came back `OPAQUE` so you know it needs a
manual background removal before it goes in a template.

## Caveats

- **Coverage isn't 100%.** Roughly 100 of ~3,000 players have no NFL.com headshot —
  usually camp bodies and undrafted rookies. Starters and anyone fantasy-relevant
  are covered. Rookie headshots appear a few weeks after the draft.
- **Licensing differs by source.** SportsDataIO images are licensed through your
  subscription — check your plan's terms for commercial/thumbnail use. The NFL.com
  and ESPN CDN images are official publicity assets used under a fair-use posture,
  which is standard practice for commentary channels but is not a license.
- **Free-CDN images are studio portraits**, shoulders-up with the helmet off.
  In-game action photography is the SportsDataIO feed's advantage.

## Protect your API key

Never commit the key or paste it into a shared file — use the
`SPORTSDATA_API_KEY` environment variable, which is why `--sdio-key` is only a
fallback. The script never writes the key to disk; only the feed response is
cached.

**One thing to handle:** the key was fully readable in the screenshot you shared
with me. If that image goes anywhere else — a Discord, a thumbnail, a stream —
rotate it in your SportsDataIO account settings first.
