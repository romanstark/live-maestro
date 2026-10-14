"""Validated external source-time maps and clip-note conversion for Live."""

from __future__ import annotations

import bisect
import math
from typing import Any


def validate_map(anchors: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Require two or more finite, strictly increasing seconds and quarter beats."""
    if len(anchors) < 2:
        raise ValueError("tempo map needs at least two anchors")
    result = []
    for item in anchors:
        seconds, beat = float(item["seconds"]), float(item["beat"])
        if not math.isfinite(seconds) or not math.isfinite(beat) or seconds < 0 or beat < 0:
            raise ValueError("tempo anchors must be finite and nonnegative")
        if result and (seconds <= result[-1]["seconds"] or beat <= result[-1]["beat"]):
            raise ValueError("tempo anchors must strictly increase in both axes")
        result.append({"seconds": seconds, "beat": beat})
    return result


def to_beat(seconds: float, anchors: list[dict[str, float]]) -> float:
    """Interpolate seconds; extrapolate using the nearest end segment."""
    index = max(
        0,
        min(
            len(anchors) - 2,
            bisect.bisect_right([item["seconds"] for item in anchors], seconds) - 1,
        ),
    )
    left, right = anchors[index : index + 2]
    return left["beat"] + (seconds - left["seconds"]) * (
        (right["beat"] - left["beat"]) / (right["seconds"] - left["seconds"])
    )


def notes_from_analysis(data: dict[str, Any], clip_origin_beat: float = 0) -> list[dict[str, Any]]:
    """Validate an interchange result before any DAW write or MIDI file export."""
    if (
        not isinstance(data, dict)
        or data.get("schema") != "maestro.drum-events"
        or data.get("schema_version") != 1
    ):
        raise ValueError("unsupported drum analysis schema")
    if not math.isfinite(clip_origin_beat) or clip_origin_beat < 0:
        raise ValueError("clip origin must be finite and nonnegative")
    anchors = validate_map(data["tempo_map"]["anchors"])
    notes = []
    for event in data["events"]:
        seconds = float(event["source_seconds"])
        pitch, velocity = event["midi_pitch"], event["velocity"]
        if (
            not math.isfinite(seconds)
            or seconds < 0
            or isinstance(pitch, bool)
            or not isinstance(pitch, int)
            or not 0 <= pitch <= 127
            or isinstance(velocity, bool)
            or not isinstance(velocity, (int, float))
            or not math.isfinite(velocity)
            or not 1 <= velocity <= 127
        ):
            raise ValueError("invalid drum event")
        beat = to_beat(seconds, anchors) - clip_origin_beat
        if beat < -1e-9:
            raise ValueError("event falls before the requested clip origin")
        notes.append(
            {"pitch": pitch, "start_time": max(0.0, beat), "duration": 0.125, "velocity": velocity}
        )
    return sorted(notes, key=lambda item: (item["start_time"], item["pitch"]))
