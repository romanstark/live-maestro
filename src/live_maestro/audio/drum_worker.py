"""Optional NumPy/SciPy worker: isolated attacks, tempo proposals, MIDI and one-shots."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

if "live_maestro" not in sys.modules:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "live_maestro", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("worker package unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_maestro"] = module
    spec.loader.exec_module(module)

from live_maestro.audio.timeline import (
    notes_from_analysis,
    validate_map,
    write_midi,
    write_tempo_import,
)


def emit(data: dict[str, Any]) -> None:
    """Keep one JSON record on the worker channel."""
    print(json.dumps(data, allow_nan=False), flush=True)


def hits(
    samples: Any, rate: int, calibration_ms: float, sensitivity: float
) -> list[dict[str, Any]]:
    """Detect positive energy rises and refine the raw-signal leading threshold crossing."""
    import numpy as np
    from scipy.signal import find_peaks

    mono = np.max(np.abs(samples), axis=1)
    hop = max(1, round(rate * 0.001))
    padded = np.pad(mono, (0, (-len(mono)) % hop))
    envelope = np.sqrt(np.mean(padded.reshape(-1, hop) ** 2, axis=1))
    rise = np.maximum(0, np.diff(envelope, prepend=0))
    floor = float(np.median(rise))
    mad = float(np.median(np.abs(rise - floor)))
    threshold = max(floor + sensitivity * mad, float(rise.max(initial=0)) * 0.025, 1e-6)
    candidates, _ = find_peaks(np.pad(rise, (1, 1)), height=threshold, distance=40)
    result = []
    for block in candidates - 1:
        point = int(block * hop)
        window_start = max(0, point - round(rate * 0.02))
        window_end = min(len(mono), point + round(rate * 0.01))
        local = mono[window_start:window_end]
        peak = float(local.max(initial=0))
        if peak < 1e-5:
            continue
        crossings = np.flatnonzero(local >= peak * 0.1)
        if not len(crossings):
            continue
        onset = window_start + int(crossings[0])
        onset -= round(calibration_ms * rate / 1000)
        if not 0 <= onset < len(samples):
            continue
        if result and onset - result[-1]["sample"] < round(rate * 0.04):
            continue
        result.append(
            {
                "sample": onset,
                "peak": peak,
                "uncertainty_samples": hop,
                "attack_definition": (
                    "first 10-percent local-peak crossing; calibrated offset subtracted"
                ),
            }
        )
    return result


def tempo_candidates(
    events: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, float]]:
    """Autocorrelation proposes tempo, explicitly preserving half/double-time ambiguity."""
    import numpy as np
    from scipy.signal import correlate, find_peaks

    hop = 0.005
    train = np.zeros(max(1, math.ceil((end - start) / hop)))
    for event in events:
        seconds = event["source_seconds"]
        if start <= seconds < end:
            train[min(len(train) - 1, round((seconds - start) / hop))] += event["peak"]
    if not np.any(train):
        return []
    score = correlate(train, train, method="fft", mode="full")[len(train) - 1 :]
    low, high = round(60 / 180 / hop), min(len(score) - 1, round(60 / 60 / hop))
    if high <= low:
        return []
    peaks, _ = find_peaks(np.pad(score[low : high + 1], (1, 1)))
    values = [(int(index - 1 + low), float(score[index - 1 + low])) for index in peaks]
    return [
        {"bpm": 60 / (lag * hop), "periodicity": max(0, value / float(score[0]))}
        for lag, value in sorted(values, key=lambda item: item[1], reverse=True)[:3]
        if lag > 0
    ]


def analyze(request: dict[str, Any]) -> dict[str, Any]:
    """Run file analysis without opening Live or importing its control dependencies."""
    import numpy as np
    import soundfile as sf

    output = Path(request["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    events, sources, arrays = [], [], []
    for index, stem in enumerate(request["stems"]):
        path = Path(stem["path"])
        samples, rate = sf.read(path, always_2d=True)
        if not samples.size or samples.shape[1] > 2 or not np.isfinite(samples).all():
            raise ValueError("stems must contain finite, nonempty mono/stereo samples")
        offset = stem["source_offset_seconds"]
        if not math.isfinite(offset) or not math.isfinite(stem["calibration_ms"]):
            raise ValueError("offset and calibration must be finite")
        detected = hits(samples, rate, stem["calibration_ms"], request["sensitivity"])
        reference = None
        if stem.get("reference_path"):
            reference, reference_rate = sf.read(stem["reference_path"], always_2d=True)
            if (
                reference_rate != rate
                or reference.shape != samples.shape
                or not np.isfinite(reference).all()
            ):
                raise ValueError("reference must have identical frames, rate and channels")
        pitch = stem.get("midi_pitch")
        if pitch is None:
            pitch = 36 if stem["instrument"] == "kick" else 38
        maximum = max((item["peak"] for item in detected), default=1)
        for hit in detected:
            source_seconds = offset + hit["sample"] / rate
            source_rate = stem.get("source_sample_rate") or rate
            events.append(
                {
                    **hit,
                    "instrument": stem["instrument"],
                    "source_index": index,
                    "sample_rate": rate,
                    "source_seconds": source_seconds,
                    "source_sample_rate": source_rate,
                    "source_sample": math.floor(source_seconds * source_rate + 0.5),
                    "midi_pitch": pitch,
                    "velocity": max(1, min(127, round(127 * math.sqrt(hit["peak"] / maximum)))),
                }
            )
        sources.append(
            {
                "path": str(path),
                "sample_rate": rate,
                "frames": len(samples),
                "source_offset_seconds": offset,
                "instrument": stem["instrument"],
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        arrays.append((samples, rate, reference))
        emit({"event": "progress", "progress": (index + 1) / (len(request["stems"]) + 2)})
    events.sort(key=lambda item: (item["source_seconds"], item["midi_pitch"]))
    end = max(
        item["source_offset_seconds"] + item["frames"] / item["sample_rate"] for item in sources
    )
    proposed = tempo_candidates(events, 0, end)
    anchors = request.get("beat_anchors")
    inferred = anchors is None and request.get("tempo_bpm") is None
    if anchors is None:
        bpm = request.get("tempo_bpm") or (proposed[0]["bpm"] if proposed else None)
        if bpm is None:
            raise ValueError("tempo unresolved: supply tempo_bpm or reviewed beat_anchors")
        anchors = [{"seconds": 0.0, "beat": 0.0}]
        previous = 0.0
        for stop in [*np.arange(8.0, end, 8.0).tolist(), end]:
            local = (
                tempo_candidates(events, previous, stop)
                if inferred and stop - previous >= 4
                else []
            )
            current = local[0]["bpm"] if local else bpm
            anchors.append(
                {"seconds": stop, "beat": anchors[-1]["beat"] + (stop - previous) * current / 60}
            )
            previous = stop
    data = {
        "schema": "maestro.drum-events",
        "schema_version": 1,
        "events": events,
        "sources": sources,
        "tempo_map": {
            "anchors": validate_map(anchors),
            "review_required": inferred,
            "tempo_candidates": proposed,
            "grid_origin": "source second zero or caller-supplied anchors",
            "meter_inferred": False,
        },
        "method": "isolated-source energy-rise detection; no mixed-kit instrument classifier",
    }
    notes_from_analysis(data)
    midi = output / "drums.mid"
    write_midi(midi, data)
    tempo_import = output / "tempo-map.mid"
    write_tempo_import(tempo_import, data)
    one_shots = []
    if request.get("cut_one_shots"):
        for number, event in enumerate(events):
            samples, rate, reference = arrays[event["source_index"]]
            start = max(0, event["sample"] - round(request["pre_roll_ms"] * rate / 1000))
            stop = min(len(samples), event["sample"] + round(request["tail_ms"] * rate / 1000))
            target = output / f"{number:04d}-{event['instrument']}.wav"
            clip = samples[start:stop]
            sf.write(target, clip, rate, subtype="PCM_24")
            overlap = sum(
                1
                for other in events
                if other is not event
                and other["source_index"] == event["source_index"]
                and start <= other["sample"] < stop
            )
            contamination: dict[str, Any] = {"available": False, "reason": "no isolated reference"}
            other_activity = []
            absolute_start = sources[event["source_index"]]["source_offset_seconds"] + start / rate
            absolute_stop = sources[event["source_index"]]["source_offset_seconds"] + stop / rate
            for source_index, (other_samples, other_rate, _) in enumerate(arrays):
                if source_index == event["source_index"]:
                    continue
                other_offset = sources[source_index]["source_offset_seconds"]
                left = max(0, math.floor((absolute_start - other_offset) * other_rate))
                right = min(
                    len(other_samples), math.ceil((absolute_stop - other_offset) * other_rate)
                )
                if right > left:
                    other_activity.append(
                        {
                            "instrument": sources[source_index]["instrument"],
                            "rms": float(np.sqrt(np.mean(other_samples[left:right] ** 2))),
                        }
                    )
            if reference is not None:
                residue = clip - reference[start:stop]
                contamination = {
                    "available": True,
                    "residual_rms": float(np.sqrt(np.mean(residue**2))),
                    "meaning": (
                        "difference from supplied aligned reference, not a universal leakage score"
                    ),
                }
            one_shots.append(
                {
                    "path": str(target),
                    "event": number,
                    "start_sample": start,
                    "end_sample": stop,
                    "attack_sample": event["sample"] - start,
                    "source_offset_seconds": sources[event["source_index"]]["source_offset_seconds"]
                    + start / rate,
                    "overlapping_hits": overlap,
                    "contamination": contamination,
                    "other_stem_activity": other_activity,
                    "activity_basis": (
                        "simultaneous energy in other supplied stems, not measured leakage"
                    ),
                }
            )
    data["one_shots"] = one_shots
    analysis_path = output / "analysis.json"
    analysis_path.write_text(json.dumps(data, allow_nan=False, indent=2), encoding="utf-8")
    return {
        "analysis_path": str(analysis_path),
        "midi_path": str(midi),
        "tempo_import_path": str(tempo_import),
        "events": len(events),
        "tempo_map": data["tempo_map"],
        "one_shots": one_shots,
        "device": "cpu",
    }


def main() -> None:
    """Keep dependency errors and processing failures explicit on the JSON channel."""
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = analyze(request)
        emit({"event": "result", "ok": True, **result})
    except Exception as error:  # noqa: BLE001
        emit(
            {
                "event": "result",
                "ok": False,
                "code": "audio_runtime_missing"
                if isinstance(error, ModuleNotFoundError)
                else "analysis_failed",
                "error": str(error),
            }
        )


if __name__ == "__main__":
    main()
