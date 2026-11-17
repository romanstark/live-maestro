"""Beat-aligned spectrum differences on synthetic audio with known notes, filters and lags."""

from __future__ import annotations

from pathlib import Path

import pytest

from live_maestro import server
from live_maestro.audio.requests import BeatAnchor
from live_maestro.audio.spectrum import compare_spectrum

RATE = 16000
STEADY = [{"seconds": 0.0, "beat": 0.0}, {"seconds": 8.0, "beat": 16.0}]


def tone(np, midi: int, seconds: float):
    t = np.arange(round(seconds * RATE)) / RATE
    frequency = 440 * 2 ** ((midi - 69) / 12)
    return sum(.2 / k * np.sin(2 * np.pi * k * frequency * t) for k in (1, 2, 3))


def write(path: Path, samples) -> str:
    sf = pytest.importorskip("soundfile")
    sf.write(path, samples, RATE, subtype="FLOAT")
    return str(path)


def request(tmp_path: Path, reference: str, candidate: str, **extra) -> dict:
    base = {"kind": "spectrum", "reference_path": reference, "reference_anchors": STEADY,
            "candidate_path": candidate, "candidate_anchors": STEADY, "from_beat": 0.0,
            "to_beat": 8.0, "resolution": "bands", "steps_per_beat": 24, "beats_per_bar": 4,
            "low_hz": 30, "high_hz": 7000, "low_note": 45, "high_note": 72,
            "chain_offset_ms": 0, "apply_estimated_offset": False, "threshold_db": 6,
            "max_regions": 40, "window_ms": None, "write_image": False,
            "output_dir": str(tmp_path)}
    return {**base, **extra}


def test_semitones_name_the_missing_and_the_extra_note(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    a3, b3, c4 = 57, 59, 60
    reference = np.r_[tone(np, a3, 2), tone(np, b3, 2), np.zeros(RATE * 4)]
    candidate = np.r_[tone(np, a3, 2), tone(np, c4, 2), np.zeros(RATE * 4)]
    report = compare_spectrum(request(
        tmp_path, write(tmp_path / "r.wav", reference), write(tmp_path / "c.wav", candidate),
        resolution="semitones"))
    regions = report["regions"]
    missing = [item for item in regions if item["kind"] == "missing"]
    excess = [item for item in regions if item["kind"] == "excess"]
    assert missing[0]["low"]["note"] == "B3" == missing[0]["high"]["note"]
    assert excess[0]["low"]["note"] == "C4" == excess[0]["high"]["note"]
    for item in (missing[0], excess[0]):
        assert 3.5 <= item["start_beat"] <= 4.5 and 7.5 <= item["end_beat"] <= 8.5
    assert not any(item["low"]["note"] == "A3" and item["end_beat"] < 3.5 for item in regions)
    assert report["sections"][1]["sound_difference_db"] > 0


def test_bands_show_highs_a_low_pass_removed(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    signal = pytest.importorskip("scipy.signal")
    noise = np.random.default_rng(3).normal(0, .1, RATE * 8)
    sos = signal.butter(8, 800, fs=RATE, output="sos")
    report = compare_spectrum(request(
        tmp_path, write(tmp_path / "r.wav", noise),
        write(tmp_path / "c.wav", signal.sosfilt(sos, noise))))
    top = report["regions"][0]
    assert top["kind"] == "missing" and top["low"] >= 700
    assert all(item["high"] <= 900 for item in report["regions"] if item["kind"] == "excess")


def clicks(np, delay_ms: float = 0):
    out = np.zeros(RATE * 8)
    burst = np.random.default_rng(5).normal(0, .5, 80) * np.exp(-np.arange(80) / 15)
    for beat in range(16):
        start = round((beat * .5 + delay_ms / 1000) * RATE)
        out[start:start + 80] += burst
    return out + .05 * np.sin(2 * np.pi * 110 * np.arange(len(out)) / RATE)


def test_offset_estimate_finds_unreported_latency(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    reference = write(tmp_path / "r.wav", clicks(np))
    candidate = write(tmp_path / "c.wav", clicks(np, 65))
    late = compare_spectrum(request(tmp_path, reference, candidate))
    assert late["offset"]["estimated_offset_ms"] == pytest.approx(65, abs=1.5)
    assert late["observations"][0].startswith("Candidate attacks lag")
    calibrated = compare_spectrum(request(tmp_path, reference, candidate, chain_offset_ms=65))
    assert calibrated["offset"]["estimated_offset_ms"] == pytest.approx(0, abs=1.5)
    assert calibrated["attacks"]["summary"]["median_delta_ms"] == pytest.approx(0, abs=1.5)
    assert calibrated["mean_abs_difference_db"] < late["mean_abs_difference_db"]
    applied = compare_spectrum(request(tmp_path, reference, candidate,
                                       apply_estimated_offset=True))
    assert applied["offset"]["applied_ms"] == pytest.approx(65, abs=1.5)
    summary = applied["attacks"]["summary"]
    assert summary["paired"] == summary["reference"] >= 7


def test_each_file_follows_its_own_tempo_map(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    a3, d4 = 57, 62
    reference = np.r_[tone(np, a3, 2.0), tone(np, d4, 1.6), np.zeros(RATE)]
    candidate = np.r_[tone(np, a3, 2.0), tone(np, d4, 2.0), np.zeros(RATE)]
    report = compare_spectrum(request(
        tmp_path, write(tmp_path / "r.wav", reference), write(tmp_path / "c.wav", candidate),
        resolution="semitones", reference_anchors=[{"seconds": 0, "beat": 0},
            {"seconds": 2, "beat": 4}, {"seconds": 3.6, "beat": 8}],
        candidate_anchors=[{"seconds": 0, "beat": 0}, {"seconds": 4, "beat": 8}]))
    assert not [item for item in report["regions"] if item["end_beat"] - item["start_beat"] > 1]


def test_anchors_must_cover_the_range(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    path = write(tmp_path / "r.wav", np.zeros(RATE))
    with pytest.raises(ValueError, match="cover"):
        compare_spectrum(request(tmp_path, path, path, to_beat=20.0))


def test_tool_plans_without_confirm_and_refuses_a_missing_candidate(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    path = write(tmp_path / "r.wav", np.zeros(RATE))
    anchors = [BeatAnchor(**item) for item in STEADY]
    plan = server.compare_spectrum(path, anchors, str(tmp_path), 0, 8,
                                   candidate_path=path, candidate_anchors=anchors)
    assert plan["executed"] is False and plan["plan"]["kind"] == "spectrum"
    refused = server.compare_spectrum(path, anchors, str(tmp_path), 0, 8)
    assert refused["ok"] is False and "candidate" in refused["error"]


def test_image_has_three_panels_when_matplotlib_is_installed(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    reference = write(tmp_path / "r.wav", clicks(np))
    report = compare_spectrum(request(tmp_path, reference, reference, write_image=True))
    image = Path(report["image_paths"][0])
    assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_k_weighting_reads_a_full_scale_1khz_sine_at_minus_3_lufs() -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    from live_maestro.audio.spectrum import k_weighted

    t = np.arange(RATE * 2) / RATE
    weighted = k_weighted(np.sin(2 * np.pi * 1000 * t), RATE)[RATE // 2:]
    assert -0.691 + 10 * np.log10(np.mean(weighted ** 2)) == pytest.approx(-3.01, abs=0.05)


def test_level_and_sound_come_apart_per_section(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    reference = clicks(np)
    gain = np.ones(len(reference))
    gain[: RATE * 2] = 10 ** (-9 / 20)
    gain[RATE * 2: RATE * 4] = 10 ** (-3 / 20)
    gain[RATE * 2: RATE * 3] = 0.0  # the candidate is silent for half of bar 2
    report = compare_spectrum(request(
        tmp_path, write(tmp_path / "r.wav", reference),
        write(tmp_path / "c.wav", reference * gain)))
    first, second = report["sections"]
    assert first["level_difference_db"] == pytest.approx(9, abs=0.3)
    assert second["level_difference_db"] == pytest.approx(3, abs=0.3)
    assert first["sound_difference_db"] < 0.5
    assert first["true_peak_reference_dbtp"] - first["true_peak_candidate_dbtp"] == pytest.approx(
        9, abs=0.3)


def test_sections_can_be_reference_seconds(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    path = write(tmp_path / "r.wav", clicks(np))
    report = compare_spectrum(request(tmp_path, path, path, sections=[
        {"from_seconds": 0.5, "to_seconds": 1.5, "from_beat": None, "to_beat": None},
        {"from_beat": 6.0, "to_beat": 7.0, "from_seconds": None, "to_seconds": None}]))
    spans = [(row["from_beat"], row["to_beat"]) for row in report["sections"]]
    assert spans == [(1.0, 3.0), (6.0, 7.0)]
    assert all(row["level_difference_db"] == pytest.approx(0, abs=0.01)
               for row in report["sections"])
