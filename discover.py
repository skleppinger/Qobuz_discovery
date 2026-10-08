#!/usr/bin/env python3
"""Build a weekly discovery playlist CSV from Last.fm listening history.

Seeds are your recent top tracks and loved tracks. Candidates come from
Last.fm's track.getSimilar, minus anything you've already scrobbled or been
recommended before. The output CSV (Title, Artist, Album) is meant for
Soundiiz's "Import playlist from file", then transfer to Qobuz.

Config (env vars or a .env file next to this script):
  LASTFM_API_KEY   free key from https://www.last.fm/api/account/create
  LASTFM_USER      your Last.fm username

Standard library only.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

API_URL = "https://ws.audioscrobbler.com/2.0/"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "recommended.json"
OUT_DIR = HERE / "out"
REQUEST_GAP_S = 0.25  # Last.fm asks for <= 5 req/s


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def norm(s):
    """Loose key for matching: lowercase, drop (...)/[...] and ' - remaster' style suffixes."""
    s = s.lower()
    s = re.sub(r"[\(\[].*?[\)\]]", "", s)
    s = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def key(artist, title):
    return f"{norm(artist)}|{norm(title)}"


class LastFM:
    def __init__(self, api_key, user):
        self.api_key = api_key
        self.user = user
        self._last = 0.0

    def call(self, method, **params):
        params.update(method=method, api_key=self.api_key, format="json")
        url = API_URL + "?" + urllib.parse.urlencode(params)
        for attempt in range(5):
            wait = REQUEST_GAP_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                try:
                    data = json.load(e)
                except Exception:
                    raise
            except urllib.error.URLError:
                time.sleep(2 ** attempt)
                continue
            if "error" in data:
                if data["error"] in (8, 11, 16, 29):  # transient / rate limited
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Last.fm {method}: {data.get('message')} ({data['error']})")
            return data
        raise RuntimeError(f"Last.fm {method}: gave up after retries")

    def paged(self, method, list_key, item_key, max_items, **params):
        items, page = [], 1
        while len(items) < max_items:
            data = self.call(method, user=self.user, page=page, limit=200, **params)
            block = data[list_key]
            batch = block.get(item_key, [])
            if isinstance(batch, dict):
                batch = [batch]
            items.extend(batch)
            total_pages = int(block.get("@attr", {}).get("totalPages", 1))
            if page >= total_pages or not batch:
                break
            page += 1
        return items[:max_items]


def artist_name(t):
    a = t.get("artist", {})
    return a.get("name") or a.get("#text") or ""


def build(lfm, size, seeds_per_source, per_artist, new_artists_only, history_depth):
    print("Fetching history...", file=sys.stderr)
    heard = set()
    for t in lfm.paged("user.getTopTracks", "toptracks", "track", history_depth, period="overall"):
        heard.add(key(artist_name(t), t["name"]))
    for t in lfm.paged("user.getRecentTracks", "recenttracks", "track", history_depth):
        heard.add(key(artist_name(t), t["name"]))

    known_artists = set()
    if new_artists_only:
        for a in lfm.paged("user.getTopArtists", "topartists", "artist", history_depth, period="overall"):
            known_artists.add(norm(a["name"]))

    already = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()

    seeds = lfm.paged("user.getTopTracks", "toptracks", "track", seeds_per_source, period="1month")
    seeds += lfm.paged("user.getLovedTracks", "lovedtracks", "track", seeds_per_source)
    seen_seeds, uniq = set(), []
    for s in seeds:
        k = key(artist_name(s), s["name"])
        if k not in seen_seeds:
            seen_seeds.add(k)
            uniq.append(s)
    print(f"{len(uniq)} seeds, {len(heard)} heard tracks", file=sys.stderr)

    # Score = sum of similarity across seeds, so tracks similar to several seeds rise.
    scores = defaultdict(float)
    info = {}
    for i, s in enumerate(uniq, 1):
        print(f"  similar [{i}/{len(uniq)}] {artist_name(s)} - {s['name']}", file=sys.stderr)
        try:
            data = lfm.call("track.getSimilar", artist=artist_name(s), track=s["name"],
                            limit=30, autocorrect=1)
        except RuntimeError as e:
            print(f"    skipped: {e}", file=sys.stderr)
            continue
        sims = data.get("similartracks", {}).get("track", [])
        if isinstance(sims, dict):
            sims = [sims]
        for t in sims:
            a, title = artist_name(t), t["name"]
            k = key(a, title)
            if k in heard or k in already:
                continue
            if new_artists_only and norm(a) in known_artists:
                continue
            scores[k] += float(t.get("match", 0))
            info.setdefault(k, (title, a))

    picked, per = [], defaultdict(int)
    for k in sorted(scores, key=scores.get, reverse=True):
        title, a = info[k]
        if per[norm(a)] >= per_artist:
            continue
        per[norm(a)] += 1
        picked.append((k, title, a))
        if len(picked) >= size:
            break

    rows = []
    for k, title, a in picked:
        album = ""
        try:
            ti = lfm.call("track.getInfo", artist=a, track=title, autocorrect=1)
            album = ti.get("track", {}).get("album", {}).get("title", "")
        except RuntimeError:
            pass
        rows.append({"Title": title, "Artist": a, "Album": album})
    return rows, already | {k for k, _, _ in picked}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--size", type=int, default=30, help="tracks in the playlist (default 30)")
    p.add_argument("--seeds", type=int, default=25, help="seeds from each of top-1-month and loved (default 25)")
    p.add_argument("--per-artist", type=int, default=2, help="max tracks per artist (default 2)")
    p.add_argument("--new-artists-only", action="store_true", help="exclude artists you've already scrobbled")
    p.add_argument("--history-depth", type=int, default=2000, help="tracks of history to exclude (default 2000)")
    p.add_argument("--no-state", action="store_true", help="don't record picks (allows repeats next run)")
    p.add_argument("-o", "--output", type=Path, help="CSV path (default out/discovery-YYYY-MM-DD.csv)")
    args = p.parse_args()

    load_dotenv(HERE / ".env")
    api_key, user = os.environ.get("LASTFM_API_KEY"), os.environ.get("LASTFM_USER")
    if not api_key or not user:
        sys.exit("Set LASTFM_API_KEY and LASTFM_USER (env or .env next to this script)")

    rows, state = build(LastFM(api_key, user), args.size, args.seeds, args.per_artist,
                        args.new_artists_only, args.history_depth)
    if not rows:
        sys.exit("No recommendations found")

    out = args.output or OUT_DIR / f"discovery-{dt.date.today().isoformat()}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Title", "Artist", "Album"])
        w.writeheader()
        w.writerows(rows)
    if not args.no_state:
        STATE_FILE.write_text(json.dumps(sorted(state), indent=0))
    print(f"Wrote {len(rows)} tracks to {out}")


if __name__ == "__main__":
    main()
