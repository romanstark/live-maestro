"""Tests for what a caller is told before an envelope is deleted.

Nothing here restores a cleared envelope, so the report shown before the
confirmation is the only chance to decide against it. A range on its own does
not describe a curve: two envelopes with the same range can be a four bar fade
and a two beat dip, and only one of them is cheap to rebuild.

Pure logic, no socket. The ``automation_read`` replies are built as data in the
shape ``docs/protocol.md`` section 5.9 specifies.
"""

from __future__ import annotations

from typing import Any

from live_maestro import server

FADE: dict[str, Any] = {
    "parameter_name": "Track Volume",
    "has_envelope": True,
    "moves": True,
    "min": 0.013,
    "max": 0.85,
    "first": 0.013,
    "last": 0.85,
    "points": [[0.25, 0.013], [4.0, 0.22], [8.0, 0.43], [12.0, 0.64], [16.0, 0.85]],
    "ignored_at_zero": 0,
}

DIP: dict[str, Any] = {
    "parameter_name": "Track Volume",
    "has_envelope": True,
    "moves": True,
    "min": 0.6,
    "max": 0.85,
    "first": 0.85,
    "last": 0.85,
    "points": [[0.25, 0.85], [16.0, 0.85], [20.0, 0.6], [24.0, 0.85], [32.0, 0.85]],
    "ignored_at_zero": 0,
}


def test_a_fade_and_a_dip_share_a_range_and_are_told_apart_by_the_beats() -> None:
    """The two figures the old report lacked are what distinguish these at all."""
    fade = server._envelope_loss("song.tracks[1].mixer_device.volume", FADE)
    dip = server._envelope_loss("song.tracks[0].mixer_device.volume", DIP)

    assert fade["lowest_at_beat"] == 0.25
    assert fade["highest_at_beat"] == 16.0
    # A dip has its low point in the middle and both ends high, which is the
    # whole difference and is invisible in a range.
    assert dip["lowest_at_beat"] == 20.0
    assert dip["highest_at_beat"] == 0.25
    assert fade["range"] != dip["range"]


def test_the_samples_that_read_the_parameter_default_are_skipped() -> None:
    """At beat 0 Live answers the parameter default, not the curve.

    A beat taken from one of those samples points at a value the envelope does
    not contain, and for a fade from silence that value is the loudest number in
    the whole reply, so it would be reported as the peak of the fade.
    """
    with_default = dict(
        FADE,
        points=[[0.0, 0.85], *FADE["points"]],
        ignored_at_zero=1,
    )
    loss = server._envelope_loss("p", with_default)
    assert loss["lowest_at_beat"] == 0.25
    assert loss["highest_at_beat"] == 16.0, "beat 0 held the default 0.85 and is not the peak"
    assert loss["sampled_points"] == 5


def test_the_report_says_how_densely_it_looked() -> None:
    """A sample every quarter beat still hides anything narrower than that.

    The figure is there so a reader can tell whether the shape they care about
    could have fallen between two samples.
    """
    loss = server._envelope_loss("p", FADE)
    assert loss["sampled_points"] == 5
    assert loss["sampled_every_beats"] == 3.9375


def test_one_usable_sample_reports_no_density_rather_than_dividing_by_zero() -> None:
    single = dict(FADE, points=[[1.0, 0.5]], ignored_at_zero=0)
    loss = server._envelope_loss("p", single)
    assert loss["sampled_points"] == 1
    assert "sampled_every_beats" not in loss
    assert loss["lowest_at_beat"] == loss["highest_at_beat"] == 1.0


def test_nothing_read_reports_nothing_known() -> None:
    loss = server._envelope_loss("song.tracks[0].mixer_device.volume", None)
    assert loss == {
        "parameter": "song.tracks[0].mixer_device.volume",
        "has_envelope": None,
    }


def test_an_envelope_with_no_usable_samples_still_names_the_parameter() -> None:
    empty = dict(FADE, points=[], ignored_at_zero=0, has_envelope=False, moves=False)
    loss = server._envelope_loss("p", empty)
    assert loss["has_envelope"] is False
    assert "lowest_at_beat" not in loss


def test_the_dry_run_samples_finely_enough_to_find_a_short_dip() -> None:
    # 16 samples over a 32 beat clip is one every two beats, and a V floor
    # between two of them was reported about 2 dB shallower than it was.
    assert server.CLEAR_DRY_RUN_POINTS >= 128


def test_clearing_everything_names_the_route_to_what_it_is_about_to_delete() -> None:
    """An all_envelopes dry run reports one flag, so it has to say where the list is.

    ``has_envelopes`` is one boolean for the whole clip, so the dry run cannot name the
    parameters from its own reading. The list does exist: ``clip.automation_envelopes``
    returns every envelope and each one carries a ``parameter`` child that names itself
    (docs/limits.md, section 2, "A clip's envelopes can be listed, its breakpoints cannot
    (LOM)"). A report that stops at the flag and points nowhere leaves a caller believing
    the question has no answer, one confirmation away from deleting curves they never saw.
    """
    raw = (getattr(server.clear_automation, "fn", server.clear_automation).__doc__) or ""
    # The docstring is wrapped, so a phrase spanning a line break is still the
    # same claim. Assert on the sentence, not on where the lines end.
    doc = " ".join(raw.split())
    assert "which envelopes exist" not in doc
    assert "clip.automation_envelopes" in doc

    said = server.ENVELOPE_INVENTORY_ROUTE
    # An answer that names no route is half an answer.
    assert "automation_envelopes" in said
    assert "parameter.name" in said
    assert "read_automation" in said
    assert "als_read" in said
