"""What unit an audio clip's times are in, and when Live's own length is out of date.

Measured against Live 12.4.6 on the separated stems of one song, all nine files exactly
228.792167 s long. The numbers in these tests are the ones Live answered with.
"""

from __future__ import annotations

from typing import Any

import pytest

from live_maestro import server

#: The file every reference clip in that set points at.
FILE_SECONDS = 228.79216666666667


def field(value: Any, unit: str = "beats", ok: bool = True) -> dict[str, Any]:
    return {"id": "clip.x", "ok": ok, "value": value, "unit": unit}


def clip_fields(*, midi: bool, warping: bool, length: float, start: float,
                end: float, looping: bool = False) -> dict[str, Any]:
    return {
        "is_midi_clip": field(midi, "none"),
        "warping": field(warping, "none"),
        "looping": field(looping, "none"),
        "length": field(length),
        "loop_start": field(0.0),
        "loop_end": field(end),
        "start_marker": field(start),
        "end_marker": field(end),
    }


@pytest.fixture
def clip(monkeypatch: pytest.MonkeyPatch):
    """Put a chosen set of clip fields in front of get_clip."""

    def install(fields: dict[str, Any]) -> dict[str, Any]:
        monkeypatch.setattr(server, "_run", lambda spec_id, **kw: {"ok": True, "value": True})
        monkeypatch.setattr(server, "_read_fields", lambda spec: (fields, None))
        monkeypatch.setattr(server, "_resolve_clip_args", lambda *a, **k: (0, 0))
        return server.get_clip(track=0, slot=0)

    return install


class TestUnit:
    def test_an_unwarped_audio_clip_reads_in_seconds(self, clip) -> None:
        out = clip(clip_fields(midi=False, warping=False, length=FILE_SECONDS,
                               start=0.0, end=FILE_SECONDS))
        assert out["times_in"] == "seconds"
        assert out["fields"]["loop_end"]["unit"] == "seconds"
        assert "in seconds on this clip" in out["notes"][0]

    def test_a_warped_audio_clip_reads_in_beats(self, clip) -> None:
        out = clip(clip_fields(midi=False, warping=True, length=488.0, start=0.0, end=488.0))
        assert out["times_in"] == "beats"
        assert out["fields"]["loop_end"]["unit"] == "beats"

    def test_a_midi_clip_is_always_in_beats(self, clip) -> None:
        out = clip(clip_fields(midi=True, warping=False, length=488.0, start=0.0, end=488.0))
        assert out["times_in"] == "beats"
        assert out["fields"]["length"]["unit"] == "beats"


class TestLengthAgainstMarkers:
    """Live does not update length when a marker is written."""

    def test_a_stale_length_is_reported(self) -> None:
        fields = clip_fields(midi=False, warping=False, length=240.793565,
                             start=0.0, end=FILE_SECONDS)
        answer = server._length_against_markers(fields)
        assert answer is not None
        assert answer["between_markers"] == pytest.approx(FILE_SECONDS, abs=1e-5)
        assert answer["gap"] == pytest.approx(12.0014, abs=1e-3)

    def test_agreement_says_nothing(self) -> None:
        fields = clip_fields(midi=False, warping=False, length=FILE_SECONDS,
                             start=0.0, end=FILE_SECONDS)
        assert server._length_against_markers(fields) is None

    def test_a_looping_clip_is_not_judged(self) -> None:
        """length is then the loop, and the markers are a different question."""
        fields = clip_fields(midi=False, warping=False, length=8.0, start=0.0,
                             end=FILE_SECONDS, looping=True)
        assert server._length_against_markers(fields) is None

    def test_a_field_that_did_not_read_is_not_guessed_at(self) -> None:
        fields = clip_fields(midi=False, warping=False, length=240.79, start=0.0,
                             end=FILE_SECONDS)
        fields["end_marker"] = field(None, "beats", ok=False)
        assert server._length_against_markers(fields) is None

    def test_float_noise_is_not_a_disagreement(self) -> None:
        fields = clip_fields(midi=False, warping=False, length=FILE_SECONDS + 0.000004,
                             start=0.0, end=FILE_SECONDS)
        assert server._length_against_markers(fields) is None
