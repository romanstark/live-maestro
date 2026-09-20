"""Tests for ``_als_locate`` and for the dry run report over a project file.

``als_write``'s ``attribute`` operation needs an ElementTree expression, and that
expression has to be read out of the file in hand: Live 11 writes the main track as
``MasterTrack`` and Live 12 writes it as ``MainTrack``, so an expression that is right
for one file selects nothing at all in the other and the write is refused for a reason
that says nothing about the cause. These tests build both shapes as XML rather than
needing a project on disk.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from live_maestro import server


def project(main_tag: str, tracks: tuple[tuple[str, str, str], ...] = ()) -> ET.Element:
    """A minimal .als tree: a main track with a tempo, and named tracks.

    ``tracks`` entries are (tag, effective name, user name).
    """
    root = ET.Element("Ableton", MinorVersion="12.0_12402")
    live_set = ET.SubElement(root, "LiveSet")
    holder = ET.SubElement(live_set, "Tracks")
    for tag, effective, user in tracks:
        track = ET.SubElement(holder, tag)
        name = ET.SubElement(track, "Name")
        ET.SubElement(name, "EffectiveName", Value=effective)
        ET.SubElement(name, "UserName", Value=user)
    main = ET.SubElement(live_set, main_tag)
    mixer = ET.SubElement(ET.SubElement(main, "DeviceChain"), "Mixer")
    tempo = ET.SubElement(mixer, "Tempo")
    ET.SubElement(tempo, "Manual", Value="124")
    return root


@pytest.mark.parametrize("main_tag", ["MasterTrack", "MainTrack"])
def test_the_tempo_expression_comes_from_the_file_and_not_from_a_constant(
    main_tag: str,
) -> None:
    """Live 11 writes MasterTrack and Live 12 writes MainTrack.

    A constant is right for one of them and silently matches nothing in the
    other, which reaches the caller as a refused write rather than as the schema
    difference it is.
    """
    root = project(main_tag)
    found = server._als_locate(root, "tempo")
    assert len(found) == 1
    assert found[0]["expression"] == f".//{main_tag}/DeviceChain/Mixer/Tempo/Manual"
    assert found[0]["current"] == "124"
    # And the expression it hands out has to actually select something.
    assert len(root.findall(found[0]["expression"])) == 1
    assert found[0]["index"] is None, "one match needs no index"


def test_a_file_with_no_tempo_answers_with_nothing_rather_than_a_guess() -> None:
    root = ET.Element("Ableton")
    ET.SubElement(root, "LiveSet")
    assert server._als_locate(root, "tempo") == []


def test_the_index_counts_matches_of_the_expression_not_tracks() -> None:
    """The index counts matches of the expression, not tracks in the project.

    ``.//MidiTrack/Name/EffectiveName`` does not see the audio tracks, so the
    third track in the project is the second match of that expression. An index
    taken from the project's own numbering edits the wrong track.
    """
    root = project(
        "MainTrack",
        (
            ("MidiTrack", "01 Trigger", "01 Trigger"),
            ("AudioTrack", "02 Reference", "02 Reference"),
            ("MidiTrack", "03 Kick", "03 Kick"),
            ("MidiTrack", "04 Clap", "04 Clap"),
        ),
    )
    found = [
        entry
        for entry in server._als_locate(root, "track_names")
        if entry["kind"] != "MainTrack"
    ]
    by_name = {entry["current"]: entry for entry in found}

    assert by_name["01 Trigger"]["index"] == 0
    assert by_name["03 Kick"]["index"] == 1, "third in the project, second MidiTrack"
    assert by_name["04 Clap"]["index"] == 2
    assert by_name["02 Reference"]["index"] == 0, "first and only AudioTrack"
    # Each index has to select the track it claims.
    for entry in found:
        matches = root.findall(entry["expression"])
        assert matches[entry["index"]].get("Value") == entry["current"]


def test_a_rename_needs_both_name_fields_and_the_answer_says_so() -> None:
    """Live stores the shown name and the typed name separately.

    Setting only the shown one lets Live recompute it from the instrument, and
    the read afterwards shows the new name while the file loses it on the next
    open.
    """
    root = project("MainTrack", (("MidiTrack", "1-Operator", ""),))
    entry = next(e for e in server._als_locate(root, "track_names") if e["kind"] == "MidiTrack")
    assert entry["expression"].endswith("/Name/EffectiveName")
    assert entry["also_set"] is not None
    assert entry["also_set"].endswith("/Name/UserName")
    assert entry["current"] == "1-Operator"
    assert entry["current_user_name"] == "", "empty means Live is naming it"


def report(tmp_path: Path, root: ET.Element, expression: str, attribute: str = "Value") -> dict:
    """Write the tree out as a .als and run the dry run's inspection over it."""
    import gzip

    target = tmp_path / "p.als"
    target.write_bytes(gzip.compress(ET.tostring(root)))
    return server._als_expression_report(target, expression, attribute)


def test_an_expression_that_selects_nothing_says_so_before_the_write(
    tmp_path: Path,
) -> None:
    out = report(tmp_path, project("MainTrack"), ".//MasterTrack/DeviceChain/Mixer/Tempo/Manual")
    assert out["matches"] == 0
    assert "selects nothing" in out["verdict"]


def test_an_ambiguous_expression_names_the_index_range(tmp_path: Path) -> None:
    root = project(
        "MainTrack",
        (("MidiTrack", "a", "a"), ("MidiTrack", "b", "b"), ("MidiTrack", "c", "c")),
    )
    out = report(tmp_path, root, ".//MidiTrack/Name/EffectiveName")
    assert out["matches"] == 3
    assert "index=0..2" in out["verdict"]


def test_one_match_carrying_the_attribute_is_reported_as_ready(tmp_path: Path) -> None:
    out = report(tmp_path, project("MainTrack"), ".//MainTrack/DeviceChain/Mixer/Tempo/Manual")
    assert out["matches"] == 1
    assert out["sample"][0]["has_attribute"] is True
    assert out["sample"][0]["current"] == "124"
    assert out["verdict"] == "one element, and it carries the attribute"


def test_a_misspelled_attribute_is_caught_before_it_invents_a_field(
    tmp_path: Path,
) -> None:
    out = report(
        tmp_path, project("MainTrack"), ".//MainTrack/DeviceChain/Mixer/Tempo/Manual", "Vaule"
    )
    assert out["matches"] == 1
    assert out["sample"][0]["has_attribute"] is False
    assert "carries no 'Vaule'" in out["verdict"]


def test_a_file_that_cannot_be_read_is_reported_and_not_raised(tmp_path: Path) -> None:
    broken = tmp_path / "broken.als"
    broken.write_bytes(b"this is not gzip")
    out = server._als_expression_report(broken, ".//Tempo", "Value")
    assert out["readable"] is False
    assert "error" in out
