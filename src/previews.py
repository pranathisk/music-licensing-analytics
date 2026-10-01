"""Make short preview clips of each catalog track for the web app.

    python -m src.previews

The full catalog (~265 MB) is not committed, so the deployed app plays these
30 s, 96 kbit/s mono excerpts instead (~17 MB in total). Each one starts a third of
the way into the track. All tracks are CC BY or CC0, which permit sharing excerpts
with attribution (see CREDITS.md).
"""
from __future__ import annotations

import subprocess

from src.db import ROOT, connect, resolve

PREVIEW_DIR = ROOT / "data" / "audio" / "previews"
PREVIEW_SEC = 30
BITRATE = "96k"


def preview_path(track_id: str):
    return PREVIEW_DIR / f"{track_id}.mp3"


def main() -> None:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    rows = connect().execute("SELECT track_id, file_path, duration_sec FROM tracks ORDER BY track_id").fetchall()
    for r in rows:
        start = max(0.0, min(r["duration_sec"] / 3, r["duration_sec"] - PREVIEW_SEC))
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", str(PREVIEW_SEC),
             "-i", str(resolve(r["file_path"])), "-ac", "1", "-codec:a", "libmp3lame", "-b:a", BITRATE,
             "-af", f"afade=t=in:d=1,afade=t=out:st={PREVIEW_SEC - 2}:d=2",
             str(preview_path(r["track_id"]))],
            check=True,
        )
    print(f"Wrote {len(rows)} previews to {PREVIEW_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
