"""Tests for the probe harness that decides what a catalog row is aimed at.

The probe is what turns a row into a measurement, so a fault here does not
produce a wrong answer. It produces a wrong *fact*, written into the catalog
and read by every caller afterwards. The faults pinned below are of that kind.

These were found by sweeping the whole catalog against a running Live rather
than by reading the code, which is why each test states the number it was
measured at: a regression that halves the coverage is otherwise silent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import probe_paths

from live_maestro.registry import default_registry


@pytest.fixture(scope="module")
def registry():
    return default_registry()


@pytest.fixture
def session() -> probe_paths.Session:
    """A set shaped like the one the sweep ran against, in miniature.

    One group track, one MIDI track carrying a device, one audio track, and
    one clip of each kind, which is the least that exercises every branch of
    :func:`probe_paths.candidate_args`.
    """
    return probe_paths.Session(
        tracks=[
            probe_paths.TrackInfo(0, "Group", is_group=True, is_midi=False, device_count=0),
            probe_paths.TrackInfo(1, "Keys", is_group=False, is_midi=True, device_count=2),
            probe_paths.TrackInfo(2, "Vox", is_group=False, is_midi=False, device_count=1),
        ],
        scenes=["A", "B"],
        returns=["A-Reverb"],
        clips=[
            probe_paths.ClipInfo(track=2, slot=0, is_audio=True, name="vox"),
            probe_paths.ClipInfo(track=1, slot=1, is_audio=False, name="keys"),
        ],
        live_version="12.4.5",
    )


def test_no_row_is_asked_for_a_target_it_does_not_declare(registry, session) -> None:
    """An argument the row does not take is refused, and the row reads as unreachable.

    ``return_device.name`` sits at
    ``song.return_tracks[{return}].devices[{device}].name``. It matches
    ``_needs_device`` because the path contains ``devices[{device}]``, and that
    branch used to pin a ``track`` as well. The executor then answered
    ``unknown argument(s) ['track']``, which the probe recorded as "no target
    could be resolved" and the catalog would have recorded as a row this set
    cannot reach.

    Measured against Live 12.4.5: 65 rows were affected, 31 under
    ``return_device`` and 34 under ``master_device``, and probing
    ``return_device`` went from 1 verified row to 29 once the extra argument
    was dropped.
    """
    offences = []
    for spec in registry.all():
        declared = {param.name for param in spec.params}
        variants, _why = probe_paths.candidate_args(spec, session, {})
        for variant in variants:
            extra = sorted(set(variant) - declared)
            if extra:
                offences.append(f"{spec.id}: passed {extra}, declares {sorted(declared)}")
    assert not offences, "rows asked for a target they do not declare:\n" + "\n".join(
        offences[:20]
    )


def test_an_override_still_reaches_a_row_that_declares_it(registry, session) -> None:
    """Dropping undeclared arguments must not drop the ones the caller pinned.

    The filter that fixes the test above runs after overrides are applied, so
    this holds it to the other side of the same rule.
    """
    spec = registry.get("return_device.name")
    variants, _why = probe_paths.candidate_args(spec, session, {"return": 0, "device": 1})
    assert variants, "a pinned target produced no candidate at all"
    assert all(variant.get("device") == 1 for variant in variants)
    assert all(variant.get("return") == 0 for variant in variants)


def test_a_clip_row_is_offered_an_audio_clip_before_a_midi_one(registry, session) -> None:
    """``clip.warping`` answers only on an audio clip, so audio is tried first.

    Which kind a row needs is not recorded anywhere machine-readable, so the
    probe tries both. The order is what decides whether a row that needs audio
    is proven or is written off, and the sweep found 14 rows in this class.
    """
    spec = registry.get("clip.warping")
    variants, _why = probe_paths.candidate_args(spec, session, {})
    assert variants[0]["track"] == 2, "the audio clip's track should be tried first"
    assert variants[0]["slot"] == 0


def test_the_writer_adds_proven_under_status_when_a_row_lacks_it() -> None:
    """A row written before ``proven`` existed still gets the field.

    The writer edits lines rather than reparsing YAML, because reparsing would
    reflow every folded ``doc`` block in the file and turn a one-row
    measurement into a whole-file diff.
    """
    lines = [
        "- id: song.tempo\n",
        "  path: song.tempo\n",
        "  access: [get, set]\n",
        "  status: verified\n",
        "  doc: >-\n",
        "    Song tempo in BPM.\n",
    ]
    location = probe_paths.RowLocation(
        file=Path("10-song.yaml"), start=0, end=len(lines)
    )
    probe_paths._set_proven(lines, location, ["get", "set"], "\n")
    assert lines[4] == "  proven: [get, set]\n"
    assert lines[3] == "  status: verified\n", "status must keep its place"

    # Writing again replaces rather than stacking a second line.
    probe_paths._set_proven(lines, location, ["get"], "\n")
    assert lines[4] == "  proven: [get]\n"
    assert sum(1 for line in lines if line.strip().startswith("proven:")) == 1


def test_a_row_that_proved_nothing_still_says_so() -> None:
    """An empty list is a statement, a missing field is an oversight."""
    lines = ["- id: x.y\n", "  status: untested\n"]
    location = probe_paths.RowLocation(file=Path("f.yaml"), start=0, end=len(lines))
    probe_paths._set_proven(lines, location, [], "\n")
    assert lines[2] == "  proven: []\n"
