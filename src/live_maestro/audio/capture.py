"""Record real Live audio with timing channels rather than assuming an OSC start offset."""

from __future__ import annotations

import asyncio
import json
import socket
import time
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
            "raw_sha256": fingerprint(raw_path), "sample_rate": rate, "frames": len(raw),
            "features": features(raw[:, :2], rate),
            "timeline": {"anchors": anchors, "counter_first": float(count[0]),
                "counter_last": float(count[-1]), "counter_step_verified": True,
                "counter_verified_from_frame": warmup,
                "host_update_max_observed_frames": resolution,
                "host_update_max_observed_ms": 1000 * resolution / rate if resolution else None,
                "basis": "host beat/transport/validity recorded alongside the same audio samples",
                "accuracy": "host fields are scheduler sampled; observed resolution is not a bound",
                "sample_accurate_song_origin": False}, "review_required": True,
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
                            "record_mode", "session_record")}
    if request.get("from_beat") is not None and (
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
        recording = True  # an open may land even if its acknowledgement is lost
        receipts.append(control.command(OPEN_ADDRESS, raw.as_posix()))
        await asyncio.sleep(.1)
        recording = True  # even a lost start acknowledgement requires a stop in finally
        receipts.append(control.command(START_ADDRESS))
        await asyncio.sleep(.2)  # record pre-roll so no transport command truncates the attack
        if request.get("from_beat") is not None:
            if (output / "cancel.request").exists():
                raise ValueError("capture cancelled before starting playback")
            transport_owned = True
            client.set("song.start_time", request["from_beat"])
            client.set("song.current_song_time", request["from_beat"])
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
                  cancelled=(output / "cancel.request").exists())
    return report


def capture(request: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_capture(request))
