"""Google Drive naming and layout policy for LMS material synchronization.

This module intentionally contains no Google API code. The deployed remote MCP
wrapper can import these helpers so upload/update logic stays deterministic and
testable.

Policy:
- course folders use a semester prefix, e.g. [2026-2] 텍스트마이닝 (BDC102)
- course materials stay flat while the folder has <= 20 visible items
- Drive file names have a semantic prefix such as [강의] or [실습]
- when a file already exists in Drive, its current Drive name is authoritative
- revised bytes may replace the existing Drive file in-place without renaming it
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


@dataclass(frozen=True)
class DriveNameDecision:
    """Result of applying the Drive naming policy."""

    source_filename: str
    drive_filename: str
    category: str
    preserved_existing_name: bool


def has_drive_prefix(name: str) -> bool:
    """Return True when name already starts with a bracketed Drive prefix."""

    return bool(_PREFIX_RE.match((name or "").strip()))


def strip_drive_prefix(name: str) -> str:
    """Remove one leading Drive prefix and surrounding whitespace."""

    return _PREFIX_RE.sub("", (name or "").strip(), count=1)


def classify_material(filename: str, *, source_title: str = "") -> str:
    """Classify an LMS attachment into the Drive prefix category.

    The order matters: solution/answer markers win over assignment markers,
    and assignment context wins over a notebook extension.
    """

    fn = (filename or "").strip()
    title = (source_title or "").strip()
    text = f"{title} {fn}".lower()
    suffix = Path(fn).suffix.lower()

    solution_markers = (
        "solution",
        "_sol",
        "-sol",
        "정답",
        "해설",
        "답안",
        "모범답안",
    )
    if any(marker in text for marker in solution_markers):
        return "정답"

    assignment_markers = (
        "assignment",
        "homework",
        "problem",
        "과제",
        "제출",
    )
    if any(marker in text for marker in assignment_markers):
        return "과제"

    syllabus_markers = ("syllabus", "강의계획서")
    if any(marker in text for marker in syllabus_markers):
        return "강의계획서"

    guide_markers = ("readme", "installation", "install", "설치", "안내", "guide")
    if any(marker in text for marker in guide_markers):
        return "안내"

    if suffix == ".ipynb":
        return "실습"

    if suffix in {".py", ".r", ".m", ".java", ".c", ".cpp", ".js", ".ts"}:
        return "코드"

    if suffix in {".csv", ".tsv", ".json", ".parquet"}:
        return "실습데이터"

    if suffix in {".zip", ".tar", ".gz", ".tgz", ".7z"}:
        return "자료"

    return "강의"


def prefixed_filename(
    source_filename: str,
    *,
    source_title: str = "",
    category: str | None = None,
) -> str:
    """Return the semantic Drive name for a newly created file."""

    source_filename = (source_filename or "").strip()
    if not source_filename:
        raise ValueError("source_filename must not be empty")

    if has_drive_prefix(source_filename):
        return source_filename

    resolved = category or classify_material(source_filename, source_title=source_title)
    return f"[{resolved}] {source_filename}"


def choose_drive_filename(
    source_filename: str,
    *,
    existing_drive_name: str | None = None,
    source_title: str = "",
    category: str | None = None,
) -> DriveNameDecision:
    """Choose the Drive filename for create/skip/update operations.

    Existing Drive names always win. This prevents a re-sync from turning
    '[강의] 7. Text classification 3.pdf' back into the LMS original name.
    """

    source_filename = (source_filename or "").strip()
    if not source_filename:
        raise ValueError("source_filename must not be empty")

    existing = (existing_drive_name or "").strip()
    if existing:
        match = _PREFIX_RE.match(existing)
        existing_category = (
            match.group(0).strip("[] ") if match else classify_material(
                source_filename, source_title=source_title
            )
        )
        return DriveNameDecision(
            source_filename=source_filename,
            drive_filename=existing,
            category=existing_category,
            preserved_existing_name=True,
        )

    resolved = category or classify_material(source_filename, source_title=source_title)
    return DriveNameDecision(
        source_filename=source_filename,
        drive_filename=prefixed_filename(
            source_filename,
            source_title=source_title,
            category=resolved,
        ),
        category=resolved,
        preserved_existing_name=False,
    )


def course_folder_name(
    year: int | str,
    semester: int | str,
    course_name: str,
    *,
    course_code: str = "",
) -> str:
    """Return the canonical course folder name with a semester prefix."""

    year_s = str(year).strip()
    semester_s = str(semester).strip()
    name = (course_name or "").strip()
    code = (course_code or "").strip()

    if not year_s or not semester_s or not name:
        raise ValueError("year, semester and course_name are required")

    base = f"[{year_s}-{semester_s}] {name}"
    if code and code.lower() not in name.lower():
        base += f" ({code})"
    return base


def should_keep_course_folder_flat(
    existing_visible_items: int,
    *,
    incoming_new_items: int = 0,
    threshold: int = 20,
) -> bool:
    """Return True when course contents should stay directly in one folder."""

    if existing_visible_items < 0 or incoming_new_items < 0:
        raise ValueError("item counts must be non-negative")
    if threshold < 1:
        raise ValueError("threshold must be positive")

    return existing_visible_items + incoming_new_items <= threshold
