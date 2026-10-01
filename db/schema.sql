-- Music Licensing Analytics schema.
-- Written in portable SQL (no SQLite-only syntax) so moving to Postgres only
-- needs a different connection, not a rewrite. BOOLEAN columns are stored as
-- 0/1 by SQLite and as true booleans by Postgres.

DROP TABLE IF EXISTS detections;
DROP TABLE IF EXISTS usage_events;
DROP TABLE IF EXISTS fingerprints;
DROP TABLE IF EXISTS licenses;
DROP TABLE IF EXISTS audio_features;
DROP TABLE IF EXISTS tracks;

CREATE TABLE tracks (
    track_id        TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    artist          TEXT,
    source_url      TEXT,
    license_type    TEXT NOT NULL,      -- cc-by-3.0, cc-by-2.5, cc0 (from ccMixter's license URL)
    license_url     TEXT,
    file_path       TEXT NOT NULL,
    duration_sec    REAL
);

CREATE TABLE audio_features (
    track_id            TEXT PRIMARY KEY REFERENCES tracks(track_id),
    tempo_bpm           REAL,
    energy_rms          REAL,
    spectral_centroid   REAL,
    zero_crossing_rate  REAL,
    is_instrumental     BOOLEAN,        -- rule-derived from uploader tags (see README)
    mood_tags           TEXT            -- comma-separated, rule-derived from uploader tags
);

CREATE TABLE licenses (
    license_id      INTEGER PRIMARY KEY,
    track_id        TEXT REFERENCES tracks(track_id),
    licensee        TEXT NOT NULL,      -- fictional licensee, e.g. "Venue A (fictional)"
    scope           TEXT,
    start_date      DATE,
    end_date        DATE,
    status          TEXT NOT NULL       -- active, expired, none
);

CREATE TABLE fingerprints (
    track_id            TEXT PRIMARY KEY REFERENCES tracks(track_id),
    fingerprint_hashes  TEXT NOT NULL,  -- JSON array of 32-bit ints from fpcalc -raw
    hash_rate_hz        REAL NOT NULL   -- hashes per second, needed for offset math
);

CREATE TABLE usage_events (
    event_id        INTEGER PRIMARY KEY,
    event_name      TEXT NOT NULL,      -- e.g. "Simulated Venue Recording 1"
    venue           TEXT,               -- matched against licenses.licensee at report time
    recording_path  TEXT NOT NULL,
    recorded_at     TIMESTAMP
);

CREATE TABLE detections (
    detection_id        INTEGER PRIMARY KEY,
    event_id            INTEGER REFERENCES usage_events(event_id),
    track_id            TEXT REFERENCES tracks(track_id),
    match_confidence    REAL NOT NULL,  -- fraction of matching bits at best offset
    peak_z              REAL,           -- how far that peak stands above the track's other offsets
    matched_offset_sec  REAL,           -- position in the reference track where the match starts
    recording_start_sec REAL,           -- where in the recording the matched audio starts
    matched_windows     INTEGER,        -- non-overlapping query windows that agreed on this alignment
    licensed            BOOLEAN,        -- filled in by report.py from the licenses table
    flagged_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
