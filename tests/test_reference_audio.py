"""Known shifts, gain, pitch differences, source offsets and recorded-clock corruption."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from live_maestro import server
from live_maestro.audio.capture import decode_capture
from live_maestro.audio.jobs import AudioJobs
from live_maestro.audio.reference import compare


def signal(path: Path, *, frequency: float = 220, delay: int = 0, gain: float = 1):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    rate = 16000
    t = np.arange(rate * 3) / rate
    samples = .2 * np.sin(2 * np.pi * frequency * t)
    samples *= .15 + np.maximum(0, np.sin(2 * np.pi * 2 * t))
    rng = np.random.default_rng(21)
    samples += rng.normal(0, .002, len(t))
    if delay:
        samples = np.r_[np.zeros(delay), samples[:-delay]]
    sf.write(path, np.column_stack((samples, samples)) * gain, rate, subtype="FLOAT")
    return samples, rate


def comparison(reference: Path, candidate: Path, output: Path) -> dict:
    return {"kind": "comparison", "reference_path": str(reference),
            "candidate_path": str(candidate), "start_seconds": 0, "end_seconds": 3,
            "candidate_start_seconds": 0, "max_alignment_ms": 100,
            "write_residual": False, "output_dir": str(output)}


def test_comparison_separates_time_gain_and_timbre(tmp_path: Path) -> None:
    reference, candidate = tmp_path / "reference.wav", tmp_path / "candidate.wav"
    signal(reference)
    signal(candidate, delay=400, gain=.5)
    result = compare(comparison(reference, candidate, tmp_path))
    assert result["candidate_delay_ms"] == pytest.approx(25, abs=.3)
    assert result["comparison_gain"] == pytest.approx(2, abs=.01)
    assert result["gain_aligned_waveform_error"] < .02
    assert result["rms_difference_db"] == pytest.approx(-6.02, abs=.1)
    assert result["quality_score"] is None
    signal(candidate, frequency=329.63)
    different = compare(comparison(reference, candidate, tmp_path))
    assert different["chroma_cosine"] < .8




def test_capture_audio_and_clock_share_the_same_frames(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    audio, rate = signal(tmp_path / "audio.wav")
    n = len(audio)
    raw = np.column_stack((audio, audio, np.arange(n) + 63,
                          np.floor(np.arange(n) / 320) * .04, np.ones(n), np.full(n, 31)))
    path = tmp_path / "raw.wav"
    sf.write(path, raw, rate, subtype="FLOAT")
    result = decode_capture(path, tmp_path)
    assert result["frames"] == n
    assert result["timeline"]["counter_first"] == 63
    assert result["timeline"]["host_update_max_observed_frames"] == 320
    assert result["timeline"]["sample_accurate_song_origin"] is False
    warmup = raw.copy()
    warmup[:, 2] = np.r_[0, np.arange(n - 1)]
    sf.write(path, warmup, rate, subtype="FLOAT")
    assert decode_capture(path, tmp_path)["timeline"]["counter_verified_from_frame"] == 1
    raw[500:, 2] += 1
    sf.write(path, raw, rate, subtype="FLOAT")
    with pytest.raises(ValueError, match="discontinuous"):
        decode_capture(path, tmp_path)


def test_file_job_verifies_and_exposes_paged_report(tmp_path: Path, monkeypatch) -> None:
    reference, candidate = tmp_path / "reference.wav", tmp_path / "candidate.wav"
    signal(reference)
    signal(candidate)
    jobs = AudioJobs(tmp_path / "state")
    try:
        request = comparison(reference, candidate, tmp_path)
        request.pop("output_dir")
        submitted = jobs.submit(request, str(tmp_path))
        deadline = time.monotonic() + 20
        while (job := jobs.get(submitted["id"]))["state"] not in {"succeeded", "failed"}:
            assert time.monotonic() < deadline
            time.sleep(.05)
        assert job["state"] == "succeeded", job
        manifest = json.loads(Path(job["manifest"]).read_text())
        assert manifest["output_sha256"]
        monkeypatch.setattr(server.audio_jobs, "manager", lambda: jobs)
        answer = server.get_audio_report(submitted["id"])
        assert answer["ok"]
        assert answer["report"]["quality_score"] is None
    finally:
        jobs.close()


def test_reference_preview_does_not_touch_live_or_create_jobs(monkeypatch) -> None:
    monkeypatch.setattr(server.audio_jobs, "manager", lambda: pytest.fail("created job manager"))
    assert server.compare_audio("a.wav", "b.wav", "output")["executed"] is False
    assert server.capture_audio("output")["executed"] is False
