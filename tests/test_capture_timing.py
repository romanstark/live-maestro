"""Capture timing: beats mapped through recorded anchors, and set-state warnings."""

from __future__ import annotations

from pathlib import Path

import pytest

from live_maestro import server
from live_maestro.audio.capture import capture_warnings, locate_beats


def _anchor(frame: int, beat: float, playing: bool = True) -> dict[str, object]:
    return {"frame": frame, "song_beat": beat, "is_playing": playing,
            "beats_valid": True, "transport_valid": True}


def test_a_beat_between_anchors_is_interpolated() -> None:
    anchors = [_anchor(0, 0.0, playing=False), _anchor(100, 16.0), _anchor(48100, 18.0)]

    (row,) = locate_beats(anchors, [17.0], 48000.0)

    assert row["basis"] == "interpolated"
    assert row["frame"] == 24100
    assert row["seconds"] == pytest.approx(24100 / 48000)


def test_a_beat_outside_the_anchors_is_extrapolated_and_says_so() -> None:
    anchors = [_anchor(0, 16.0), _anchor(24000, 17.0)]

    (row,) = locate_beats(anchors, [18.0], 48000.0)

    assert row["basis"] == "extrapolated"
    assert row["frame"] == 48000


def test_without_playing_anchors_nothing_is_guessed() -> None:
    (row,) = locate_beats([_anchor(0, 0.0, playing=False)], [4.0], 48000.0)
    assert row["frame"] is None and row["basis"] == "no_anchors"


def test_a_lit_back_to_arrangement_is_named() -> None:
    warnings = capture_warnings({"back_to_arranger": True}, None, None)
    assert any("Back to Arrangement" in w for w in warnings)


def test_a_mid_song_start_without_lead_in_is_named_and_a_lead_in_is_not() -> None:
    assert capture_warnings({"back_to_arranger": False}, 64.0, None)
    assert capture_warnings({"back_to_arranger": False}, 64.0, 0.0) == []
    assert capture_warnings({"back_to_arranger": False}, 0.0, None) == []


def test_a_lead_in_after_the_window_is_refused() -> None:
    answer = server.capture_audio("/tmp/x", from_beat=8.0, play_from_beat=16.0)
    assert answer["ok"] is False


def test_a_lead_in_without_a_window_is_refused() -> None:
    answer = server.capture_audio("/tmp/x", play_from_beat=0.0)
    assert answer["ok"] is False


def test_the_plan_carries_the_lead_in() -> None:
    answer = server.capture_audio("/tmp/x", from_beat=64.0, play_from_beat=0.0)
    assert answer["plan"]["play_from_beat"] == 0.0


class _EnvelopeClient:
    def __init__(self, has: bool) -> None:
        self.has = has

    def call(self, path: str, method: str, args: object) -> dict[str, object]:
        return {"path": path, "method": method, "result": None}

    def batch(self, ops: object, **_: object) -> dict[str, object]:
        return {"results": [{"status": "success", "value": self.has}]}


@pytest.mark.parametrize("has", [True, False])
def test_moving_notes_warns_only_when_the_clip_has_envelopes(
    monkeypatch: pytest.MonkeyPatch, has: bool
) -> None:
    monkeypatch.setattr(server, "_client_instance", lambda: _EnvelopeClient(has))
    answer = server.lom_call(
        "song.tracks[0].arrangement_clips[0]", "duplicate_region", [0, 4, 4, -1, 0]
    )
    assert ("warning" in answer) is has


def test_a_group_reference_is_the_sum_of_its_stems(tmp_path: Path) -> None:
    import numpy as np
    import soundfile as sf

    from live_maestro.audio.reference import read_sum

    folder = tmp_path
    tone = 0.25 * np.sin(np.linspace(0, 200 * np.pi, 4800))
    sf.write(folder / "a.wav", np.c_[tone, tone], 48000)
    sf.write(folder / "b.wav", tone, 48000)

    summed, rate, info = read_sum(
        [str(folder / "a.wav"), str(folder / "b.wav")], 0, None)

    assert rate == 48000 and summed.shape == (4800, 2)
    assert np.allclose(summed[:, 0], 2 * tone, atol=1e-3)
    assert len(info["summed_from"]) == 2


class _ArrangementClient:
    def batch(self, ops: list[dict[str, str]], **_: object) -> dict[str, object]:
        values = {"start_time": 32.0, "end_time": 40.0, "start_marker": 0.0, "length": 8.0,
                  "looping": False, "has_envelopes": True}
        return {"results": [{"status": "success", "value": values[op["path"].rsplit(".", 1)[1]]}
                            for op in ops]}

    def send(self, handler: str, params: dict[str, object]) -> dict[str, object]:
        assert params["path"] == "song.tracks[1].arrangement_clips[0]"
        return {"count": 1, "api": "get_notes_extended",
                "notes": [{"pitch": 60, "start_time": 1.0, "duration": 0.5, "velocity": 100,
                           "mute": False}]}


def test_an_arrangement_clip_is_read_with_its_place_in_the_song(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_client_instance", lambda: _ArrangementClient())

    answer = server.read_clip_notes(path="song.tracks[1].arrangement_clips[0]")

    assert answer["ok"] is True, answer
    assert answer["notes"][0]["pitch"] == 60
    assert answer["clip_start_time"] == 32.0
    assert answer["clip_has_envelopes"] is True
    assert answer["check"]["ok"] is True, answer["check"]
