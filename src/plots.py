"""Result figures.

    python -m src.plots

results/feature_space.png        tempo vs RMS energy for the whole catalog, with the
                                 spec's example brief region and its top matches
results/confidence_by_offset.png (a) bit agreement at every reference offset for one
                                 query window; (b) peak_z per window across a whole
                                 recording, against the detection threshold
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from src.db import ROOT  # noqa: E402
from src.detector import GROUND_TRUTH, agreement_by_offset, analyse_recording, load_threshold  # noqa: E402
from src.fingerprint import HASH_RATE_HZ, fingerprint_file, load_fingerprints  # noqa: E402
from src.scorer import EXAMPLE_BRIEFS, level_ranges, load_catalog, score_catalog  # noqa: E402

RESULTS = ROOT / "results"
# Reference palette (light mode): categorical slots 1-2, text and surface tokens.
SERIES = ["#2a78d6", "#eb6834"]
MUTED = "#8a8985"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e6e5e1"
SURFACE = "#fcfcfb"
SHADE = "#f0efec"
EXAMPLE_EVENT = "test_02"


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_2, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.xaxis.label.set_color(TEXT_2)
    ax.yaxis.label.set_color(TEXT_2)


def feature_space() -> None:
    df = load_catalog()
    desc, brief = EXAMPLE_BRIEFS[0]
    ranked = score_catalog(df, brief)
    high_lo, high_hi = level_ranges(df["energy_rms"])["high"]

    fig, ax = plt.subplots(figsize=(8, 5.2), facecolor=SURFACE)
    style(ax)
    ax.add_patch(plt.Rectangle((brief.tempo[0], high_lo), brief.tempo[1] - brief.tempo[0], high_hi - high_lo,
                               facecolor=SHADE, edgecolor=GRID, zorder=0))
    ax.text(brief.tempo[0] + 1, high_hi + 0.004, "example brief: 90-120 BPM, high energy",
            fontsize=8.5, color=TEXT_2, va="bottom")
    for flag, label, color, marker in ((1, "instrumental", SERIES[0], "o"), (0, "vocals / not tagged", SERIES[1], "^")):
        sub = df[df["is_instrumental"] == flag]
        ax.scatter(sub["tempo_bpm"], sub["energy_rms"], s=46, c=color, marker=marker, label=label,
                   edgecolors=SURFACE, linewidths=1.5, zorder=3)
    for i, r in ranked.head(3).iterrows():
        row = df[df["track_id"] == r["track_id"]].iloc[0]
        ax.annotate(f"#{i + 1} {r['title'][:22]}", (row["tempo_bpm"], row["energy_rms"]),
                    xytext=(8, 4), textcoords="offset points", fontsize=8.5, color=TEXT)
    ax.set_xlabel("tempo (BPM, librosa beat_track)")
    ax.set_ylabel("mean RMS energy")
    ax.set_title(f"Catalog feature space ({len(df)} tracks)", loc="left", fontsize=12, color=TEXT)
    ax.legend(frameon=False, fontsize=9, labelcolor=TEXT_2, loc="upper right")
    fig.tight_layout()
    fig.savefig(RESULTS / "feature_space.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def confidence_by_offset() -> None:
    events = {e["key"]: e for e in json.loads(GROUND_TRUTH.read_text())["events"]}
    ev = events[EXAMPLE_EVENT]
    refs = load_fingerprints()
    threshold = load_threshold()
    ws = analyse_recording(ev["recording_path"], refs)
    t = ws.window_start_sec + ws.window_len / HASH_RATE_HZ / 2  # window centres
    segs = [s for s in ev["segments"] if s["in_catalog"]]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 7.2), facecolor=SURFACE,
                                   gridspec_kw={"height_ratios": [1, 1.15]})
    # (a) one window from the middle of the second track vs that track's reference
    seg = segs[1]
    query = fingerprint_file(ev["recording_path"])
    start = int((seg["recording_start_sec"] + 15) * HASH_RATE_HZ)
    scores = agreement_by_offset(query[start:start + ws.window_len], refs[seg["track_id"]])
    offs = np.arange(len(scores)) / HASH_RATE_HZ
    k = int(np.argmax(scores))
    style(ax1)
    ax1.plot(offs, scores, color=SERIES[0], linewidth=1.2)
    ax1.scatter([offs[k]], [scores[k]], s=60, color=SERIES[0], edgecolors=SURFACE, linewidths=2, zorder=3)
    z = (scores[k] - scores.mean()) / scores.std()
    ax1.annotate(f"best offset {offs[k]:.1f} s\nbit agreement {scores[k]:.3f}, peak_z {z:.1f}",
                 (offs[k], scores[k]), xytext=(12, -6), textcoords="offset points", fontsize=8.5,
                 color=TEXT, va="top")
    ax1.axhline(scores.mean(), color=MUTED, linewidth=1, linestyle=":", label="mean over all offsets")
    ax1.legend(frameon=False, fontsize=8.5, labelcolor=TEXT_2, loc="lower right")
    ax1.set_xlabel(f"offset into reference track {seg['track_id']} (s)")
    ax1.set_ylabel("bit agreement")
    ax1.set_title(f"(a) One 10 s query window from {EXAMPLE_EVENT}, slid across the reference",
                  loc="left", fontsize=11, color=TEXT)

    # (b) peak_z per window across the recording
    style(ax2)
    for s in segs:
        ax2.axvspan(s["recording_start_sec"], s["recording_end_sec"], color=SHADE, zorder=0)
    others = [j for j, tid in enumerate(ws.track_ids) if tid not in {s["track_id"] for s in segs}]
    ax2.plot(t, ws.peak_z[:, others].max(axis=1), color=MUTED, linewidth=1.2,
             label="best of the other 45 tracks")
    for color, s in zip(SERIES, segs):
        j = ws.track_ids.index(s["track_id"])
        ax2.plot(t, ws.peak_z[:, j], color=color, linewidth=2, label=s["track_id"])
        ax2.text((s["recording_start_sec"] + s["recording_end_sec"]) / 2, ws.peak_z.max() + 0.4,
                 f"{s['track_id']} playing", ha="center", fontsize=8.5, color=TEXT_2)
    ax2.axhline(threshold, color=TEXT_2, linewidth=1, linestyle="--", label=f"detection threshold ({threshold})")
    ax2.set_ylim(top=ws.peak_z.max() + 1.2)
    ax2.set_xlabel("time in recording (s)")
    ax2.set_ylabel("peak_z")
    ax2.set_title(f"(b) Per-window match strength across {EXAMPLE_EVENT} ({ev['snr_db']} dB SNR)",
                  loc="left", fontsize=11, color=TEXT)
    ax2.legend(frameon=False, fontsize=9, labelcolor=TEXT_2, loc="upper center", bbox_to_anchor=(0.5, -0.16),
               ncol=2)
    fig.tight_layout()
    fig.savefig(RESULTS / "confidence_by_offset.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    feature_space()
    confidence_by_offset()
    print("Wrote results/feature_space.png and results/confidence_by_offset.png")


if __name__ == "__main__":
    main()
