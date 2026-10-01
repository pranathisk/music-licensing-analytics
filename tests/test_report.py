"""License matching rules used by the flag report."""
from datetime import date

import pytest

from src.db import connect, init_schema
from src.report import license_status


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_schema(c)
    c.executemany("INSERT INTO tracks (track_id, title, license_type, file_path) VALUES (?, ?, 'cc0', 'x')",
                  [("t1", "One"), ("t2", "Two"), ("t3", "Three"), ("t4", "Four")])
    c.executemany(
        "INSERT INTO licenses (track_id, licensee, scope, start_date, end_date, status) VALUES (?, ?, 'bg', ?, ?, ?)",
        [("t1", "Venue A", "2026-01-01", "2026-12-31", "active"),
         ("t2", "Venue A", "2025-01-01", "2025-12-31", "expired"),
         ("t3", "Venue A", "2026-06-01", "2027-05-31", "active"),
         ("t4", "Venue A", None, None, "none")])
    return c


def test_active_license_covering_date(conn):
    assert license_status(conn, "t1", "Venue A", date(2026, 3, 1))[0] is True


def test_license_for_other_venue_does_not_count(conn):
    licensed, reason, _ = license_status(conn, "t1", "Venue B", date(2026, 3, 1))
    assert not licensed and "no license" in reason


def test_expired_not_started_none_and_missing(conn):
    assert "expired" in license_status(conn, "t2", "Venue A", date(2026, 3, 1))[1]
    licensed, reason, _ = license_status(conn, "t3", "Venue A", date(2026, 3, 1))
    assert not licensed and "runs 2026-06-01" in reason
    assert "status 'none'" in license_status(conn, "t4", "Venue A", date(2026, 3, 1))[1]
    assert license_status(conn, "t1", "Venue A", date(2027, 1, 1))[0] is False
