# Project details

The full write-up behind the [README](../README.md): catalog and licensing, setup, database, how both
modules work, what was tested, and findings. All result tables are in [RESULTS.md](RESULTS.md).

## Contents

- [Catalog and licensing](#catalog-and-licensing)
- [Setup and reproduction](#setup-and-reproduction)
- [Database](#database)
- [Module A: how the scorer works](#module-a-how-the-scorer-works)
- [Module B: how the detector works](#module-b-how-the-detector-works)
- [What was and wasn't tested](#what-was-and-wasnt-tested)
- [Findings worth knowing about](#findings-worth-knowing-about)
- [Repository layout](#repository-layout)

## Catalog and licensing

- **47 tracks from [ccMixter](https://ccmixter.org), licensed CC BY 2.5, CC BY 3.0 or CC0 only.** No
  NonCommercial, NoDerivatives or commercial tracks appear anywhere in this repo. Each track's license
  is read from ccMixter's API (`license_url`) and stored in `tracks.license_type`, never typed by hand.
  Full attribution is in [CREDITS.md](../CREDITS.md).
- The catalog was picked by hand from ccMixter queries across genres (ambient, hip hop, rock,
  electronic, funk, jazz, acoustic, dubstep, techno) for variety in tempo and energy. The IDs are in
  `data/catalog_ids.txt`.
- **Background noise for the simulated recordings:** a CC0 large-crowd recording (eguobyte, via
  Freesound and Wikimedia Commons) and a public-domain recording of a busy cafeteria (aradlaw, via
  pdsounds.org and Wikimedia Commons). One extra CC BY 3.0 ccMixter track that is **not** in the
  catalog is used as a distractor in the negative controls.
- **Tags are rule-derived, not hand-listened.** `is_instrumental` and `mood_tags` come from fixed
  rules applied to each uploader's own ccMixter tags (`MOOD_RULES` in `data/fetch_catalog.py`). For
  example, `dnb`, `techno` or `rock` maps to `driving`, and an `instrumental` tag means instrumental.
  No vocal-detection model is used, and nobody listened to every track. A track with no tag either way
  is treated as possibly having vocals. You can edit `data/catalog_seed.csv` to override any tag;
  `fetch_catalog.py --keep-tags` preserves those edits.
- **Audio storage.** The catalog is about 265 MB, so `data/audio/raw/` and `data/audio/noise/` are
  gitignored. `data/fetch_catalog.py` downloads them again. The simulated recordings are only about
  8.5 MB and are committed, so you can listen to what the detector heard.
- **About "unlicensed."** CC BY and CC0 already allow public performance (CC BY with attribution).
  The `licenses` table is a fictional licensing setup layered on top so the flag logic has something
  to check against. A "flagged" result here means "no matching row in a fictional table," not
  infringement.

## Setup and reproduction

Requires Python 3.12 (tested), plus two command-line tools: `fpcalc` (Chromaprint 1.6) and `ffmpeg`.
`requirements.txt` has what the app and pipeline need. `requirements-dev.txt` adds pytest and the
notebook tooling.

**Just the app, no downloads:** the database (~1 MB), the simulated recordings and 30 s preview clips
are committed, so `pip install -r requirements.txt && streamlit run app.py` works on a fresh clone as
long as `fpcalc` and `ffmpeg` are installed.

```bash
brew install chromaprint ffmpeg            # macOS; on Debian/Ubuntu: apt install libchromaprint-tools ffmpeg
python3.12 -m venv .venv                   # or: uv venv --python 3.12 .venv
.venv/bin/pip install -r requirements-dev.txt   # app + pipeline + tests/notebook
./run_pipeline.sh                               # downloads, builds the DB, runs both modules, tests
```

`run_pipeline.sh` runs every step in order, and every file in `results/` comes from it. On an M-series
Mac it takes about 2 minutes, plus download time on the first run. You can also run the steps one at
a time:

| Step | Command | Output |
|---|---|---|
| Download catalog and noise | `python data/fetch_catalog.py --keep-tags` | `data/audio/raw/`, `data/catalog_seed.csv` |
| Schema, tracks, licenses | `python -m db.seed` | `db/licensing.db` |
| Module A features | `python -m src.features` | `audio_features` table |
| Module A examples | `python -m src.scorer --examples` | `results/scoring_examples.json` |
| Fingerprints | `python -m src.fingerprint` | `fingerprints` table |
| Simulated recordings | `python -m src.simulate_event` | `data/audio/simulated_events/` |
| Threshold tuning | `python -m src.evaluate --tune` | `results/threshold_tuning.json` |
| Detection | `python -m src.detector` | `usage_events`, `detections` tables |
| Evaluation | `python -m src.evaluate --test --snr-sweep --ablation` | `results/detector_evaluation.json`, `snr_sweep.json`, `degradation_ablation.json` |
| Flag report | `python -m src.report` | `results/detection_report.json` |
| Figures | `python -m src.plots` | `results/*.png` |
| Preview clips for the app | `python -m src.previews` | `data/audio/previews/` |
| Results write-up and credits | `python -m src.readme_results` | README summary, `docs/RESULTS.md`, `CREDITS.md` |
| Tests | `python -m pytest` | |

**Web app.** `app.py` is a Streamlit front end for both modules:

```bash
.venv/bin/streamlit run app.py      # opens http://localhost:8501
```

- **Find a track:** set a brief (tempo, energy, brightness, mood tags, instrumental). The ranked
  matches come with explanations, audio previews and a feature-space chart.
- **Check a recording:** pick one of the simulated venue recordings or upload your own audio file,
  choose a venue and date, and see which catalog tracks were detected, their match strength over time,
  and whether each use is licensed.
- **Flag report:** the committed `results/detection_report.json` plus the accuracy and noise-sweep
  numbers.

The app only reads the database and never writes detections, so `results/` stays the output of
`run_pipeline.sh`. Without the full catalog audio (for example when deployed), the app plays the
committed 30 s previews in `data/audio/previews/`.

**Deploying (Streamlit Community Cloud).** `packages.txt` installs `ffmpeg` and `libchromaprint-tools`
(which provides `fpcalc`), and `requirements.txt` installs the Python packages. Choose Python 3.12 under
*Advanced settings* when deploying.

Try the scorer from the command line:

```bash
.venv/bin/python -m src.scorer --tempo 90-120 --energy high --instrumental --tags upbeat,driving
```

Run the detector on any audio file (it isn't evaluated unless you add ground truth):

```bash
.venv/bin/python -m src.detector --recording path/to/clip.mp3 --venue "Venue A (fictional)" --date 2026-03-14
.venv/bin/python -m src.report
```

The test suite includes a check that `fpcalc -raw` produces exactly the same fingerprint as
pyacoustid's `fingerprint_file` + `chromaprint.decode_fingerprint`. It's skipped unless libchromaprint
can be loaded; on macOS with Homebrew, run
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python -m pytest` to include it.

## Database

SQLite through Python's built-in `sqlite3` module, with no ORM. The schema in `db/schema.sql` sticks to
portable SQL (`TEXT`, `REAL`, `INTEGER`, `BOOLEAN`, `DATE`, `TIMESTAMP`, plain foreign keys), so moving
to Postgres means changing the connection in `src/db.py`, not the schema.

| Table | Written by | Contents |
|---|---|---|
| `tracks` | `db/seed.py` | title, artist, ccMixter URL, license type/URL, file path, decoded duration |
| `audio_features` | `src/features.py` | tempo, RMS energy, spectral centroid, zero-crossing rate, instrumental flag, mood tags |
| `licenses` | `db/seed.py` from `data/licenses_seed.csv` | fictional licensee, scope, start/end date, status (`active`/`expired`/`none`) |
| `fingerprints` | `src/fingerprint.py` | JSON array of raw 32-bit Chromaprint sub-fingerprints, hash rate |
| `usage_events` | `src/detector.py` | recording name, venue, path, date |
| `detections` | `src/detector.py`, `src/report.py` | track, bit agreement, peak_z, offsets, supporting windows, licensed flag |

Changes from the original spec's schema: `tracks.license_url`, `usage_events.venue` (so a license can
be checked against the venue that holds it), and `detections.peak_z`, `recording_start_sec` and
`matched_windows`.

## Module A: how the scorer works

**Features** (`src/features.py`). Each track is decoded to mono 22,050 Hz and summarised by librosa:
global tempo (`beat.beat_track`), mean RMS energy, mean spectral centroid ("brightness") and mean
zero-crossing rate. These are measured from the audio, not looked up from a service.

**Scoring** (`src/scorer.py`):

1. For each numeric target in the brief, measure how far the track falls *outside* the target range,
   in units of that feature's standard deviation across the catalog. Inside the range the distance is
   0, plus a small pull toward the middle of the range to break ties. Energy and brightness levels
   (`low`/`med`/`high`) are catalog terciles, so "high energy" means the loudest third of this catalog.
2. The weighted mean distance `D` becomes `fit = exp(-D)`.
3. `score = 100 × (0.7 × fit + 0.3 × tag_match) / (0.7 + 0.3)`, where `tag_match` is the fraction of
   requested mood tags the track has. All weights can be changed with `--w-*` flags.
4. `--instrumental` and `--strict-tags` are hard filters.
5. **Tempo octave errors.** Beat trackers often report half or double the felt tempo. In the notebook,
   comparing librosa to the BPM uploaders entered on ccMixter finds 7 octave errors among the 42 tracks that have one (see
   `notebooks/eda.ipynb`). If a track's raw tempo misses the range but its half- or double-time
   reading lands inside it, the scorer uses that reading and says so in the explanation (for example
   "185 BPM read as 92 (half-time)"). `--no-octave` turns this off.

Zero-crossing rate is stored and explored, but it isn't part of the brief: it correlates strongly with
spectral centroid in this catalog (see the notebook), so it would mostly duplicate "brightness".

## Module B: how the detector works

This is the most technically interesting part, so here it is step by step.

**Fingerprints** (`src/fingerprint.py`). Chromaprint (`fpcalc -raw -length 0`) turns audio into a
sequence of 32-bit integers, one every 1365 samples at 11,025 Hz (about 8.08 per second). Each bit
records whether a particular filtered chroma (pitch-class energy) feature is rising or falling. Two
recordings of the same audio produce sub-fingerprints that agree in most bits even after compression
or noise. Unrelated music agrees in about half (the notebook measures this). All matching runs
locally: the AcoustID web service only knows commercially registered releases, and none of this
catalog is among them.

**Sliding-window matching** (`src/detector.py`):

1. Fingerprint the event recording the same way.
2. **Cut it into windows**: 10 s long, a new one every 2.5 s. Short windows mean a recording can hold
   several tracks, or talking before and after a track, without diluting the score.
3. **Slide each window across each reference**, one sub-fingerprint at a time. At each offset, XOR the
   aligned integers and count the differing bits (Hamming distance):
   `bit agreement = 1 − differing_bits / (32 × window_length)`.
   Keep the best offset. This is vectorised with numpy's `sliding_window_view` and `bitwise_count`, so
   47 references × about 40 windows takes well under a second per recording.
4. **Normalise each peak (`peak_z`).** Raw bit agreement isn't comparable across references. Against
   crowd noise, some references (especially quiet or textural ones) give much wider score
   distributions than others, so their best offset reaches high agreement by chance. Each window's best
   score is therefore expressed as standard deviations above the mean of that same reference's other
   offsets: `peak_z = (best − mean) / std`. A real match is a sharp peak. A lucky match on a
   low-information reference isn't.
5. **Require agreement on alignment.** While a track plays, every window that matches it implies the
   same alignment (reference time minus recording time). Windows with `peak_z` ≥ threshold are grouped
   by alignment (±0.5 s). A track counts as detected only when one group has **at least two windows
   that don't overlap each other**. Overlapping windows share audio, so they don't count as
   independent evidence. Plot (b) below shows why this step is needed: individual windows of unrelated
   tracks cross the threshold regularly, but they rarely agree on an alignment. That happened once in
   this whole evaluation (`tune_neg_01` in the results).
6. **Report.** `match_confidence` is the best bit agreement in the winning group (the fraction of bits
   that agree at the best offset), with `peak_z`, the matched position in the reference, and where in
   the recording the match starts.

![confidence by offset](../results/confidence_by_offset.png)

**Simulated recordings** (`src/simulate_event.py`). Each one takes a mid-track excerpt (so the
detector has to find a non-zero offset), adds room reverb and a random playback level, places it
after a stretch of noise-only lead-in, mixes in crowd and/or talking noise at a set signal-to-noise
ratio, band-limits it to 100–7000 Hz like a small phone mic, and encodes it as a 64 kbit/s mono MP3.
Two recordings contain two tracks crossfaded like a DJ set, and one contains only 20 s of a track.
Negative controls are noise only, or noise plus the non-catalog CC track. Ground truth is in
`data/audio/simulated_events/ground_truth.json`. All of it is seeded, so it's reproducible.

**Threshold selection without peeking.** The recordings are split into a **tune** set (7 recordings)
and a **test** set (7 recordings) that share no catalog tracks. The threshold is chosen only on the
tune set (best F1, middle of the widest tied range of thresholds). The test set is scored once with
that threshold. The minimum of two supporting windows was fixed by design beforehand, not tuned.

**Flag report** (`src/report.py`). A detection is licensed only if `licenses` has a row for that
track, held by the event's venue, with status `active`, whose date range covers the event date.
Otherwise it's flagged with the reason: expired, not started yet, status `none`, or no license on
file.

## What was and wasn't tested

**Tested (all synthetic):**

- 14 simulated recordings: 10 containing catalog music (12 track appearances in total, including one
  deliberately extreme −5 dB stress recording in the tune set) and 4 negative controls (2 noise-only,
  2 with non-catalog music).
- Real recorded crowd and conversation noise, synthetic room reverb, phone-style band-limiting,
  64 kbit/s MP3, excerpts from 20 s to 60 s, two-track DJ crossfades, offsets well into the track.
- An SNR sweep from +10 to −10 dB, and an ablation of each degradation on its own.

**Not tested:**

- Real phone recordings at real venues: real PA systems, room acoustics, distance, clipping, and
  phone automatic gain control or noise suppression.
- Music that is time-stretched, pitch-shifted, live-performed (a cover band playing a catalog song)
  or heavily EQ'd by a DJ. Chromaprint isn't designed for these and would likely fail.
- A catalog bigger than 47 tracks. False-positive risk rises with catalog size, because every extra
  reference is another chance for a lucky peak. Real systems use an indexed lookup over millions of
  tracks rather than the brute-force comparison used here.
- Any statistically meaningful sample. The test set has 6 catalog-track appearances and 2 negative
  controls. A perfect score on it is a smoke test, not an accuracy estimate. The tune split, where the
  threshold was fit, still has one miss and one false positive (see the per-recording table in [RESULTS.md](RESULTS.md)).

## Findings worth knowing about

- **Threshold margin is thin.** On the tune split, a noise-only recording produced a false detection
  at the chosen threshold. Raising the threshold enough to remove it also lost a true detection. The
  SNR sweep shows the same trade-off: detection falls off quickly below 5 dB, and at −10 dB the
  detector produced one false positive. In practice, the alignment-agreement rule does more than the
  threshold does.
- **The phone-mic simulation mattered more than the noise.** A first draft cut everything below
  150 Hz with a steep filter. That one change lowered bit agreement more than crowd noise at 0 dB
  SNR did (see the ablation table in [RESULTS.md](RESULTS.md)), because Chromaprint's chroma features rely on bass notes. Real phone mics
  roll off more gently, so the simulation uses a 100 Hz, 2nd-order filter. The rejected version is
  kept in the ablation for comparison.
- **Remixes match each other.** The highest agreement between two "different" catalog tracks is
  "Reflections in the Rain" against "Quiet Rain", because the first is a ccMixter remix of the second.
  For licensing that's a feature, not a bug: playing a remix can mean using the licensed original.
  Neither track appears in the test recordings.
- **Loop-based tracks are positionally ambiguous.** A window of a looping techno track matches its own
  reference almost as well at other repeats of the loop, so the exact offset can be off by a loop
  length even when the track is identified correctly. In the ablation, the best offset isn't the true
  one even for clean audio in a few windows.
- **Rule-derived tags show their limits.** In the "funky/jazzy bar" brief, the top result is a classical
  guitar prelude. It's tagged `jazzy` because its uploader added the ccMixter tag `jazz`. No catalog
  track carries both `funky` and `jazzy`, so no result scores full marks on tags. Hand-tagging would
  fix both problems.
- **One catalog MP3 is slightly corrupt.** ccm_57952 has a damaged frame near its end. fpcalc
  fingerprints everything up to that point, and the pipeline logs a warning.
- **MP3 headers can lie.** One file's header claimed 227 s of audio, but it decodes to 165 s, so
  durations are measured by decoding.



## Repository layout

```
├── README.md, CREDITS.md, run_pipeline.sh, pytest.ini
├── requirements.txt, requirements-dev.txt, packages.txt   # Python deps, dev deps, apt deps for deployment
├── app.py                         # Streamlit web app for both modules
├── docs/                          # DETAILS.md (this file), RESULTS.md (generated tables)
├── data/
│   ├── fetch_catalog.py           # downloads catalog + noise from ccMixter / Wikimedia Commons
│   ├── catalog_ids.txt            # the 47 ccMixter upload IDs
│   ├── catalog_seed.csv           # track_id, title, artist, source_url, license_type, tags, declared_bpm
│   ├── licenses_seed.csv          # fictional licenses
│   ├── noise_sources.csv          # noise clips + distractor track, with licenses
│   └── audio/
│       ├── raw/                   # catalog MP3s (gitignored, downloaded)
│       ├── noise/                 # noise clips (gitignored, downloaded)
│       ├── previews/              # 30 s preview of each track for the web app (committed)
│       └── simulated_events/      # 14 simulated recordings + ground_truth.json (committed)
├── db/
│   ├── schema.sql
│   └── seed.py
├── src/
│   ├── db.py                      # shared connection helpers
│   ├── features.py, scorer.py     # Module A
│   ├── fingerprint.py, simulate_event.py, detector.py, report.py   # Module B
│   ├── evaluate.py                # threshold tuning, test metrics, SNR sweep, ablation
│   ├── plots.py, readme_results.py, previews.py
├── notebooks/eda.ipynb            # feature distributions, tempo check, fingerprint sanity checks
├── results/                       # every number and figure, from run_pipeline.sh
└── tests/                         # features, scorer, detector (incl. true negatives), report
```
