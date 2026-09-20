"""Drive the probe device and measure what ``buffer~`` and ``sfrecord~`` do inside Live.

The buffer stage loads a file, dumps the buffer to a WAV and measures it: ``read_failed`` is
no reply, ``silent`` a header parsed and a payload not decoded, ``noise`` ciphertext read as
samples, ``audio`` the one good outcome. Every encrypted specimen gets an uncompressed
control. The record stage times ``sfrecord~``'s lag behind the message that starts it, from
a one sample impulse every ``IMPULSE_PERIOD`` samples off a free running counter read at
start, quantised to one signal vector at each end. Both need the device on a track in a
running Live with its audio engine running: a suspended engine stops counter and recorder.

Usage:
    .venv/bin/python scripts/spike_probe.py ping
    .venv/bin/python scripts/spike_probe.py buffer --specimens 4
    .venv/bin/python scripts/spike_probe.py record --seconds 3
    .venv/bin/python scripts/spike_probe.py all --out measurements/spikes
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import socket
import struct
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from live_maestro.analyzer.protocol import decode, encode
from live_maestro.audio.sample_header import (
    SampleHeader,
    SampleHeaderError,
    read_sample_header,
)
from live_maestro.probe_protocol import (
    BUFFER_ADDRESS,
    BUFFER_REPLY_ADDRESS,
    BUFFER_REPLY_FIELDS,
    DEVICE_PORT,
    DUMP_ADDRESS,
    IMPULSE_PERIOD,
    PING_ADDRESS,
    PONG_ADDRESS,
    REC_MARK_FIELDS,
    REC_MARK_REPLY_ADDRESS,
    REC_OPEN_ADDRESS,
    REC_START_ADDRESS,
    REC_STOP_ADDRESS,
    REPLY_PORT,
)

#: Where the device is told to write. A dump lands outside the repository on purpose:
#: it is raw factory audio and has no business in version control.
SCRATCH = Path("/tmp/live-maestro-spike")

#: How long to wait for one reply. A buffer~ replace of a long file is disk bound, and
#: Live's main thread is doing it, so this is generous rather than tight.
REPLY_TIMEOUT_S = 20.0

#: Roots searched for specimens, in order.
SPECIMEN_ROOTS = (
    Path.home() / "Music/Ableton/Factory Packs",
    Path.home() / "Music/Ableton/User Library",
)


class ProbeTimeout(RuntimeError):
    """No reply arrived inside :data:`REPLY_TIMEOUT_S`."""


@dataclass
class Dump:
    """What the samples in a written dump look like, without deciding what they mean."""

    frames: int
    channels: int
    sample_rate: float
    bit_depth: int
    encoding: str
    peak: float
    rms: float
    crest_db: float
    zero_fraction: float
    zero_crossing_rate: float
    verdict: str


class Probe:
    """One UDP conversation with the device."""

    def __init__(self, *, host: str = "127.0.0.1") -> None:
        self.target = (host, DEVICE_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", REPLY_PORT))
        self.sock.settimeout(0.25)
        self._next_id = 1

    def close(self) -> None:
        self.sock.close()

    def request_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def send(self, address: str, args: list[int | float | str]) -> None:
        self.sock.sendto(encode(address, args), self.target)

    def drain(self) -> None:
        """Throw away anything still in flight from an earlier request."""
        while True:
            try:
                self.sock.recv(65535)
            except TimeoutError:
                return
            except OSError:
                return

    def await_reply(
        self, address: str, request_id: int, *, timeout: float = REPLY_TIMEOUT_S
    ) -> list[int | float | str]:
        """Wait for one reply at ``address`` carrying ``request_id`` as its first arg.

        Replies for other ids are dropped rather than queued: the device answers one
        request at a time and a stale reply is the previous question, not this one.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet = self.sock.recv(65535)
            except TimeoutError:
                continue
            try:
                got_address, args = decode(packet)
            except (ValueError, struct.error):
                continue
            if got_address == address and args and int(args[0]) == request_id:
                return args
        raise ProbeTimeout(f"no {address} for request {request_id} in {timeout:.0f}s")

    def ping(self, *, timeout: float = 2.0) -> bool:
        self.drain()
        self.send(PING_ADDRESS, [])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet = self.sock.recv(65535)
            except TimeoutError:
                continue
            try:
                address, _ = decode(packet)
            except (ValueError, struct.error):
                continue
            if address == PONG_ADDRESS:
                return True
        return False


def read_pcm(path: Path) -> tuple[SampleHeader, list[float]]:
    """Every sample of a dump as a float, whatever width the device wrote it at.

    ``wave`` refuses float32 WAV files, which is what a buffer~ dump can be, so the data
    chunk is read against :func:`read_sample_header` instead.
    """
    header = read_sample_header(path)
    data = next((c for c in header.chunks if c.chunk_id in ("data", "SSND")), None)
    if data is None:
        raise SampleHeaderError(
            "no_sample_data", f"{path} carries no sample data chunk"
        )
    raw = path.read_bytes()
    # Chunk.offset is the chunk header, so the payload is always eight bytes further in.
    # SSND puts two more longs of its own there, an offset and a block size, and the
    # first of them says how much padding follows before the samples begin.
    start = data.offset + 8
    size = data.size
    if data.chunk_id == "SSND":
        padding = struct.unpack_from(">I", raw, start)[0]
        start += 8 + padding
        size = max(size - 8 - padding, 0)
    body = raw[start : start + size]
    width = (header.bit_depth + 7) // 8
    little = header.container == "WAVE"

    values: list[float] = []
    if header.compression in ("fl32", "FL32", "float") or header.compression in (
        "fl64",
        "FL64",
    ):
        code = ("<" if little else ">") + ("d" if width == 8 else "f")
        for offset in range(0, len(body) - width + 1, width):
            values.append(struct.unpack_from(code, body, offset)[0])
        return header, values

    full = float(1 << (header.bit_depth - 1))
    order = "little" if little else "big"
    for offset in range(0, len(body) - width + 1, width):
        sample = int.from_bytes(body[offset : offset + width], order, signed=True)
        values.append(sample / full)
    return header, values


def describe_dump(path: Path) -> Dump:
    """Measure a dump, and say which of the four outcomes it looks like."""
    header, values = read_pcm(path)
    if not values:
        return Dump(0, header.channels, header.sample_rate, header.bit_depth,
                    header.compression, 0.0, 0.0, 0.0, 1.0, 0.0, "empty")

    peak = max(abs(v) for v in values)
    rms = math.sqrt(sum(v * v for v in values) / len(values))
    zeros = sum(1 for v in values if v == 0.0)
    crossings = sum(
        1 for a, b in itertools.pairwise(values) if (a < 0.0) != (b < 0.0)
    )
    crest = 20.0 * math.log10(peak / rms) if rms > 0.0 and peak > 0.0 else 0.0
    zero_fraction = zeros / len(values)
    crossing_rate = crossings / max(1, len(values) - 1)

    # Noise crosses zero on about half of all sample pairs and has a crest factor near
    # 11 dB. Music and one shots cross far less often and peak far above their RMS.
    if peak == 0.0:
        verdict = "silent"
    elif crossing_rate > 0.40 and crest < 15.0:
        verdict = "noise"
    else:
        verdict = "audio"
    return Dump(
        frames=len(values) // max(1, header.channels),
        channels=header.channels,
        sample_rate=header.sample_rate,
        bit_depth=header.bit_depth,
        encoding=header.compression,
        peak=round(peak, 6),
        rms=round(rms, 6),
        crest_db=round(crest, 2),
        zero_fraction=round(zero_fraction, 6),
        zero_crossing_rate=round(crossing_rate, 6),
        verdict=verdict,
    )


def find_specimens(count: int, *, seed: int = 7) -> tuple[list[Path], list[Path]]:
    """``count`` encrypted files and ``count`` readable ones, drawn reproducibly."""
    pool: list[Path] = []
    for root in SPECIMEN_ROOTS:
        if root.is_dir():
            pool.extend(root.rglob("*.aif"))
    random.Random(seed).shuffle(pool)

    encrypted: list[Path] = []
    readable: list[Path] = []
    for path in pool:
        if len(encrypted) >= count and len(readable) >= count:
            break
        try:
            header = read_sample_header(path)
        except (SampleHeaderError, OSError):
            continue
        if header.code == "encrypted" and len(encrypted) < count:
            encrypted.append(path)
        elif header.audio_readable and len(readable) < count:
            readable.append(path)
    return encrypted, readable


def probe_one(probe: Probe, path: Path) -> dict[str, Any]:
    """Load one file into the buffer, dump it, and measure the dump."""
    header = read_sample_header(path)
    entry: dict[str, Any] = {
        "path": str(path),
        "header": {
            "container": header.container,
            "compression": header.compression,
            "code": header.code,
            "channels": header.channels,
            "frames": header.frames,
            "bit_depth": header.bit_depth,
            "sample_rate": header.sample_rate,
            "duration_s": round(header.duration_s, 6),
            "same_size_as_pcm": header.payload_bytes == header.expected_pcm_bytes,
        },
    }

    probe.drain()
    load_id = probe.request_id()
    probe.send(BUFFER_ADDRESS, [load_id, str(path)])
    try:
        args = probe.await_reply(BUFFER_REPLY_ADDRESS, load_id)
    except ProbeTimeout as exc:
        entry["buffer"] = {"answered": False, "why": str(exc)}
        entry["outcome"] = "read_failed"
        return entry

    reading = dict(zip(BUFFER_REPLY_FIELDS, args, strict=False))
    reading["answered"] = True
    entry["buffer"] = reading

    dump_path = SCRATCH / f"dump_{load_id:03d}.wav"
    dump_id = probe.request_id()
    probe.send(DUMP_ADDRESS, [dump_id, str(dump_path)])
    try:
        probe.await_reply(BUFFER_REPLY_ADDRESS, dump_id)
    except ProbeTimeout as exc:
        entry["dump"] = {"written": False, "why": str(exc)}
        entry["outcome"] = "dump_failed"
        return entry

    if not dump_path.is_file():
        entry["dump"] = {"written": False, "why": f"{dump_path} is not there"}
        entry["outcome"] = "dump_missing"
        return entry

    measured = describe_dump(dump_path)
    entry["dump"] = asdict(measured)
    entry["dump"]["path"] = str(dump_path)
    entry["outcome"] = measured.verdict
    return entry


def stage_buffer(
    probe: Probe, specimens: int, named: list[Path] | None = None
) -> dict[str, Any]:
    """Probe encrypted files against readable controls.

    ``named`` replaces the drawn specimens with exactly those files, sorted into the two
    groups by their own headers. The control that matters most cannot be drawn at random:
    this library holds 19,725 encrypted AIFC files and exactly one that is not, and
    without it a failure to read AIFC at all is indistinguishable from a failure to
    decrypt.
    """
    if named:
        encrypted, readable = [], []
        for path in named:
            try:
                header = read_sample_header(path)
            except (SampleHeaderError, OSError) as exc:
                print(f"  skipped    {path}: {exc}", flush=True)
                continue
            (encrypted if header.code == "encrypted" else readable).append(path)
    else:
        encrypted, readable = find_specimens(specimens)
    out: dict[str, Any] = {"encrypted": [], "control": []}
    for label, paths in (("encrypted", encrypted), ("control", readable)):
        for path in paths:
            print(f"  {label:<10} {path.name}", flush=True)
            entry = probe_one(probe, path)
            print(f"             -> {entry['outcome']}", flush=True)
            out[label].append(entry)
    verdicts = {e["outcome"] for e in out["encrypted"]}
    controls = {e["outcome"] for e in out["control"]}
    out["answer"] = {
        "encrypted_outcomes": sorted(verdicts),
        "control_outcomes": sorted(controls),
        "buffer_reads_encrypted_factory_samples": verdicts == {"audio"},
        "control_proves_the_method": controls == {"audio"},
    }
    return out


def stage_record(probe: Probe, seconds: float, repeat: int = 1) -> dict[str, Any]:
    """Measure ``sfrecord~`` start latency over ``repeat`` runs.

    The spread is the answer, not the mean. A recorder that always lags by the same
    amount can be calibrated out with a constant; one that lands somewhere in a range
    cannot, and a caller then needs a marker inside the recording rather than an offset
    applied to it.
    """
    runs = [_record_once(probe, seconds) for _ in range(max(1, repeat))]
    good = [
        r["answer"]["start_latency_samples"]
        for r in runs
        if r.get("answer", {}).get("conclusive")
    ]
    summary: dict[str, Any] = {"runs": runs, "conclusive_runs": len(good), "runs_asked": repeat}
    if good:
        rate = float(runs[0].get("written", {}).get("sample_rate") or 48000.0)
        vector = sorted({(v + 1) % 64 for v in good})
        summary["latency_samples"] = {
            "readings": good,
            "min": min(good),
            "max": max(good),
            "spread": max(good) - min(good),
            "distinct": sorted(set(good)),
        }
        summary["latency_ms"] = {
            "min": round(1000.0 * min(good) / rate, 4),
            "max": round(1000.0 * max(good) / rate, 4),
        }
        summary["all_on_a_64_sample_vector"] = vector == [0]
        summary["constant"] = len(set(good)) == 1
    return summary


def _record_once(probe: Probe, seconds: float) -> dict[str, Any]:
    """One open, start, wait, stop cycle, measured."""
    target = SCRATCH / "record.wav"
    target.unlink(missing_ok=True)

    probe.drain()
    open_id = probe.request_id()
    probe.send(REC_OPEN_ADDRESS, [open_id, str(target)])
    time.sleep(0.3)

    start_id = probe.request_id()
    sent_at = time.monotonic()
    probe.send(REC_START_ADDRESS, [start_id])
    start = dict(
        zip(REC_MARK_FIELDS, probe.await_reply(REC_MARK_REPLY_ADDRESS, start_id),
            strict=False)
    )
    time.sleep(seconds)
    stop_id = probe.request_id()
    probe.send(REC_STOP_ADDRESS, [stop_id])
    stop = dict(
        zip(REC_MARK_FIELDS, probe.await_reply(REC_MARK_REPLY_ADDRESS, stop_id),
            strict=False)
    )
    wall = time.monotonic() - sent_at
    time.sleep(0.5)

    result: dict[str, Any] = {
        "file": str(target),
        "start_mark": start,
        "stop_mark": stop,
        "wall_seconds": round(wall, 4),
        "impulse_period_samples": IMPULSE_PERIOD,
    }
    if not target.is_file():
        result["answer"] = {"recorded": False, "why": "sfrecord~ wrote no file"}
        return result

    header, values = read_pcm(target)
    result["written"] = {
        "container": header.container,
        "compression": header.compression,
        "channels": header.channels,
        "frames": header.frames,
        "sample_rate": header.sample_rate,
        "bit_depth": header.bit_depth,
        "duration_s": round(header.duration_s, 6),
    }

    impulses = [i for i, v in enumerate(values) if abs(v) > 0.5]
    start_count = float(start.get("count", 0.0))
    stop_count = float(stop.get("count", 0.0))
    spacing = [b - a for a, b in itertools.pairwise(impulses)]

    # Three ways this stage can produce a number that means nothing. A counter standing
    # still makes every sample an impulse; a silent recording makes none; a spacing that
    # is not the period means the signal reaching the recorder is not the train this
    # measures.
    if not impulses:
        why = "no impulse above half scale: nothing reached the recorder"
    elif stop_count <= start_count:
        why = (
            f"count~ did not advance between the marks ({start_count} -> {stop_count}), "
            f"so the counter is off and the impulse train is a constant"
        )
    elif set(spacing) - {IMPULSE_PERIOD}:
        why = (
            f"impulse spacing {sorted(set(spacing))[:5]} is not the period "
            f"{IMPULSE_PERIOD}, so the recorded signal is not the train this measures"
        )
    else:
        why = ""
    if why:
        result["answer"] = {
            "recorded": True,
            "conclusive": False,
            "impulses_found": len(impulses),
            "impulse_spacing_samples": sorted(set(spacing))[:5],
            "why": why,
        }
        return result

    # The impulses sit at multiples of the period on the global clock. Sample zero of
    # the file is therefore at global sample (impulse_global - first), and the latency
    # is that minus the counter reading taken when the start message was handled.
    first = impulses[0]
    latency = -(start_count + first) % IMPULSE_PERIOD
    result["answer"] = {
        "recorded": True,
        "conclusive": True,
        "impulses_found": len(impulses),
        "first_impulse_sample": first,
        "impulse_spacing_samples": sorted(set(spacing))[:5],
        "start_latency_samples": latency,
        "start_latency_ms": round(1000.0 * latency / max(1.0, header.sample_rate), 4),
        "quantisation": (
            "snapshot~ reads the counter at the last signal vector boundary and "
            "sfrecord~ starts at the next one, so one vector at each end is the floor "
            "under this figure"
        ),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/spike_probe.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("stage", choices=("ping", "buffer", "record", "all"))
    parser.add_argument("--specimens", type=int, default=4,
                        help="how many encrypted files, and as many controls")
    parser.add_argument("--file", type=Path, action="append", default=[], metavar="PATH",
                        help="probe exactly this file instead of drawing specimens; "
                             "repeatable, and each is grouped by its own header")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="how long to record for the sfrecord~ stage")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="how many recordings to take; the spread is the answer")
    parser.add_argument("--out", type=Path, default=None, metavar="DIR",
                        help="write a JSON record of the run into this directory")
    args = parser.parse_args(argv)

    SCRATCH.mkdir(parents=True, exist_ok=True)
    probe = Probe()
    try:
        if not probe.ping():
            print(
                f"no answer on UDP {DEVICE_PORT}. The device has to be on a track in a "
                f"running Live, and Live has to have been restarted since it was "
                f"installed.",
                file=sys.stderr,
            )
            return 2
        print(f"device answers on UDP {DEVICE_PORT}")
        if args.stage == "ping":
            return 0

        record: dict[str, Any] = {
            "measured_on": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
            "stage": args.stage,
        }
        if args.stage in ("buffer", "all"):
            print("spike 1: buffer~ against encrypted factory samples")
            record["spike_1_buffer"] = stage_buffer(probe, args.specimens, args.file)
        if args.stage in ("record", "all"):
            print("spike 2: sfrecord~ start latency")
            record["spike_2_sfrecord"] = stage_record(
                probe, args.seconds, args.repeat
            )

        text = json.dumps(record, indent=1, ensure_ascii=False)
        if args.out is not None:
            args.out.mkdir(parents=True, exist_ok=True)
            destination = args.out / f"spike-probe-raw-{args.stage}.json"
            destination.write_text(text + "\n")
            print(f"-> {destination}")
        else:
            print(text)
        return 0
    finally:
        probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
