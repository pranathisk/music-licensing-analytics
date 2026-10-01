"""Download the catalog from ccMixter and build data/catalog_seed.csv.

Every track comes from ccMixter (https://ccmixter.org) and is licensed CC BY or
CC0, both of which permit redistribution. The license for each track is read
from ccMixter's API response, never typed in by hand.

`is_instrumental` and `mood_tags` are derived by fixed rules from the uploader's
own ccMixter tags (see INSTRUMENTAL_* and MOOD_RULES below). No one listened to
the tracks to assign them. Edit catalog_seed.csv by hand to override any tag.
Rerunning this script overwrites those edits unless --keep-tags is passed.

Usage:  python data/fetch_catalog.py [--keep-tags]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent
RAW_DIR = DATA_DIR / "audio" / "raw"
IDS_FILE = DATA_DIR / "catalog_ids.txt"
META_FILE = DATA_DIR / "ccmixter_metadata.json"
SEED_FILE = DATA_DIR / "catalog_seed.csv"
API = "https://ccmixter.org/api/query"

# Uploader tags that settle is_instrumental one way or the other.
INSTRUMENTAL_TAGS = {"instrumental"}
VOCAL_TAGS = {"vocals", "male_vocals", "female_vocals", "spoken_word", "backing_vocals",
              "harmonies", "rap", "singer_songwriter"}

# Controlled mood vocabulary: mood -> uploader tags that imply it. These are
# genre/descriptor tags mapped by rule, not hand-listened judgements.
MOOD_RULES: dict[str, set[str]] = {
    "chill": {"chill", "downtempo", "lounge", "trip_hop", "mellow", "lo_fi", "lofi"},
    "ambient": {"ambient", "atmospheric", "drone", "ethereal", "beatless", "pads"},
    "cinematic": {"music_for_film", "orchestral", "epic", "movie", "cinematic", "music_for_video"},
    "dark": {"dark", "scary", "haunted", "angry", "bitter"},
    "melancholic": {"sad", "melancholy", "heart_broken", "contemplative", "blues", "slow_rock"},
    "romantic": {"love", "romantic", "ballad", "valentine", "waltz"},
    "upbeat": {"upbeat", "dance", "house", "surf", "surf_music", "funky", "boogie", "happy", "uplifting"},
    "driving": {"dnb", "drum_and_bass", "techno", "trance", "rock", "dubstep", "rumblestep", "jungle", "techstep"},
    "funky": {"funk", "funky", "soul", "clavinet"},
    "jazzy": {"jazz", "jazzy", "nu_jazz", "swing"},
    "acoustic": {"acoustic", "folk", "classical", "steel_string"},
    "experimental": {"experimental", "glitch", "improvisation"},
}

# Audio used only to build the simulated event recordings; NOT part of the catalog.
#   - two background-noise clips (crowd, talking) from Wikimedia Commons
#   - one non-catalog ccMixter track, mixed into a negative-control recording to
#     check that unrelated music doesn't trigger a false match
NOISE_DIR = DATA_DIR / "audio" / "noise"
EXTRAS_FILE = DATA_DIR / "noise_sources.csv"
EXTRAS = [
    {
        "name": "crowd",
        "file": "crowd_eguobyte_360703.wav",
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a9/360703_eguobyte_large-crowd-medium-distance-stereo.wav",
        "page": "https://commons.wikimedia.org/wiki/File:360703_eguobyte_large-crowd-medium-distance-stereo.wav",
        "artist": "eguobyte (via freesound.org/people/eguobyte/sounds/360703/)",
        "license": "cc0",
    },
    {
        "name": "chatter",
        "file": "cafeteria_chatter_aradlaw.ogg",
        "url": "https://upload.wikimedia.org/wikipedia/commons/d/df/High_school_cafeteria.ogg",
        "page": "https://commons.wikimedia.org/wiki/File:High_school_cafeteria.ogg",
        "artist": "aradlaw (via pdsounds.org)",
        "license": "public-domain",
    },
    {
        "name": "non_catalog_music",
        "file": "noncatalog_ccm_30239.mp3",
        "url": "https://ccmixter.org/content/AlexBeroza/AlexBeroza_-_Emerge_In_Love.mp3",
        "page": "https://ccmixter.org/files/AlexBeroza/30239",
        "artist": "Alex Beroza - Emerge In Love",
        "license": "cc-by-3.0",
    },
]

SEED_COLUMNS = ["track_id", "title", "artist", "source_url", "license_type", "license_url",
                "is_instrumental", "mood_tags", "ccmixter_tags", "declared_bpm", "file_path"]


def license_type_from_url(url: str) -> str:
    """'http://creativecommons.org/licenses/by/3.0/' -> 'cc-by-3.0'; CC0 -> 'cc0'."""
    if "publicdomain/zero" in url:
        return "cc0"
    m = re.search(r"licenses/([a-z-]+)/(\d\.\d)", url)
    if not m:
        raise ValueError(f"Unrecognised license URL: {url}")
    code, version = m.groups()
    if code != "by":
        raise ValueError(f"Catalog must be CC BY or CC0 only, got {code} ({url})")
    return f"cc-by-{version}"


def read_ids() -> list[int]:
    lines = IDS_FILE.read_text().splitlines()
    return [int(x) for x in (l.strip() for l in lines) if x and not x.startswith("#")]


def fetch_metadata(ids: list[int]) -> list[dict]:
    # ccMixter's server sends an oversized header for large multi-id queries,
    # so query one upload at a time.
    by_id = {}
    for upload_id in ids:
        resp = requests.get(API, params={"f": "json", "ids": upload_id}, timeout=60)
        resp.raise_for_status()
        by_id.update({u["upload_id"]: u for u in resp.json()})
    missing = set(ids) - set(by_id)
    if missing:
        print(f"warning: ccMixter returned nothing for {sorted(missing)}", file=sys.stderr)
    return [by_id[i] for i in ids if i in by_id]


def main_mp3(upload: dict) -> dict:
    # Prefer the file ccMixter labels "mp3" (the full mix); fall back to the
    # first mp3 of any label.
    mp3s = [f for f in upload["files"] if f["file_format_info"].get("default-ext") == "mp3"]
    for f in mp3s:
        if f["file_nicname"] == "mp3":
            return f
    if mp3s:
        return mp3s[0]
    raise ValueError(f"No main mp3 for upload {upload['upload_id']}")


def derive_tags(usertags: list[str]) -> tuple[bool, list[str]]:
    tagset = set(usertags)
    if tagset & INSTRUMENTAL_TAGS:
        instrumental = True
    elif tagset & VOCAL_TAGS or any("vocal" in t for t in tagset):
        instrumental = False
    else:
        # No explicit signal either way: conservatively assume it may have vocals.
        instrumental = False
    moods = [mood for mood, triggers in MOOD_RULES.items() if tagset & triggers]
    return instrumental, moods


def download(url: str, dest: Path, page_url: str) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    # ccMixter serves files only to requests that come from the track's page
    # (hotlink protection), the same as clicking "download" in a browser.
    headers = {"User-Agent": "Mozilla/5.0 (music-licensing-analytics catalog fetch)", "Referer": page_url}
    with requests.get(url, stream=True, timeout=120, headers=headers) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        tmp.rename(dest)
    time.sleep(0.5)  # be polite to ccMixter


def fetch_extras() -> None:
    NOISE_DIR.mkdir(parents=True, exist_ok=True)
    for extra in EXTRAS:
        print(f"extra: {extra['name']} -> {extra['file']}")
        download(extra["url"], NOISE_DIR / extra["file"], extra["page"])
    with open(EXTRAS_FILE, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(EXTRAS[0]))
        writer.writeheader()
        writer.writerows(EXTRAS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-tags", action="store_true",
                        help="keep is_instrumental/mood_tags from an existing catalog_seed.csv")
    args = parser.parse_args()

    existing: dict[str, dict] = {}
    if args.keep_tags and SEED_FILE.exists():
        with open(SEED_FILE) as fh:
            existing = {row["track_id"]: row for row in csv.DictReader(fh)}

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    uploads = fetch_metadata(read_ids())
    META_FILE.write_text(json.dumps(uploads, indent=1))

    rows = []
    for u in uploads:
        track_id = f"ccm_{u['upload_id']}"
        usertags = [t for t in u["upload_extra"].get("usertags", "").split(",") if t]
        instrumental, moods = derive_tags(usertags)
        mp3 = main_mp3(u)
        dest = RAW_DIR / f"{track_id}.mp3"
        print(f"{track_id}: {u['upload_name']} ({mp3['file_format_info'].get('ps')})")
        download(mp3["download_url"], dest, u["file_page_url"])
        row = {
            "track_id": track_id,
            "title": u["upload_name"],
            "artist": u["user_real_name"] or u["user_name"],
            "source_url": u["file_page_url"],
            "license_type": license_type_from_url(u["license_url"]),
            "license_url": u["license_url"],
            "is_instrumental": int(instrumental),
            "mood_tags": ",".join(moods),
            "ccmixter_tags": ",".join(usertags),
            # BPM the uploader typed into ccMixter, if any. Only used to sanity-check
            # librosa's tempo estimates in the EDA notebook.
            "declared_bpm": u["upload_extra"].get("bpm") or "",
            "file_path": str(dest.relative_to(DATA_DIR.parent)),
        }
        if track_id in existing:
            row["is_instrumental"] = existing[track_id]["is_instrumental"]
            row["mood_tags"] = existing[track_id]["mood_tags"]
        rows.append(row)

    with open(SEED_FILE, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SEED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} tracks to {SEED_FILE.relative_to(DATA_DIR.parent)}")
    fetch_extras()


if __name__ == "__main__":
    main()
