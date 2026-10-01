"""Module B: identify catalog tracks inside an event recording.

    python -m src.detector                 # run every recording in ground_truth.json
    python -m src.detector --recording path/to.mp3 --venue "Venue A (fictional)" --date 2026-03-14

Matching approach
-----------------
A Chromaprint fingerprint is a sequence of 32-bit sub-fingerprints, about 8.08
per second. Each bit encodes whether one filtered chroma feature rises or falls,
so two recordings of the same audio produce sub-fingerprints that agree in most
bits even after noise and compression. Unrelated audio agrees in about half.

1. Fingerprint the recording with fpcalc, the same way as the catalog.
2. Cut the query into overlapping windows (WINDOW_SEC long, every HOP_SEC).
   Short windows let one recording contain several tracks, or silence and
   talking around a track, without diluting the score.
3. For each window and each reference track, slide the window across the
   reference one sub-fingerprint at a time. At each offset, XOR the aligned
   arrays, count differing bits (Hamming distance) and convert to
   bit agreement = 1 - differing_bits / (32 * window_length).
   The best offset and its agreement are kept. This is vectorised with
   numpy's sliding_window_view and bitwise_count.
4. Peak prominence. Raw bit agreement isn't comparable across tracks: Chromaprint
   bits are not uniformly random, so a window of crowd noise can agree with
   some references at 0.48 on a typical offset and with others at 0.64. Each
   window's best agreement is therefore turned into a z-score against that
   same window's agreement at every other offset of the same reference:
   peak_z = (best - mean) / std. A true match is a sharp peak; a lucky
   match on a low-information reference is not.
5. A window "hits" a track when peak_z clears THRESHOLD (tuned on the tune split).
6. Alignment check: if a track really is playing, hit windows imply the same
   alignment (reference position minus recording position). Hits are grouped
   by alignment (within ALIGN_TOL_SEC), and a track is detected only when one
   group contains at least MIN_WINDOWS windows that don't overlap each other.
   Overlapping windows share audio, so they don't count as independent evidence.

match_confidence is the highest bit agreement in the winning group, i.e. the
fraction of bits that agree at the best offset. peak_z is reported next to it.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from src.db import ROOT, connect, resolve
from src.fingerprint import HASH_RATE_HZ, fingerprint_file, load_fingerprints

WINDOW_SEC = 10.0
HOP_SEC = 2.5
MIN_WINDOWS = 2
ALIGN_TOL_SEC = 0.5
# Fallback only. The threshold actually used comes from results/threshold_tuning.json,
# written by `python -m src.evaluate --tune` from the tune split.
DEFAULT_THRESHOLD = 5.0
TUNING_PATH = ROOT / "results" / "threshold_tuning.json"
GROUND_TRUTH = ROOT / "data" / "audio" / "simulated_events" / "ground_truth.json"


def load_threshold() -> float:
    if TUNING_PATH.exists():
        return float(json.loads(TUNING_PATH.read_text())["chosen_threshold"])
    print(f"warning: {TUNING_PATH.relative_to(ROOT)} missing, using untuned default {DEFAULT_THRESHOLD}")
    return DEFAULT_THRESHOLD


def agreement_by_offset(query: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Bit agreement between `query` and every full-overlap position in `ref`.

    Returns an array of length len(ref) - len(query) + 1 where element k is
    1 - popcount(query XOR ref[k:k+len(query)]) / (32 * len(query)).
    """
    m = len(query)
    if len(ref) < m:
        return np.zeros(0)
    windows = sliding_window_view(ref, m)                 # (n - m + 1, m), no copy
    differing = np.bitwise_count(windows ^ query).sum(axis=1, dtype=np.int64)
    return 1.0 - differing / (32.0 * m)


@dataclass
class WindowScores:
    """Best match of every query window against every reference track."""
    track_ids: list[str]
    window_starts: np.ndarray     # query index where each window starts
    best_score: np.ndarray        # (n_windows, n_tracks) bit agreement at best offset
    peak_z: np.ndarray            # (n_windows, n_tracks) prominence of that peak over other offsets
    best_offset: np.ndarray       # (n_windows, n_tracks) reference index of best offset
    window_len: int

    @property
    def window_start_sec(self) -> np.ndarray:
        return self.window_starts / HASH_RATE_HZ


def score_windows(query: np.ndarray, refs: dict[str, np.ndarray],
                  window_sec: float = WINDOW_SEC, hop_sec: float = HOP_SEC) -> WindowScores:
    win = int(round(window_sec * HASH_RATE_HZ))
    hop = max(1, int(round(hop_sec * HASH_RATE_HZ)))
    if len(query) < win:
        starts = np.array([0])
        win = len(query)
    else:
        starts = np.arange(0, len(query) - win + 1, hop)
    track_ids = sorted(refs)
    best_score = np.zeros((len(starts), len(track_ids)))
    peak_z = np.zeros((len(starts), len(track_ids)))
    best_offset = np.zeros((len(starts), len(track_ids)), dtype=np.int64)
    for j, tid in enumerate(track_ids):
        ref = refs[tid]
        for i, s in enumerate(starts):
            scores = agreement_by_offset(query[s:s + win], ref)
            if len(scores) > 1:
                k = int(np.argmax(scores))
                best_score[i, j], best_offset[i, j] = scores[k], k
                peak_z[i, j] = (scores[k] - scores.mean()) / max(scores.std(), 1e-9)
    return WindowScores(track_ids, starts, best_score, peak_z, best_offset, win)


@dataclass
class Detection:
    track_id: str
    match_confidence: float       # best bit agreement in the winning alignment group
    mean_confidence: float        # mean bit agreement over the group's windows
    peak_z: float                 # highest peak prominence in the group
    matched_offset_sec: float     # reference-track position where the matched audio starts
    recording_start_sec: float    # recording position of the first matched window
    recording_end_sec: float      # recording position where the last matched window ends
    matched_windows: int          # non-overlapping windows supporting this alignment

    @property
    def alignment_sec(self) -> float:
        """Reference time minus recording time; constant while a track plays."""
        return self.matched_offset_sec - self.recording_start_sec


def count_non_overlapping(starts: np.ndarray, window_len: int) -> int:
    """Greedy count of windows (by start index) that don't overlap each other."""
    count, next_free = 0, -np.inf
    for s in np.sort(starts):
        if s >= next_free:
            count, next_free = count + 1, s + window_len
    return count


def best_alignment_group(ws: WindowScores, j: int, threshold: float,
                         align_tol_sec: float = ALIGN_TOL_SEC) -> tuple[np.ndarray, int]:
    """Hit windows for track j that share the best-supported alignment.

    Returns (window indices in the group, number of non-overlapping windows).
    """
    hits = np.flatnonzero(ws.peak_z[:, j] >= threshold)
    if not len(hits):
        return hits, 0
    tol = align_tol_sec * HASH_RATE_HZ
    align = ws.best_offset[hits, j] - ws.window_starts[hits]
    best, best_n = hits[:0], 0
    for a in np.unique(align):
        group = hits[np.abs(align - a) <= tol]
        n = count_non_overlapping(ws.window_starts[group], ws.window_len)
        if n > best_n or (n == best_n and len(group) > len(best)):
            best, best_n = group, n
    return best, best_n


def detect(ws: WindowScores, threshold: float, min_windows: int = MIN_WINDOWS,
           align_tol_sec: float = ALIGN_TOL_SEC) -> list[Detection]:
    detections = []
    for j, tid in enumerate(ws.track_ids):
        group, n_independent = best_alignment_group(ws, j, threshold, align_tol_sec)
        if n_independent < min_windows:
            continue
        scores = ws.best_score[group, j]
        first = group[0]
        detections.append(Detection(
            track_id=tid,
            match_confidence=float(scores.max()),
            mean_confidence=float(scores.mean()),
            peak_z=float(ws.peak_z[group, j].max()),
            matched_offset_sec=float(ws.best_offset[first, j] / HASH_RATE_HZ),
            recording_start_sec=float(ws.window_starts[first] / HASH_RATE_HZ),
            recording_end_sec=float((ws.window_starts[group[-1]] + ws.window_len) / HASH_RATE_HZ),
            matched_windows=n_independent,
        ))
    return sorted(detections, key=lambda d: d.recording_start_sec)


def analyse_recording(path: str | Path, refs: dict[str, np.ndarray]) -> WindowScores:
    return score_windows(fingerprint_file(resolve(path)), refs)


def register_event(conn, name: str, venue: str, path: str, recorded_at: str) -> int:
    """Insert (or replace) a usage_events row for this recording and return its id."""
    row = conn.execute("SELECT event_id FROM usage_events WHERE recording_path = ?", (path,)).fetchone()
    if row:
        conn.execute("DELETE FROM detections WHERE event_id = ?", (row["event_id"],))
        conn.execute("UPDATE usage_events SET event_name = ?, venue = ?, recorded_at = ? WHERE event_id = ?",
                     (name, venue, recorded_at, row["event_id"]))
        return row["event_id"]
    cur = conn.execute("INSERT INTO usage_events (event_name, venue, recording_path, recorded_at)"
                       " VALUES (?, ?, ?, ?)", (name, venue, path, recorded_at))
    return cur.lastrowid


def store_detections(conn, event_id: int, detections: list[Detection]) -> None:
    for d in detections:
        conn.execute(
            "INSERT INTO detections (event_id, track_id, match_confidence, peak_z, matched_offset_sec,"
            " recording_start_sec, matched_windows) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, d.track_id, round(d.match_confidence, 4), round(d.peak_z, 3),
             round(d.matched_offset_sec, 2), round(d.recording_start_sec, 2), d.matched_windows),
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Detect catalog tracks in event recordings.")
    p.add_argument("--recording", help="a single recording to analyse (default: all simulated events)")
    p.add_argument("--name", help="event name for --recording")
    p.add_argument("--venue", help="venue for --recording (matched against licenses.licensee)")
    p.add_argument("--date", help="recording date/time for --recording, e.g. 2026-03-14")
    p.add_argument("--threshold", type=float, help="override the tuned threshold")
    a = p.parse_args()

    threshold = a.threshold if a.threshold is not None else load_threshold()
    conn = connect()
    refs = load_fingerprints(conn)
    if a.recording:
        events = [{"event_name": a.name or Path(a.recording).stem, "venue": a.venue,
                   "recording_path": a.recording, "recorded_at": a.date}]
    else:
        events = json.loads(GROUND_TRUTH.read_text())["events"]

    print(f"peak_z threshold={threshold:.3f}  window={WINDOW_SEC}s hop={HOP_SEC}s  min_windows={MIN_WINDOWS}")
    for ev in events:
        ws = analyse_recording(ev["recording_path"], refs)
        found = detect(ws, threshold)
        event_id = register_event(conn, ev["event_name"], ev["venue"], ev["recording_path"], ev["recorded_at"])
        store_detections(conn, event_id, found)
        summary = ", ".join(f"{d.track_id} (bits {d.match_confidence:.3f}, z {d.peak_z:.1f})"
                            for d in found) or "no detections"
        print(f"[{event_id:2d}] {ev['event_name'][:55]:55s} {summary}")
    conn.commit()


if __name__ == "__main__":
    main()
