"""Module B: compute Chromaprint fingerprints for every catalog track.

    python -m src.fingerprint

Runs `fpcalc -raw -length 0` (Chromaprint 1.6, default algorithm) on each file
and stores the raw sub-fingerprint array, one 32-bit integer per frame, in the
`fingerprints` table. Matching is done locally by src/detector.py. Nothing is
sent to the AcoustID web service, which only knows commercially registered
releases.

fpcalc's raw output is the same integer sequence that pyacoustid's
`acoustid.fingerprint_file(..., force_fpcalc=True)` + `chromaprint.decode_fingerprint`
returns (checked in tests/test_detector.py when libchromaprint is importable).
Calling fpcalc directly avoids needing libchromaprint on the dynamic-library
path just to decode.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

from src.db import connect, resolve

# Chromaprint resamples to 11,025 Hz and emits one sub-fingerprint per
# 4096-sample frame with 2/3 overlap, i.e. a hop of 4096 - 2731 = 1365 samples.
CHROMAPRINT_SR = 11025
CHROMAPRINT_HOP = 1365
HASH_RATE_HZ = CHROMAPRINT_SR / CHROMAPRINT_HOP  # ~8.077 hashes per second


def fingerprint_file(path: str | Path) -> np.ndarray:
    """Return the raw Chromaprint sub-fingerprints of a whole file as uint32."""
    fpcalc = shutil.which("fpcalc")
    if fpcalc is None:
        raise RuntimeError("fpcalc not found; install Chromaprint (e.g. `brew install chromaprint`)")
    out = subprocess.run([fpcalc, "-raw", "-json", "-length", "0", str(path)], capture_output=True, text=True)
    # fpcalc exits non-zero on a decode error (e.g. a corrupt frame at the end of
    # an MP3) but still prints the fingerprint of everything it decoded. Keep
    # that partial result and warn; fail only if there is no fingerprint at all.
    try:
        hashes = json.loads(out.stdout)["fingerprint"]
    except (json.JSONDecodeError, KeyError):
        raise RuntimeError(f"fpcalc failed on {path}: {out.stderr.strip()}") from None
    if out.returncode != 0:
        print(f"warning: fpcalc exit {out.returncode} on {Path(path).name} "
              f"({out.stderr.strip()}); kept {len(hashes)} hashes")
    return np.array(hashes, dtype=np.uint32)


def load_fingerprints(conn=None) -> dict[str, np.ndarray]:
    conn = conn or connect()
    rows = conn.execute("SELECT track_id, fingerprint_hashes FROM fingerprints").fetchall()
    return {r["track_id"]: np.array(json.loads(r["fingerprint_hashes"]), dtype=np.uint32) for r in rows}


def main() -> None:
    conn = connect()
    tracks = conn.execute("SELECT track_id, title, file_path FROM tracks ORDER BY track_id").fetchall()
    conn.execute("DELETE FROM fingerprints")
    for i, t in enumerate(tracks, 1):
        hashes = fingerprint_file(resolve(t["file_path"]))
        conn.execute(
            "INSERT INTO fingerprints (track_id, fingerprint_hashes, hash_rate_hz) VALUES (?, ?, ?)",
            (t["track_id"], json.dumps(hashes.tolist()), HASH_RATE_HZ),
        )
        print(f"[{i}/{len(tracks)}] {t['track_id']} {len(hashes)} hashes "
              f"({len(hashes) / HASH_RATE_HZ:.1f}s)")
    conn.commit()


if __name__ == "__main__":
    main()
