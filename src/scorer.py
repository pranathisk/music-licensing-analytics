"""Module A: rank the catalog against a sync-licensing brief.

    python -m src.scorer --tempo 90-120 --energy high --instrumental --tags upbeat,driving
    python -m src.scorer --examples        # regenerate results/scoring_examples.json

How a track is scored
---------------------
1. Numeric fit. For each numeric target in the brief (tempo range, energy level,
   optional brightness level) compute how far the track's feature falls outside
   the target range, in units of that feature's catalog standard deviation.
   Inside the range the gap is 0. A small "centering" term (CENTER_WEIGHT)
   breaks ties among in-range tracks by favouring the middle of the range.
   The weighted mean of those distances, D, becomes fit = exp(-D), so a track
   inside every range scores close to 1.0.
2. Tempo octave ambiguity. Beat trackers often report half or double the
   felt tempo (e.g. 87 BPM for a 174 BPM drum & bass track). Unless
   --no-octave is set, a track whose raw tempo misses the range but whose
   half- or double-time reading lands inside it is scored on that reading,
   and the explanation says so. Otherwise the raw tempo is used.
3. Energy and brightness levels (low/med/high) are catalog terciles of
   energy_rms and spectral_centroid. "high energy" means the top third of this
   catalog, not an absolute loudness.
4. Tags. tag_match = fraction of the brief's mood tags the track carries.
   The final score is 100 * (W_FIT * fit + W_TAGS * tag_match) / (W_FIT + W_TAGS),
   or 100 * fit if the brief has no tags. --strict-tags drops tracks missing
   any requested tag. --instrumental drops tracks not tagged instrumental.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from src.db import ROOT, connect

LEVELS = ("low", "med", "high")
DEFAULT_WEIGHTS = {"tempo": 1.0, "energy": 1.0, "brightness": 0.5, "fit": 0.7, "tags": 0.3}
CENTER_WEIGHT = 0.1
EXAMPLES_PATH = ROOT / "results" / "scoring_examples.json"


@dataclass
class Brief:
    tempo: tuple[float, float] | None = None
    energy: str | None = None          # low / med / high
    brightness: str | None = None      # low / med / high
    tags: list[str] = field(default_factory=list)
    instrumental: bool = False
    strict_tags: bool = False
    octave: bool = True
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


def load_catalog(conn=None) -> pd.DataFrame:
    conn = conn or connect()
    return pd.read_sql_query(
        "SELECT t.track_id, t.title, t.artist, t.license_type, f.tempo_bpm, f.energy_rms,"
        " f.spectral_centroid, f.zero_crossing_rate, f.is_instrumental, f.mood_tags"
        " FROM tracks t JOIN audio_features f USING (track_id)",
        conn,
    )


def level_ranges(values: pd.Series) -> dict[str, tuple[float, float]]:
    """Split a feature into catalog terciles: low / med / high."""
    q1, q2 = values.quantile([1 / 3, 2 / 3])
    return {"low": (values.min(), q1), "med": (q1, q2), "high": (q2, values.max())}


def range_distance(x: float, lo: float, hi: float, scale: float) -> float:
    """Gap outside [lo, hi] plus a small pull toward the centre, in units of `scale`."""
    gap = max(lo - x, 0.0, x - hi)
    centre = abs(x - (lo + hi) / 2)
    return (gap + CENTER_WEIGHT * centre) / scale


def effective_tempo(tempo: float, lo: float, hi: float, octave: bool) -> tuple[float, str]:
    """Use the raw tempo unless only a half/double-time reading lands inside the range."""
    if not octave or lo <= tempo <= hi:
        return tempo, ""
    for alt, note in ((tempo * 2, "double-time"), (tempo / 2, "half-time")):
        if lo <= alt <= hi:
            return alt, note
    return tempo, ""


def score_catalog(df: pd.DataFrame, brief: Brief) -> pd.DataFrame:
    w = {**DEFAULT_WEIGHTS, **brief.weights}
    tags = [t.strip() for t in brief.tags if t.strip()]
    energy_levels = level_ranges(df["energy_rms"])
    bright_levels = level_ranges(df["spectral_centroid"])
    tempo_std = df["tempo_bpm"].std()
    energy_std = df["energy_rms"].std()
    bright_std = df["spectral_centroid"].std()

    rows = []
    for _, r in df.iterrows():
        track_tags = set(filter(None, (r["mood_tags"] or "").split(",")))
        if brief.instrumental and not r["is_instrumental"]:
            continue
        matched = [t for t in tags if t in track_tags]
        if brief.strict_tags and len(matched) < len(tags):
            continue

        dist, wsum, why = 0.0, 0.0, []
        if brief.tempo:
            lo, hi = brief.tempo
            t_eff, note = effective_tempo(r["tempo_bpm"], lo, hi, brief.octave)
            dist += w["tempo"] * range_distance(t_eff, lo, hi, tempo_std)
            wsum += w["tempo"]
            reading = f"{r['tempo_bpm']:.0f} BPM" + (f" read as {t_eff:.0f} ({note})" if note else "")
            if lo <= t_eff <= hi:
                why.append(f"tempo {reading} in {lo:.0f}-{hi:.0f}")
            else:
                off = lo - t_eff if t_eff < lo else t_eff - hi
                why.append(f"tempo {reading} is {off:.0f} BPM {'below' if t_eff < lo else 'above'} range")
        for name, target, value, levels, std in (
            ("energy", brief.energy, r["energy_rms"], energy_levels, energy_std),
            ("brightness", brief.brightness, r["spectral_centroid"], bright_levels, bright_std),
        ):
            if not target:
                continue
            lo, hi = levels[target]
            dist += w[name] * range_distance(value, lo, hi, std)
            wsum += w[name]
            actual = next(lvl for lvl in LEVELS if value <= levels[lvl][1] or lvl == "high")
            unit = f"RMS {value:.3f}" if name == "energy" else f"centroid {value:.0f} Hz"
            why.append(f"{name} {actual} ({unit})" + ("" if actual == target else f", wanted {target}"))

        fit = math.exp(-dist / wsum) if wsum else 1.0
        if tags:
            tag_match = len(matched) / len(tags)
            score = 100 * (w["fit"] * fit + w["tags"] * tag_match) / (w["fit"] + w["tags"])
            why.append(f"tags {len(matched)}/{len(tags)}" + (f" ({', '.join(matched)})" if matched else ""))
        else:
            tag_match = None
            score = 100 * fit
        rows.append({
            "track_id": r["track_id"], "title": r["title"], "artist": r["artist"],
            "license_type": r["license_type"], "score": round(score, 2), "fit": round(fit, 4),
            "tag_match": tag_match, "tempo_bpm": round(r["tempo_bpm"], 1),
            "energy_rms": round(r["energy_rms"], 4), "instrumental": bool(r["is_instrumental"]),
            "mood_tags": r["mood_tags"], "why": "; ".join(why),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["score", "track_id"], ascending=[False, True]).reset_index(drop=True)


def parse_tempo(s: str) -> tuple[float, float]:
    lo, hi = (float(x) for x in s.split("-"))
    if lo > hi:
        raise argparse.ArgumentTypeError("tempo range must be LOW-HIGH")
    return lo, hi


EXAMPLE_BRIEFS = [
    ("High-energy instrumental for a sports promo (the spec's example CLI brief)",
     Brief(tempo=(90, 120), energy="high", instrumental=True, tags=["upbeat", "driving"])),
    ("Calm, dark-ish instrumental bed under a documentary interview",
     Brief(tempo=(60, 100), energy="low", brightness="low", instrumental=True, tags=["ambient", "cinematic"])),
    ("Funky/jazzy background music for a bar, vocals OK",
     Brief(tempo=(95, 115), energy="med", tags=["funky", "jazzy"])),
    ("Acoustic cue for an indie short film (acoustic tag required)",
     Brief(tempo=(80, 110), energy="med", tags=["acoustic"], strict_tags=True)),
]


def brief_to_dict(brief: Brief) -> dict:
    d = asdict(brief)
    d["tempo"] = list(brief.tempo) if brief.tempo else None
    return d


def run_examples(top: int) -> None:
    df = load_catalog()
    out = []
    for desc, brief in EXAMPLE_BRIEFS:
        ranked = score_catalog(df, brief)
        out.append({
            "description": desc,
            "brief": brief_to_dict(brief),
            "candidates_after_filters": len(ranked),
            "top": ranked.head(top).to_dict(orient="records"),
        })
    EXAMPLES_PATH.parent.mkdir(exist_ok=True)
    EXAMPLES_PATH.write_text(json.dumps({
        "generated_by": "python -m src.scorer --examples",
        "catalog_size": len(df),
        "weights": DEFAULT_WEIGHTS,
        "examples": out,
    }, indent=2, default=lambda o: o.item() if isinstance(o, np.generic) else str(o)))
    print(f"Wrote {len(out)} examples to {EXAMPLES_PATH.relative_to(ROOT)}")


def main() -> None:
    p = argparse.ArgumentParser(description="Rank the catalog against a licensing brief.")
    p.add_argument("--tempo", type=parse_tempo, help="BPM range, e.g. 90-120")
    p.add_argument("--energy", choices=LEVELS)
    p.add_argument("--brightness", choices=LEVELS)
    p.add_argument("--tags", default="", help="comma-separated mood tags, e.g. upbeat,driving")
    p.add_argument("--strict-tags", action="store_true", help="drop tracks missing any requested tag")
    p.add_argument("--instrumental", action="store_true", help="only instrumental tracks")
    p.add_argument("--no-octave", action="store_true", help="don't consider half/double-time tempo readings")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument("--examples", action="store_true", help="write results/scoring_examples.json")
    for k, v in DEFAULT_WEIGHTS.items():
        p.add_argument(f"--w-{k}", type=float, default=v, help=f"weight for {k} (default {v})")
    a = p.parse_args()

    if a.examples:
        run_examples(a.top)
        return
    brief = Brief(
        tempo=a.tempo, energy=a.energy, brightness=a.brightness,
        tags=[t for t in a.tags.split(",") if t], instrumental=a.instrumental,
        strict_tags=a.strict_tags, octave=not a.no_octave,
        weights={k: getattr(a, f"w_{k}") for k in DEFAULT_WEIGHTS},
    )
    ranked = score_catalog(load_catalog(), brief).head(a.top)
    if a.json:
        print(ranked.to_json(orient="records", indent=2))
        return
    if ranked.empty:
        print("No tracks match the brief's filters.")
        return
    for i, r in ranked.iterrows():
        print(f"{i + 1:2d}. {r['score']:6.2f}  {r['title'][:38]:38s} {r['artist'][:16]:16s} [{r['license_type']}]")
        print(f"            {r['why']}")


if __name__ == "__main__":
    main()
