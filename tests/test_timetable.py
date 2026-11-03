import ku_portal_mcp.timetable as timetable
from ku_portal_mcp.auth import Session
from ku_portal_mcp.timetable import (
    TimetableEntry,
    resolve_period_time,
    timetable_to_ics,
)


def test_legacy_kpd_timetable_client_is_removed():
    """폐지된 EkuSchedule.kpd 클라이언트는 Session.ssotoken을 읽어 AttributeError가 났다."""
    assert not hasattr(timetable, "fetch_timetable_day")
    assert not hasattr(timetable, "fetch_full_timetable")
    assert not hasattr(timetable, "_parse_timetable_html")
    session = Session(cookies=[], created_at=0.0)
    assert not hasattr(session, "ssotoken")
    assert not hasattr(session, "portal_session_id")


def testresolve_period_time_for_range():
    assert resolve_period_time("2-3") == ("10:30", "13:15")


def testresolve_period_time_returns_original_value_for_unknown_period():
    assert resolve_period_time("A") == ("A", "A")


def test_timetable_to_ics_generates_weekly_event():
    entries = [
        TimetableEntry(
            day_of_week="월",
            period="1",
            subject_name="알고리즘",
            classroom="과학도서관 101",
            start_time="09:00",
            end_time="10:15",
        )
    ]

    ics = timetable_to_ics(entries, semester_start="2026-03-02")

    assert "BEGIN:VCALENDAR" in ics
    assert "SUMMARY:알고리즘" in ics
    assert "LOCATION:과학도서관 101" in ics
    assert "DTSTART:20260302T090000" in ics
    assert "DTEND:20260302T101500" in ics
    assert "RRULE:FREQ=WEEKLY;COUNT=16" in ics
