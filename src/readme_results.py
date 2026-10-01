"""Regenerate the results sections of README.md and docs/RESULTS.md from results/.

    python -m src.readme_results

Rewrites everything between the RESULTS:START and RESULTS:END markers: a short summary in
README.md and the full tables in docs/RESULTS.md. Every number is copied from a results
file, never typed by hand. Also writes CREDITS.md.
"""
from __future__ import annotations

import json

from src.db import ROOT

README = ROOT / "README.md"
RESULTS_DOC = ROOT / "docs" / "RESULTS.md"
CREDITS = ROOT / "CREDITS.md"
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"


def load(name: str) -> dict:
    return json.loads((ROOT / "results" / name).read_text())


def pct(x) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def scoring_section() -> list[str]:
    d = load("scoring_examples.json")
    out = ["### Module A: example briefs", "",
           f"From `results/scoring_examples.json` (catalog of {d['catalog_size']} tracks; top 3 shown per brief).", ""]
    for ex in d["examples"]:
        b = ex["brief"]
        parts = []
        if b["tempo"]:
            parts.append(f"tempo {b['tempo'][0]:.0f}-{b['tempo'][1]:.0f}")
        for k in ("energy", "brightness"):
            if b[k]:
                parts.append(f"{k} {b[k]}")
        if b["tags"]:
            parts.append(("required " if b["strict_tags"] else "") + "tags " + ",".join(b["tags"]))
        if b["instrumental"]:
            parts.append("instrumental only")
        out += [f"**{ex['description']}** ({'; '.join(parts)}). {ex['candidates_after_filters']} tracks pass the filters.",
                "", "| # | Score | Track | License | Why |", "|---|---|---|---|---|"]
        for i, r in enumerate(ex["top"][:3], 1):
            out.append(f"| {i} | {r['score']:.2f} | {r['title']} ({r['artist']}) | {r['license_type']} | {r['why']} |")
        out.append("")
    return out


def detection_section() -> list[str]:
    tune = load("threshold_tuning.json")
    ev = load("detector_evaluation.json")
    rep = load("detection_report.json")
    sweep = load("snr_sweep.json")
    abl = load("degradation_ablation.json")
    out = ["### Module B: threshold (tune split only)", "",
           f"Statistic: `peak_z`. Best tune-split F1 = {tune['best_f1']} for thresholds "
           f"{tune['best_range'][0]}-{tune['best_range'][1]}; chosen threshold = **{tune['chosen_threshold']}** "
           "(middle of that range). Full sweep: `results/threshold_tuning.json`.", "",
           "### Module B: detection accuracy", "",
           "| Split | Recordings | TP | FP | FN | Precision | Recall | Offsets within 1 s | Negative controls with no detection |",
           "|---|---|---|---|---|---|---|---|---|"]
    for split in ("test", "tune"):
        m = ev["splits"][split]["metrics"]
        n = len(ev["splits"][split]["events"])
        label = "test (held out)" if split == "test" else "tune (threshold chosen here)"
        out.append(f"| {label} | {n} | {m['true_positives']} | {m['false_positives']} | {m['false_negatives']} | "
                   f"{pct(m['precision'])} | {pct(m['recall'])} | {m['offset_correct']}/{m['true_positives']} | "
                   f"{m['negative_controls_clean']}/{m['negative_controls_total']} |")
    out += ["", "Per-recording results (`results/detector_evaluation.json`):", "",
            "| Recording | SNR (dB) | Expected | Detected (bit agreement / peak_z / offset error s) | Missed |",
            "|---|---|---|---|---|"]
    for split in ("test", "tune"):
        for e in ev["splits"][split]["events"]:
            dets = "; ".join(
                f"{d['track_id']}{'' if d['correct_track'] else ' **(false positive)**'} "
                f"({d['match_confidence']:.3f} / {d['peak_z']:.2f}"
                + (f" / {d['offset_error_sec']:+.2f}" if d["offset_error_sec"] is not None else "") + ")"
                for d in e["detections"]) or "none"
            snr = "noise only" if e["snr_db"] is None else e["snr_db"]
            out.append(f"| {e['key']} | {snr} | {', '.join(e['expected_tracks']) or 'none'} | {dets} | "
                       f"{', '.join(e['missed_tracks']) or '-'} |")
    out += ["", "### Module B: stress test across noise levels", "",
            f"`results/snr_sweep.json`: each of the {len([r for r in sweep['runs'] if r['snr_db'] == 10])} "
            "test-split excerpts re-mixed with crowd + talking noise at each SNR, threshold "
            f"{sweep['threshold']}.", "",
            "| SNR (dB) | Detected | False positives |", "|---|---|---|"]
    for snr, s in sweep["summary_by_snr_db"].items():
        out.append(f"| {snr} | {s['detected']}/{s['of']} | {s['false_positives']} |")
    out += ["", "### Module B: which degradation hurts most", "",
            f"`results/degradation_ablation.json`: {abl['setup']}.", "",
            "| Condition | Mean best bit agreement | Mean peak_z | Best offset = true offset |", "|---|---|---|---|"]
    for name, c in abl["conditions"].items():
        out.append(f"| {name} | {c['mean_best_bit_agreement']:.3f} | {c['mean_peak_z']:.2f} | "
                   f"{c['best_offset_is_true_offset']:.0%} |")
    t = rep["totals"]
    out += ["", "### Module B: flag report", "",
            f"`results/detection_report.json`: {t['detections']} detections across {t['events']} recordings, "
            f"{t['licensed']} licensed, {t['flagged']} flagged (all venues and licenses fictional).", "",
            "| Recording | Venue | Date | Track | Licensed | Reason |", "|---|---|---|---|---|---|"]
    for e in rep["events"]:
        for d in e["detections"]:
            fp = " (false positive)" if d["matches_ground_truth"] is False else ""
            out.append(f"| {e['event_name']} | {e['venue']} | {e['recorded_at'][:10]} | {d['title']}{fp} | "
                       f"{'yes' if d['licensed'] else '**no**'} | {d['reason']} |")
    return out


def write_credits() -> None:
    """CC BY requires attribution: list every track and audio clip with its source and license."""
    import csv
    with open(ROOT / "data" / "catalog_seed.csv") as fh:
        tracks = list(csv.DictReader(fh))
    with open(ROOT / "data" / "noise_sources.csv") as fh:
        extras = list(csv.DictReader(fh))
    lines = ["# Credits", "",
             "All music comes from [ccMixter](https://ccmixter.org). Licenses were read from ccMixter's API",
             "(`data/fetch_catalog.py`). Tracks were not modified except for the excerpting, noise mixing and",
             "re-encoding in `data/audio/simulated_events/`. Generated by `python -m src.readme_results`.", "",
             "## Catalog", "", "| Track ID | Title | Artist | License | Source |", "|---|---|---|---|---|"]
    for t in tracks:
        lines.append(f"| {t['track_id']} | {t['title']} | {t['artist']} | "
                     f"[{t['license_type']}]({t['license_url']}) | {t['source_url']} |")
    lines += ["", "## Audio used only to build the simulated recordings", "",
              "| Use | Artist / work | License | Source |", "|---|---|---|---|"]
    for e in extras:
        lines.append(f"| {e['name']} | {e['artist']} | {e['license']} | {e['page']} |")
    CREDITS.write_text("\n".join(lines) + "\n")


def summary_section() -> list[str]:
    """The short version for the README front page."""
    ev = load("detector_evaluation.json")
    sweep = load("snr_sweep.json")
    ex = load("scoring_examples.json")["examples"][0]
    out = ["**Detector, simulated recordings** (threshold chosen on the tune set only):", "",
           "| Set | Tracks found | Missed | False alarms | No-music controls clean |", "|---|---|---|---|---|"]
    for split, label in (("test", "Test (held out)"), ("tune", "Tune")):
        m = ev["splits"][split]["metrics"]
        out.append(f"| {label} | {m['true_positives']}/{m['true_positives'] + m['false_negatives']} | "
                   f"{m['false_negatives']} | {m['false_positives']} | "
                   f"{m['negative_controls_clean']}/{m['negative_controls_total']} |")
    out += ["", "**Noise stress test**: the same test excerpts re-mixed at each noise level "
            "(SNR = how much louder the music is than the crowd):", "",
            "| SNR (dB) | " + " | ".join(sweep["summary_by_snr_db"]) + " |",
            "|---|" + "---|" * len(sweep["summary_by_snr_db"]),
            "| Detected | " + " | ".join(f"{v['detected']}/{v['of']}" for v in sweep["summary_by_snr_db"].values()) + " |",
            "| False alarms | " + " | ".join(str(v["false_positives"]) for v in sweep["summary_by_snr_db"].values()) + " |",
            "", f"**Scorer, example brief**: {ex['description'].split(' (')[0].lower()}:", ""]
    for i, r in enumerate(ex["top"][:3], 1):
        out.append(f"{i}. **{r['title']}** ({r['score']:.1f}): {r['why']}")
    out += ["", "Full tables (per-recording results, threshold sweep, degradation ablation, flag report): "
            "[docs/RESULTS.md](docs/RESULTS.md)."]
    return out


def replace_block(path, lines: list[str]) -> None:
    text = path.read_text()
    if START not in text or END not in text:
        raise SystemExit(f"{path.name} is missing the RESULTS markers")
    before, rest = text.split(START, 1)
    after = rest.split(END, 1)[1]
    path.write_text(before + "\n".join([START, "", *lines, "", END]) + after)


def main() -> None:
    write_credits()
    replace_block(README, summary_section())
    replace_block(RESULTS_DOC, [*scoring_section(), *detection_section()])
    print("Updated results in README.md and docs/RESULTS.md")

if __name__ == "__main__":
    main()
