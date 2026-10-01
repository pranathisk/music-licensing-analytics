"""Create the schema and load tracks + licenses.

    python -m db.seed

Drops and recreates every table, so run it first. Loads data/catalog_seed.csv
into `tracks` and data/licenses_seed.csv into `licenses`. Duration is measured by
decoding the audio, because some MP3 headers report the wrong length.
"""
from __future__ import annotations

import csv

import librosa

from src.db import ROOT, connect, init_schema, resolve

CATALOG_CSV = ROOT / "data" / "catalog_seed.csv"
LICENSES_CSV = ROOT / "data" / "licenses_seed.csv"


def main() -> None:
    conn = connect()
    init_schema(conn)

    with open(CATALOG_CSV) as fh:
        tracks = list(csv.DictReader(fh))
    for t in tracks:
        y, sr = librosa.load(resolve(t["file_path"]), sr=8000, mono=True)
        duration = len(y) / sr
        conn.execute(
            "INSERT INTO tracks (track_id, title, artist, source_url, license_type, license_url,"
            " file_path, duration_sec) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t["track_id"], t["title"], t["artist"], t["source_url"], t["license_type"],
             t["license_url"], t["file_path"], round(duration, 3)),
        )

    with open(LICENSES_CSV) as fh:
        licenses = list(csv.DictReader(fh))
    for lic in licenses:
        conn.execute(
            "INSERT INTO licenses (license_id, track_id, licensee, scope, start_date, end_date, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(lic["license_id"]), lic["track_id"], lic["licensee"], lic["scope"],
             lic["start_date"] or None, lic["end_date"] or None, lic["status"]),
        )

    conn.commit()
    print(f"Loaded {len(tracks)} tracks and {len(licenses)} licenses into the database")


if __name__ == "__main__":
    main()
