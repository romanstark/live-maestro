"""Catalog corrections: rows whose access, status or doc disagreed with a probe.

Four rows where the catalog and the docs had to follow a probe:
``song.back_to_arranger``, which clears asynchronously; the one-clip note id rule, which
belongs to ACE Studio and is untested against Live; ``song.current_song_time``, which a
write bounds by ``song_length``; and interpolation across a sparse automation layer.

Every assertion below reads the artefact that ships: the YAML the wheel carries and the
markdown a clone gets, never a fixture standing in for them.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from live_maestro.server import play, set_arrangement_time

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "src" / "live_maestro" / "catalog"
LIMITS_MD = ROOT / "docs" / "limits.md"
CATALOG_MD = ROOT / "docs" / "catalog.md"
PROTOCOL_MD = ROOT / "docs" / "protocol.md"

NOTE_ID_ROWS = (
    "clip.get_notes_by_id",
    "clip.remove_notes_by_id",
    "clip.duplicate_notes_by_id",
    "arrangement_clip.get_notes_by_id",
    "arrangement_clip.remove_notes_by_id",
    "arrangement_clip.duplicate_notes_by_id",
)

# "read_back_attempts", "read-back attempts" and "read back attempts" all count.
READ_BACK_ATTEMPTS = re.compile(r"read[-_ ]?back[-_ ]?attempts", re.IGNORECASE)


def _load_catalog_file(filename: str) -> list[dict]:
    path = CATALOG_DIR / filename
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _catalog_row(filename: str, row_id: str) -> dict:
    """Return one catalog row, failing with the row id when the file no longer carries it."""
    row = next((r for r in _load_catalog_file(filename) if r.get("id") == row_id), None)
    assert row is not None, f"Row {row_id} missing in {filename}"
    return row


def _row_doc(filename: str, row_id: str) -> str:
    """Return a row's doc with every run of whitespace collapsed, so line wrapping cannot hide."""
    return " ".join(_catalog_row(filename, row_id).get("doc", "").split())


def _markdown_section(text: str, start: str, end: str) -> str:
    """Return the slice of a document between two headings, both matched literally."""
    begin = text.find(start)
    assert begin >= 0, f"heading {start!r} not found"
    finish = text.find(end, begin + len(start))
    assert finish > begin, f"heading {end!r} not found after {start!r}"
    return text[begin:finish]


def test_d1_back_to_arranger_writability_documented() -> None:
    """song.back_to_arranger must be documented as writable asynchronously, clearing the flag."""
    row = _catalog_row("10-song.yaml", "song.back_to_arranger")

    assert "set" in row.get("access", []), "song.back_to_arranger must allow set"
    doc = row.get("doc", "")
    assert "writable" in doc.lower() or "writing false" in doc.lower()
    assert "asynchronously" in doc

    # play tool docstring must not claim it cannot be cleared
    play_doc = " ".join((play.__doc__ or "").split())
    assert "cannot clear it" not in play_doc
    assert "clears it when written false" in play_doc


def test_d1_back_to_arranger_puts_every_number_after_the_date_that_earned_it() -> None:
    """Each back_to_arranger reading follows the date of the session that took it."""
    doc = _row_doc("10-song.yaml", "song.back_to_arranger")

    assert "It is writable" not in doc, "the bare writability verdict is back in the row"
    assert "2026-09-06" in doc, "the 2026-09-06 one-way measurement was dropped"
    assert "2026-09-10 to 2026-09-13" in doc, "the production-session measurement was dropped"

    attempts = READ_BACK_ATTEMPTS.search(doc)
    assert attempts is not None, (
        "the row no longer says how many read-back attempts the write needs"
    )
    earliest_date = min(doc.index("2026-09-06"), doc.index("2026-09-10 to 2026-09-13"))
    assert attempts.start() > earliest_date, (
        "the read-back attempt count precedes the earlier of the two dated measurements"
    )


def test_d2_note_id_rule_is_attributed_and_not_pinned_on_live() -> None:
    """The one-clip note id rule names ACE Studio, says it is untested, and names its probe."""
    for row_id in NOTE_ID_ROWS:
        doc = _row_doc("30-clip.yaml", row_id)
        assert "triggers an INVALID_ARG error in Live" not in doc, (
            f"{row_id} still asserts a Live error code Live does not have"
        )
        assert "INVALID_ARG" not in doc or "ACE Studio" in doc, (
            f"{row_id} names INVALID_ARG without attributing it to ACE Studio"
        )
        assert "untested" in doc, (
            f"{row_id} does not say the one-clip rule is untested against Live"
        )
        assert "notes_get" in doc, f"{row_id} does not name the probe that would settle the rule"
        assert "live_error" in doc, f"{row_id} does not name the answer that would settle the rule"


def test_no_catalog_row_pins_a_foreign_error_code_on_live() -> None:
    """A code Live cannot raise may only appear in a row that says whose code it is."""
    unattributed: list[str] = []
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        for row in yaml.safe_load(path.read_text(encoding="utf-8")):
            doc = " ".join((row.get("doc") or "").split())
            if "INVALID_ARG" in doc and "ACE Studio" not in doc:
                unattributed.append(f"{path.name}:{row.get('id')}")
    assert not unattributed, f"rows naming INVALID_ARG without attributing it: {unattributed}"

    error_codes = _markdown_section(
        PROTOCOL_MD.read_text(encoding="utf-8"), "### Error Codes", "## 5. Handlers"
    )
    assert "INVALID_ARG" not in error_codes, (
        "docs/protocol.md presents INVALID_ARG as one of Live's own codes"
    )


def test_d3_current_song_time_bound_by_song_length_documented() -> None:
    """song.current_song_time must document the write bound to song_length."""
    doc = _row_doc("10-song.yaml", "song.current_song_time")
    assert "song_length" in doc
    assert "Cannot set the Songtime behind the Songlength" in doc

    sat_doc = set_arrangement_time.__doc__ or ""
    assert "song_length" in sat_doc


def test_d3_song_length_bound_is_attributed_to_the_session_that_measured_it() -> None:
    """The song_length bound, its quoted Live error and the 626.95 reading all carry their date."""
    doc = _row_doc("10-song.yaml", "song.current_song_time")

    assert "Cannot set the Songtime behind the Songlength" in doc, "the quoted Live error was lost"
    assert "626.95" in doc, "the 626.95 reading was lost"
    assert "2026-09-10 to 2026-09-13" in doc, "the session that measured the bound is not named"
    assert doc.index("2026-09-10 to 2026-09-13") < doc.index("626.95"), (
        "the 626.95 reading precedes the session that measured it"
    )


def test_d4_sparse_automation_interpolation_documented() -> None:
    """Sparse automation interpolation behavior must be documented in limits.md and catalog."""
    limits_text = LIMITS_MD.read_text(encoding="utf-8")
    assert "Sparse automation layers and interpolation behavior" in limits_text
    assert "insert_step" in limits_text
    assert "write_automation" in limits_text

    catalog_text = CATALOG_MD.read_text(encoding="utf-8")
    assert "Sparse automation layers" in catalog_text


def test_d4_envelope_row_does_not_credit_live_with_the_writers_own_behaviour() -> None:
    """The envelope row credits the staircase to write_automation and dates what it claims."""
    doc = _row_doc("30-clip.yaml", "clip.automation_envelope")

    assert "the final value is held to the end of the clip" not in doc, (
        "the row still reads the writer's own behaviour back as a property of Live"
    )
    assert "it is write_automation, not Live" in doc, "the staircase is not credited to its writer"
    assert "not measured here" in doc, "the gap between two non-touching steps is not marked open"
    assert "automation_read" in doc, "the probe for that gap is not named"
    assert "2026-09-08" in doc, "the sole-write-primitive claim lost the date that established it"


def test_protocol_says_the_batch_cap_is_not_on_the_wire() -> None:
    """Section 5.7 must place the op-count cap in the tool layer and date the throughput figures."""
    section = _markdown_section(PROTOCOL_MD.read_text(encoding="utf-8"), "### 5.7", "### 5.8")
    assert "MAX_BATCH_OPS" in section, (
        "section 5.7 does not say the op-count cap is a tool-layer policy"
    )
    assert "2026-09-16" in section, "section 5.7 carries throughput figures without their date"


def test_protocol_section_10_says_the_end_marker_refusal_is_tool_layer_only() -> None:
    """A direct wire client must be told the end_marker write is accepted and does nothing."""
    section = _markdown_section(PROTOCOL_MD.read_text(encoding="utf-8"), "## 10.", "## 11.")
    for token in ("arrangement_clips", "end_marker", "end_time"):
        assert token in section, f"section 10 never mentions {token}"


def test_protocol_section_7_separates_the_two_handle_key_forms() -> None:
    """Section 7 must separate the reply key path from the argument key __path__."""
    section = _markdown_section(PROTOCOL_MD.read_text(encoding="utf-8"), "## 7.", "## 8.")
    assert "__path__" in section, "section 7 never names the argument-side handle key"
    assert "never carries" in section, "section 7 does not say which direction never carries which"


#: The note-id rows whose call returns a note list, so "an answer covering only the
#: local note" is an outcome a reader can actually see.
NOTE_ID_READING_ROWS = ("clip.get_notes_by_id", "arrangement_clip.get_notes_by_id")

#: The note-id rows whose call returns nothing. Their effect is seen by reading the
#: clip back, so a probe that asks the reader to inspect an answer names an outcome
#: these calls cannot produce.
NOTE_ID_WRITING_ROWS = tuple(row for row in NOTE_ID_ROWS if row not in NOTE_ID_READING_ROWS)


def test_d2_note_id_probe_names_an_outcome_the_documented_call_can_produce() -> None:
    """A probe on a call that answers nothing must be read back, not inspected.

    ``remove_notes_by_id`` and ``duplicate_notes_by_id`` return no note list: the
    rows' own measurements observe them by re-reading with ``notes_get``. A probe
    that says to look at "an answer covering only the local note" therefore names
    a sighting nobody can take, and the silent-drop case the block calls the worse
    one stays invisible. Worse, it never says to look at the other clip, which is
    where a set-wide id would do its damage.
    """
    for row_id in NOTE_ID_WRITING_ROWS:
        doc = _row_doc("30-clip.yaml", row_id)
        assert "an answer covering only the local note" not in doc, (
            f"{row_id} tells the reader to inspect an answer its call does not return"
        )
        assert "the second clip" in doc, (
            f"{row_id} never says to read the other clip back, where a set-wide id would show"
        )
        assert "notes_get on both clips" in doc, (
            f"{row_id} does not say the probe is settled by reading both clips back"
        )

    for row_id in NOTE_ID_READING_ROWS:
        doc = _row_doc("30-clip.yaml", row_id)
        assert "an answer covering only the local note" in doc, (
            f"{row_id} lost the outcome its call really can produce"
        )


def test_d4_envelope_row_dates_the_member_list_and_not_the_inference() -> None:
    """The 2026-09-08 measurement is a member list, and must not carry a verdict.

    What was measured that day, and recorded in docs/limits.md, is which members
    an ``Envelope`` publishes. Whether ``create_event`` writes was not measured,
    and ``delete_events_in_range`` plainly does write: the Remote Script empties
    an envelope with it. A sentence that says ``insert_step`` is the only write
    the LOM offers and then lists two other writers as its evidence refutes
    itself in its own breath.
    """
    doc = _row_doc("30-clip.yaml", "clip.automation_envelope")

    assert "the only envelope write the LOM offers" not in doc, (
        "the row still dates an inference about Live to a measurement of a member list"
    )
    assert "create_event" in doc, "the measured member list lost create_event"
    assert "What create_event does is not measured here" in doc, (
        "the row asserts create_event is not a write without having measured it"
    )
    assert "delete_events_in_range" in doc, "the measured member list lost delete_events_in_range"

    limits_text = LIMITS_MD.read_text(encoding="utf-8")
    assert "gives a clip envelope one write primitive" not in limits_text, (
        "docs/limits.md still reads the same inference back as a property of the Object Model"
    )


def test_d_end_marker_row_quotes_the_refusal_it_points_at() -> None:
    """The row, the refusal and docs/protocol.md must describe one sighting alike.

    What 2026-09-10 recorded is that the write was accepted and ``end_time`` did
    not move. docs/protocol.md says flatly that no ``read_back`` was recorded for
    it, so a row that dates "the value stored" to that sighting claims a reading
    nobody took. The store belongs where the row already puts it, on its own
    ``proven`` field, which rests on a different and earlier read-back.
    """
    from live_maestro.server import ARRANGEMENT_END_MARKER_REFUSAL

    doc = _row_doc("30-clip.yaml", "arrangement_clip.end_marker")

    assert "observation that the value stored" not in doc, (
        "the row still attributes a read-back to the sighting that never took one"
    )
    assert "the write was accepted" in ARRANGEMENT_END_MARKER_REFUSAL, (
        "the refusal constant no longer carries the wording the row is matched to"
    )
    assert "the write was accepted and end_time did not move" in doc, (
        "the row does not describe the sighting the way the refusal it points at does"
    )
    assert "proven field records that the value stores" in doc, (
        "the row lost the place where the store claim legitimately lives"
    )
