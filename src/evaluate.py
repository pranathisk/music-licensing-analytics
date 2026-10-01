"""Tune the detection threshold and measure the detector on the simulated recordings.

    python -m src.evaluate --tune         # pick threshold on the tune split -> results/threshold_tuning.json
    python -m src.evaluate --test         # metrics on the test split       -> results/detector_evaluation.json
    python -m src.evaluate --snr-sweep    # stress test across noise levels -> results/snr_sweep.json
    python -m src.evaluate --ablation     # which degradation hurts most    -> results/degradation_ablation.json

Scoring rules (track level, per recording):
  true positive   a catalog track that is in the recording and is detected
  false negative  a catalog track that is in the recording and is not detected
  false positive  a detected track that is not in the recording
Offsets are checked separately: a true positive's alignment (reference time
minus recording time) is "offset correct" if within OFFSET_TOL_SEC of the truth.

The threshold is chosen ONLY from the tune split (maximum F1, middle of the
widest tied range). The test split shares no tracks with the tune split and is
scored once with that threshold. With 6 test positives and 2 negative
controls, these numbers describe this small synthetic set and nothing more.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile

import numpy as np
from scipy.signal import butter, sosfilt

from src.db import ROOT, connect, resolve
from src.detector import (ALIGN_TOL_SEC, GROUND_TRUTH, HOP_SEC, MIN_WINDOWS, TUNING_PATH, WINDOW_SEC,
                          agreement_by_offset, analyse_recording, detect, load_threshold, score_windows)
from src.fingerprint import HASH_RATE_HZ, fingerprint_file, load_fingerprints
from src.simulate_event import (MP3_BITRATE, PHONE_BAND_HZ, PHONE_FILTER_ORDER, SR, decode, noise_bed, rms,
                                room_reverb)

RESULTS = ROOT / "results"
OFFSET_TOL_SEC = 1.0
THRESHOLD_GRID = np.round(np.arange(2.0, 9.0001, 0.05), 2)
SNR_SWEEP_DB = [10, 5, 0, -5, -10]


def load_events(split: str | None = None) -> list[dict]:
    events = json.loads(GROUND_TRUTH.read_text())["events"]
    return [e for e in events if split is None or e["split"] == split]


def truth_alignments(event: dict) -> dict[str, float]:
    """track_id -> true alignment (reference time minus recording time) in seconds."""
    return {s["track_id"]: s["ref_start_sec"] - s["recording_start_sec"]
            for s in event["segments"] if s["in_catalog"]}


def score_event(event: dict, ws, threshold: float) -> dict:
    truth = truth_alignments(event)
    found = detect(ws, threshold)
    found_ids = {d.track_id for d in found}
    rows = []
    for d in found:
        expected = truth.get(d.track_id)
        rows.append({
            "track_id": d.track_id,
            "correct_track": expected is not None,
            "offset_error_sec": None if expected is None else round(d.alignment_sec - expected, 3),
            "match_confidence": round(d.match_confidence, 4),
            "peak_z": round(d.peak_z, 3),
            "matched_windows": d.matched_windows,
            "recording_start_sec": round(d.recording_start_sec, 2),
            "matched_offset_sec": round(d.matched_offset_sec, 2),
        })
    tp = sum(r["correct_track"] for r in rows)
    return {
        "key": event["key"],
        "kind": event["kind"],
        "snr_db": event["snr_db"],
        "expected_tracks": sorted(truth),
        "detections": rows,
        "missed_tracks": sorted(set(truth) - found_ids),
        "tp": tp,
        "fp": len(rows) - tp,
        "fn": len(set(truth) - found_ids),
        "offset_correct": sum(r["correct_track"] and abs(r["offset_error_sec"]) <= OFFSET_TOL_SEC for r in rows),
    }


def metrics(per_event: list[dict]) -> dict:
    tp = sum(e["tp"] for e in per_event)
    fp = sum(e["fp"] for e in per_event)
    fn = sum(e["fn"] for e in per_event)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else 0.0
    negatives = [e for e in per_event if e["kind"] == "negative_control"]
    return {
        "true_positives": tp, "false_positives": fp, "false_negatives": fn,
        "precision": None if precision is None else round(precision, 4),
        "recall": None if recall is None else round(recall, 4),
        "f1": round(f1, 4),
        "offset_correct": sum(e["offset_correct"] for e in per_event),
        "negative_controls_clean": sum(e["fp"] == 0 for e in negatives),
        "negative_controls_total": len(negatives),
    }


def window_scores_for(events: list[dict], refs) -> dict[str, object]:
    out = {}
    for ev in events:
        out[ev["key"]] = analyse_recording(ev["recording_path"], refs)
        print(f"  scored {ev['key']}")
    return out


def tune() -> None:
    refs = load_fingerprints()
    events = load_events("tune")
    ws_by_key = window_scores_for(events, refs)
    sweep = []
    for t in THRESHOLD_GRID:
        m = metrics([score_event(ev, ws_by_key[ev["key"]], t) for ev in events])
        sweep.append({"threshold": float(t), **m})
    best_f1 = max(r["f1"] for r in sweep)
    # Widest contiguous run of thresholds achieving the best F1; take its middle.
    runs, run = [], []
    for r in sweep:
        if r["f1"] == best_f1:
            run.append(r["threshold"])
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    widest = max(runs, key=len)
    chosen = round(widest[len(widest) // 2], 2)
    TUNING_PATH.write_text(json.dumps({
        "generated_by": "python -m src.evaluate --tune",
        "statistic": "peak_z (prominence of the best-offset bit agreement over the reference's other offsets)",
        "split": "tune",
        "events": [e["key"] for e in events],
        "rule": "max F1 on the tune split; middle of the widest tied threshold range",
        "detector_settings": {"window_sec": WINDOW_SEC, "hop_sec": HOP_SEC, "min_windows": MIN_WINDOWS,
                              "align_tol_sec": ALIGN_TOL_SEC},
        "best_f1": best_f1,
        "best_range": [widest[0], widest[-1]],
        "chosen_threshold": chosen,
        "at_chosen": next(r for r in sweep if r["threshold"] == chosen),
        "sweep": sweep,
    }, indent=2))
    print(f"best tune F1 {best_f1} for thresholds {widest[0]}-{widest[-1]}; chose {chosen}")


def test() -> None:
    threshold = load_threshold()
    refs = load_fingerprints()
    out = {"generated_by": "python -m src.evaluate --test", "threshold": threshold,
           "offset_tolerance_sec": OFFSET_TOL_SEC, "splits": {}}
    for split in ("tune", "test"):
        events = load_events(split)
        ws_by_key = window_scores_for(events, refs)
        per_event = [score_event(ev, ws_by_key[ev["key"]], threshold) for ev in events]
        out["splits"][split] = {"metrics": metrics(per_event), "events": per_event}
        print(split, out["splits"][split]["metrics"])
    out["note"] = ("Threshold was chosen on the tune split, so tune metrics are optimistic; the test split is "
                   "the held-out number. Both are tiny synthetic sets.")
    (RESULTS / "detector_evaluation.json").write_text(json.dumps(out, indent=2))


def encode_and_fingerprint(x: np.ndarray, mp3: bool = True) -> np.ndarray:
    suffix = ".mp3" if mp3 else ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-"]
        if mp3:
            cmd += ["-codec:a", "libmp3lame", "-b:a", MP3_BITRATE]
        subprocess.run(cmd + [f.name], input=x.astype(np.float32).tobytes(), check=True)
        return fingerprint_file(f.name)


def snr_sweep() -> None:
    """Re-run the full detector on each test-split track at a range of SNRs."""
    threshold = load_threshold()
    refs = load_fingerprints()
    conn = connect()
    paths = {r["track_id"]: r["file_path"] for r in conn.execute("SELECT track_id, file_path FROM tracks")}
    segments = [s for e in load_events("test") for s in e["segments"] if s["in_catalog"]]
    sos = butter(PHONE_FILTER_ORDER, PHONE_BAND_HZ, btype="bandpass", fs=SR, output="sos")
    lead = 10.0
    rows = []
    for seg_i, seg in enumerate(segments):
        length = seg["recording_end_sec"] - seg["recording_start_sec"]
        clip = decode(resolve(paths[seg["track_id"]]), offset=seg["ref_start_sec"], duration=length).astype(float)
        for snr in SNR_SWEEP_DB:
            rng = np.random.default_rng(1000 + seg_i)
            music = room_reverb(clip, rng)
            n_lead = int(lead * SR)
            total = n_lead + len(music) + n_lead
            mix = noise_bed(("crowd", "chatter"), total, rng) * rms(music) / 10 ** (snr / 20)
            mix[n_lead:n_lead + len(music)] += music
            mix = sosfilt(sos, mix)
            mix = 0.9 * mix / np.max(np.abs(mix))
            ws = score_windows(encode_and_fingerprint(mix), refs)
            found = detect(ws, threshold)
            hit = next((d for d in found if d.track_id == seg["track_id"]), None)
            others = [d.track_id for d in found if d.track_id != seg["track_id"]]
            rows.append({
                "track_id": seg["track_id"], "excerpt_sec": round(length, 1), "snr_db": snr,
                "detected": hit is not None,
                "match_confidence": None if hit is None else round(hit.match_confidence, 4),
                "peak_z": None if hit is None else round(hit.peak_z, 3),
                "offset_error_sec": None if hit is None else
                round(hit.alignment_sec - (seg["ref_start_sec"] - lead), 3),
                "false_positives": others,
            })
            print(f"  {seg['track_id']} snr={snr:4d}  detected={hit is not None}  fp={others}")
    summary = {}
    for snr in SNR_SWEEP_DB:
        r = [x for x in rows if x["snr_db"] == snr]
        summary[str(snr)] = {"detected": sum(x["detected"] for x in r), "of": len(r),
                             "false_positives": sum(len(x["false_positives"]) for x in r)}
    (RESULTS / "snr_sweep.json").write_text(json.dumps({
        "generated_by": "python -m src.evaluate --snr-sweep",
        "threshold": threshold,
        "setup": "each test-split excerpt, 10 s noise lead-in/out, crowd+chatter noise, room reverb, "
                 f"{PHONE_BAND_HZ[0]}-{PHONE_BAND_HZ[1]} Hz band-limit, {MP3_BITRATE} MP3",
        "summary_by_snr_db": summary,
        "runs": rows,
    }, indent=2))


def ablation() -> None:
    """Apply one degradation at a time to clean excerpts and measure the true-offset match."""
    refs = load_fingerprints()
    conn = connect()
    paths = {r["track_id"]: r["file_path"] for r in conn.execute("SELECT track_id, file_path FROM tracks")}
    segments = [s for e in load_events("test") for s in e["segments"] if s["in_catalog"]]
    win = int(round(WINDOW_SEC * HASH_RATE_HZ))
    conditions = {
        "clean (lossless)": lambda x, rng: (x, False),
        f"{MP3_BITRATE} MP3 only": lambda x, rng: (x, True),
        "room reverb only": lambda x, rng: (room_reverb(x, rng), False),
        f"phone band-limit {PHONE_BAND_HZ[0]}-{PHONE_BAND_HZ[1]} Hz, order {PHONE_FILTER_ORDER} (used)":
            lambda x, rng: (sosfilt(butter(PHONE_FILTER_ORDER, PHONE_BAND_HZ, btype="bandpass", fs=SR,
                                           output="sos"), x), False),
        "steeper band-limit 150-7000 Hz, order 4 (first draft, rejected)":
            lambda x, rng: (sosfilt(butter(4, [150, 7000], btype="bandpass", fs=SR, output="sos"), x), False),
        **{f"crowd+chatter noise at {snr} dB SNR": (lambda s: lambda x, rng: (
            x + noise_bed(("crowd", "chatter"), len(x), rng) * rms(x) / 10 ** (s / 20), False))(snr)
           for snr in (10, 5, 0, -5)},
    }
    results = {}
    for name, fn in conditions.items():
        bits, zs, aligned = [], [], []
        for i, seg in enumerate(segments):
            length = seg["recording_end_sec"] - seg["recording_start_sec"]
            clip = decode(resolve(paths[seg["track_id"]]), offset=seg["ref_start_sec"], duration=length)
            x, mp3 = fn(clip.astype(float), np.random.default_rng(i))
            q = encode_and_fingerprint(x, mp3=mp3)
            ref = refs[seg["track_id"]]
            true_k = seg["ref_start_sec"] * HASH_RATE_HZ
            for s in range(0, len(q) - win + 1, win):
                scores = agreement_by_offset(q[s:s + win], ref)
                k = int(np.argmax(scores))
                bits.append(scores[k])
                zs.append((scores[k] - scores.mean()) / scores.std())
                aligned.append(abs(k - s - true_k) <= ALIGN_TOL_SEC * HASH_RATE_HZ)
        results[name] = {"windows": len(bits), "mean_best_bit_agreement": round(float(np.mean(bits)), 4),
                         "mean_peak_z": round(float(np.mean(zs)), 3),
                         "best_offset_is_true_offset": round(float(np.mean(aligned)), 4)}
        print(f"  {name:62s} {results[name]}")
    (RESULTS / "degradation_ablation.json").write_text(json.dumps({
        "generated_by": "python -m src.evaluate --ablation",
        "setup": f"test-split excerpts, non-overlapping {WINDOW_SEC:.0f} s windows, one degradation at a time",
        "conditions": results,
    }, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tune", action="store_true")
    p.add_argument("--test", action="store_true")
    p.add_argument("--snr-sweep", action="store_true")
    p.add_argument("--ablation", action="store_true")
    a = p.parse_args()
    if not any(vars(a).values()):
        p.error("pick at least one of --tune / --test / --snr-sweep / --ablation")
    RESULTS.mkdir(exist_ok=True)
    if a.tune:
        tune()
    if a.test:
        test()
    if a.snr_sweep:
        snr_sweep()
    if a.ablation:
        ablation()


if __name__ == "__main__":
    main()
