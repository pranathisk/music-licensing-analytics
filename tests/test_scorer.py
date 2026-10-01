"""Scoring behaviour on a small hand-built catalog."""
import pandas as pd

from src.scorer import Brief, effective_tempo, level_ranges, score_catalog


def catalog():
    return pd.DataFrame([
        # track_id, tempo, rms, centroid, zcr, instrumental, tags
        ("in_range_tagged", 110, 0.30, 2000, 0.05, 1, "upbeat,driving"),
        ("in_range_untagged", 112, 0.31, 2100, 0.05, 1, "chill"),
        ("too_slow", 70, 0.30, 2000, 0.05, 1, "upbeat,driving"),
        ("vocal", 110, 0.30, 2000, 0.05, 0, "upbeat,driving"),
        ("quiet", 110, 0.05, 1500, 0.04, 1, "upbeat"),
        ("half_time_reading", 58, 0.29, 1900, 0.05, 1, "driving"),
        ("filler_a", 150, 0.15, 3000, 0.08, 1, ""),
        ("filler_b", 90, 0.10, 1000, 0.03, 0, "ambient"),
    ], columns=["track_id", "tempo_bpm", "energy_rms", "spectral_centroid", "zero_crossing_rate",
                "is_instrumental", "mood_tags"]).assign(title=lambda d: d.track_id, artist="x", license_type="cc0")


def ranks(df):
    return list(df["track_id"])


def test_in_range_beats_out_of_range():
    out = score_catalog(catalog(), Brief(tempo=(100, 120), octave=False))
    order = ranks(out)
    assert order.index("in_range_tagged") < order.index("too_slow")


def test_instrumental_filter_drops_vocal_tracks():
    out = score_catalog(catalog(), Brief(tempo=(100, 120), instrumental=True))
    assert "vocal" not in ranks(out) and "filler_b" not in ranks(out)


def test_tags_boost_and_strict_filter():
    df = catalog()
    soft = ranks(score_catalog(df, Brief(tempo=(100, 120), tags=["upbeat", "driving"])))
    assert soft.index("in_range_tagged") < soft.index("in_range_untagged")
    strict = ranks(score_catalog(df, Brief(tempo=(100, 120), tags=["upbeat", "driving"], strict_tags=True)))
    assert "in_range_untagged" not in strict and "in_range_tagged" in strict


def test_energy_level_uses_catalog_terciles():
    df = catalog()
    levels = level_ranges(df["energy_rms"])
    assert levels["low"][1] <= levels["med"][1] <= levels["high"][1]
    out = score_catalog(df, Brief(energy="high"))
    order = ranks(out)
    assert order.index("in_range_tagged") < order.index("quiet")


def test_octave_reading_only_when_it_lands_in_range():
    assert effective_tempo(58, 100, 130, octave=True) == (116, "double-time")
    assert effective_tempo(58, 100, 130, octave=False) == (58, "")
    assert effective_tempo(140, 60, 100, octave=True) == (70, "half-time")
    # Neither reading in range: keep the raw tempo.
    assert effective_tempo(108, 60, 100, octave=True) == (108, "")
    out = score_catalog(catalog(), Brief(tempo=(100, 120)))
    why = out.set_index("track_id").loc["half_time_reading", "why"]
    assert "double-time" in why


def test_scores_are_bounded_and_sorted():
    out = score_catalog(catalog(), Brief(tempo=(90, 120), energy="med", tags=["upbeat"]))
    assert out["score"].between(0, 100).all()
    assert list(out["score"]) == sorted(out["score"], reverse=True)
