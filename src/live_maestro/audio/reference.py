"""Measured source excerpts and specific reconstruction differences; no quality score."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path: str, start: float, end: float | None) -> tuple[Any, int, dict[str, Any]]:
    import numpy as np
    import soundfile as sf

    source = Path(path)
    with sf.SoundFile(source) as audio:
        rate = audio.samplerate
        first = math.floor(start * rate)
        last = min(audio.frames, math.floor(end * rate)) if end is not None else audio.frames
        if first < 0 or last <= first or audio.channels not in (1, 2):
            raise ValueError("excerpt must intersect finite mono/stereo audio")
        audio.seek(first)
        samples = audio.read(last - first, always_2d=True, dtype="float64")
        info = {"path": str(source), "sha256": fingerprint(source), "sample_rate": rate,
                "source_frames": audio.frames, "channels": audio.channels,
                "first_frame": first, "frames": len(samples), "start_seconds": first / rate,
                "end_seconds": (first + len(samples)) / rate}
    if not np.isfinite(samples).all():
        raise ValueError("audio contains nonfinite samples")
    return samples, rate, info


def _db(value: float) -> float:
    return round(20 * math.log10(max(1e-12, value)), 3)


def features(samples: Any, rate: int) -> dict[str, Any]:
    import numpy as np
    from scipy.signal import find_peaks, stft

    from live_maestro.analyzer.protocol import BANDS

    block = max(1, round(rate * .01))
    usable = len(samples) // block * block
    if usable < block:
        raise ValueError("excerpt must be at least 10 ms long")
    envelope = np.sqrt(np.mean(samples[:usable].reshape(-1, block, samples.shape[1]) ** 2,
                               axis=(1, 2)))
    rise = np.maximum(0, np.diff(envelope, prepend=0))
    threshold = max(float(np.median(rise) + 4 * np.median(np.abs(rise - np.median(rise)))),
                    float(rise.max()) * .06, 1e-6)
    peaks, _ = find_peaks(rise, height=threshold, distance=5)
    frame = min(4096, len(samples))
    boundary: Any = None  # SciPy's runtime accepts None; its annotation lists only strings.
    frequencies, _, spectrum = stft(samples.T, fs=rate, nperseg=frame,
                                    noverlap=frame * 3 // 4, boundary=boundary)
    power = np.mean(np.abs(spectrum) ** 2, axis=(0, 2))
    total = float(power.sum())
    bands: dict[str, float | None] = {}
    for name, low, high in BANDS:
        selected = (frequencies >= low) & (frequencies < (high or rate / 2 + 1))
        energy = float(power[selected].sum())
        bands[name] = round(energy / total, 8) if total > 1e-24 else None
    chroma = np.zeros(12)
    voiced = (frequencies >= 40) & (frequencies <= min(6000, rate / 2))
    note_bins = np.rint(69 + 12 * np.log2(frequencies[voiced] / 440)).astype(int) % 12
    np.add.at(chroma, note_bins, power[voiced])
    chroma_total = float(chroma.sum())
    chroma = chroma / chroma_total if chroma_total else chroma
    rms = float(np.sqrt(np.mean(samples ** 2)))
    peak = float(np.max(np.abs(samples)))
    correlation = None
    if samples.shape[1] == 2 and np.min(np.std(samples, axis=0)) > 1e-8:
        correlation = float(np.corrcoef(samples.T)[0, 1])
    return {"rms_dbfs": _db(rms), "sample_peak_dbfs": _db(peak),
            "crest_factor_db": round(_db(peak) - _db(rms), 3) if rms > 1e-8 else None,
            "silent": rms < 1e-8, "stereo_correlation": correlation,
            "band_power_shares": bands, "chroma": chroma.tolist(),
            "chroma_basis": "FFT harmonic energy; not identified notes or chords",
            "onsets_seconds": (peaks * block / rate).tolist(),
            "onset_resolution_seconds": block / rate,
            "envelope_hop_seconds": block / rate,
            "envelope": envelope.tolist(), "centroid_hz":
            float(np.dot(frequencies, power) / total) if total > 1e-24 else None}




def _cosine(left: Any, right: Any) -> float | None:
    import numpy as np
    norm = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(np.dot(left, right) / norm) if norm > 1e-24 else None


def compare(request: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    import soundfile as sf
    from scipy.signal import correlate, resample_poly

    start, end = request["start_seconds"], request["end_seconds"]
    reference, rate, reference_info = read(request["reference_path"], start, end)
    candidate_start = request["candidate_start_seconds"]
    candidate, candidate_rate, candidate_info = read(
        request["candidate_path"], candidate_start, candidate_start + end - start
    )
    if candidate_rate != rate:
        factor = math.gcd(rate, candidate_rate)
        candidate = resample_poly(candidate, rate // factor, candidate_rate // factor)
    if reference.shape[1] != candidate.shape[1]:
        raise ValueError("comparison requires matching channel counts; convert explicitly")
    limit = round(request["max_alignment_ms"] * rate / 1000)
    # Bound the expensive search at 4 kHz, then refine in an energetic one-second window.
    hop = max(1, rate // 4000)
    left = reference[::hop]
    right = candidate[::hop]
    alignment_available = bool(np.std(left) > 1e-8 and np.std(right) > 1e-8)
    lag = 0
    if alignment_available:
        # Sum per-channel correlations so an antiphase stereo source is not silence.
        correlation = np.sum([correlate(right[:, channel], left[:, channel], mode="full",
                                       method="fft") for channel in range(left.shape[1])], axis=0)
        zero, radius = len(left) - 1, math.ceil(limit / hop)
        low, high = max(0, zero - radius), min(len(correlation), zero + radius + 1)
        lag = (low + int(np.argmax(correlation[low:high])) - zero) * hop
        lag = max(-limit, min(limit, lag))
        low_lag, high_lag = max(-limit, lag - hop), min(limit, lag + hop)
        first = max(0, -low_lag)
        last = min(len(reference), len(candidate) - high_lag)
        if last > first:
            width = min(rate, last - first)
            windows = [(index, index + width) for index in range(first, last - width + 1, width)]
            first, last = max(windows,
                key=lambda pair: float(np.mean(reference[pair[0]:pair[1]] ** 2)))
            estimates = [(shift, float(np.sum(reference[first:last] *
                candidate[first + shift:last + shift])))
                for shift in range(low_lag, high_lag + 1)]
            lag = max(estimates, key=lambda item: item[1])[0]
    if lag > 0:
        candidate = candidate[lag:]
    elif lag < 0:
        reference = reference[-lag:]
    frames = min(len(reference), len(candidate))
    reference, candidate = reference[:frames], candidate[:frames]
    rf, cf = features(reference, rate), features(candidate, rate)
    gain = (float(np.sum(reference * candidate) / np.sum(candidate ** 2))
            if not cf["silent"] else None)
    residual = reference - candidate * (gain if gain is not None else 1)
    normalized_error = (float(np.sqrt(np.mean(residual ** 2)) /
                              np.sqrt(np.mean(reference ** 2))) if not rf["silent"] else None)
    band_deltas = {name: round(10 * math.log10(max(1e-12, cf["band_power_shares"][name]) /
                                             max(1e-12, share)), 3)
                   for name, share in rf["band_power_shares"].items()
                   if share is not None and cf["band_power_shares"][name] is not None}
    audio_paths = []
    if request.get("write_residual"):
        path = Path(request["output_dir"]) / "gain-aligned-residual.wav"
        sf.write(path, residual, rate, subtype="FLOAT")
        audio_paths.append(str(path))
    observations = [f"Candidate RMS differs by {cf['rms_dbfs'] - rf['rms_dbfs']:+.2f} dB."]
    if not alignment_available:
        observations.append("Delay cannot be estimated from silent or constant audio.")
    if abs(lag / rate) > .002:
        observations.append(f"Candidate delay relative to reference: {1000 * lag / rate:+.2f} ms.")
    if gain is not None and gain < 0:
        observations.append("The gain fit inverts polarity; this is not a sound-quality verdict.")
    observations += [f"Relative {name.removeprefix('band_')} power differs by {delta:+.2f} dB."
                     for name, delta in band_deltas.items() if abs(delta) > 4]
    return {"schema": "maestro.comparison", "schema_version": 1,
            "reference": reference_info, "candidate": candidate_info,
            "aligned_frames": frames, "analysis_sample_rate": rate,
            "candidate_delay_ms": 1000 * lag / rate if alignment_available else None,
            "alignment_available": alignment_available,
            "alignment_resolution_ms": 1000 / rate,
            "alignment_basis": "coarse 4 kHz correlation plus per-sample refinement in a "
                               "high-energy window; waveform offset, not song-clock accuracy",
            "alignment_at_search_boundary": bool(limit and abs(lag) == limit),
            "rms_difference_db": cf["rms_dbfs"] - rf["rms_dbfs"],
            "comparison_gain": gain, "gain_aligned_waveform_error": normalized_error,
            "waveform_error_basis": "phase-sensitive difference; not musical similarity or leakage",
            "band_power_differences_db": band_deltas,
            "chroma_cosine": _cosine(np.asarray(rf["chroma"]), np.asarray(cf["chroma"])),
            "envelope_cosine": _cosine(np.asarray(rf["envelope"]), np.asarray(cf["envelope"])),
            "stereo_reference": rf["stereo_correlation"],
            "stereo_candidate": cf["stereo_correlation"],
            "onsets_reference": rf["onsets_seconds"], "onsets_candidate": cf["onsets_seconds"],
            "observations": observations, "audio_paths": audio_paths,
            "quality_score": None, "quality_basis": "specific measurements; no auditory verdict"}
