"""card_view.finish_edit() / stash_edit() / show_pending_feedback() —
docs/archive/EDIT_FLOW_PLAN.md Phase 0, rule 1: every write ends in one helper that
clears caches, stashes its outcome, and reruns; setup_page() shows the
outcome exactly once on the next run. Page-level adoption is covered in
test_pages_smoke.py."""
from streamlit.testing.v1 import AppTest

from dashboard_lib.card_view import result_from_lines


def test_result_from_lines_splits_successes_from_failures():
    result = result_from_lines(
        ["✅ Saved 2 edit(s).", "❌ Sol Ring: already planned", "✅ Applied Arcane Signet."],
        collection=True,
    )
    assert result == {
        "message": ["Saved 2 edit(s).", "Applied Arcane Signet."],
        "errors": ["Sol Ring: already planned"],
        "collection": True,
    }


def test_result_from_lines_with_nothing():
    assert result_from_lines([]) == {"message": [], "errors": []}


def _script():
    # AppTest runs this function's source as its own script, so it can't
    # see this module's names; conftest already put the project on sys.path.
    import streamlit as st
    from dashboard_lib import card_view as cv

    cv.setup_page("t", "🧪")
    pending = st.session_state.pop("write", None)
    if pending is not None:
        cv.finish_edit(pending)
    st.write("page body")


def _run(write):
    at = AppTest.from_function(_script, default_timeout=15)
    at.run()
    at.session_state["write"] = write
    at.run()
    return at


def test_one_line_is_a_toast_shown_once():
    at = _run({"message": "Renamed to 'X'."})
    assert not at.exception
    assert [t.body for t in at.toast] == ["Renamed to 'X'."]
    at.run()  # the next interaction: the message is gone
    assert list(at.toast) == []


def test_three_or_more_lines_are_one_success_list_plus_errors_and_warnings():
    at = _run({
        "message": ["Applied A.", "Applied B.", "Applied C."],
        "warnings": ["Skipped D — not owned."],
        "errors": ["E: already applied"],
    })
    assert not at.exception
    assert len(at.success) == 1
    assert "- Applied A." in at.success[0].value and "- Applied C." in at.success[0].value
    assert [t.body for t in at.toast] == ["Skipped D — not owned."]
    assert [e.value for e in at.error] == ["E: already applied"]


def test_no_pending_write_shows_nothing():
    at = AppTest.from_function(_script, default_timeout=15)
    at.run()
    assert not at.exception
    assert list(at.toast) == [] and list(at.success) == [] and list(at.error) == []
