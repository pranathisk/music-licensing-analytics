#!/usr/bin/env bash
# Rebuild everything from scratch: download, database, both modules, evaluation, report, figures.
# Every number in results/ comes from this script.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-.venv/bin/python}

$PY data/fetch_catalog.py --keep-tags     # catalog + noise clips (skips files already downloaded)
$PY -m db.seed                            # schema + tracks + licenses
$PY -m src.features                       # Module A: librosa features
$PY -m src.scorer --examples              # Module A: results/scoring_examples.json
$PY -m src.fingerprint                    # Module B: Chromaprint fingerprints
$PY -m src.simulate_event                 # Module B: synthetic event recordings + ground truth
$PY -m src.evaluate --tune                # pick threshold on the tune split
$PY -m src.detector                       # detections for every recording -> DB
$PY -m src.evaluate --test --snr-sweep --ablation
$PY -m src.report                         # results/detection_report.json
$PY -m src.plots                          # results/*.png
$PY -m src.readme_results                 # README results section + CREDITS.md
.venv/bin/jupyter nbconvert --to notebook --execute --inplace notebooks/eda.ipynb
$PY -m pytest -q
