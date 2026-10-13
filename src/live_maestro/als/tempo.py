"""Tempo envelopes in new saved-set copies, and analytic musical-time conversion."""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path
from typing import Any


def validate(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require an explicit beat-zero tempo and ordered step/ramp segments."""
    if not points or points[0]["beat"] != 0:
        raise ValueError("tempo events must begin at beat zero")
    result = []
    for point in points:
        beat, bpm = float(point["beat"]), float(point["bpm"])
        shape = point.get("shape", "step")
        if (
            not math.isfinite(beat)
            or beat < 0
            or not math.isfinite(bpm)
            or not 20 <= bpm <= 999
            or shape not in ("step", "ramp")
            or (result and beat <= result[-1]["beat"])
        ):
            raise ValueError("tempo events need finite ordered beats, 20-999 BPM and step/ramp")
        result.append({"beat": beat, "bpm": bpm, "shape": shape})
    # The final point has no following segment to interpolate toward.
    result[-1]["shape"] = "step"
    return result


def envelope(root: ET.Element) -> tuple[ET.Element, ET.Element]:
    """Resolve the tempo envelope by its actual AutomationTarget, never a fixed ID."""
    main = root.find(".//LiveSet/MainTrack")
    if main is None:
        main = root.find(".//LiveSet/MasterTrack")
    if main is None:
        raise ValueError("set has no MainTrack/MasterTrack")
    tempo = main.find("./DeviceChain/Mixer/Tempo")
    if tempo is None or tempo.find("AutomationTarget") is None:
        raise ValueError("set has no tempo automation target")
    target = tempo.find("AutomationTarget")
    assert target is not None
    matching = []
    for row in main.iter("AutomationEnvelope"):
        pointee = row.find("./EnvelopeTarget/PointeeId")
        if pointee is not None and pointee.get("Value") == target.get("Id"):
            matching.append(row)
    if len(matching) != 1:
        raise ValueError("tempo target must have exactly one existing envelope")
    events = matching[0].find("./Automation/Events")
    if events is None:
        raise ValueError("tempo envelope has no Events container")
    return tempo, events


def read(path: str | Path) -> dict[str, Any]:
    """Read persisted tempo automation; this does not claim the open set is saved."""
    source = Path(path).expanduser().resolve()
    raw = source.read_bytes()
    root = ET.fromstring(gzip.decompress(raw))
    tempo, events = envelope(root)
    raw_events = []
    for event in events:
        if event.tag != "FloatEvent":
            raise ValueError("unsupported tempo event type")
        if any(key.startswith("CurveControl") for key in event.attrib):
            raise ValueError("curved tempo requires a separate curve-aware representation")
        raw_events.append(
            {"beat": float(event.attrib["Time"]), "bpm": float(event.attrib["Value"])}
        )
    points = []
    for event in raw_events:
        if event["beat"] < 0:
            continue
        if points and points[-1]["beat"] == event["beat"]:
            if len(points) > 1:
                points[-2]["shape"] = "step"
            points[-1]["bpm"] = event["bpm"]
        else:
            if points:
                points[-1]["shape"] = "ramp"
            points.append({**event, "shape": "step"})
    manual = tempo.find("Manual")
    if manual is None:
        raise ValueError("tempo has no Manual value")
    if not points or points[0]["beat"] > 0:
        seed = next(
            (row["bpm"] for row in reversed(raw_events) if row["beat"] < 0),
            float(manual.attrib["Value"]),
        )
        points.insert(0, {"beat": 0.0, "bpm": seed, "shape": "step"})
    return {
        "path": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "events": validate(points),
        "raw_events": raw_events,
        "basis": "saved set tempo envelope; not live memory",
    }


def write_copy(
    source: str | Path, destination: str | Path, points: list[dict[str, Any]], confirm: bool
) -> dict[str, Any]:
    """Write a new file exclusively, preserving source bytes and all other set nodes."""
    points = validate(points)
    original, target = Path(source).expanduser().resolve(), Path(destination).expanduser().resolve()
    if original == target or target.exists():
        raise ValueError("output must be a new .als copy, never an existing set")
    if target.suffix.lower() != ".als":
        raise ValueError("output must end in .als")
    raw = original.read_bytes()
    root = ET.fromstring(gzip.decompress(raw))
    tempo, events = envelope(root)
    plan = {
        "source": str(original),
        "output": str(target),
        "events": points,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not confirm:
        return {"ok": False, "blocked": True, "plan": plan}
    events.clear()
    encoded = [(-63072000.0, points[0]["bpm"]), (0.0, points[0]["bpm"])]
    for left, right in pairwise(points):
        if left["shape"] == "step":
            encoded.append((right["beat"], left["bpm"]))
        encoded.append((right["beat"], right["bpm"]))
    for index, (beat, bpm) in enumerate(encoded):
        ET.SubElement(
            events,
            "FloatEvent",
            Id=str(index),
            Time=format(beat, ".17g"),
            Value=format(bpm, ".17g"),
        )
    manual = tempo.find("Manual")
    if manual is None:
        raise ValueError("tempo has no Manual value")
    manual.set("Value", format(points[0]["bpm"], ".17g"))
    content = gzip.compress(ET.tostring(root, encoding="utf-8", xml_declaration=True), mtime=0)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    back = read(target)
    if back["events"] != points:
        raise ValueError("tempo copy failed its event read-back")
    if original.read_bytes() != raw:
        raise ValueError("source changed while the copy was being written")
    return {
        "ok": True,
        "plan": plan,
        "xml_verified": True,
        "live_verified": False,
        "output_sha256": hashlib.sha256(content).hexdigest(),
        "read_back": back,
    }


def seconds_at(beat: float, points: list[dict[str, Any]]) -> float:
    """Integrate linear-in-beat BPM ramps exactly, in quarter-note beats."""
    if not math.isfinite(beat) or beat < 0:
        raise ValueError("beat must be finite and nonnegative")
    points = validate(points)
    total = 0.0
    for index, left in enumerate(points):
        right = points[index + 1] if index + 1 < len(points) else None
        distance = min(beat, right["beat"] if right else beat) - left["beat"]
        if distance <= 0:
            break
        slope = (
            (right["bpm"] - left["bpm"]) / (right["beat"] - left["beat"])
            if right and left["shape"] == "ramp"
            else 0.0
        )
        total += (
            60 * math.log1p(slope * distance / left["bpm"]) / slope
            if abs(slope) > 1e-12
            else 60 * distance / left["bpm"]
        )
        if not right or beat <= right["beat"]:
            break
    return total


def beat_at(seconds: float, points: list[dict[str, Any]]) -> float:
    """Invert monotonic tempo time with a bounded analytic segment search."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("seconds must be finite and nonnegative")
    points = validate(points)
    for index, left in enumerate(points):
        start = seconds_at(left["beat"], points)
        right = points[index + 1] if index + 1 < len(points) else None
        if right and seconds > seconds_at(right["beat"], points):
            continue
        slope = (
            (right["bpm"] - left["bpm"]) / (right["beat"] - left["beat"])
            if right and left["shape"] == "ramp"
            else 0.0
        )
        duration = seconds - start
        distance = (
            left["bpm"] * math.expm1(slope * duration / 60) / slope
            if abs(slope) > 1e-12
            else duration * left["bpm"] / 60
        )
        return left["beat"] + distance
    raise ValueError("tempo conversion failed")
