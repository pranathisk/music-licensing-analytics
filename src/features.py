"""Module A: extract audio features with librosa and store them in `audio_features`.

    python -m src.features

Each track is decoded to mono at 22,050 Hz and summarised by four numbers:

  tempo_bpm           librosa.beat.beat_track global tempo estimate
  energy_rms          mean frame RMS amplitude (depends on the track's mastering loudness)
  spectral_centroid   mean spectral centroid in Hz ("brightness")
  zero_crossing_rate  mean fraction of sign changes per frame (noisiness/percussiveness)

`is_instrumental` and `mood_tags` are copied from data/catalog_seed.csv. They are
rule-derived from the uploader's ccMixter tags, not computed from audio.
"""
from __future__ import annotations

import csv

import librosa
import numpy as np

from src.db import ROOT, connect, resolve

SR = 22050
CATALOG_CSV = ROOT / "data" / "catalog_seed.csv"


def extract_features(y: np.ndarray, sr: int = SR) -> dict[str, float]:
    """Return the four numeric features for a mono signal."""
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    return {
        "tempo_bpm": float(np.atleast_1d(tempo)[0]),
        "energy_rms": float(librosa.feature.rms(y=y).mean()),
        "spectral_centroid": float(librosa.feature.spectral_centroid(y=y, sr=sr).mean()),
        "zero_crossing_rate": float(librosa.feature.zero_crossing_rate(y).mean()),
    }


def main() -> None:
    with open(CATALOG_CSV) as fh:
        tags = {row["track_id"]: row for row in csv.DictReader(fh)}

    conn = connect()
    tracks = conn.execute("SELECT track_id, title, file_path FROM tracks ORDER BY track_id").fetchall()
    conn.execute("DELETE FROM audio_features")
    for i, t in enumerate(tracks, 1):
        y, sr = librosa.load(resolve(t["file_path"]), sr=SR, mono=True)
        feats = extract_features(y, sr)
        seed = tags[t["track_id"]]
        conn.execute(
            "INSERT INTO audio_features (track_id, tempo_bpm, energy_rms, spectral_centroid,"
            " zero_crossing_rate, is_instrumental, mood_tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (t["track_id"], feats["tempo_bpm"], feats["energy_rms"], feats["spectral_centroid"],
             feats["zero_crossing_rate"], int(seed["is_instrumental"]), seed["mood_tags"]),
        )
        print(f"[{i}/{len(tracks)}] {t['track_id']} {t['title'][:40]:40s} "
              f"tempo={feats['tempo_bpm']:.1f} rms={feats['energy_rms']:.4f}")
    conn.commit()


if __name__ == "__main__":
    main()
