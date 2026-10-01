"""Feature extraction sanity checks on synthetic signals with known properties."""
import numpy as np
import librosa
import pytest

from src.features import SR, extract_features
from src.db import DB_PATH, connect


def test_tempo_of_click_track():
    # 120 BPM clicks for 30 s; beat trackers may report the octave, so accept 60/120/240.
    y = librosa.clicks(times=np.arange(0, 30, 0.5), sr=SR, length=30 * SR)
    tempo = extract_features(y)["tempo_bpm"]
    assert any(abs(tempo - t) / t < 0.03 for t in (60, 120, 240)), tempo


def test_noise_is_brighter_and_noisier_than_low_sine():
    t = np.arange(5 * SR) / SR
    sine = 0.5 * np.sin(2 * np.pi * 220 * t)
    noise = 0.5 * np.random.default_rng(0).uniform(-1, 1, len(t))
    fs, fn = extract_features(sine), extract_features(noise)
    assert fn["zero_crossing_rate"] > 10 * fs["zero_crossing_rate"]
    assert fn["spectral_centroid"] > 10 * fs["spectral_centroid"]


def test_rms_scales_with_amplitude():
    t = np.arange(5 * SR) / SR
    quiet = 0.1 * np.sin(2 * np.pi * 440 * t)
    loud = extract_features(4 * quiet)["energy_rms"]
    assert loud == pytest.approx(4 * extract_features(quiet)["energy_rms"], rel=1e-3)


@pytest.mark.skipif(not DB_PATH.exists(), reason="database not built (run the pipeline first)")
def test_every_track_has_complete_features():
    conn = connect()
    n_tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    rows = conn.execute("SELECT * FROM audio_features").fetchall()
    assert len(rows) == n_tracks > 0
    for r in rows:
        assert 40 < r["tempo_bpm"] < 250
        assert r["energy_rms"] > 0 and r["spectral_centroid"] > 0 and 0 < r["zero_crossing_rate"] < 1
