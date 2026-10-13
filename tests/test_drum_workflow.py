"""Known attacks, source offsets, tempo changes, MIDI export and real worker cancellation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from live_maestro.audio.jobs import AudioJobs
from live_maestro.audio.timeline import notes_from_analysis, to_beat, validate_map, write_midi


def fixture(path: Path, positions: list[float], rate: int = 44100, snare: bool = False):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    samples = np.zeros((rate * 4, 2))
    rng = np.random.default_rng(1234)
    for number, seconds in enumerate(positions):
        start = round(seconds * rate)
        t = np.arange(round(rate * 0.07)) / rate
        carrier = rng.uniform(-1, 1, len(t)) if snare else np.sin(2 * np.pi * 70 * t)
        hit = (0.5 + number * 0.03) * carrier * np.exp(-t * 60)
        samples[start : start + len(hit)] += hit[:, None]
    sf.write(path, samples, rate, subtype="PCM_24")
    return samples


def request(kick: Path, snare: Path) -> dict:
    return {
        "stems": [
            {
                "path": str(kick),
                "instrument": "kick",
                "source_offset_seconds": 5,
                "calibration_ms": 0,
                "midi_pitch": None,
                "reference_path": str(kick),
            },
            {
                "path": str(snare),
                "instrument": "snare",
                "source_offset_seconds": 5,
                "calibration_ms": 0,
                "midi_pitch": 40,
                "reference_path": None,
            },
        ],
        "tempo_bpm": None,
        "beat_anchors": [
            {"seconds": 0, "beat": 0},
            {"seconds": 5, "beat": 10},
            {"seconds": 9, "beat": 16},
        ],
        "sensitivity": 6,
        "cut_one_shots": True,
        "pre_roll_ms": 10,
        "tail_ms": 100,
    }


def terminal(jobs: AudioJobs, identifier: str):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        job = jobs.get(identifier)
        if job["state"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("audio worker did not complete")


def test_worker_known_hits_source_offset_midi_and_one_shots(tmp_path: Path):
    pytest.importorskip("scipy")
    kick, snare = tmp_path / "kick.wav", tmp_path / "snare.wav"
    fixture(kick, [0.1, 0.6, 1.1, 1.6, 2.1, 2.6, 3.1, 3.6])
    fixture(snare, [0.35, 1.35, 2.35, 3.35], snare=True)
    jobs = AudioJobs(tmp_path / "data")
    try:
        started = jobs.submit(request(kick, snare), str(tmp_path / "outputs"))
        result = terminal(jobs, started["id"])
        assert result["state"] == "succeeded", result
        data = json.loads(Path(result["result"]["analysis_path"]).read_text())
        kick_events = [event for event in data["events"] if event["instrument"] == "kick"]
        snare_events = [event for event in data["events"] if event["instrument"] == "snare"]
        assert len(kick_events) == 8 and len(snare_events) == 4, data["events"]
        for event, expected in zip(kick_events, [5.1, 5.6, 6.1, 6.6, 7.1, 7.6, 8.1, 8.6]):
            assert abs(event["source_seconds"] - expected) < 0.002
        assert all(event["midi_pitch"] == 40 for event in snare_events)
        notes = notes_from_analysis(data)
        assert abs(notes[0]["start_time"] - 10.15) < 0.003
        assert not data["tempo_map"]["review_required"]
        assert Path(result["result"]["midi_path"]).read_bytes().startswith(b"MThd")
        for item in data["one_shots"]:
            assert Path(item["path"]).exists()
            assert item["attack_sample"] > 0 and item["overlapping_hits"] == 0
            if "kick" in item["path"]:
                assert item["contamination"]["residual_rms"] == 0
            else:
                assert not item["contamination"]["available"]
        assert Path(result["manifest"]).exists()
    finally:
        jobs.close()


def test_real_worker_cancel_queued_and_resume(tmp_path: Path):
    pytest.importorskip("scipy")
    kick, snare = tmp_path / "kick.wav", tmp_path / "snare.wav"
    fixture(kick, [0.1, 0.6])
    fixture(snare, [0.35], snare=True)
    jobs = AudioJobs(tmp_path / "data")
    try:
        first = jobs.submit(request(kick, snare), str(tmp_path / "outputs"))
        second = jobs.submit(request(kick, snare), str(tmp_path / "outputs"))
        deadline = time.monotonic() + 10
        while first["id"] not in jobs.processes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert jobs.cancel(second["id"])["state"] == "cancelled"
        assert jobs.cancel(first["id"])["state"] == "cancelled"
        following = jobs.submit(request(kick, snare), str(tmp_path / "outputs"))
        assert terminal(jobs, following["id"])["state"] == "succeeded"
        assert jobs.get(first["id"])["state"] == "cancelled"
        assert not (Path(first["output_dir"]) / "manifest.json").exists()
    finally:
        jobs.close()


def test_piecewise_seconds_to_beats_and_invalid_events(tmp_path: Path):
    anchors = validate_map(
        [{"seconds": 0, "beat": 0}, {"seconds": 2, "beat": 4}, {"seconds": 6, "beat": 8}]
    )
    assert to_beat(1, anchors) == 2 and to_beat(4, anchors) == 6
    data = {
        "schema": "maestro.drum-events",
        "schema_version": 1,
        "tempo_map": {"anchors": anchors},
        "events": [
            {"source_seconds": 1, "midi_pitch": 36, "velocity": 100},
            {"source_seconds": 4, "midi_pitch": 38, "velocity": 80},
        ],
    }
    path = tmp_path / "drums.mid"
    write_midi(path, data)
    raw = path.read_bytes()
    assert raw.count(b"\xff\x51\x03") == 2
    assert b"\x99\x24\x64" in raw and b"\x99\x26\x50" in raw
    assert [item["start_time"] for item in notes_from_analysis(data)] == [2, 6]
    with pytest.raises(ValueError):
        notes_from_analysis(data, clip_origin_beat=3)
    with pytest.raises(ValueError):
        validate_map([{"seconds": 0, "beat": 0}, {"seconds": 1, "beat": 0}])


def test_daw_previews_never_contact_live(tmp_path: Path, monkeypatch):
    from live_maestro import server

    data = {
        "schema": "maestro.drum-events",
        "schema_version": 1,
        "tempo_map": {"anchors": [{"seconds": 0, "beat": 0}, {"seconds": 2, "beat": 4}]},
        "events": [{"source_seconds": 1, "midi_pitch": 36, "velocity": 100}],
    }
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(
        server, "_client_instance", lambda: (_ for _ in ()).throw(AssertionError("Live contacted"))
    )
    assert server.write_drum_clip(str(path), 0, 0)["blocked"]
    assert server.apply_tempo_map(str(path))["blocked"]
    data["tempo_map"]["anchors"].append({"seconds": 4, "beat": 6})
    path.write_text(json.dumps(data))
    assert server.apply_tempo_map(str(path), True)["code"] == "arrangement_import_required"
