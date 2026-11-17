"""Record real Live audio with timing channels rather than assuming an OSC start offset."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

from live_maestro.analyzer.client import AnalyzerClient
from live_maestro.analyzer.protocol import DEFAULT_DEVICE_PORT, decode, encode
from live_maestro.audio.capture_protocol import (
    CAPTURE_SCHEMA,
    OPEN_ADDRESS,
    PATH_ADDRESS,
    REPORT_ADDRESS,
    REPORT_FIELDS,
    START_ADDRESS,
    STOP_ADDRESS,
)
from live_maestro.audio.reference import fingerprint
from live_maestro.client import LiveClient


class CaptureControl:
    """One request-owned socket; writes are sent once and never retried."""

    def __init__(self, track_id: int, port: int = DEFAULT_DEVICE_PORT):
        self.track_id = track_id
        self.port = port
        self.sequence = 0
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(2)

    def command(self, address: str, path: str | None = None) -> dict[str, Any]:
        self.sequence += 1
        payload: list[int | float | str] = [
            self.track_id, self.sequence, self.socket.getsockname()[1]
        ]
        if path is not None:
            payload.append(path)
        self.socket.sendto(encode(address, payload), ("127.0.0.1", self.port))
        deadline = time.monotonic() + 2
        path_echo = None
        while time.monotonic() < deadline:
            self.socket.settimeout(max(.01, deadline - time.monotonic()))
            data, _ = self.socket.recvfrom(65536)
            reply_address, values = decode(data)
            if reply_address == PATH_ADDRESS:
                path_echo = " ".join(str(value) for value in values)
                continue
            if reply_address != REPORT_ADDRESS or len(values) != len(REPORT_FIELDS):
                continue
            report = dict(zip(REPORT_FIELDS, values, strict=True))
            if report["request_id"] == self.sequence and report["track_id"] == self.track_id:
                if report["schema"] != CAPTURE_SCHEMA:
                    raise ValueError("Analyzer capture schema mismatch; reload the current device")
                return {**report, "path_echo": path_echo}
        raise TimeoutError("Analyzer capture did not answer; install/reload the updated device")

    def close(self) -> None:
        self.socket.close()


#: Channel order of the recorder's raw file. Only the first two carry audio.
RAW_CHANNELS = ("left", "right", "dsp_counter", "host_beat", "is_playing", "host_flags")

#: Seconds of transport between opening the recording and reaching from_beat after a lead-in.
PREROLL_SECONDS = 0.5


def decode_capture(raw_path: Path, output: Path) -> dict[str, Any]:
    """Validate a sample counter recorded alongside L/R and expose host-clock limitations."""
    import numpy as np
    import soundfile as sf

    from live_maestro.audio.reference import features

    raw, rate = sf.read(raw_path, always_2d=True, dtype="float64")
    if raw.shape[1] != 6 or len(raw) < 2 or not np.isfinite(raw).all():
        raise ValueError("capture must have six finite audio/timing channels")
    count = raw[:, 2]
    nonzero = np.flatnonzero(count > 0)
    warmup = max(0, int(nonzero[0]) - 1) if len(nonzero) else len(count)
    if (warmup > 64 or not np.all(count[:warmup] == 0)
            or not np.allclose(np.diff(count[warmup:]), 1, atol=.01, rtol=0)):
        raise ValueError("recorded DSP counter is discontinuous; capture timing is not verified")
    active = raw[:, 4] >= .5
    flags = np.rint(raw[:, 5]).astype(int)
    valid = (flags & 2) != 0
    transport_valid = (flags & 16) != 0
    changes = np.flatnonzero(np.r_[True, (np.diff(raw[:, 3]) != 0)
                                  | (np.diff(active) != 0) | (np.diff(valid) != 0)
                                  | (np.diff(transport_valid) != 0)])
    anchors = [{"frame": int(index), "seconds": float(index / rate),
                "song_beat": float(raw[index, 3]), "is_playing": bool(active[index]),
                "beats_valid": bool(valid[index]),
                "transport_valid": bool(transport_valid[index])} for index in changes]
    active_changes = [item["frame"] for item in anchors
                      if item["is_playing"] and item["beats_valid"] and item["transport_valid"]]
    resolution = (max(np.diff(active_changes).tolist(), default=0)
                  if len(active_changes) > 1 else None)
    path = output / "capture.wav"
    sf.write(path, raw[:, :2], rate, subtype="FLOAT")
    return {"schema": "maestro.capture", "schema_version": 1,
            "audio_paths": [str(path)], "raw_path": str(raw_path),
            "raw_channels": list(RAW_CHANNELS),
            "audio_basis": "audio_paths[0] is the stereo audio; raw_path is not audio and "
                           "anchor frames index both files alike",
            "raw_sha256": fingerprint(raw_path), "sample_rate": rate, "frames": len(raw),
            "features": features(raw[:, :2], rate),
            "timeline": {"anchors": anchors, "counter_first": float(count[0]),
                "counter_last": float(count[-1]), "counter_step_verified": True,
                "counter_verified_from_frame": warmup,
                "host_update_max_observed_frames": resolution,
                "host_update_max_observed_ms": 1000 * resolution / rate if resolution else None,
                "basis": "host beat/transport/validity recorded alongside the same audio samples",
                "accuracy": "host fields are scheduler sampled; observed resolution is not a bound",
                "sample_accurate_song_origin": False,
                "time_basis": "map song beats to frames with get_audio_report(section="
                    "'timeline', beats=[...]); a start estimated from the tempo map can be "
                    "beats off",
                "beat_witness_lag_ms": None,
                "beat_witness_lag_basis": "unmeasured; plugin latency makes it set dependent, "
                    "so calibrate per set (docs/reference-workflows.md)"},
            "review_required": True,
            "audio_position": "signal at Analyzer; main-track reading is before its fader"}


async def _capture(request: dict[str, Any]) -> dict[str, Any]:
    output = Path(request["output_dir"])
    raw = output / "capture-with-timing.wav"
    client = LiveClient(host=request.get("host", "127.0.0.1"), port=request.get("port", 9878))
    target = request["track"].strip()
    if target.casefold() in {"master", "main"}:
        target = str(client.get("song.master_track.name")["value"])
    elif target.isdigit():
        target = str(client.get(f"song.tracks[{int(target)}].name")["value"])
    analyzer = AnalyzerClient()
    replies = await analyzer.measure()
    matching = [reply for reply in replies if reply.track_name.casefold() == target.casefold()]
    if len(matching) != 1 or not matching[0].track_id:
        client.close()
        raise ValueError("capture requires exactly one Analyzer on the named track")
    control = CaptureControl(matching[0].track_id, analyzer.port)
    before = {field: client.get(f"song.{field}")["value"]
              for field in ("file_path", "is_playing", "current_song_time", "start_time",
                            "record_mode", "session_record", "back_to_arranger", "tempo")}
    from_beat, play_from = request.get("from_beat"), request.get("play_from_beat")
    if from_beat is not None and (
        before["is_playing"] or before["record_mode"] or before["session_record"]
    ):
        client.close()
        control.close()
        raise ValueError("from_beat capture requires a stopped transport with recording disabled; "
                         "current playback is preserved")
    recording, transport_owned = False, False
    receipts = []
    try:
        if (output / "cancel.request").exists():
            raise ValueError("capture cancelled before recording")
        if play_from is not None and from_beat is not None:
            # Play the lead-in unrecorded so instruments that remember what they last
            # played (keyswitches, CC) reach the window in the state the song gives them.
            transport_owned = True
            client.set("song.start_time", play_from)
            client.set("song.current_song_time", play_from)
            client.call("song", "start_playing")
            lead = PREROLL_SECONDS * float(before["tempo"]) / 60.0
            limit = time.monotonic() + (from_beat - play_from) * 60.0 / float(before["tempo"]) + 30
            while float(client.get("song.current_song_time")["value"]) < from_beat - lead:
                if (output / "cancel.request").exists():
                    raise ValueError("capture cancelled during the lead-in")
                if time.monotonic() > limit or not client.get("song.is_playing")["value"]:
                    raise ValueError("the lead-in did not reach from_beat; check loop and tempo")
                await asyncio.sleep(.02)
        recording = True  # an open may land even if its acknowledgement is lost
        receipts.append(control.command(OPEN_ADDRESS, raw.as_posix()))
        await asyncio.sleep(.1)
        recording = True  # even a lost start acknowledgement requires a stop in finally
        receipts.append(control.command(START_ADDRESS))
        await asyncio.sleep(.2)  # record pre-roll so no transport command truncates the attack
        if from_beat is not None and play_from is None:
            if (output / "cancel.request").exists():
                raise ValueError("capture cancelled before starting playback")
            transport_owned = True
            client.set("song.start_time", from_beat)
            client.set("song.current_song_time", from_beat)
            client.call("song", "start_playing")
        started = time.monotonic()
        while time.monotonic() - started < request["duration_seconds"]:
            if (output / "cancel.request").exists():
                break
            print(json.dumps({"event": "progress", "progress": min(.95,
                (time.monotonic() - started) / request["duration_seconds"])}), flush=True)
            await asyncio.sleep(.1)
    finally:
        try:
            if recording:
                receipts.append(control.command(STOP_ADDRESS))
        finally:
            try:
                if transport_owned and client.get("song.file_path")["value"] == before["file_path"]:
                    client.call("song", "stop_playing")
                    client.set("song.start_time", before["start_time"])
                    client.set("song.current_song_time", before["current_song_time"])
            finally:
                client.close()
                control.close()
                (output / "capture-control.json").write_text(json.dumps({
                    "receipts": receipts, "requested_path": str(raw), "transport_before": before,
                }), encoding="utf-8")
    report = decode_capture(raw, output)
    report.update(control_receipts=receipts, transport_before=before,
                  cancelled=(output / "cancel.request").exists(),
                  warnings=capture_warnings(before, from_beat, play_from))
    return report


def capture_warnings(
    before: dict[str, Any], from_beat: float | None, play_from: float | None
) -> list[str]:
    """Return what about the set's state can make a capture misleading."""
    warnings = []
    if before.get("back_to_arranger"):
        warnings.append(
            "Back to Arrangement was lit: at least one track was following the Session, and "
            "such a track does not play its Arrangement clips, which reads like a silent "
            "instrument. Press Back to Arrangement and capture again.")
    if from_beat and play_from is None:
        warnings.append(
            "recording started mid-song without a lead-in: instruments that remember what "
            "they last played (keyswitches, CC) start in whatever state they were left in. "
            "Pass play_from_beat to play from earlier and record only the window.")
    return warnings


def locate_beats(anchors: list[dict[str, Any]], beats: list[float],
                 sample_rate: float) -> list[dict[str, Any]]:
    """Map song beats to frames of a capture through its recorded anchors.

    Interpolates linearly between the two valid playing anchors around each beat and
    extrapolates from the nearest pair outside them. The result is the host beat
    witness, which can lead the audio by a set-dependent lag.

    Args:
        anchors: ``timeline.anchors`` of a capture report.
        beats: Song positions in quarter beats.
        sample_rate: The capture's sample rate.

    Returns:
        One row per beat with ``frame``, ``seconds`` and ``basis``
        (``interpolated``, ``extrapolated`` or ``no_anchors``).
    """
    valid = sorted(
        ((float(a["song_beat"]), int(a["frame"])) for a in anchors
         if a.get("is_playing") and a.get("beats_valid") and a.get("transport_valid")),
        key=lambda pair: pair[1])
    points = [p for i, p in enumerate(valid) if i == 0 or p[0] > valid[i - 1][0]]
    rows = []
    for beat in beats:
        if len(points) < 2:
            rows.append({"beat": beat, "frame": None, "seconds": None, "basis": "no_anchors"})
            continue
        pair = next(((a, b) for a, b in pairwise(points)
                     if a[0] <= beat <= b[0]), None)
        basis = "interpolated"
        if pair is None:
            pair = (points[0], points[1]) if beat < points[0][0] else (points[-2], points[-1])
            basis = "extrapolated"
        (beat_a, frame_a), (beat_b, frame_b) = pair
        frame = frame_a + (beat - beat_a) * (frame_b - frame_a) / (beat_b - beat_a)
        rows.append({"beat": beat, "frame": round(frame), "seconds": frame / sample_rate,
                     "basis": basis})
    return rows


def capture(request: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_capture(request))
