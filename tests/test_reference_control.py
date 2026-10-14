"""Capture failure cleanup, source comparisons and optional runtime boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_reference_audio import comparison, signal

from live_maestro import server
from live_maestro.audio import capture
from live_maestro.audio.capture_protocol import OPEN_ADDRESS, START_ADDRESS, STOP_ADDRESS
from live_maestro.audio.reference import compare


def test_silence_has_no_invented_delay_or_gain(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    reference, candidate = tmp_path / "ref.wav", tmp_path / "silence.wav"
    signal(reference)
    sf.write(candidate, np.zeros((48000, 2)), 16000)
    result = compare(comparison(reference, candidate, tmp_path))
    assert result["candidate_delay_ms"] is None
    assert result["comparison_gain"] is None
    assert result["gain_aligned_waveform_error"] == pytest.approx(1)
    result = compare(comparison(candidate, reference, tmp_path))
    assert result["gain_aligned_waveform_error"] is None


def test_alignment_does_not_cancel_antiphase_stereo(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    reference, candidate = tmp_path / "ref.wav", tmp_path / "delayed.wav"
    audio, rate = signal(reference)
    sf.write(reference, np.column_stack((audio, -audio)), rate, subtype="FLOAT")
    delayed = np.r_[np.zeros(400), audio[:-400]]
    sf.write(candidate, np.column_stack((delayed, -delayed)), rate, subtype="FLOAT")
    result = compare(comparison(reference, candidate, tmp_path))
    assert result["candidate_delay_ms"] == pytest.approx(25, abs=.3)
    assert result["gain_aligned_waveform_error"] < .02


@pytest.mark.parametrize("failure", ["open_ack", "playback"])
def test_capture_failure_stops_recorder_and_restores_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    calls: list[Any] = []
    before = {"file_path": "test.als", "is_playing": False,
              "current_song_time": 23.5, "start_time": 8.0,
              "record_mode": False, "session_record": False}
    state = dict(before)

    class Client:
        def __init__(self, **kwargs):
            pass

        def get(self, path):
            return {"value": "Main" if path.endswith("master_track.name")
                    else state[path.removeprefix("song.")]}

        def set(self, path, value):
            state[path.removeprefix("song.")] = value
            calls.append((path, value))

        def call(self, path, method):
            calls.append(method)
            state["is_playing"] = method == "start_playing"
            if method == "start_playing" and failure == "playback":
                raise TimeoutError("playback acknowledgement lost")

        def close(self):
            calls.append("client closed")

    class Analyzer:
        port = 9882

        async def measure(self):
            return [SimpleNamespace(track_name="Main", track_id=1)]

    class Control:
        def __init__(self, *args):
            pass

        def command(self, address, path=None):
            calls.append(address)
            if address == OPEN_ADDRESS and failure == "open_ack":
                raise TimeoutError("open acknowledgement lost")
            return {"phase": address}

        def close(self):
            calls.append("control closed")

    async def no_delay(*args):
        pass

    monkeypatch.setattr(capture, "LiveClient", Client)
    monkeypatch.setattr(capture, "AnalyzerClient", Analyzer)
    monkeypatch.setattr(capture, "CaptureControl", Control)
    monkeypatch.setattr(capture.asyncio, "sleep", no_delay)
    with pytest.raises(TimeoutError):
        capture.capture({"output_dir": str(tmp_path), "track": "master",
                         "duration_seconds": 1, "from_beat": 0})
    assert STOP_ADDRESS in calls
    assert state == before
    assert "client closed" in calls and "control closed" in calls
    receipt = json.loads((tmp_path / "capture-control.json").read_text())
    assert receipt["transport_before"] == before
    if failure == "playback":
        assert START_ADDRESS in calls and "stop_playing" in calls


def test_explicit_start_uses_marker_and_restores_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def run(identifier, **kwargs):
        calls.append((identifier, kwargs))
        return {"ok": True, "value": 7 if identifier == "song.start_time" else True}

    monkeypatch.setattr(server, "_run", run)
    assert server.play(from_beat=0, continue_playing=True)["ok"]
    assert ("song.start_time", {"value": 0}) in calls
    assert ("song.start_playing", {}) in calls
    assert ("song.continue_playing", {}) not in calls
    assert calls[-2] == ("song.start_time", {"value": 7})






def test_direct_invalid_ranges_fail_before_creating_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.audio_jobs, "manager", lambda: pytest.fail("created manager"))
    assert not server.capture_audio("unused", duration_seconds=float("nan"), confirm=True)["ok"]
    assert not server.compare_audio("a", "b", "unused", max_alignment_ms=float("inf"))["ok"]
