"""Beat-aligned time-frequency difference between a reference and a rendered candidate.

Each file is read through its own source-second/quarter-beat anchors and sampled on
one beat grid, so a reference tempo map and a capture's host clock line up. Rows are
1/12-octave bands or semitones; semitone rows use harmonic salience (f0 to 5 f0) from a
harmonic sieve, so a difference names a note rather than a partial. Each section
(bars, beat ranges or seconds) is matched on K-weighted loudness over the blocks where
both files sound, so the sound difference and the level difference come out apart.
Positive differences are energy the candidate lacks, negative ones excess. High-band
envelopes give attack positions, attack widths and an offset estimate, which finds
plugin latency that Live never hears about. Nothing here is an auditory verdict.
"""

from __future__ import annotations

import importlib
import importlib.util
import itertools
import math
from pathlib import Path
from typing import Any

from live_maestro.audio.reference import fingerprint
from live_maestro.audio.timeline import validate_map

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
#: Partial weights and semitone distances used for semitone salience.
HARMONICS = ((1.0, 0), (0.5, 12), (0.33, 19), (0.25, 24), (0.2, 28))
#: Most voices the semitone sieve takes from one frame.
MAX_VOICES = 6
#: The sieve stops at voices this far below the frame's strongest.
VOICE_RANGE_DB = 30.0
#: Cells further below the loudest reference cell are clipped to this floor.
FLOOR_DB = 60.0
#: A frame takes part when its loudest row is within this of the loudest cell.
ACTIVE_DB = 35.0
#: Absolute gate for a sounding loudness block, as in BS.1770.
LOUDNESS_GATE_LUFS = -70.0
#: A block quieter than this below its file's loudest block in the section is a pause.
SECTION_GATE_DB = 20.0
#: Loudness block length in seconds.
LOUDNESS_BLOCK = 0.1
#: Offset search radius. Unreported plugin latency was measured at 45 to 55 ms.
OFFSET_SEARCH_MS = 150.0
#: Band whose envelope marks attacks: transients, not sustained pitch.
ATTACK_BAND_HZ = (1500.0, 8000.0)
#: Attacks this close in time are paired as one attack in both files.
ATTACK_MATCH_MS = 60.0


def note_name(midi: int) -> str:
    """Return a note name with octave, middle C as C4."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def beat_seconds(beats: Any, anchors: list[dict[str, Any]]) -> Any:
    """Return source seconds for quarter beats inside the anchors' range.

    Raises:
        ValueError: when the anchors are invalid or do not cover every beat.
    """
    import numpy as np

    mapping = validate_map(anchors)
    known = np.array([item["beat"] for item in mapping])
    seconds = np.array([item["seconds"] for item in mapping])
    values = np.asarray(beats, dtype=float)
    if values.min() < known[0] - 1e-9 or values.max() > known[-1] + 1e-9:
        raise ValueError("anchors must cover from_beat..to_beat; extrapolation is refused")
    return np.interp(values, known, seconds)


def _excerpt(path: str, first: float, last: float) -> tuple[Any, int, float, dict[str, Any]]:
    """Read mono samples covering [first, last] seconds, zero padded beyond the file."""
    import numpy as np
    import soundfile as sf

    source = Path(path)
    with sf.SoundFile(source) as audio:
        rate = audio.samplerate
        if audio.channels not in (1, 2):
            raise ValueError("audio must be mono or stereo; a capture's raw_path is not audio")
        start = math.floor(first * rate)
        stop = math.ceil(last * rate)
        audio.seek(min(max(0, start), audio.frames))
        data = audio.read(max(0, min(stop, audio.frames) - max(0, start)),
                          always_2d=True, dtype="float64")
        info = {"path": str(source), "sha256": fingerprint(source), "sample_rate": rate,
                "frames": audio.frames, "channels": audio.channels}
    if not np.isfinite(data).all():
        raise ValueError("audio contains nonfinite samples")
    mono = data.mean(axis=1)
    mono = np.r_[np.zeros(max(0, -start)), mono]
    mono = np.r_[mono, np.zeros(max(0, stop - start - len(mono)))]
    return mono, rate, start / rate, info


def _power(samples: Any, rate: int, origin: float, times: Any, size: int) -> Any:
    """Return Hann-windowed power spectra centred on each time, one row per time."""
    import numpy as np

    window = np.hanning(size)
    centres = np.rint((np.asarray(times) - origin) * rate).astype(int) - size // 2
    out = np.empty((len(centres), size // 2 + 1))
    span = np.arange(size)
    for first in range(0, len(centres), 256):
        index = np.clip(centres[first:first + 256, None] + span, 0, len(samples) - 1)
        out[first:first + 256] = np.abs(np.fft.rfft(samples[index] * window, axis=1)) ** 2
    return out


def _rows(power: Any, frequencies: Any, centres: Any) -> Any:
    """Sum power per 1/12-octave row; rows narrower than a bin interpolate the spectrum."""
    import numpy as np

    rows = np.zeros((power.shape[0], len(centres)))
    for column, centre in enumerate(centres):
        if centre >= frequencies[-1]:
            continue
        low, high = centre * 2 ** (-1 / 24), centre * 2 ** (1 / 24)
        selected = (frequencies >= low) & (frequencies < high)
        if selected.any():
            rows[:, column] = power[:, selected].sum(axis=1)
        else:
            right = int(np.searchsorted(frequencies, centre))
            below, above = frequencies[right - 1], frequencies[right]
            weight = (centre - below) / (above - below)
            rows[:, column] = power[:, right - 1] * (1 - weight) + power[:, right] * weight
    return rows


def _salience(power: Any, frequencies: Any, notes: Any) -> Any:
    """Return per-semitone salience from a harmonic sieve, one voice at a time.

    Only spectral peaks count, so window leakage into the neighbouring semitone
    stays silent. Each round takes the strongest harmonic sum in every frame, keeps
    it for that pitch and removes its partials, so an overtone is not a note. Notes
    up to two octaves below the rows compete too. An octave doubling folds into the
    lower note. Voices weaker than VOICE_RANGE_DB below the first are dropped.
    """
    import numpy as np

    lowest = int(notes[0]) - 24
    pitches = np.arange(lowest, int(notes[-1]) + HARMONICS[-1][1] + 1)
    energy = _rows(power, frequencies, 440 * 2 ** ((pitches - 69) / 12))
    padded = np.pad(energy, ((0, 0), (1, 1)))
    energy[(energy < padded[:, :-2]) | (energy < padded[:, 2:])] = 0
    candidates = np.arange(lowest, int(notes[-1]) + 1) - lowest
    steps = np.array([step for _, step in HARMONICS])
    weights = np.array([weight for weight, _ in HARMONICS])
    frames = np.arange(len(energy))
    result = np.zeros((len(energy), len(candidates)))
    first = None
    for _ in range(MAX_VOICES):
        sums = energy[:, candidates[:, None] + steps] @ weights
        best = np.argmax(sums, axis=1)
        value = sums[frames, best]
        first = value if first is None else first
        keep = (value > 0) & (value >= first * 10 ** (-VOICE_RANGE_DB / 10))
        result[frames[keep], best[keep]] = value[keep]
        for step in steps:
            energy[frames[keep], candidates[best[keep]] + step] = 0
    return result[:, int(notes[0]) - lowest:]


def _envelope(samples: Any, rate: int, origin: float, times: Any) -> Any:
    """Return the attack-band RMS envelope (1 ms blocks) sampled at the given times."""
    import numpy as np
    from scipy.signal import butter, sosfiltfilt

    high = min(ATTACK_BAND_HZ[1], 0.45 * rate)
    sos = butter(4, [ATTACK_BAND_HZ[0], high], btype="band", fs=rate, output="sos")
    filtered = sosfiltfilt(sos, samples)
    block = max(1, round(rate * 0.001))
    usable = len(filtered) // block * block
    rms = np.sqrt(np.mean(filtered[:usable].reshape(-1, block) ** 2, axis=1))
    centres = origin + (np.arange(len(rms)) + 0.5) * block / rate
    return np.interp(times, centres, rms)


def estimate_offset(reference: Any, candidate: Any, step_ms: float) -> tuple[float | None, float]:
    """Return the candidate's lag in ms (positive: later) and the normalised correlation.

    Both envelopes share one grid of step_ms. Onset strength (positive envelope rise)
    is correlated, so sustained level does not pull the estimate.
    """
    import numpy as np

    left = np.maximum(0, np.diff(reference))
    right = np.maximum(0, np.diff(candidate))
    norm = float(np.linalg.norm(left) * np.linalg.norm(right))
    radius = min(len(left) - 1, int(OFFSET_SEARCH_MS / step_ms))
    if norm < 1e-18 or radius < 1:
        return None, 0.0
    lags = np.arange(-radius, radius + 1)
    scores = np.array([float(np.dot(left[max(0, -lag):len(left) - max(0, lag)],
                                    right[max(0, lag):len(right) - max(0, -lag)]))
                       for lag in lags]) / norm
    best = int(np.argmax(scores))
    shift = float(lags[best])
    if 0 < best < len(scores) - 1:
        bend = scores[best - 1] - 2 * scores[best] + scores[best + 1]
        if bend < 0:
            shift += 0.5 * (scores[best - 1] - scores[best + 1]) / bend
    return shift * step_ms, float(scores[best])


def _attacks(envelope: Any, beats: Any, step_ms: float) -> list[dict[str, Any]]:
    """Return envelope peaks with their position and half-height width."""
    from scipy.signal import find_peaks, peak_widths

    top = float(envelope.max(initial=0))
    if top <= 1e-12:
        return []
    peaks, _ = find_peaks(envelope, prominence=0.1 * top,
                          distance=max(1, int(40 / step_ms)))
    widths = peak_widths(envelope, peaks, rel_height=0.5)[0] if len(peaks) else []
    return [{"beat": round(float(beats[peak]), 4), "width_ms": round(float(width) * step_ms, 2),
             "level_db": round(20 * math.log10(max(1e-12, float(envelope[peak]) / top)), 2)}
            for peak, width in zip(peaks, widths, strict=True)]


def _pair_attacks(reference: list[dict[str, Any]], candidate: list[dict[str, Any]],
                  ms_per_beat: float) -> dict[str, Any]:
    """Pair each reference attack with the nearest unused candidate attack in reach."""
    import numpy as np

    pairs, used = [], set()
    for item in reference:
        near = [(abs(other["beat"] - item["beat"]) * ms_per_beat, index)
                for index, other in enumerate(candidate) if index not in used]
        near = [entry for entry in near if entry[0] <= ATTACK_MATCH_MS]
        if near:
            index = min(near)[1]
            used.add(index)
            other = candidate[index]
            pairs.append({"beat": item["beat"],
                          "delta_ms": round((other["beat"] - item["beat"]) * ms_per_beat, 2),
                          "width_reference_ms": item["width_ms"],
                          "width_candidate_ms": other["width_ms"]})
    missing = [item for item in reference
               if not any(pair["beat"] == item["beat"] for pair in pairs)]
    extra = [other for index, other in enumerate(candidate) if index not in used]

    def median(key: str) -> float | None:
        return round(float(np.median([pair[key] for pair in pairs])), 2) if pairs else None

    return {"summary": {"reference": len(reference), "candidate": len(candidate),
                        "paired": len(pairs), "reference_only": len(missing),
                        "candidate_only": len(extra), "median_delta_ms": median("delta_ms"),
                        "median_width_reference_ms": median("width_reference_ms"),
                        "median_width_candidate_ms": median("width_candidate_ms")},
            "pairs": pairs, "reference_only": missing, "candidate_only": extra}


def _position(beat: float, beats_per_bar: float) -> dict[str, float | int]:
    bar = math.floor(beat / beats_per_bar + 1e-9)
    return {"bar": bar + 1, "beat_in_bar": round(beat - bar * beats_per_bar + 1, 4)}


def _regions(difference: Any, beats: Any, step: float, edges: list[dict[str, Any]],
             threshold: float, beats_per_bar: float) -> list[dict[str, Any]]:
    """Return connected cells beyond the threshold, the largest dB-beat area first."""
    import numpy as np
    from scipy.ndimage import find_objects, label

    found = []
    for kind, mask in (("missing", difference >= threshold), ("excess", difference <= -threshold)):
        labels = label(mask)[0]  # type: ignore[index]  # SciPy's stub types this as int
        for number, box in enumerate(find_objects(labels), start=1):
            if box is None:
                continue
            cells = labels[box] == number
            values = difference[box][cells]
            first, last = box[0].start, box[0].stop - 1
            low, high = edges[box[1].start], edges[box[1].stop - 1]
            peak = float(values[np.argmax(np.abs(values))])
            found.append({"kind": kind, "start_beat": round(float(beats[first]), 4),
                          "end_beat": round(float(beats[last]) + step, 4),
                          "start": _position(float(beats[first]), beats_per_bar),
                          "end": _position(float(beats[last]) + step, beats_per_bar),
                          "low": low["low"], "high": high["high"],
                          "mean_db": round(float(values.mean()), 2), "peak_db": round(peak, 2),
                          "area_db_beats": round(float(np.abs(values).sum()) * step, 3),
                          "cells": int(cells.sum())})
    return sorted(found, key=lambda item: -item["area_db_beats"])


def _image(path: Path, panels: list[tuple[str, Any, str, tuple[float, float]]], beats: Any,
           step: float, labels: list[str], beats_per_bar: float, ylabel: str) -> None:
    """Draw reference, candidate and difference above each other with bar lines."""
    import numpy as np

    # Imported by name: matplotlib is the optional plot extra, absent from type checks.
    importlib.import_module("matplotlib").use("Agg")
    plt = importlib.import_module("matplotlib.pyplot")

    extent = (beats[0] / beats_per_bar + 1, (beats[-1] + step) / beats_per_bar + 1,
              0, len(labels))
    figure, axes = plt.subplots(len(panels), 1, figsize=(16, 3.6 * len(panels)), sharex=True)
    ticks = list(range(0, len(labels), max(1, len(labels) // 12)))
    for axis, (title, matrix, colours, limits) in zip(axes, panels, strict=True):
        image = axis.imshow(matrix.T, origin="lower", aspect="auto", extent=extent,
                            cmap=colours, vmin=limits[0], vmax=limits[1],
                            interpolation="nearest")
        axis.set_title(title)
        axis.set_yticks([tick + 0.5 for tick in ticks], [labels[tick] for tick in ticks])
        axis.set_ylabel(ylabel)
        figure.colorbar(image, ax=axis, pad=0.01, label="dB")
        for beat in np.arange(math.ceil(beats[0]), beats[-1] + step + 1e-9, 1.0):
            major = abs(beat / beats_per_bar - round(beat / beats_per_bar)) < 1e-9
            axis.axvline(beat / beats_per_bar + 1, color="k" if colours == "RdBu_r" else "w",
                         lw=1.0 if major else 0.3, alpha=0.5)
    axes[-1].set_xlabel("bar")
    figure.tight_layout()
    figure.savefig(path, dpi=80)
    plt.close(figure)


def k_weighted(samples: Any, rate: int) -> Any:
    """Return the signal through the BS.1770 K-weighting (high shelf, then high-pass)."""
    import numpy as np
    from scipy.signal import lfilter

    k = math.tan(math.pi * 1681.974450955533 / rate)
    gain, q = 10 ** (3.999843853973347 / 20), 0.7071752369554196
    boost = gain ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf_b = np.array([gain + boost * k / q + k * k, 2 * (k * k - gain),
                        gain - boost * k / q + k * k]) / a0
    shelf_a = np.array([1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    k = math.tan(math.pi * 38.13547087602444 / rate)
    q = 0.5003270373238773
    a0 = 1 + k / q + k * k
    high_a = np.array([1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    return lfilter([1.0, -2.0, 1.0], high_a, lfilter(shelf_b, shelf_a, samples))


def sections_of(request: dict[str, Any], first: float, last: float) -> list[tuple[float, float]]:
    """Return (from_beat, to_beat) per section: explicit beats or seconds, else even lengths.

    Seconds are reference-file seconds and go through the reference anchors.
    """
    import numpy as np

    chosen = request.get("sections")
    if chosen:
        mapping = validate_map(request["reference_anchors"])
        known = np.array([item["seconds"] for item in mapping])
        beats = np.array([item["beat"] for item in mapping])
        out = []
        for item in chosen:
            if item.get("from_seconds") is not None:
                start = float(np.interp(item["from_seconds"], known, beats))
                stop = float(np.interp(item["to_seconds"], known, beats))
            else:
                start, stop = float(item["from_beat"]), float(item["to_beat"])
            start, stop = max(start, first), min(stop, last)
            if stop > start:
                out.append((start, stop))
        return out
    length = float(request.get("section_beats") or request["beats_per_bar"])
    edge = math.floor(first / length + 1e-9) * length
    out = []
    while edge < last - 1e-9:
        start, stop = max(edge, first), min(edge + length, last)
        if stop > start:
            out.append((start, stop))
        edge += length
    return out


def _section_level(reference: Any, reference_seconds: Any, candidate: Any,
                   candidate_seconds: Any, rate: int) -> dict[str, Any]:
    """Return K-weighted loudness of both signals over the blocks where both sound.

    Args:
        reference, candidate: K-weighted excerpts.
        reference_seconds, candidate_seconds: block edges as sample positions in them.
    """
    import numpy as np

    def powers(signal: Any, edges: Any) -> Any:
        return np.array([float(np.mean(signal[a:b] ** 2)) if b > a else 0.0
                         for a, b in itertools.pairwise(edges)])

    left, right = powers(reference, reference_seconds), powers(candidate, candidate_seconds)
    floor = 10 ** ((LOUDNESS_GATE_LUFS + 0.691) / 10)

    def sounding(values: Any) -> Any:
        top = float(values.max(initial=0.0))
        return (values > floor) & (values >= top * 10 ** (-SECTION_GATE_DB / 10))

    both = sounding(left) & sounding(right)
    if not both.any():
        return {"common_blocks": 0, "loudness_reference_lufs": None,
                "loudness_candidate_lufs": None, "level_difference_db": None}
    loud_left = -0.691 + 10 * math.log10(float(left[both].mean()))
    loud_right = -0.691 + 10 * math.log10(float(right[both].mean()))
    return {"common_blocks": int(both.sum()),
            "loudness_reference_lufs": round(loud_left, 2),
            "loudness_candidate_lufs": round(loud_right, 2),
            "level_difference_db": round(loud_left - loud_right, 2)}


def _true_peak(signal: Any) -> float | None:
    """Return the 4x oversampled peak in dBTP."""
    import numpy as np
    from scipy.signal import resample_poly

    if not len(signal):
        return None
    peak = float(np.max(np.abs(resample_poly(signal, 4, 1))))
    return round(20 * math.log10(max(peak, 1e-12)), 2)


def compare_spectrum(request: dict[str, Any]) -> dict[str, Any]:
    """Return the beat-aligned difference report and write its optional image.

    Args:
        request: paths, anchors, beat range, resolution, offsets and output_dir as
            built by the compare_spectrum tool. chain_offset_ms is added to the
            candidate's anchor times: positive means its audio lags its anchors.

    Returns:
        A maestro.spectrum report. regions, per_bar and attacks hold the lists.
    """
    import numpy as np

    first_beat, last_beat = float(request["from_beat"]), float(request["to_beat"])
    steps = int(request["steps_per_beat"])
    per_bar = float(request["beats_per_bar"])
    count = round((last_beat - first_beat) * steps)
    if count < 2 or count > 60000:
        raise ValueError("beat range must hold 2 to 60000 grid steps")
    step = 1 / steps
    beats = first_beat + np.arange(count) * step
    for key in ("reference_anchors", "candidate_anchors"):
        beat_seconds([first_beat, last_beat], request[key])
    reference_times = beat_seconds(beats, request["reference_anchors"])
    anchor_times = beat_seconds(beats, request["candidate_anchors"])
    chain = float(request.get("chain_offset_ms", 0)) / 1000
    semitones = request["resolution"] == "semitones"
    window = float(request.get("window_ms") or (170 if semitones else 85)) / 1000
    margin = window + OFFSET_SEARCH_MS / 1000 + 0.05
    reference, rate, reference_origin, reference_info = _excerpt(
        request["reference_path"], reference_times[0] - margin, reference_times[-1] + margin)
    for extra in request.get("extra_reference_paths", []):
        more, more_rate, _, more_info = _excerpt(
            extra, reference_times[0] - margin, reference_times[-1] + margin)
        if more_rate != rate:
            raise ValueError("summed reference stems need one sample rate; convert explicitly")
        reference = reference + more
        reference_info.setdefault("summed_from", []).append(more_info)
    candidate, candidate_rate, candidate_origin, candidate_info = _excerpt(
        request["candidate_path"], anchor_times[0] + chain - margin,
        anchor_times[-1] + chain + margin)
    if candidate_rate != rate:
        from scipy.signal import resample_poly

        factor = math.gcd(rate, candidate_rate)
        candidate = resample_poly(candidate, rate // factor, candidate_rate // factor)

    seconds_per_beat = (reference_times[-1] - reference_times[0]) / (beats[-1] - beats[0])
    fine_step = min(step, 0.001 / seconds_per_beat)
    fine = np.arange(first_beat, last_beat, fine_step)
    step_ms = fine_step * seconds_per_beat * 1000
    reference_envelope = _envelope(reference, rate, reference_origin,
                                   beat_seconds(fine, request["reference_anchors"]))
    fine_anchor = beat_seconds(fine, request["candidate_anchors"])
    candidate_envelope = _envelope(candidate, rate, candidate_origin, fine_anchor + chain)
    estimate, correlation = estimate_offset(reference_envelope, candidate_envelope, step_ms)
    applied = chain
    if request.get("apply_estimated_offset") and estimate is not None:
        applied += estimate / 1000
        candidate_envelope = _envelope(candidate, rate, candidate_origin, fine_anchor + applied)
    attacks = _pair_attacks(_attacks(reference_envelope, fine, step_ms),
                            _attacks(candidate_envelope, fine, step_ms),
                            seconds_per_beat * 1000)

    size = 2 ** max(8, round(math.log2(window * rate)))
    frequencies = np.fft.rfftfreq(size, 1 / rate)
    reference_power = _power(reference, rate, reference_origin, reference_times, size)
    candidate_power = _power(candidate, rate, candidate_origin, anchor_times + applied, size)
    if semitones:
        notes = np.arange(int(request["low_note"]), int(request["high_note"]) + 1)
        original = _salience(reference_power, frequencies, notes)
        rebuilt = _salience(candidate_power, frequencies, notes)
        labels = [note_name(int(note)) for note in notes]
        edges = [{"low": {"midi": int(note), "note": note_name(int(note))},
                  "high": {"midi": int(note), "note": note_name(int(note))}} for note in notes]
    else:
        low, high = float(request["low_hz"]), min(float(request["high_hz"]), 0.45 * rate)
        centres = low * 2 ** (np.arange(0, math.log2(high / low) + 1e-9, 1 / 12))
        original = _rows(reference_power, frequencies, centres)
        rebuilt = _rows(candidate_power, frequencies, centres)
        labels = [f"{centre:.0f}" for centre in centres]
        edges = [{"low": round(float(centre * 2 ** (-1 / 24)), 1),
                  "high": round(float(centre * 2 ** (1 / 24)), 1)} for centre in centres]
    original = 10 * np.log10(np.maximum(original, 0) + 1e-20)
    rebuilt = 10 * np.log10(np.maximum(rebuilt, 0) + 1e-20)
    top = float(original.max())
    weighted_reference = k_weighted(reference, rate)
    weighted_candidate = k_weighted(candidate, rate)
    reference_anchors = request["reference_anchors"]
    candidate_anchors = request["candidate_anchors"]
    section_rows: list[dict[str, Any]] = []
    for start, stop in sections_of(request, first_beat, last_beat):
        span = float(beat_seconds([stop], reference_anchors)[0]
                     - beat_seconds([start], reference_anchors)[0])
        blocks = np.linspace(start, stop, max(1, round(span / LOUDNESS_BLOCK)) + 1)
        at_reference = np.rint((beat_seconds(blocks, reference_anchors) - reference_origin)
                               * rate).astype(int)
        at_candidate = np.rint((beat_seconds(blocks, candidate_anchors) + applied
                                - candidate_origin) * rate).astype(int)
        level = _section_level(weighted_reference, at_reference, weighted_candidate,
                               at_candidate, rate)
        level["true_peak_reference_dbtp"] = _true_peak(
            reference[max(0, at_reference[0]):max(0, at_reference[-1])])
        level["true_peak_candidate_dbtp"] = _true_peak(
            candidate[max(0, at_candidate[0]):max(0, at_candidate[-1])])
        section_rows.append({"from_beat": round(start, 4), "to_beat": round(stop, 4),
                             "start": _position(start, per_bar),
                             "end": _position(stop, per_bar), **level})
    measured = [row["level_difference_db"] for row in section_rows
                if row["level_difference_db"] is not None]
    fallback = float(np.median(measured)) if measured else 0.0
    gains = np.full(count, fallback)
    for row in section_rows:
        selected = (beats >= row["from_beat"] - 1e-9) & (beats < row["to_beat"] - 1e-9)
        gains[selected] = (row["level_difference_db"] if row["level_difference_db"] is not None
                           else fallback)
    rebuilt = rebuilt + gains[:, None]
    original = np.maximum(original, top - FLOOR_DB)
    rebuilt = np.maximum(rebuilt, top - FLOOR_DB)
    active = (original.max(axis=1) > top - ACTIVE_DB) | (rebuilt.max(axis=1) > top - ACTIVE_DB)
    difference = original - rebuilt
    difference[~active] = 0

    for row in section_rows:
        selected = ((beats >= row["from_beat"] - 1e-9) & (beats < row["to_beat"] - 1e-9)
                    & active)
        row["active_steps"] = int(selected.sum())
        row["sound_difference_db"] = (round(float(np.abs(difference[selected]).mean()), 3)
                                      if selected.any() else None)
    regions = _regions(difference, beats, step, edges, float(request["threshold_db"]), per_bar)

    image_paths, image_note = [], None
    if request.get("write_image"):
        if importlib.util.find_spec("matplotlib") is None:
            image_note = "matplotlib is not installed; install the 'plot' extra for an image"
        else:
            target = Path(request["output_dir"]) / "spectrum.png"
            _image(target, [("Reference", original, "magma", (top - FLOOR_DB, top)),
                            ("Candidate (loudness matched per section)", rebuilt, "magma",
                             (top - FLOOR_DB, top)),
                            ("Reference minus candidate (red: missing, blue: excess)",
                             difference, "RdBu_r", (-24, 24))],
                   beats, step, labels, per_bar, "note" if semitones else "Hz")
            image_paths.append(str(target))

    observations = []
    if estimate is not None and correlation >= 0.3 and abs(estimate) >= 5:
        observations.append(
            f"Candidate attacks lag the reference by {estimate:+.1f} ms after the chain offset. "
            "Track Delay is not in the Object Model: ask the user to set "
            f"{-estimate:+.0f} ms on the candidate track, or pass apply_estimated_offset.")
    selected_mean = np.abs(difference[active]).mean() if active.any() else None
    observations += [
        f"{item['kind'].capitalize()} {item['mean_db']:+.1f} dB, beats "
        f"{item['start_beat']:g} to {item['end_beat']:g}, {_describe(item, semitones)}."
        for item in regions[:5]]
    return {"schema": "maestro.spectrum", "schema_version": 1,
            "reference": reference_info, "candidate": candidate_info,
            "grid": {"from_beat": first_beat, "to_beat": last_beat, "steps_per_beat": steps,
                     "beats_per_bar": per_bar, "frames": count,
                     "resolution": request["resolution"], "rows": labels,
                     "window_samples": size, "analysis_sample_rate": rate,
                     "row_basis": "harmonic salience per semitone" if semitones
                     else "power per 1/12-octave band"},
            "offset": {"chain_offset_ms": chain * 1000,
                       "estimated_offset_ms": None if estimate is None else round(estimate, 2),
                       "estimate_correlation": round(correlation, 3),
                       "estimate_basis": "onset strength of the 1.5-8 kHz envelope, "
                                         f"searched within +/-{OFFSET_SEARCH_MS:g} ms",
                       "estimate_applied": bool(request.get("apply_estimated_offset")
                                                and estimate is not None),
                       "applied_ms": round(applied * 1000, 2)},
            "level_match": {
                "basis": "K-weighted loudness (BS.1770 filters, mono mix) per section, over "
                         "100 ms blocks where both files sound; true peak is reported only",
                "fallback_db": round(fallback, 2), "sections_measured": len(measured)},
            "mean_abs_difference_db": None if selected_mean is None
            else round(float(selected_mean), 3),
            "difference_basis": "dB per cell after loudness match; positive is missing in "
                                "the candidate, negative is excess; cells below the floor clip",
            "level_basis": "level_difference_db is reference minus candidate loudness: raise "
                           "the candidate's fader or automation by that much",
            "sections": section_rows, "regions": regions[:int(request["max_regions"])],
            "regions_total": len(regions), "attacks": attacks,
            "image_paths": image_paths, "image_note": image_note,
            "observations": observations, "review_required": True, "quality_score": None}


def _describe(region: dict[str, Any], semitones: bool) -> str:
    if semitones:
        low, high = region["low"]["note"], region["high"]["note"]
        return low if low == high else f"{low} to {high}"
    return f"{region['low']:g} to {region['high']:g} Hz"
