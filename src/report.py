"""Module B: join detections with licenses and write the flag report.

    python -m src.report

A detection counts as licensed when the `licenses` table has a row for the same
track whose licensee is the event's venue, whose status is 'active', and whose
start_date..end_date covers the event date. Otherwise it is flagged, with the
reason (expired, not yet started, status 'none', or no license on file). The
result is written back to detections.licensed and to results/detection_report.json.

The licenses table is fictional demo data. Every catalog track is CC BY or CC0,
which in reality already permits public performance (CC BY with attribution).
A flag here shows the pipeline working against a simulated licensing regime.
It is not a finding that anyone infringed anything.
"""
from __future__ import annotations

import json
from datetime import date

from src.db import ROOT, connect
from src.detector import GROUND_TRUTH, load_threshold

REPORT_PATH = ROOT / "results" / "detection_report.json"
EVAL_PATH = ROOT / "results" / "detector_evaluation.json"
SNR_PATH = ROOT / "results" / "snr_sweep.json"

DISCLAIMER = (
    "Portfolio demonstration only. Small, self-built fingerprinting system tested on synthetic "
    "recordings (catalog excerpts mixed with crowd/talking noise, room reverb, phone band-limit and "
    "64 kbit/s MP3). Not a validated production system and not legal evidence. Venues, licensees and "
    "licenses are fictional. All catalog tracks are CC BY or CC0, which already permit public "
    "performance (CC BY with attribution), so 'unlicensed' here only means 'no matching row in the "
    "fictional licenses table'."
)


def license_status(conn, track_id: str, venue: str | None, event_day: date) -> tuple[bool, str, int | None]:
    """Decide whether a detection is covered; return (licensed, reason, license_id)."""
    rows = conn.execute(
        "SELECT license_id, scope, start_date, end_date, status FROM licenses"
        " WHERE track_id = ? AND licensee = ? ORDER BY license_id",
        (track_id, venue),
    ).fetchall()
    if not rows:
        return False, f"no license on file for {track_id} at {venue}", None
    for r in rows:
        if r["status"] == "active" and r["start_date"] and r["end_date"]:
            if date.fromisoformat(r["start_date"]) <= event_day <= date.fromisoformat(r["end_date"]):
                return True, (f"active license #{r['license_id']} ({r['scope']}) covers {event_day} "
                              f"[{r['start_date']} to {r['end_date']}]"), r["license_id"]
    r = rows[-1]
    if r["status"] == "active":
        return False, (f"license #{r['license_id']} is active but runs {r['start_date']} to {r['end_date']}; "
                       f"event was {event_day}"), r["license_id"]
    if r["status"] == "expired":
        return False, f"license #{r['license_id']} expired on {r['end_date']}", r["license_id"]
    return False, f"license #{r['license_id']} on file with status '{r['status']}'", r["license_id"]


def main() -> None:
    conn = connect()
    truth_by_path = {e["recording_path"]: e for e in json.loads(GROUND_TRUTH.read_text())["events"]}
    events = conn.execute("SELECT * FROM usage_events ORDER BY event_id").fetchall()
    report_events = []
    totals = {"events": 0, "detections": 0, "licensed": 0, "flagged": 0}
    for ev in events:
        event_day = date.fromisoformat(ev["recorded_at"][:10])
        dets = conn.execute(
            "SELECT d.*, t.title, t.artist, t.license_type AS work_license FROM detections d"
            " JOIN tracks t USING (track_id) WHERE d.event_id = ? ORDER BY d.recording_start_sec",
            (ev["event_id"],),
        ).fetchall()
        truth = truth_by_path.get(ev["recording_path"])
        truth_ids = None if truth is None else {s["track_id"] for s in truth["segments"] if s["in_catalog"]}
        items = []
        for d in dets:
            licensed, reason, license_id = license_status(conn, d["track_id"], ev["venue"], event_day)
            conn.execute("UPDATE detections SET licensed = ? WHERE detection_id = ?",
                         (int(licensed), d["detection_id"]))
            items.append({
                "track_id": d["track_id"], "title": d["title"], "artist": d["artist"],
                "work_license": d["work_license"],
                "match_confidence": d["match_confidence"], "peak_z": d["peak_z"],
                "recording_start_sec": d["recording_start_sec"], "matched_offset_sec": d["matched_offset_sec"],
                "matched_windows": d["matched_windows"],
                "licensed": licensed, "license_id": license_id, "reason": reason,
                # Only known for simulated recordings: was this track really in the audio?
                "matches_ground_truth": None if truth_ids is None else d["track_id"] in truth_ids,
            })
            totals["detections"] += 1
            totals["licensed" if licensed else "flagged"] += 1
        report_events.append({
            "event_id": ev["event_id"], "event_name": ev["event_name"], "venue": ev["venue"],
            "recorded_at": ev["recorded_at"], "recording_path": ev["recording_path"],
            "split": truth["split"] if truth else None,
            "ground_truth_tracks": None if truth_ids is None else sorted(truth_ids),
            "detections": items,
        })
        totals["events"] += 1
    conn.commit()

    report = {
        "generated_by": "python -m src.report",
        "disclaimer": DISCLAIMER,
        "detector": {"statistic": "peak_z", "threshold": load_threshold()},
        "totals": totals,
        "events": report_events,
    }
    if EVAL_PATH.exists():
        ev = json.loads(EVAL_PATH.read_text())
        report["evaluation"] = {
            "source": str(EVAL_PATH.relative_to(ROOT)),
            "test_split": ev["splits"]["test"]["metrics"],
            "tune_split": ev["splits"]["tune"]["metrics"],
            "note": ev["note"],
        }
    if SNR_PATH.exists():
        report["snr_sweep"] = {"source": str(SNR_PATH.relative_to(ROOT)),
                               "summary_by_snr_db": json.loads(SNR_PATH.read_text())["summary_by_snr_db"]}
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(f"{totals['detections']} detections across {totals['events']} recordings: "
          f"{totals['licensed']} licensed, {totals['flagged']} flagged")
    for e in report_events:
        for d in e["detections"]:
            flag = "LICENSED" if d["licensed"] else "FLAGGED "
            print(f"  {flag} {e['event_name'][:42]:42s} {d['title'][:28]:28s} {d['reason']}")


if __name__ == "__main__":
    main()
