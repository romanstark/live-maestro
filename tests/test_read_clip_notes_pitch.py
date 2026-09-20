"""Reading part of a clip by pitch.

The LOM takes a pitch window on ``get_notes_extended`` and answers it with note handles
that carry no fields, so a caller could count notes in a range and never see them. This
reads them. The filter is applied here rather than in the Remote Script, so an installed
script of any age answers the same.
"""

from __future__ import annotations

from typing import Any

import pytest

from live_maestro import server

#: One bar of a guitar part: two notes below a guitar's lowest string and three on it.
CLIP_NOTES: list[dict[str, Any]] = [
    {"pitch": 30, "start_time": 0.0, "duration": 0.5, "velocity": 90.0, "mute": False},
    {"pitch": 38, "start_time": 0.5, "duration": 0.5, "velocity": 90.0, "mute": False},
    {"pitch": 40, "start_time": 1.0, "duration": 0.5, "velocity": 90.0, "mute": False},
    {"pitch": 55, "start_time": 1.5, "duration": 0.5, "velocity": 90.0, "mute": False},
    {"pitch": 67, "start_time": 2.0, "duration": 0.5, "velocity": 90.0, "mute": False},
]


class _FakeLive:
    """Answers ``notes_get`` with a fixed clip, and records what was asked for."""

    def __init__(self) -> None:
        self.asked: dict[str, Any] = {}

    def send(self, handler: str, params: dict[str, Any]) -> dict[str, Any]:
        assert handler == "notes_get"
        self.asked = params
        if params.get("count_only"):
            return {"count": len(CLIP_NOTES)}
        return {"notes": list(CLIP_NOTES), "count": len(CLIP_NOTES), "api": "extended"}


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> _FakeLive:
    fake = _FakeLive()
    monkeypatch.setattr(server, "_client_instance", lambda: fake)
    return fake


def test_a_ceiling_returns_only_what_is_under_it(live: _FakeLive) -> None:
    out = server.read_clip_notes(track=0, slot=0, pitch_max=39, check=False)
    assert [n["pitch"] for n in out["notes"]] == [30, 38]


def test_a_floor_returns_only_what_is_over_it(live: _FakeLive) -> None:
    out = server.read_clip_notes(track=0, slot=0, pitch_min=55, check=False)
    assert [n["pitch"] for n in out["notes"]] == [55, 67]


def test_both_bounds_are_inclusive(live: _FakeLive) -> None:
    out = server.read_clip_notes(track=0, slot=0, pitch_min=38, pitch_max=40, check=False)
    assert [n["pitch"] for n in out["notes"]] == [38, 40]


def test_the_count_is_of_what_came_back_not_of_the_clip(live: _FakeLive) -> None:
    """A count beside a filtered list has to mean the same thing the list does."""
    out = server.read_clip_notes(track=0, slot=0, pitch_max=39, check=False)
    assert out["count"] == 2
    assert out["pitch_range"] == [None, 39]


def test_counting_a_range_counts_here_because_live_cannot(live: _FakeLive) -> None:
    out = server.read_clip_notes(track=0, slot=0, pitch_max=39, count_only=True)
    assert out["count"] == 2
    assert not live.asked.get("count_only")


def test_counting_the_whole_clip_still_counts_inside_live(live: _FakeLive) -> None:
    """Without a range there is nothing to filter, so the notes stay in Live."""
    out = server.read_clip_notes(track=0, slot=0, count_only=True)
    assert out["count"] == len(CLIP_NOTES)
    assert live.asked.get("count_only") is True
    assert out["pitch_range"] is None


def test_no_range_leaves_every_note_in_place(live: _FakeLive) -> None:
    out = server.read_clip_notes(track=0, slot=0, check=False)
    assert len(out["notes"]) == len(CLIP_NOTES)
    assert "pitch_range" not in out
