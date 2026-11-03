"""교시 시각 변환과 ICS 내보내기.

시간표 데이터는 AMS 격자를 편다. 레거시 포털 위젯
(``POST /front/EkuSchedule.kpd``)은 ``*.kpd`` 폐지 이후 호출할 수 없고,
당시 ``Session``에도 ``ssotoken`` 필드가 없어 남겨 두면 ``AttributeError``만
난다. 그 클라이언트는 제거했다.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta


# Standard period-to-time mapping for Korea University
PERIOD_TIMES = {
    "1": ("09:00", "10:15"),
    "2": ("10:30", "11:45"),
    "3": ("12:00", "13:15"),
    "4": ("13:30", "14:45"),
    "5": ("15:00", "16:15"),
    "6": ("16:30", "17:45"),
    "7": ("18:00", "18:50"),
    "8": ("19:00", "19:50"),
    "9": ("20:00", "20:50"),
    "10": ("21:00", "21:50"),
    "11": ("22:00", "22:50"),
}


@dataclass
class TimetableEntry:
    day_of_week: str
    period: str
    subject_name: str
    classroom: str
    start_time: str
    end_time: str


def resolve_period_time(period: str) -> tuple[str, str]:
    """Convert period string (e.g., '1', '2-3') to start/end times."""
    parts = re.split(r"[-~]", period.strip())
    start_period = parts[0].strip()
    end_period = parts[-1].strip()

    start_time = PERIOD_TIMES.get(start_period, ("", ""))[0]
    end_time = PERIOD_TIMES.get(end_period, ("", ""))[1]

    return start_time or period, end_time or period


def timetable_to_ics(entries: list[TimetableEntry], semester_start: str = "") -> str:
    """Convert timetable entries to ICS (iCalendar) format.

    Args:
        entries: Timetable entries.
        semester_start: Semester start date (YYYY-MM-DD). Defaults to next Monday.

    Returns:
        ICS calendar string.
    """
    if not semester_start:
        today = datetime.now()
        # Find next Monday
        days_ahead = 0 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        start_date = today + timedelta(days=days_ahead)
    else:
        start_date = datetime.strptime(semester_start, "%Y-%m-%d")

    day_to_offset = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ku-portal-mcp//KU Timetable//KO",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:KU 수업시간표",
    ]

    for entry in entries:
        offset = day_to_offset.get(entry.day_of_week, 0)
        event_date = start_date + timedelta(days=offset - start_date.weekday())
        if event_date < start_date:
            event_date += timedelta(weeks=1)

        if ":" in entry.start_time and ":" in entry.end_time:
            sh, sm = entry.start_time.split(":")
            eh, em = entry.end_time.split(":")
            dtstart = event_date.replace(hour=int(sh), minute=int(sm))
            dtend = event_date.replace(hour=int(eh), minute=int(em))
        else:
            continue

        uid = f"{entry.subject_name}-{entry.day_of_week}-{entry.period}@kupid"

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"DTSTART:{dtstart.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND:{dtend.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{entry.subject_name}",
                f"LOCATION:{entry.classroom}",
                f"DESCRIPTION:교시: {entry.period}",
                f"UID:{uid}",
                "RRULE:FREQ=WEEKLY;COUNT=16",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
