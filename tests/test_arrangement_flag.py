"""Tests for the survey's answer to "does this track hold Arrangement material?".

The field exists because the Session grid alone is not enough to decide whether a
track is safe to delete. What it must never do is answer "empty" when it does not
know: a false all-clear on that question is worse than no answer, because the
next step is a deletion.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from live_maestro.lom import introspect


class NeverReached:
    """Stands in for the client, and complains if anything actually calls it.

    Every read in these tests goes through a patched ``_get_many``, so a call
    landing here means the probe reached Live and the test is measuring
    something other than the decision it was written for.
    """

    def get(self, path: str) -> Mapping[str, Any]:
        raise AssertionError(f"the probe read {path!r} outside _get_many")

    def describe(self, path: str) -> Mapping[str, Any]:
        raise AssertionError(f"the probe described {path!r}")

    def batch(self, ops: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        raise AssertionError("the probe batched outside _get_many")


TRACKS: list[dict[str, Any]] = [
    {"path": "song.tracks[0]", "kind": "midi"},
    {"path": "song.tracks[1]", "kind": "audio"},
    {"path": "song.tracks[2]", "kind": "group"},
    {"path": "song.tracks[3]", "kind": "midi"},
]


def answers(monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any] | None]) -> dict:
    """Run the probe against a fixed set of batch replies."""
    seen: list[str] = []

    def fake(_client: object, wanted: Sequence[str]) -> list[dict[str, Any] | None]:
        seen.extend(wanted)
        return entries

    monkeypatch.setattr(introspect, "_get_many", fake)
    flags = introspect._read_arrangement_flags(NeverReached(), TRACKS)
    return {"flags": flags, "probed": seen}


def test_it_probes_one_path_per_track_and_skips_group_tracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One read per track, not a walk of the collection: the question is whether
    # anything is there, and index 0 settles that.
    out = answers(monkeypatch, [{"value": "a"}, {"value": "b"}, {"value": "c"}])
    assert out["probed"] == [
        "song.tracks[0].arrangement_clips[0].name",
        "song.tracks[1].arrangement_clips[0].name",
        "song.tracks[3].arrangement_clips[0].name",
    ]
    # A group track holds no Arrangement clips at all, so probing it would
    # produce a refusal that means nothing.
    assert "song.tracks[2]" not in out["flags"]


def test_a_readable_index_zero_means_there_is_something_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = answers(monkeypatch, [{"value": "Kick 8bar"}, {"value": ""}, {"value": "x"}])
    assert out["flags"]["song.tracks[0]"] is True
    # An empty clip name is still a clip.
    assert out["flags"]["song.tracks[1]"] is True


def test_index_out_of_range_is_the_answer_none(monkeypatch: pytest.MonkeyPatch) -> None:
    out = answers(
        monkeypatch,
        [
            {"code": "index_out_of_range", "message": "index 0 is beyond the 0 entries"},
            {"value": "a"},
            {"code": "index_out_of_range"},
        ],
    )
    assert out["flags"]["song.tracks[0]"] is False
    assert out["flags"]["song.tracks[1]"] is True
    assert out["flags"]["song.tracks[3]"] is False


def test_any_other_refusal_stays_unknown_rather_than_becoming_an_all_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """False means "the Arrangement is empty". Nothing else may produce it.

    A refusal this probe does not understand says nothing about whether the
    track holds material, and reporting it as empty would invite a deletion on
    the strength of a read that failed.
    """
    out = answers(
        monkeypatch,
        [{"code": "no_such_path"}, {"status": "error"}, None],
    )
    assert out["flags"]["song.tracks[0]"] is None
    assert out["flags"]["song.tracks[1]"] is None
    assert out["flags"]["song.tracks[3]"] is None


def test_no_tracks_means_no_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        introspect, "_get_many", lambda *a, **k: calls.append(a) or []  # type: ignore[func-returns-value]
    )
    assert introspect._read_arrangement_flags(NeverReached(), []) == {}
    assert introspect._read_arrangement_flags(
        NeverReached(), [{"path": "x", "kind": "group"}]
    ) == {}
    assert calls == [], "a survey with nothing to probe must not talk to Live"


def test_the_survey_field_carries_the_flag_through_to_the_dictionary() -> None:
    view = introspect.TrackView(
        index=0, path="song.tracks[0]", name="Drums", kind="midi", has_arrangement_clips=True
    )
    payload = view.to_dict()
    assert payload["has_arrangement_clips"] is True
    # The Session list says in its name what it covers, so a reader who sees it
    # empty does not conclude the track is empty.
    assert payload["session_clips"] == []
    assert "clips" not in payload
