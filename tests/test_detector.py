"""Detector tests: the matching math on synthetic hashes, then the real simulated recordings."""
import json

import numpy as np
import pytest

from src.db import DB_PATH, resolve
from src.detector import (GROUND_TRUTH, WindowScores, agreement_by_offset, analyse_recording,
                          count_non_overlapping, detect, load_threshold, score_windows)
from src.fingerprint import HASH_RATE_HZ, fingerprint_file, load_fingerprints

rng = np.random.default_rng(42)


def random_hashes(n):
    return rng.integers(0, 2**32, n, dtype=np.uint64).astype(np.uint32)


def flip_bits(x, fraction):
    """Flip each bit of x independently with probability `fraction`."""
    mask = np.zeros(len(x), dtype=np.uint32)
    for bit in range(32):
        mask |= (rng.random(len(x)) < fraction).astype(np.uint32) << np.uint32(bit)
    return x ^ mask


# ---------- matching math ----------

def test_exact_slice_found_at_its_offset():
    ref = random_hashes(2000)
    scores = agreement_by_offset(ref[137:137 + 80], ref)
    assert int(np.argmax(scores)) == 137
    assert scores[137] == 1.0


def test_noisy_slice_still_aligns():
    ref = random_hashes(2000)
    query = flip_bits(ref[900:980], 0.2)
    scores = agreement_by_offset(query, ref)
    assert int(np.argmax(scores)) == 900
    assert 0.75 < scores[900] < 0.85


def test_unrelated_hashes_agree_about_half():
    scores = agreement_by_offset(random_hashes(80), random_hashes(2000))
    assert abs(scores.mean() - 0.5) < 0.01
    assert scores.max() < 0.65


def test_query_longer_than_reference_returns_empty():
    assert len(agreement_by_offset(random_hashes(100), random_hashes(50))) == 0


def test_count_non_overlapping():
    assert count_non_overlapping(np.array([0, 20, 40, 81, 100, 162]), 81) == 3


def test_detects_embedded_track_and_ignores_absent_one():
    playing, absent = random_hashes(3000), random_hashes(3000)
    # Recording: 20 s of unrelated hashes, then 40 s of `playing` from 60 s in (noisy), then 20 s unrelated.
    pre, post = random_hashes(int(20 * HASH_RATE_HZ)), random_hashes(int(20 * HASH_RATE_HZ))
    start = int(60 * HASH_RATE_HZ)
    body = flip_bits(playing[start:start + int(40 * HASH_RATE_HZ)], 0.25)
    query = np.concatenate([pre, body, post])
    ws = score_windows(query, {"playing": playing, "absent": absent})
    found = detect(ws, threshold=5.0)
    assert [d.track_id for d in found] == ["playing"]
    d = found[0]
    true_alignment = 60 - 20  # reference seconds minus recording seconds
    assert abs(d.alignment_sec - true_alignment) < 0.5
    assert d.matched_windows >= 2


def test_single_lucky_window_is_not_a_detection():
    # One window scores above threshold but nothing corroborates it.
    ws = WindowScores(track_ids=["t"], window_starts=np.array([0, 20, 40, 60, 80, 100]),
                      best_score=np.full((6, 1), 0.6), peak_z=np.array([[2.0], [9.0], [2.0], [2.0], [2.0], [2.0]]),
                      best_offset=np.array([[5], [300], [9], [70], [500], [3]]), window_len=81)
    assert detect(ws, threshold=4.0) == []


# ---------- real simulated recordings ----------

needs_pipeline = pytest.mark.skipif(
    not (DB_PATH.exists() and GROUND_TRUTH.exists()),
    reason="database or simulated events missing (run the pipeline first)",
)


@needs_pipeline
@pytest.mark.parametrize("key", ["test_01", "test_02", "test_03", "test_04", "test_05", "test_neg_01", "test_neg_02"])
def test_held_out_recordings_match_ground_truth(key):
    """Every catalog track in a test recording is found at the right alignment, and nothing else is.

    The negative controls (crowd noise only; a non-catalog CC track) must produce no
    detections, and in the positive recordings every catalog track that is NOT
    playing (the other 45-46 tracks) must also stay undetected: the true-negative case.
    """
    events = {e["key"]: e for e in json.loads(GROUND_TRUTH.read_text())["events"]}
    ev = events[key]
    if not resolve(ev["recording_path"]).exists():
        pytest.skip("recording not generated")
    found = {d.track_id: d for d in detect(analyse_recording(ev["recording_path"], load_fingerprints()),
                                           load_threshold())}
    expected = {s["track_id"]: s["ref_start_sec"] - s["recording_start_sec"]
                for s in ev["segments"] if s["in_catalog"]}
    assert set(found) == set(expected), f"detected {sorted(found)}, expected {sorted(expected)}"
    for tid, alignment in expected.items():
        assert abs(found[tid].alignment_sec - alignment) <= 1.0


@needs_pipeline
def test_fpcalc_matches_pyacoustid_when_libchromaprint_available():
    """fpcalc -raw must equal pyacoustid's decoded fingerprint (skipped if libchromaprint can't load)."""
    try:
        import acoustid
        import chromaprint
    except ImportError:
        pytest.skip("libchromaprint not on the dynamic-library path; "
                    "run with DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib to include this check")
    path = resolve(json.loads(GROUND_TRUTH.read_text())["events"][0]["recording_path"])
    _, encoded = acoustid.fingerprint_file(str(path), maxlength=100000, force_fpcalc=True)
    decoded, _ = chromaprint.decode_fingerprint(encoded)
    ours = fingerprint_file(path)
    assert [x & 0xFFFFFFFF for x in decoded] == ours.tolist()
