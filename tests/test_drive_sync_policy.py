from ku_portal_mcp.drive_sync_policy import (
    choose_drive_filename,
    classify_material,
    course_folder_name,
    has_drive_prefix,
    prefixed_filename,
    should_keep_course_folder_flat,
    strip_drive_prefix,
)


def test_preserves_existing_prefixed_drive_name():
    decision = choose_drive_filename(
        "7. Text classification 3.pdf",
        existing_drive_name="[강의] 7. Text classification 3.pdf",
    )
    assert decision.drive_filename == "[강의] 7. Text classification 3.pdf"
    assert decision.preserved_existing_name is True
    assert decision.category == "강의"


def test_preserves_existing_custom_drive_name_even_without_prefix():
    decision = choose_drive_filename(
        "Topic 0 - Introduction-1.pdf",
        existing_drive_name="Topic 0 - Introduction.pdf",
    )
    assert decision.drive_filename == "Topic 0 - Introduction.pdf"
    assert decision.preserved_existing_name is True


def test_new_pdf_defaults_to_lecture_prefix():
    assert prefixed_filename("1. Introduction.pdf") == "[강의] 1. Introduction.pdf"


def test_notebook_defaults_to_practice_prefix():
    assert prefixed_filename("실습.ipynb") == "[실습] 실습.ipynb"


def test_assignment_context_wins_over_notebook_extension():
    assert (
        classify_material(
            "problem1.ipynb",
            source_title="[과제] Assignment #2",
        )
        == "과제"
    )


def test_solution_markers_win_over_assignment_markers():
    assert classify_material("assignment2_solution.pdf") == "정답"
    assert classify_material("assignment2_sol.pdf") == "정답"


def test_syllabus_and_guide_categories():
    assert classify_material("AAI107_IntroML_Syllabus.pdf") == "강의계획서"
    assert classify_material("README.txt") == "안내"


def test_code_and_data_categories():
    assert classify_material("run_assignment2.py") == "과제"
    assert classify_material("model.py") == "코드"
    assert classify_material("data.csv") == "실습데이터"


def test_prefix_helpers_are_idempotent():
    name = "[실습] notebook.ipynb"
    assert has_drive_prefix(name)
    assert strip_drive_prefix(name) == "notebook.ipynb"
    assert prefixed_filename(name) == name


def test_course_folder_name_adds_semester_and_code():
    assert (
        course_folder_name(2026, 2, "텍스트마이닝", course_code="BDC102")
        == "[2026-2] 텍스트마이닝 (BDC102)"
    )


def test_course_folder_does_not_duplicate_code():
    assert (
        course_folder_name(2026, 2, "텍스트마이닝 (BDC102)", course_code="BDC102")
        == "[2026-2] 텍스트마이닝 (BDC102)"
    )


def test_flat_threshold_is_inclusive():
    assert should_keep_course_folder_flat(19, incoming_new_items=1)
    assert should_keep_course_folder_flat(20)
    assert not should_keep_course_folder_flat(20, incoming_new_items=1)
