"""Verified native file operations and clip musical-time transforms, without UVR imports."""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import time
import wave
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path
from typing import Any

from live_maestro.als.tempo import beat_at, read, seconds_at
from live_maestro.client import LiveClient


def fingerprint(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            result.update(block)
    return result.hexdigest()


def validate_attack(source: Path, attack_sample: int) -> None:
    """Check nonzero attack offsets before creating or replacing any Live device."""
    if isinstance(attack_sample, bool) or not isinstance(attack_sample, int) or attack_sample < 0:
        raise ValueError("attack_sample must be a nonnegative integer frame")
    if attack_sample:
        try:
            with wave.open(str(source), "rb") as handle:
                if attack_sample >= handle.getnframes():
                    raise ValueError(
                        "attack_sample exceeds source WAV frames; nothing was replaced"
                    )
        except wave.Error as error:
            raise ValueError("nonzero attack_sample currently requires a PCM WAV") from error


def load_sample(
    client: LiveClient,
    audio_path: str,
    device_path: str,
    attack_sample: int,
    warp: bool,
    confirm: bool,
) -> dict[str, Any]:
    """Replace one existing Simpler sample, preserving its surrounding device chain."""
    if not re.fullmatch(
        r"song\.tracks\[\d+\]\.devices\[\d+\]"
        r"(?:\.drum_pads\[\d+\]\.chains\[\d+\]\.devices\[\d+\])?",
        device_path,
    ):
        raise ValueError("device_path must identify a track or pad-chain Simpler")
    source = Path(audio_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("audio file is missing")
    validate_attack(source, attack_sample)
    plan = {
        "audio_path": str(source),
        "device_path": device_path,
        "attack_sample": attack_sample,
        "warp": warp,
    }
    if not confirm:
        return {"ok": False, "blocked": True, "plan": plan}
    if client.get(device_path + ".class_name")["value"] != "OriginalSimpler":
        raise ValueError("target must be a Simpler device")
    expected = fingerprint(source)
    client.call(device_path, "replace_sample", [str(source)])
    actual = Path(client.get(device_path + ".sample.file_path")["value"])
    verified = actual.is_file() and fingerprint(actual) == expected
    if not verified:
        return {"ok": False, "verified": False, "actual_sample": str(actual), "partial": True}
    length = client.get(device_path + ".sample.length")["value"]
    if attack_sample >= length:
        raise ValueError("attack_sample exceeds the loaded sample; sample was replaced")
    client.set(device_path + ".sample.warping", warp)
    client.set(device_path + ".sample.start_marker", attack_sample)
    observed_warp = client.get(device_path + ".sample.warping")["value"]
    observed_attack = client.get(device_path + ".sample.start_marker")["value"]
    verified = observed_warp == warp and observed_attack == attack_sample
    return {
        "ok": verified,
        "verified": verified,
        "sample_path": str(actual),
        "sha256": expected,
        "frames": length,
        "attack_sample": observed_attack,
        "warp": observed_warp,
        "device_path": device_path,
    }


def load_pad(
    client: LiveClient,
    audio_path: str,
    rack_path: str,
    pad_note: int,
    attack_sample: int,
    confirm: bool,
) -> dict[str, Any]:
    """Populate an empty pad via a real browser item, or replace its existing Simpler."""
    if not re.fullmatch(r"song\.tracks\[(\d+)\]\.devices\[\d+\]", rack_path):
        raise ValueError("rack_path must identify a Drum Rack on a track")
    if not 0 <= pad_note <= 127:
        raise ValueError("pad_note must be 0-127")
    source = Path(audio_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("audio file is missing")
    validate_attack(source, attack_sample)
    pad = f"{rack_path}.drum_pads[{pad_note}]"
    if not confirm:
        return {
            "ok": False,
            "blocked": True,
            "plan": {
                "source": str(source),
                "pad": pad,
                "attack_sample": attack_sample,
                "empty_pad_route": "copy if needed into saved project, then browser load",
            },
        }
    if not client.get(rack_path + ".can_have_drum_pads")["value"]:
        raise ValueError("target must be a Drum Rack")
    if client.get(pad + ".note")["value"] != pad_note:
        raise ValueError("pad collection does not match requested MIDI note")
    chains = client.get(pad + ".chains")
    if chains.get("count"):
        candidates = []
        for chain in chains["value"]:
            for device in client.get(chain["path"] + ".devices")["value"]:
                if client.get(device["path"] + ".class_name")["value"] == "OriginalSimpler":
                    candidates.append(device["path"])
        if len(candidates) != 1:
            raise ValueError(
                "nonempty pad must have exactly one Simpler; complex layers are refused"
            )
        device_path = candidates[0]
    else:
        opened = client.get("song.file_path")["value"]
        if not opened:
            raise ValueError("save the set first to populate an empty pad from a file")
        project = next(
            (
                folder
                for folder in Path(opened).resolve().parents
                if (folder / "Ableton Project Info").is_dir()
            ),
            None,
        )
        if project is None:
            raise ValueError("saved set has no identifiable project folder")
        indexed = source
        if not source.is_relative_to(project):
            digest = fingerprint(source)
            indexed = (
                project / "Samples/Imported/Maestro" / ("maestro-" + digest[:16] + source.suffix)
            )
            indexed.parent.mkdir(parents=True, exist_ok=True)
            if indexed.exists():
                if fingerprint(indexed) != digest:
                    raise ValueError("staged sample filename collision; existing bytes preserved")
            else:
                with source.open("rb") as reader, indexed.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
        track = re.match(r"song\.tracks\[\d+\]", rack_path)
        assert track is not None
        client.set("song.view.selected_track", {"__path__": track.group(0)})
        client.set(rack_path + ".view.selected_drum_pad", {"__path__": pad})
        client.set("app.browser.hotswap_target", {"__path__": pad})
        try:
            # Targeting a pad changes browser filters and invalidates old item indexes.
            matches = []
            for _ in range(6):
                reply = client.send(
                    "browser_walk", {"root": "current_project", "query": indexed.name, "limit": 100}
                )
                matches = [
                    item for item in reply.get("matches", []) if item["name"] == indexed.name
                ]
                if matches:
                    break
                time.sleep(0.25)
            if len(matches) != 1:
                raise ValueError("sample is not uniquely indexed in the current project")
            client.call("app.browser", "load_item", [{"__path__": matches[0]["path"]}])
        finally:
            client.set("app.browser.hotswap_target", {"__path__": None})
        device_path = pad + ".chains[0].devices[0]"
        source = indexed
    result = load_sample(client, str(source), device_path, attack_sample, False, True)
    if not result.get("verified"):
        return result
    client.set(device_path + ".playback_mode", 1)
    observed_mode = client.get(device_path + ".playback_mode")["value"]
    result.update(
        ok=observed_mode == 1,
        verified=observed_mode == 1,
        pad_note=pad_note,
        playback_mode=observed_mode,
        selection_note="empty-pad creation leaves selection on the destination",
    )
    return result


def import_audio(
    client: LiveClient, audio_path: str, track: int, at_beat: float, warp: bool, confirm: bool
) -> dict[str, Any]:
    """Create directly in Arrangement and identify the added clip by before/after state."""
    source = Path(audio_path).expanduser().resolve()
    if not source.is_file() or not math.isfinite(at_beat) or not 0 <= at_beat <= 1576800:
        raise ValueError("file must exist and position must be finite, nonnegative and in range")
    plan = {"audio_path": str(source), "track": track, "at_beat": at_beat, "warp": warp}
    if not confirm:
        return {"ok": False, "blocked": True, "plan": plan}
    target = f"song.tracks[{track}]"
    if not client.get(target + ".has_audio_input")["value"]:
        raise ValueError("destination must be an audio track")
    before = client.get(target + ".arrangement_clips")["count"]
    # The native create may replace overlapping clips. Refuse instead of silently slicing them.
    if before:
        raise ValueError("direct import currently requires an empty Arrangement audio track")
    expected = fingerprint(source)
    client.call(target, "create_audio_clip", [str(source), at_beat])
    after = client.get(target + ".arrangement_clips")
    if after.get("count") != 1:
        return {"ok": False, "partial": True, "error": "added clip could not be identified"}
    path = after["value"][0]["path"]
    client.set(path + ".warping", warp)
    if not warp:
        frames = client.get(path + ".sample_length")["value"]
        rate = client.get(path + ".sample_rate")["value"]
        # Live can retain an old beat-valued end marker after Warp is disabled.
        client.set(path + ".looping", False)
        client.set(path + ".loop_start", 0)
        client.set(path + ".start_marker", 0)
        client.set(path + ".loop_end", frames / rate)
        client.set(path + ".end_marker", frames / rate)
    observed = {
        key: client.get(path + "." + key)["value"]
        for key in (
            "file_path",
            "start_time",
            "end_time",
            "start_marker",
            "end_marker",
            "loop_start",
            "loop_end",
            "sample_length",
            "sample_rate",
            "warping",
            "looping",
        )
    }
    actual = Path(observed["file_path"])
    verified = (
        actual.is_file()
        and fingerprint(actual) == expected
        and abs(observed["start_time"] - at_beat) < 1e-6
        and observed["warping"] == warp
    )
    if not warp:
        source_duration = observed["sample_length"] / observed["sample_rate"]
        verified = (verified and not observed["looping"]
                    and abs(observed["start_marker"]) <= 1 / observed["sample_rate"]
                    and abs(observed["loop_start"]) <= 1 / observed["sample_rate"]
                    and abs(observed["end_marker"] - source_duration) <= 1 / observed["sample_rate"]
                    and abs(observed["loop_end"] - source_duration) <= 1 / observed["sample_rate"])
    timing: dict[str, Any] = {"duration_verified": None, "duration_basis": "tempo not inspected"}
    if not warp:
        opened = client.get("song.file_path")["value"]
        if opened and Path(opened).is_file():
            try:
                events = read(opened)["events"]
                duration = seconds_at(observed["end_time"], events) - seconds_at(
                    observed["start_time"], events
                )
                source_duration = observed["sample_length"] / observed["sample_rate"]
                timing = {
                    "duration_verified": abs(duration - source_duration) <= 0.001,
                    "duration_seconds": duration,
                    "source_seconds": source_duration,
                    "duration_basis": "native geometry against persisted tempo; save set first",
                }
            except (OSError, ValueError, KeyError, ET.ParseError):
                timing["duration_basis"] = "persisted tempo could not be inspected"
    return {
        "ok": verified,
        "verified": verified,
        "partial": not verified,
        "clip_path": path,
        "observed": observed,
        "sha256": expected,
        "native_method": "Track.create_audio_clip",
        **timing,
    }


def convert_time(
    client: LiveClient, path: str, value: float, direction: str, tempo_path: str
) -> dict[str, Any]:
    """Combine source warp mapping or physical seconds with Arrangement tempo integration."""
    if not re.fullmatch(r"song\.tracks\[\d+\]\.arrangement_clips\[\d+\]", path):
        raise ValueError("path must identify an Arrangement audio clip")
    if not math.isfinite(value) or value < 0:
        raise ValueError("value must be finite and nonnegative")
    if direction not in ("source_to_song", "song_to_source"):
        raise ValueError("unknown conversion direction")
    properties = {
        key: client.get(path + "." + key)["value"]
        for key in (
            "is_audio_clip",
            "warping",
            "start_time",
            "start_marker",
            "looping",
        )
    }
    if not properties["is_audio_clip"]:
        raise ValueError("clip must be audio")
    if properties["looping"]:
        raise ValueError(
            "looped clips have multiple source-time occurrences; select a non-looping clip"
        )
    start, marker = properties["start_time"], properties["start_marker"]
    if properties["warping"]:
        nodes = []
        for item in client.get(path + ".warp_markers")["value"]:
            node = item["path"]
            nodes.append(
                (
                    client.get(node + ".sample_time")["value"],
                    client.get(node + ".beat_time")["value"],
                )
            )
        nodes.sort()
        if len(nodes) < 2:
            raise ValueError("warp map needs at least two points")
        if any(not math.isfinite(s) or not math.isfinite(b) for s, b in nodes) or any(
            right[0] <= left[0] or right[1] <= left[1] for left, right in pairwise(nodes)
        ):
            raise ValueError("warp map is not finite and strictly invertible")
        points = nodes if direction == "source_to_song" else [(b, s) for s, b in nodes]
        query = value if direction == "source_to_song" else value - start + marker
        left, right = points[0], points[1]
        for a, b in pairwise(points):
            left, right = a, b
            if query <= b[0]:
                break
        if right[0] <= left[0]:
            raise ValueError("warp map is not invertible")
        interpolated = left[1] + (query - left[0]) * (right[1] - left[1]) / (right[0] - left[0])
        result = interpolated - marker + start if direction == "source_to_song" else interpolated
        basis = "live warp markers, clip origin and start marker"
    else:
        if not tempo_path:
            raise ValueError("unwarped conversion requires a reviewed saved tempo map")
        opened = client.get("song.file_path")["value"]
        if not opened or Path(opened).resolve() != Path(tempo_path).expanduser().resolve():
            raise ValueError("tempo_path must be the saved set currently open in Live")
        events = read(tempo_path)["events"]
        result = (
            beat_at(seconds_at(start, events) + value - marker, events)
            if direction == "source_to_song"
            else seconds_at(value, events) - seconds_at(start, events) + marker
        )
        basis = "unwarped source seconds integrated against persisted tempo; save set first"
    return {
        "ok": True,
        "value": result,
        "unit": "song_quarter_beats" if direction == "source_to_song" else "source_seconds",
        "basis": basis,
        "extrapolation": "nearest end segment",
    }
