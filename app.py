"""Streamlit front end for both modules.

    .venv/bin/streamlit run app.py

Tabs:
  Find a track       Module A: brief -> ranked catalog, with audio previews
  Check a recording  Module B: pick a simulated recording or upload your own -> detections + license check
  Flag report        the committed results/detection_report.json and evaluation numbers
  About              what this is and isn't

The app only reads the database; it never writes detections, so results/ stays the
single source of truth produced by run_pipeline.sh.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from src.db import DB_PATH, ROOT, connect, resolve
from src.detector import GROUND_TRUTH, HOP_SEC, MIN_WINDOWS, WINDOW_SEC, detect, load_threshold, score_windows
from src.fingerprint import HASH_RATE_HZ, fingerprint_file, load_fingerprints
from src.report import DISCLAIMER, license_status
from src.scorer import LEVELS, Brief, load_catalog, score_catalog

st.set_page_config(page_title="Music Licensing Analytics", page_icon="🎵", layout="wide")

BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#8a8985"


# ---------- cached data ----------

@st.cache_data
def catalog() -> pd.DataFrame:
    df = load_catalog(connect())
    paths = pd.read_sql_query("SELECT track_id, file_path, source_url, duration_sec FROM tracks", connect())
    return df.merge(paths, on="track_id")


@st.cache_resource
def references():
    return load_fingerprints(connect())


@st.cache_data
def venues() -> list[str]:
    rows = connect().execute("SELECT DISTINCT licensee FROM licenses ORDER BY licensee").fetchall()
    return [r[0] for r in rows]


@st.cache_data
def simulated_events() -> list[dict]:
    return json.loads(GROUND_TRUTH.read_text())["events"] if GROUND_TRUTH.exists() else []


@st.cache_data(show_spinner=False)
def fingerprint_path(path: str):
    return fingerprint_file(path)


def load_json(name: str) -> dict | None:
    p = ROOT / "results" / name
    return json.loads(p.read_text()) if p.exists() else None


if not DB_PATH.exists():
    st.error("Database not found. Run `./run_pipeline.sh` first.")
    st.stop()

df = catalog()
titles = df.set_index("track_id")["title"].to_dict()

st.title("Music Licensing Analytics")
st.caption("Sync-licensing scorer and unlicensed-use detector over a 47-track Creative Commons catalog. "
           "Portfolio demo: fictional venues and licenses, simulated recordings.")

tab_find, tab_check, tab_report, tab_about = st.tabs(["Find a track", "Check a recording", "Flag report", "About"])

# ---------- Module A ----------

with tab_find:
    left, right = st.columns([1, 2.4], gap="large")
    with left:
        st.subheader("Creative brief")
        use_tempo = st.checkbox("Target tempo", value=True)
        tempo = st.slider("Tempo range (BPM)", 50, 200, (90, 120), disabled=not use_tempo)
        energy = st.selectbox("Energy", ["any", *LEVELS], index=3,
                              help="Catalog thirds of mean RMS energy: 'high' = loudest third of this catalog")
        brightness = st.selectbox("Brightness", ["any", *LEVELS], index=0,
                                  help="Catalog thirds of spectral centroid")
        all_tags = sorted({t for tags in df["mood_tags"].fillna("") for t in tags.split(",") if t})
        tags = st.multiselect("Mood tags", all_tags, default=["upbeat", "driving"])
        strict = st.checkbox("Require every tag", value=False)
        instrumental = st.checkbox("Instrumental only", value=True)
        octave = st.checkbox("Allow half/double-time tempo readings", value=True,
                             help="Beat trackers often report half or double the felt tempo")
        top_n = st.slider("Show top", 3, 20, 8)

    brief = Brief(tempo=tuple(map(float, tempo)) if use_tempo else None,
                  energy=None if energy == "any" else energy,
                  brightness=None if brightness == "any" else brightness,
                  tags=tags, instrumental=instrumental, strict_tags=strict, octave=octave)
    ranked = score_catalog(df, brief)

    with right:
        st.subheader("Ranked matches")
        if ranked.empty:
            st.info("No tracks pass these filters. Try fewer required tags or turn off 'Instrumental only'.")
        else:
            st.caption(f"{len(ranked)} of {len(df)} tracks pass the filters.")
            top = ranked.head(top_n).merge(df[["track_id", "file_path", "source_url"]], on="track_id")
            for i, r in top.iterrows():
                with st.container(border=True):
                    c1, c2 = st.columns([3, 2])
                    c1.markdown(f"**{i + 1}. {r['title']}** · {r['artist']}  \n"
                                f"<span style='color:{GRAY}'>{r['why']}</span>", unsafe_allow_html=True)
                    c1.caption(f"License: {r['license_type']} · [source]({r['source_url']})")
                    c2.metric("Score", f"{r['score']:.1f}")
                    path = resolve(r["file_path"])
                    if path.exists():
                        c2.audio(str(path))

            st.subheader("Where the matches sit")
            plot_df = df.assign(
                group=lambda d: d["track_id"].map(
                    lambda t: "top matches" if t in set(top["track_id"]) else "rest of catalog"))
            chart = alt.Chart(plot_df).mark_circle(size=80, stroke="white", strokeWidth=1).encode(
                x=alt.X("tempo_bpm:Q", title="tempo (BPM)", scale=alt.Scale(zero=False)),
                y=alt.Y("energy_rms:Q", title="mean RMS energy"),
                color=alt.Color("group:N", scale=alt.Scale(domain=["top matches", "rest of catalog"],
                                                          range=[BLUE, "#c9c8c3"]), legend=alt.Legend(title=None)),
                tooltip=["title", "artist", alt.Tooltip("tempo_bpm:Q", format=".0f"),
                         alt.Tooltip("energy_rms:Q", format=".3f"), "mood_tags"],
            ).properties(height=340)
            st.altair_chart(chart, width="stretch")

# ---------- Module B ----------

with tab_check:
    threshold = load_threshold()
    st.subheader("Which catalog tracks were played in this recording?")
    st.caption(f"Sliding-window Chromaprint matching: {WINDOW_SEC:.0f} s windows every {HOP_SEC} s, "
               f"peak_z threshold {threshold} (tuned on the tune split), at least {MIN_WINDOWS} "
               "non-overlapping windows agreeing on alignment.")

    events = simulated_events()
    source = st.radio("Recording", ["Simulated venue recording", "Upload my own"], horizontal=True)
    rec_path, truth, default_venue, default_date = None, None, venues()[0], date(2026, 3, 14)
    if source == "Simulated venue recording":
        labels = {f"{e['event_name']}  ({e['split']} set)": e for e in events}
        choice = st.selectbox("Pick one", list(labels))
        ev = labels[choice]
        rec_path = resolve(ev["recording_path"])
        truth = ev
        default_venue = ev["venue"]
        default_date = date.fromisoformat(ev["recorded_at"][:10])
        snr = "no music (noise only)" if ev["snr_db"] is None else f"{ev['snr_db']} dB SNR"
        st.caption(f"{ev['duration_sec']:.0f} s · {snr} · noise: {', '.join(ev['noise'])} · {ev['notes']}")
    else:
        up = st.file_uploader("Audio file", type=["mp3", "wav", "ogg", "m4a", "flac", "aac"])
        st.caption("Tip: record a few seconds of a catalog track playing from your speakers. "
                   "Anything not in the 47-track catalog should come back with no detections.")
        if up is not None:
            tmp = Path(tempfile.gettempdir()) / f"mla_upload_{up.name}"
            tmp.write_bytes(up.getvalue())
            rec_path = tmp

    c1, c2 = st.columns(2)
    vlist = venues()
    venue = c1.selectbox("Venue (licensee to check)", vlist,
                         index=vlist.index(default_venue) if default_venue in vlist else 0)
    when = c2.date_input("Event date", value=default_date)

    if rec_path is not None and rec_path.exists():
        st.audio(str(rec_path))
        with st.spinner("Fingerprinting and matching against 47 references..."):
            query = fingerprint_path(str(rec_path))
            ws = score_windows(query, references())
            found = detect(ws, threshold)

        if truth is not None:
            expected = [s["track_id"] for s in truth["segments"] if s["in_catalog"]]
            st.caption("Ground truth: " + (", ".join(f"{titles[t]} ({t})" for t in expected)
                                           if expected else "no catalog track in this recording"))

        if not found:
            st.success("No catalog track detected.")
        for d in found:
            licensed, reason, _ = license_status(connect(), d.track_id, venue, when)
            with st.container(border=True):
                a, b, c, e = st.columns([3, 1, 1, 1])
                a.markdown(f"**{titles[d.track_id]}** ({d.track_id})  \n"
                           f"heard from {d.recording_start_sec:.1f} s in the recording, "
                           f"matching {d.matched_offset_sec:.1f} s into the original")
                b.metric("Bit agreement", f"{d.match_confidence:.3f}")
                c.metric("peak_z", f"{d.peak_z:.2f}")
                e.metric("Windows", d.matched_windows)
                if licensed:
                    st.success(f"Licensed: {reason}")
                else:
                    st.error(f"Flagged: {reason}")
                if truth is not None and d.track_id not in expected:
                    st.warning("This detection is NOT in the ground truth: a false positive.")

        st.markdown("**Match strength over time** (each point is one 10 s window)")
        t_mid = ws.window_start_sec + ws.window_len / HASH_RATE_HZ / 2
        found_ids = [d.track_id for d in found]
        rows = []
        for j, tid in enumerate(ws.track_ids):
            label = f"{titles[tid]} ({tid})" if tid in found_ids else None
            for t, z in zip(t_mid, ws.peak_z[:, j]):
                rows.append({"time_sec": t, "peak_z": z, "series": label, "track_id": tid})
        long = pd.DataFrame(rows)
        best_other = (long[long["series"].isna()].groupby("time_sec")["peak_z"].max()
                      .reset_index().assign(series="best of all other tracks"))
        plot = pd.concat([long.dropna(subset=["series"])[["time_sec", "peak_z", "series"]], best_other])
        domain = [f"{titles[t]} ({t})" for t in found_ids] + ["best of all other tracks"]
        palette = [BLUE, ORANGE, "#1baf7a"][: len(found_ids)] + [GRAY]
        lines = alt.Chart(plot).mark_line(strokeWidth=2).encode(
            x=alt.X("time_sec:Q", title="time in recording (s)"),
            y=alt.Y("peak_z:Q", title="peak_z"),
            color=alt.Color("series:N", scale=alt.Scale(domain=domain, range=palette),
                            legend=alt.Legend(title=None, orient="bottom")),
            tooltip=["series", alt.Tooltip("time_sec:Q", format=".1f"), alt.Tooltip("peak_z:Q", format=".2f")],
        )
        rule = alt.Chart(pd.DataFrame({"y": [threshold]})).mark_rule(strokeDash=[5, 4], color=GRAY).encode(y="y:Q")
        layers = [lines, rule]
        if truth is not None:
            segs = pd.DataFrame([{"start": s["recording_start_sec"], "end": s["recording_end_sec"]}
                                 for s in truth["segments"] if s["in_catalog"]])
            if not segs.empty:
                layers.insert(0, alt.Chart(segs).mark_rect(color=GRAY, opacity=0.18).encode(x="start:Q", x2="end:Q"))
        st.altair_chart(alt.layer(*layers).properties(height=300), width="stretch")
        st.caption("Dashed line: detection threshold. Shaded: where a catalog track really plays (simulated "
                   "recordings only). Single windows of unrelated tracks can cross the line; a detection also "
                   "needs two non-overlapping windows that agree on alignment.")

# ---------- Flag report ----------

with tab_report:
    rep = load_json("detection_report.json")
    if rep is None:
        st.info("results/detection_report.json not found. Run `./run_pipeline.sh`.")
    else:
        st.warning(rep["disclaimer"])
        t = rep["totals"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Recordings", t["events"])
        m2.metric("Detections", t["detections"])
        m3.metric("Licensed", t["licensed"])
        m4.metric("Flagged", t["flagged"])
        rows = [{"Recording": e["event_name"], "Set": e["split"], "Venue": e["venue"],
                 "Date": e["recorded_at"][:10], "Track": d["title"],
                 "Bit agreement": d["match_confidence"], "peak_z": d["peak_z"],
                 "Licensed": "yes" if d["licensed"] else "FLAGGED",
                 "Really played?": {True: "yes", False: "no (false positive)", None: "unknown"}[d["matches_ground_truth"]],
                 "Reason": d["reason"]}
                for e in rep["events"] for d in e["detections"]]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        if "evaluation" in rep:
            st.subheader("How accurate is the detector?")
            ev = rep["evaluation"]
            mt = pd.DataFrame([{"Set": name, **{k: v for k, v in ev[key].items()}}
                               for name, key in (("test (held out)", "test_split"), ("tune", "tune_split"))])
            st.dataframe(mt, width="stretch", hide_index=True)
            st.caption(ev["note"])
        if "snr_sweep" in rep:
            st.markdown("**Detection rate vs. background noise** (6 test excerpts per level)")
            sw = pd.DataFrame([{"SNR (dB)": int(k), "Detected": v["detected"], "Out of": v["of"],
                                "False positives": v["false_positives"]}
                               for k, v in rep["snr_sweep"]["summary_by_snr_db"].items()])
            st.dataframe(sw, width="stretch", hide_index=True)

# ---------- About ----------

with tab_about:
    st.markdown(f"""
**What this is.** A portfolio project showing both sides of music licensing on one catalog and one
database: helping someone find a track to license (sync scoring), and spotting tracks that were played
without a license (audio fingerprinting).

**What it isn't.** {DISCLAIMER} This tool cannot be used to bring or support an infringement claim
against any real venue or business.

**Catalog.** 47 tracks from ccMixter under CC BY 2.5/3.0 or CC0. Credits are in `CREDITS.md`. Mood and
instrumental tags come from rules applied to the uploaders' own ccMixter tags, not from listening.

**How detection works.** Chromaprint turns audio into about 8 32-bit codes per second. The recording is
cut into 10 s windows. Each window slides across every catalog track, and at each position the app
counts how many bits agree. The best position's score is compared with that track's other positions
(peak_z), and a track is reported only when separate windows agree on the same alignment. See the
README for details and the full results.
""")
