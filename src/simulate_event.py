"""Build synthetic "event recordings" that simulate a phone recording at a venue.

    python -m src.simulate_event

For each configured event:
  1. Cut an excerpt from the middle of one or more catalog tracks (so the
     detector has to find a non-zero offset into the reference), crossfading
     consecutive tracks like a DJ set.
  2. Add light room reverb and a random playback gain to the music.
  3. Place it inside a longer recording that starts and ends with background
     noise only, so the music does not begin at t = 0.
  4. Mix in real background noise (a CC0 crowd recording and/or a public-domain
     recording of people talking, see data/noise_sources.csv) at a set
     signal-to-noise ratio, measured over the part where music plays.
  5. Band-limit the mix to 100-7000 Hz (small phone mic) and encode it as
     64 kbit/s mono MP3 with ffmpeg.

Two kinds of negative control are built: noise only, and noise mixed with a
CC-licensed track that is NOT in the catalog (by an artist who does have
tracks in the catalog, so it's a stylistically close distractor).

Events are split into a "tune" set (used only to pick the detection threshold)
and a "test" set (used only to report precision/recall), with no track shared
between the two. Ground truth goes to data/audio/simulated_events/ground_truth.json.
Everything is seeded, so rerunning reproduces the same files.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

from src.db import ROOT, connect, resolve

SR = 22050
OUT_DIR = ROOT / "data" / "audio" / "simulated_events"
GROUND_TRUTH = OUT_DIR / "ground_truth.json"
NOISE_DIR = ROOT / "data" / "audio" / "noise"
NOISE_FILES = {"crowd": "crowd_eguobyte_360703.wav", "chatter": "cafeteria_chatter_aradlaw.ogg"}
NON_CATALOG = NOISE_DIR / "noncatalog_ccm_30239.mp3"
CROSSFADE_SEC = 3.0
MP3_BITRATE = "64k"
# Small phone mic: gentle roll-off below ~100 Hz and above ~7 kHz.
PHONE_BAND_HZ = (100, 7000)
PHONE_FILTER_ORDER = 2


@dataclass
class Segment:
    track_id: str          # catalog track id, or "noncatalog" for the distractor
    ref_start_sec: float   # where in the source track the excerpt starts
    length_sec: float


@dataclass
class EventSpec:
    key: str
    name: str
    venue: str
    recorded_at: str
    split: str             # "tune" or "test"
    segments: list[Segment]
    snr_db: float | None   # None for noise-only recordings
    noise: tuple[str, ...]
    lead_sec: float = 10.0
    tail_sec: float = 10.0
    noise_only_sec: float = 0.0
    seed: int = 0
    notes: str = ""
    extra: dict = field(default_factory=dict)


EVENTS = [
    # ---- test split: the only events precision/recall are reported on ----
    EventSpec("test_01", "Simulated Venue Recording 1", "Venue A (fictional)", "2026-03-14 21:30:00", "test",
              [Segment("ccm_30513", 45, 60)], 10, ("crowd",), lead_sec=15, seed=101,
              notes="single techno track, moderate crowd noise"),
    EventSpec("test_02", "Simulated Venue Recording 2 (DJ set)", "Venue A (fictional)", "2026-03-21 23:00:00", "test",
              [Segment("ccm_33345", 30, 50), Segment("ccm_43098", 60, 50)], 5, ("crowd", "chatter"), lead_sec=8,
              seed=102, notes="two tracks back to back with a 3 s crossfade"),
    EventSpec("test_03", "Simulated Venue Recording 3 (quiet piano under talking)", "Venue B (fictional)",
              "2026-04-02 19:15:00", "test", [Segment("ccm_2905", 20, 60)], 0, ("chatter",), lead_sec=20,
              seed=103, notes="quiet ambient piano at 0 dB SNR against conversation"),
    EventSpec("test_04", "Simulated Venue Recording 4", "Venue B (fictional)", "2026-04-10 20:45:00", "test",
              [Segment("ccm_31670", 90, 45)], 5, ("crowd", "chatter"), lead_sec=12, seed=104,
              notes="downtempo track with vocals"),
    EventSpec("test_05", "Simulated Venue Recording 5 (short excerpt)", "Venue C (fictional)", "2026-05-01 22:10:00",
              "test", [Segment("ccm_54335", 40, 20)], 5, ("crowd",), lead_sec=25, seed=105,
              notes="only 20 s of the track is audible"),
    EventSpec("test_neg_01", "Negative control: crowd and talking only", "Venue A (fictional)", "2026-03-28 22:00:00",
              "test", [], None, ("crowd", "chatter"), noise_only_sec=90, seed=106,
              notes="no music at all"),
    EventSpec("test_neg_02", "Negative control: non-catalog CC track", "Venue C (fictional)", "2026-05-08 21:00:00",
              "test", [Segment("noncatalog", 30, 60)], 5, ("crowd",), lead_sec=10, seed=107,
              notes="music that is NOT in the catalog (Alex Beroza - Emerge In Love, CC BY 3.0), "
                    "same artist as three catalog tracks"),
    # ---- tune split: used only to choose the detection threshold ----
    EventSpec("tune_01", "Tuning Recording 1", "Venue A (fictional)", "2026-02-07 21:00:00", "tune",
              [Segment("ccm_29630", 30, 50)], 10, ("crowd",), seed=201),
    EventSpec("tune_02", "Tuning Recording 2", "Venue B (fictional)", "2026-02-08 21:00:00", "tune",
              [Segment("ccm_17432", 50, 50)], 5, ("chatter",), seed=202),
    EventSpec("tune_03", "Tuning Recording 3", "Venue C (fictional)", "2026-02-09 21:00:00", "tune",
              [Segment("ccm_64427", 20, 40), Segment("ccm_26157", 60, 40)], 0, ("crowd", "chatter"), seed=203),
    EventSpec("tune_04", "Tuning Recording 4 (stress: -5 dB SNR)", "Venue B (fictional)", "2026-02-10 21:00:00",
              "tune", [Segment("ccm_58268", 30, 50)], -5, ("crowd", "chatter"), seed=204),
    EventSpec("tune_05", "Tuning Recording 5", "Venue C (fictional)", "2026-02-11 21:00:00", "tune",
              [Segment("ccm_4209", 100, 45)], 5, ("crowd",), seed=205),
    EventSpec("tune_neg_01", "Tuning negative: crowd and talking only", "Venue A (fictional)", "2026-02-12 21:00:00",
              "tune", [], None, ("crowd", "chatter"), noise_only_sec=90, seed=206),
    EventSpec("tune_neg_02", "Tuning negative: non-catalog CC track", "Venue B (fictional)", "2026-02-13 21:00:00",
              "tune", [Segment("noncatalog", 140, 50)], 5, ("chatter",), seed=207),
]


def decode(path, sr: int = SR, offset: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Decode any audio file to mono float32 at `sr` using ffmpeg."""
    cmd = ["ffmpeg", "-v", "error", "-ss", str(offset), "-i", str(path)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-12))


def room_reverb(x: np.ndarray, rng: np.random.Generator, rt60: float = 0.5, wet: float = 0.25) -> np.ndarray:
    """Cheap room simulation: convolve with exponentially decaying noise."""
    n = int(rt60 * SR)
    t = np.arange(n) / SR
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    ir /= np.sqrt(np.sum(ir ** 2))
    tail = fftconvolve(x, ir)[: len(x)]
    return (1 - wet) * x + wet * tail * rms(x) / max(rms(tail), 1e-9)


def noise_bed(kinds: tuple[str, ...], length: int, rng: np.random.Generator) -> np.ndarray:
    """Loop each noise clip from a random start to `length` samples and sum them at equal level."""
    bed = np.zeros(length, dtype=np.float64)
    for kind in kinds:
        clip = decode(NOISE_DIR / NOISE_FILES[kind])
        clip = clip / rms(clip)
        start = int(rng.integers(0, len(clip)))
        reps = int(np.ceil((start + length) / len(clip))) + 1
        bed += np.tile(clip, reps)[start:start + length]
    return bed / rms(bed)


def build_music(spec: EventSpec, rng: np.random.Generator, track_paths: dict[str, str]):
    """Concatenate segments with crossfades; return audio and per-segment ground truth."""
    fade = int(CROSSFADE_SEC * SR)
    music = np.zeros(0, dtype=np.float64)
    truth = []
    for seg in spec.segments:
        path = NON_CATALOG if seg.track_id == "noncatalog" else resolve(track_paths[seg.track_id])
        clip = decode(path, offset=seg.ref_start_sec, duration=seg.length_sec).astype(np.float64)
        clip *= 10 ** (rng.uniform(-3, 3) / 20)  # playback level varies per track
        start = len(music) if not len(music) else len(music) - fade
        if len(music):
            ramp = np.linspace(0, 1, fade)
            music[-fade:] = music[-fade:] * (1 - ramp) + clip[:fade] * ramp
            music = np.concatenate([music, clip[fade:]])
        else:
            music = clip
        truth.append({
            "track_id": seg.track_id,
            "in_catalog": seg.track_id != "noncatalog",
            "ref_start_sec": seg.ref_start_sec,
            "recording_start_sec": round(spec.lead_sec + start / SR, 3),
            "recording_end_sec": round(spec.lead_sec + (start + len(clip)) / SR, 3),
        })
    return room_reverb(music, rng), truth


def build_event(spec: EventSpec, track_paths: dict[str, str]) -> dict:
    rng = np.random.default_rng(spec.seed)
    if spec.segments:
        music, truth = build_music(spec, rng, track_paths)
        lead, tail = int(spec.lead_sec * SR), int(spec.tail_sec * SR)
        total = lead + len(music) + tail
        bed = noise_bed(spec.noise, total, rng)
        # Scale noise so that music RMS / noise RMS over the music section = SNR.
        noise_gain = rms(music) / 10 ** (spec.snr_db / 20)
        mix = bed * noise_gain
        mix[lead:lead + len(music)] += music
    else:
        truth = []
        total = int(spec.noise_only_sec * SR)
        mix = noise_bed(spec.noise, total, rng) * 0.1

    sos = butter(PHONE_FILTER_ORDER, PHONE_BAND_HZ, btype="bandpass", fs=SR, output="sos")
    mix = sosfilt(sos, mix)
    mix = (0.9 * mix / np.max(np.abs(mix))).astype(np.float32)

    out_path = OUT_DIR / f"{spec.key}.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE, str(out_path)],
        input=mix.tobytes(), check=True,
    )
    return {
        "key": spec.key,
        "event_name": spec.name,
        "venue": spec.venue,
        "recorded_at": spec.recorded_at,
        "split": spec.split,
        "recording_path": str(out_path.relative_to(ROOT)),
        "duration_sec": round(total / SR, 3),
        "snr_db": spec.snr_db,
        "noise": list(spec.noise),
        "mp3_bitrate": MP3_BITRATE,
        "kind": "positive" if any(t["in_catalog"] for t in truth) else "negative_control",
        "segments": truth,
        "notes": spec.notes,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    track_paths = {r["track_id"]: r["file_path"] for r in conn.execute("SELECT track_id, file_path FROM tracks")}
    tune_tracks = {s.track_id for e in EVENTS if e.split == "tune" for s in e.segments}
    test_tracks = {s.track_id for e in EVENTS if e.split == "test" for s in e.segments}
    overlap = (tune_tracks & test_tracks) - {"noncatalog"}
    assert not overlap, f"tracks shared between tune and test splits: {overlap}"

    events = []
    for spec in EVENTS:
        info = build_event(spec, track_paths)
        events.append(info)
        tracks = ", ".join(s["track_id"] for s in info["segments"]) or "none"
        print(f"{spec.key:12s} {info['duration_sec']:6.1f}s  snr={spec.snr_db}  tracks: {tracks}")
    GROUND_TRUTH.write_text(json.dumps({
        "generated_by": "python -m src.simulate_event",
        "sample_rate": SR,
        "events": events,
    }, indent=2))
    print(f"Wrote {len(events)} recordings and {GROUND_TRUTH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
