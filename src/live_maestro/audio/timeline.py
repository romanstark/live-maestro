"""Validated sample/seconds/beat interchange and standard MIDI drum export."""

from __future__ import annotations

import bisect
import math
import struct
from itertools import pairwise
from pathlib import Path
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


def vlq(value: int) -> bytes:
    """Encode the nonnegative SMF variable length integer."""
    if not 0 <= value <= 0x0FFFFFFF:
        raise ValueError("MIDI delta outside the SMF range")
    output = [value & 127]
    value >>= 7
    while value:
        output.insert(0, (value & 127) | 128)
        value >>= 7
    return bytes(output)


def write_midi(path: Path, data: dict[str, Any], clip_origin_beat: float = 0) -> None:
    """Write SMF type 0, channel 10, with a piecewise tempo map at 960 PPQ."""
    notes = notes_from_analysis(data, clip_origin_beat)
    anchors = validate_map(data["tempo_map"]["anchors"])
    events: list[tuple[int, int, bytes]] = []
    segments = list(pairwise(anchors))
    active = max(
        0,
        min(
            len(segments) - 1,
            bisect.bisect_right([item["beat"] for item in anchors], clip_origin_beat) - 1,
        ),
    )
    for index, (left, right) in enumerate(segments[active:], start=active):
        beat = 0.0 if index == active else left["beat"] - clip_origin_beat
        micros = round(
            1_000_000 * (right["seconds"] - left["seconds"]) / (right["beat"] - left["beat"])
        )
        if not 1 <= micros <= 0xFFFFFF:
            raise ValueError("tempo cannot be represented by SMF")
        events.append((round(beat * 960), 0, b"\xff\x51\x03" + micros.to_bytes(3, "big")))
    for note in notes:
        tick = round(note["start_time"] * 960)
        velocity = round(note["velocity"])
        events.extend(
            [
                (tick, 2, bytes([0x99, note["pitch"], velocity])),
                (tick + 120, 1, bytes([0x89, note["pitch"], 0])),
            ]
        )
    stream, previous = bytearray(), 0
    for tick, _, payload in sorted(events):
        stream.extend(vlq(tick - previous) + payload)
        previous = tick
    stream.extend(b"\0\xff\x2f\0")
    path.write_bytes(
        b"MThd"
        + struct.pack(">IHHH", 6, 0, 1, 960)
        + b"MTrk"
        + struct.pack(">I", len(stream))
        + stream
    )


def write_tempo_import(path: Path, data: dict[str, Any]) -> None:
    """Supply a spanning carrier note for Live's documented Arrangement tempo import."""
    anchors = validate_map(data["tempo_map"]["anchors"])
    events: list[tuple[int, int, bytes]] = [(0, 1, b"\x90\x00\x01")]
    for left, right in pairwise(anchors):
        micros = round(
            1_000_000 * (right["seconds"] - left["seconds"]) / (right["beat"] - left["beat"])
        )
        if not 1 <= micros <= 0xFFFFFF:
            raise ValueError("tempo outside SMF range")
        events.append((round(left["beat"] * 960), 0, b"\xff\x51\x03" + micros.to_bytes(3, "big")))
    events.append((max(1, round(anchors[-1]["beat"] * 960)), 2, b"\x80\x00\x00"))
    stream, previous = bytearray(), 0
    for tick, _, payload in sorted(events):
        stream.extend(vlq(tick - previous) + payload)
        previous = tick
    stream.extend(b"\0\xff\x2f\0")
    path.write_bytes(
        b"MThd"
        + struct.pack(">IHHH", 6, 0, 1, 960)
        + b"MTrk"
        + struct.pack(">I", len(stream))
        + stream
    )
