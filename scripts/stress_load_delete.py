#!/usr/bin/env python
"""Load one device and delete it again, many times, and watch what Live's memory does.

Guards come before measurement. Nothing runs without ``--expect-set``, ``--sentinel``,
``--track`` and a journal path; the default ``dry`` stage writes nothing and is wrapped in
:class:`ReadOnlyGate`, and every writing stage additionally needs ``--go``. The set and the
target track are identified before the first write, and the selection is aimed once in the
preflight and never again: a selection that moved mid-run means a person is clicking in
Live, which is a reason to stop rather than to aim again. A cycle is accepted on
read-back evidence and never on ``ok``, and one that is not accepted stops the run
rather than retrying a write that may have landed.

Stages ``dry`` (3 cycles) -> ``a`` (25) -> ``b`` (100) -> ``c`` (1000). Each writing stage
needs the prior stage's journal through ``--prior``, saying it completed, and its measured
seconds per cycle projecting under ``--max-hours``. Memory is sampled per cycle with ``ps``
and every ``--footprint-every`` cycles with ``footprint``; the two disagree about an idle
Live and can drift in opposite directions, which is why both are sampled. A run is read as
a slope over hundreds of samples against an idle control arm, not as a before/after pair.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Protocol, Self, TextIO

# scripts/ is not a package and this file is run as a path, not as a module. A
# checkout that has not been `pip install -e .`-ed still has src/ next door.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_REPO_ROOT / "src"))

# Imported after the path bootstrap above, which is why they are not at the very
# top of the file.
from live_maestro.client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    READ_ONLY_HANDLERS,
    LiveClient,
    LiveError,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Exit codes. ``2`` is left to argparse, which uses it for a usage error.
CODE_REFUSED = 4
CODE_STOPPED = 3
CODE_WRITE_IN_DRY_MODE = 5

#: Live's own default track names, as a shape: "1 MIDI", "3 Audio", "2-Audio".
#: Read from Live's naming convention, not measured across versions or locales.
#: A sentinel matching this is refused: the catalog records that an instrument load
#: renames a track still carrying Live's default name, and a rename mid-cycle is
#: indistinguishable here from the selection having moved to another track.
_DEFAULT_TRACK_NAME = re.compile(r"^\d+[\s_-]*(midi|audio)$", re.IGNORECASE)

#: Pause after each write. Precaution, not a measurement. What is measured, and
#: recorded in the load_device tool itself, is that loading plugins in rapid
#: succession can crash Live.
DEFAULT_PACE = 1.0

#: How often the expensive footprint sample is taken. ``ps`` costs single-digit
#: milliseconds and footprint costs tens, so footprint rides at a lower rate.
DEFAULT_FOOTPRINT_EVERY = 25

#: The longest run a stage gate will approve without being told otherwise.
DEFAULT_MAX_HOURS = 3.0

#: How many scene names the population guard will read. A stress set has almost none,
#: and a set with more scenes than this has already tripped the guard several times over.
SCENE_NAME_CAP = 64

#: How full a set may be before this harness refuses it. Deliberately tight: the
#: stress set is meant to hold one track and nothing else, so anything beyond that
#: is a set somebody made for another purpose. Overridable, unlike the set identity
#: and the sentinel, which are not.
POPULATION_LIMITS: dict[str, int] = {
    "tracks": 4,
    "renamed tracks": 2,
    "devices": 1,
    "named scenes": 1,
}

_BANNER = """
================================================================================
  stress_load_delete.py LOADS AND DELETES DEVICES IN A RUNNING ABLETON LIVE SET,
  up to a thousand times in a row.

  Use a set made for this and nothing else. Live's undo is not a backup, and a
  crash mid-run is not undone.

  Stage 'dry' writes nothing at all. Every other stage needs --go.
================================================================================
"""


@dataclass(frozen=True)
class StageSpec:
    """One rung of the staircase from a dry rehearsal to the full run."""

    name: str
    cycles: int
    writes: bool
    prior: str | None


STAGES: dict[str, StageSpec] = {
    "dry": StageSpec("dry", 3, False, None),
    "a": StageSpec("a", 25, True, "dry"),
    "b": StageSpec("b", 100, True, "a"),
    "c": StageSpec("c", 1000, True, "b"),
}


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class LomClient(Protocol):
    """The part of :class:`live_maestro.client.LiveClient` this harness uses."""

    def send(
        self,
        handler: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one request to the Remote Script and return its result."""
        ...


class WriteAttempted(RuntimeError):
    """Raised when something tried to write to Live from a read-only stage."""


class ReadOnlyGate:
    """A client wrapper that raises rather than let a write reach Live.

    Refusal sits at the transport, below every intention in this file. Anything that is
    not one of the Remote Script's read-only handlers is refused, and a ``lom_batch`` is
    refused unless every op in it is a ``get``, because ``lom_batch`` as a handler can
    carry a ``set`` or a ``call``.
    """

    def __init__(self, inner: LomClient) -> None:
        self._inner = inner
        #: Every handler this gate saw, in order, whether or not it was allowed.
        self.handlers: list[str] = []

    def send(
        self,
        handler: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Pass a read through to the wrapped client, and refuse anything else."""
        self.handlers.append(handler)
        refusal = write_refusal(handler, params)
        if refusal is not None:
            raise WriteAttempted(refusal)
        return self._inner.send(handler, params, timeout=timeout)


def write_refusal(handler: str, params: Mapping[str, Any] | None) -> str | None:
    """Return why this request writes, or None when it only reads.

    A returned string rather than a raise, so the reason can travel into the journal.
    """
    if handler in READ_ONLY_HANDLERS:
        return None
    if handler != "lom_batch":
        return f"handler {handler!r} is not read-only"
    ops = params.get("ops") if params is not None else None
    if not isinstance(ops, Sequence) or isinstance(ops, (str, bytes)):
        return "lom_batch without a readable list of ops"
    for index, op in enumerate(ops):
        if not isinstance(op, Mapping):
            return f"lom_batch op {index} is not an object"
        kind = op.get("op")
        if kind != "get":
            return f"lom_batch op {index} is {kind!r}, not 'get'"
    return None


@dataclass(frozen=True)
class Read:
    """One value read back from Live, with whether the read succeeded."""

    path: str
    ok: bool
    value: Any = None
    error: str = ""


class Reader:
    """Batched reads, in the shape ``lom_batch`` actually answers in.

    Results come back in request order (docs/protocol.md section 5.7), and this
    pairs them with the requested paths rather than trusting a lookup by path: two
    reads of the same path in one batch would collapse onto one key.
    """

    def __init__(self, client: LomClient) -> None:
        self._client = client
        #: How many round trips this reader has spent. One per batch, whatever its size.
        self.round_trips = 0

    def read(self, paths: Sequence[str]) -> list[Read]:
        """Read every path in one round trip."""
        if not paths:
            return []
        ops = [{"op": "get", "path": path} for path in paths]
        self.round_trips += 1
        reply = self._client.send("lom_batch", {"ops": ops})
        results = reply.get("results")
        if not isinstance(results, Sequence):
            return [Read(path, False, error="lom_batch returned no results") for path in paths]
        out: list[Read] = []
        for index, path in enumerate(paths):
            if index >= len(results):
                out.append(Read(path, False, error="no result for this op"))
                continue
            entry = results[index]
            if not isinstance(entry, Mapping):
                out.append(Read(path, False, error="result is not an object"))
                continue
            ok = entry.get("status") == "success"
            out.append(
                Read(
                    path=path,
                    ok=ok,
                    value=entry.get("value"),
                    error="" if ok else str(entry.get("error") or entry.get("code") or "failed"),
                )
            )
        return out


def aim_selection(client: LomClient, track: int) -> dict[str, Any]:
    """Point ``song.view.selected_track`` at one ordinary track.

    ``Browser.load_item`` takes no track argument, so the selection is the only way to
    choose where a device lands. The handle form, not an index. Measured 2026-09-17
    against Live 12.4.6 on an empty four-track set: the handle write moved the selection
    from "1-MIDI" to "3-Audio" and read back ``applied``. A plain integer is a different
    mechanism and the catalog row's doc expects it to fail with ``type_error``.
    """
    return client.send(
        "lom_set",
        {"path": "song.view.selected_track", "value": {"__path__": f"song.tracks[{track}]"}},
    )


# --------------------------------------------------------------------------- #
# What macOS will say about a running Live
# --------------------------------------------------------------------------- #

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one short command and capture its output."""
    # Fixed argument vectors, never a shell string.
    return subprocess.run(
        list(command), capture_output=True, text=True, timeout=30, check=False
    )


@dataclass(frozen=True)
class Sample:
    """One memory reading, with the command that produced it and what it cost.

    ``cost_ms`` is timed as the sample is taken rather than written in here, because a
    figure in this file would be a claim about a machine. The command travels with the
    sample so that a reader can re-run it.
    """

    kind: str
    command: tuple[str, ...]
    cost_ms: float
    ok: bool
    fields: Mapping[str, float]
    error: str = ""

    def as_record(self) -> dict[str, Any]:
        """The sample as it goes into the journal."""
        return {
            "kind": self.kind,
            "command": list(self.command),
            "cost_ms": round(self.cost_ms, 3),
            "ok": self.ok,
            "fields": dict(self.fields),
            "error": self.error,
        }


def parse_ps_memory(text: str) -> dict[str, float]:
    """Parse ``ps -o rss=,vsz=,pmem=`` output: three numbers on one line.

    RSS and VSZ are kibibytes, which is what ``ps`` prints without a unit.
    """
    parts = text.split()
    if len(parts) < 3:
        raise ValueError(f"expected three fields from ps, got {text.strip()!r}")
    return {
        "rss_kib": float(parts[0]),
        "vsz_kib": float(parts[1]),
        "percent_memory": float(parts[2]),
    }


def parse_ps_threads(text: str, pid: int) -> dict[str, float]:
    """Count threads in ``ps -M`` output.

    The first line is the header and the second is the process itself. Every line
    after that is one thread, indented, with the pid repeated in its first column.
    Counting indented lines rather than subtracting two keeps a changed header from
    turning into a silently wrong count.
    """
    threads = 0
    for line in text.splitlines():
        if not line or not line[0].isspace():
            continue
        fields = line.split()
        if fields and fields[0] == str(pid):
            threads += 1
    return {"threads": float(threads)}


_FOOTPRINT_TOTAL = re.compile(r"Footprint:\s+([\d.]+)\s+([KMG]B)")
_FOOTPRINT_PEAK = re.compile(r"phys_footprint_peak:\s+([\d.]+)\s+([KMG]B)")
_UNIT_MIB: dict[str, float] = {"KB": 1.0 / 1024.0, "MB": 1.0, "GB": 1024.0}


def parse_footprint(text: str) -> dict[str, float]:
    """Parse ``footprint`` output into mebibytes.

    ``footprint`` prints its own unit per figure, so the unit is read rather than
    assumed. It reports whole mebibytes, which is coarser than RSS from ``ps`` and
    is why both are sampled.
    """
    out: dict[str, float] = {}
    total = _FOOTPRINT_TOTAL.search(text)
    if total is not None:
        out["footprint_mib"] = float(total.group(1)) * _UNIT_MIB[total.group(2)]
    peak = _FOOTPRINT_PEAK.search(text)
    if peak is not None:
        out["footprint_peak_mib"] = float(peak.group(1)) * _UNIT_MIB[peak.group(2)]
    if not out:
        raise ValueError("no footprint figure in the output")
    return out


class MemorySampler:
    """Reads what macOS will say about one running process.

    Nothing here touches Live over the socket, so a sample costs Live nothing but
    the scheduling of another process. Every sample carries its own measured cost.
    """

    def __init__(
        self,
        pid: int,
        *,
        runner: CommandRunner | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.pid = pid
        # Resolved when a sample is taken, not bound here, so that a test can put
        # a recording runner in place of the real one without reaching inside.
        self._runner = runner
        self._clock = clock

    def _sample(
        self, kind: str, command: Sequence[str], parse: Callable[[str], dict[str, float]]
    ) -> Sample:
        runner = self._runner if self._runner is not None else run_command
        started = self._clock()
        try:
            done = runner(command)
        except (OSError, subprocess.SubprocessError) as exc:
            cost = (self._clock() - started) * 1000.0
            return Sample(kind, tuple(command), cost, False, {}, f"{type(exc).__name__}: {exc}")
        cost = (self._clock() - started) * 1000.0
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()[:200]
            why = f"exit {done.returncode}: {detail}"
            return Sample(kind, tuple(command), cost, False, {}, why)
        try:
            fields = parse(done.stdout)
        except ValueError as exc:
            return Sample(kind, tuple(command), cost, False, {}, str(exc))
        return Sample(kind, tuple(command), cost, True, fields)

    def ps(self) -> Sample:
        """Resident size, virtual size and percentage of physical memory."""
        return self._sample("ps", ("/bin/ps", "-o", "rss=,vsz=,pmem=", "-p", str(self.pid)),
                            parse_ps_memory)

    def threads(self) -> Sample:
        """Thread count, which a leak in a task queue shows up in before memory does."""
        return self._sample(
            "ps_threads",
            ("/bin/ps", "-M", "-p", str(self.pid)),
            lambda text: parse_ps_threads(text, self.pid),
        )

    def footprint(self) -> Sample:
        """Phys footprint and its peak, which is the figure macOS itself accounts by."""
        return self._sample("footprint", ("/usr/bin/footprint", "-p", str(self.pid)),
                            parse_footprint)


def find_live_pid(runner: CommandRunner | None = None) -> tuple[int | None, str]:
    """Find the one running Live process, or say why there is not exactly one."""
    call = runner if runner is not None else run_command
    try:
        done = call(("/usr/bin/pgrep", "-x", "Live"))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"pgrep failed: {type(exc).__name__}: {exc}"
    pids = [line for line in done.stdout.split() if line.isdigit()]
    if not pids:
        return None, "no process named 'Live' is running"
    if len(pids) > 1:
        return None, f"{len(pids)} processes named 'Live' are running: {', '.join(pids)}"
    return int(pids[0]), ""


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Check:
    """One guard, its verdict, and what it saw.

    ``overridable`` is false for everything that identifies the set. A heuristic
    about how full a set looks can be waved through by somebody who knows the set.
    "This is not the file you named" cannot.
    """

    name: str
    ok: bool
    detail: str
    observed: Any = None
    overridable: bool = False

    def as_record(self) -> dict[str, Any]:
        """The check as it goes into the journal."""
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "observed": self.observed,
            "overridable": self.overridable,
        }


@dataclass(frozen=True)
class Target:
    """Everything the operator had to name before this harness would do anything."""

    expect_set: str
    sentinel: str
    track: int
    item_path: str
    expect_item_name: str
    expect_device: str
    root: str = ""


def same_set_path(observed: str, expected: str) -> bool:
    """Compare two project paths the way a file system comparison has to.

    ``os.path.normpath`` and nothing more: no ``realpath``, because resolving a
    symlink would let a link named like the stress set stand in for the real one,
    and no case folding, because a path that differs in case is a path the operator
    did not type.
    """
    if not observed or not expected:
        return False
    return os.path.normpath(observed) == os.path.normpath(expected)


def check_expected_set(observed: Read, expected: str) -> Check:
    """The open set must be the one named on the command line."""
    if not observed.ok:
        return Check("set identity", False, f"song.file_path could not be read: {observed.error}")
    value = observed.value
    if not isinstance(value, str) or not value:
        return Check(
            "set identity",
            False,
            "this set has never been saved, so it has no path to identify it by. Save "
            "the stress set to its own file first.",
            observed=value,
        )
    if not same_set_path(value, expected):
        return Check(
            "set identity",
            False,
            f"the open set is {value!r}, not {expected!r}. Refusing.",
            observed=value,
        )
    return Check("set identity", True, f"the open set is {value!r}", observed=value)


def check_sentinel_shape(sentinel: str) -> Check:
    """The sentinel must be a name Live would never have given a track itself.

    A load onto a default-named MIDI track renames the track, and from the outside that
    rename is indistinguishable from the selection having moved elsewhere. Every later
    cycle would then report ``selected_track_same`` as null and measure nothing.
    """
    if not sentinel.strip():
        return Check("sentinel shape", False, "the sentinel name is empty")
    if _DEFAULT_TRACK_NAME.match(sentinel.strip()):
        return Check(
            "sentinel shape",
            False,
            f"{sentinel!r} looks like one of Live's own default track names. An "
            "instrument load renames a default-named track, which would make every "
            "cycle report an unknown track identity. Rename the track to something "
            "Live would not choose, such as 'LM-STRESS'.",
            observed=sentinel,
        )
    return Check("sentinel shape", True, f"{sentinel!r} is not a Live default name",
                 observed=sentinel)


def check_track_name(observed: Read, sentinel: str, track: int) -> Check:
    """The target track must already carry the sentinel name."""
    if not observed.ok:
        return Check("sentinel track", False, f"track {track} name unreadable: {observed.error}")
    if observed.value != sentinel:
        return Check(
            "sentinel track",
            False,
            f"song.tracks[{track}] is named {observed.value!r}, not {sentinel!r}. Rename "
            "it in Live before running, or point --track at the right index.",
            observed=observed.value,
        )
    return Check("sentinel track", True, f"song.tracks[{track}] is {sentinel!r}",
                 observed=observed.value)


def check_track_kind(has_midi_input: Read, is_foldable: Read, track: int) -> Check:
    """The target must be an ordinary MIDI track, not a group and not audio."""
    if not has_midi_input.ok or not is_foldable.ok:
        return Check("track kind", False, f"track {track} kind unreadable")
    if is_foldable.value is True:
        return Check("track kind", False, f"song.tracks[{track}] is a group track",
                     observed="group")
    if has_midi_input.value is not True:
        return Check(
            "track kind",
            False,
            f"song.tracks[{track}] takes no MIDI input, so an instrument cannot load onto it",
            observed="audio",
        )
    return Check("track kind", True, f"song.tracks[{track}] is an ordinary MIDI track",
                 observed="midi")


def check_chain_empty(observed: Read, track: int) -> Check:
    """The target chain must start empty, so a load of one device is unambiguous."""
    if not observed.ok:
        return Check("empty chain", False, f"track {track} devices unreadable: {observed.error}")
    value = observed.value
    if not isinstance(value, list):
        return Check("empty chain", False, "the device list did not read back as a list",
                     observed=value)
    if value:
        return Check(
            "empty chain",
            False,
            f"song.tracks[{track}] already holds {len(value)} device(s). This harness "
            "deletes device 0 every cycle and will not aim at a chain it did not build.",
            observed=len(value),
        )
    return Check("empty chain", True, f"song.tracks[{track}] holds no devices", observed=0)


def check_transport_idle(is_playing: Read, record_mode: Read, session_record: Read) -> Check:
    """Nothing may be rolling or recording while devices come and go."""
    unreadable = [r.path for r in (is_playing, record_mode, session_record) if not r.ok]
    if unreadable:
        return Check("transport idle", False, f"unreadable: {', '.join(unreadable)}")
    rolling = [
        r.path
        for r in (is_playing, record_mode, session_record)
        if r.value is True
    ]
    if rolling:
        return Check("transport idle", False, f"running: {', '.join(rolling)}", observed=rolling)
    return Check("transport idle", True, "transport stopped, nothing recording")


def check_selection(observed: Read, sentinel: str) -> Check:
    """The selection must be on the sentinel track."""
    if not observed.ok:
        return Check("selection", False, f"the selected track could not be read: {observed.error}")
    if observed.value != sentinel:
        return Check(
            "selection",
            False,
            f"the selected track is {observed.value!r}, not {sentinel!r}. Somebody "
            "clicked in Live, or the track was renamed. Stopping rather than aiming "
            "again: the next load would put an instrument on a track nobody named.",
            observed=observed.value,
        )
    return Check("selection", True, f"the selected track is {sentinel!r}", observed=observed.value)


def check_item_name(observed: Read, expected: str) -> Check:
    """The browser item_path must still point at the item it pointed at before.

    ``item_path`` is positional into a list the separate Ableton Index process
    maintains, and nothing pins it across a reindex. Re-proving it costs one op in a
    batch the cycle takes anyway, against loading whatever moved into that slot.
    """
    if not observed.ok:
        return Check("item path", False, f"the browser item could not be read: {observed.error}")
    if observed.value != expected:
        return Check(
            "item path",
            False,
            f"the browser item at that path is now {observed.value!r}, not {expected!r}. "
            "The index moved underneath this path.",
            observed=observed.value,
        )
    return Check("item path", True, f"the browser item is still {expected!r}",
                 observed=observed.value)


def check_population(counts: Mapping[str, int], limits: Mapping[str, int]) -> Check:
    """Report whether the set looks like one made for something other than this run.

    Thresholds are low on purpose, and this is the one guard that can be waved through:
    a false alarm costs a flag, and the set identity guard is what protects the project.
    """
    tripped = {name: value for name, value in counts.items() if value > limits.get(name, 0)}
    if tripped:
        listed = ", ".join(
            f"{name}={value} (limit {limits[name]})" for name, value in tripped.items()
        )
        return Check(
            "population",
            False,
            f"this set holds more than a stress set should: {listed}. Open a set made "
            "for this run, or pass --i-know-what-im-doing.",
            observed=dict(counts),
            overridable=True,
        )
    return Check("population", True, "this set is as empty as a stress set should be",
                 observed=dict(counts))


def count_population(
    names: Sequence[Read], devices: Sequence[Read], scene_names: Sequence[Read]
) -> dict[str, int]:
    """Turn the survey reads into the counts the population guard weighs.

    Scenes are counted by their names and not by how many there are. A new Live set
    opens with eight empty scenes, so a count of scenes would refuse the one kind of
    set this harness is meant for. A scene somebody named is the signal.
    """
    readable = [r for r in names if r.ok and isinstance(r.value, str)]
    renamed = sum(1 for r in readable if not _DEFAULT_TRACK_NAME.match(str(r.value).strip()))
    device_count = sum(
        len(r.value) for r in devices if r.ok and isinstance(r.value, list)
    )
    named_scenes = sum(
        1 for r in scene_names if r.ok and isinstance(r.value, str) and r.value.strip()
    )
    return {
        "tracks": len(readable),
        "renamed tracks": renamed,
        "devices": device_count,
        "named scenes": named_scenes,
    }


def preflight(reader: Reader, target: Target, *, limits: Mapping[str, int]) -> list[Check]:
    """Every guard, all of them reads, before the first write of the run.

    Two round trips: one that surveys the set and one that inspects the target. The
    survey has to come first because its track count decides how much of the second
    batch there is to ask for.
    """
    checks: list[Check] = [check_sentinel_shape(target.sentinel)]

    survey_paths = [
        "song.file_path",
        "song.is_playing",
        "song.record_mode",
        "song.session_record",
        "song.tracks",
        "song.scenes",
        "song.view.selected_track.name",
    ]
    survey = reader.read(survey_paths)
    file_path, is_playing, record_mode, session_record, tracks, scenes, selected = survey

    checks.append(check_expected_set(file_path, target.expect_set))
    checks.append(check_transport_idle(is_playing, record_mode, session_record))

    track_count = len(tracks.value) if tracks.ok and isinstance(tracks.value, list) else 0
    if track_count == 0:
        checks.append(
            Check("track list", False, f"song.tracks did not read as a list: {tracks.error}")
        )
        return checks
    if not 0 <= target.track < track_count:
        checks.append(
            Check(
                "track index",
                False,
                f"--track {target.track} is outside song.tracks, which holds {track_count}",
                observed=track_count,
            )
        )
        return checks

    scene_count = len(scenes.value) if scenes.ok and isinstance(scenes.value, list) else 0
    scene_count = min(scene_count, SCENE_NAME_CAP)
    detail_paths = [f"song.tracks[{i}].name" for i in range(track_count)]
    detail_paths += [f"song.tracks[{i}].devices" for i in range(track_count)]
    detail_paths += [f"song.scenes[{i}].name" for i in range(scene_count)]
    detail_paths += [
        f"song.tracks[{target.track}].has_midi_input",
        f"song.tracks[{target.track}].is_foldable",
        f"{target.item_path}.name",
    ]
    detail = reader.read(detail_paths)
    names = detail[:track_count]
    device_lists = detail[track_count : 2 * track_count]
    scene_names = detail[2 * track_count : 2 * track_count + scene_count]
    has_midi_input, is_foldable, item_name = detail[2 * track_count + scene_count :]

    checks.append(check_track_name(names[target.track], target.sentinel, target.track))
    checks.append(check_selection(selected, target.sentinel))
    checks.append(check_track_kind(has_midi_input, is_foldable, target.track))
    checks.append(check_chain_empty(device_lists[target.track], target.track))
    checks.append(check_item_name(item_name, target.expect_item_name))
    checks.append(
        check_population(count_population(names, device_lists, scene_names), limits)
    )
    return checks


def refusals(checks: Sequence[Check], *, override: bool) -> list[Check]:
    """The checks that stop the run, given whether the override was passed."""
    return [c for c in checks if not c.ok and not (c.overridable and override)]


# --------------------------------------------------------------------------- #
# Judging one cycle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Judgement:
    """Whether a load or a delete counted, and on what evidence."""

    accepted: bool
    code: str
    detail: str


def judge_load(result: Mapping[str, Any], *, expect_device: str) -> Judgement:
    """Accept a load only where the evidence says which track it landed on.

    ``selected_track_same`` is true or null, never false: the LOM hands this bridge no
    track id, so "the same track" means the selected track's name and the number of
    tracks read the same on both sides, and either of those moving gives null with a
    reason. A null means the next load would land somewhere nobody has identified, and
    on a MIDI track that load replaces the instrument that is there.
    """
    same = result.get("selected_track_same")
    if same is not True:
        reason = result.get("displaced_unknown_reason") or "unknown"
        return Judgement(
            False,
            "identity_unknown" if same is None else "identity_false",
            f"selected_track_same is {same!r} (reason {reason!r}). The track the device "
            "landed on is not known to be the target, so the run stops here.",
        )
    if result.get("ok") is not True:
        return Judgement(False, "load_failed", f"load_device reported ok={result.get('ok')!r}")
    if result.get("chain_before") != []:
        return Judgement(
            False,
            "chain_not_empty",
            f"the chain was {result.get('chain_before')!r} before the load, not empty",
        )
    if result.get("displaced") != []:
        return Judgement(
            False,
            "displaced",
            f"the load displaced {result.get('displaced')!r}",
        )
    if result.get("loaded_device_index") != 0:
        return Judgement(
            False,
            "wrong_index",
            f"the new device landed at index {result.get('loaded_device_index')!r}, not 0",
        )
    if result.get("chain_after") != [expect_device]:
        return Judgement(
            False,
            "wrong_device",
            f"the chain is {result.get('chain_after')!r} after the load, not {[expect_device]!r}",
        )
    return Judgement(True, "loaded", f"{expect_device!r} is device 0 on the target track")


def judge_delete(result: Mapping[str, Any]) -> Judgement:
    """Accept a delete only where the device count actually went from one to zero."""
    if result.get("ok") is not True:
        return Judgement(False, "delete_failed", f"delete_device reported ok={result.get('ok')!r}")
    if result.get("verified") is not True:
        return Judgement(
            False,
            "delete_unverified",
            f"verified is {result.get('verified')!r}: "
            f"{result.get('verified_note') or 'the count was not confirmed to have shrunk'}",
        )
    before = result.get("device_count_before")
    after = result.get("device_count_after")
    if before != 1 or after != 0:
        return Judgement(
            False,
            "wrong_count",
            f"the device count went {before!r} -> {after!r}, not 1 -> 0",
        )
    if result.get("devices_after") not in ([], None):
        return Judgement(
            False,
            "chain_not_empty_after",
            f"the chain is {result.get('devices_after')!r} after the delete, not empty",
        )
    return Judgement(True, "deleted", "the chain went from one device to none")


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #


class Journal:
    """One JSON object per line, on disk before the next line is built.

    ``flush`` pushes the line out of Python's buffer and ``fsync`` out of the operating
    system's. Without both, a crash mid-run can leave a file that is empty or that ends
    mid-line, losing every cycle already measured.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None
        #: How many records reached disk.
        self.records = 0

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open(self) -> None:
        """Create the journal's directory if needed and open it for appending."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        """Close the file, if it is open."""
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()

    def write(self, record: Mapping[str, Any]) -> None:
        """Append one record and do not return until it is on disk."""
        handle = self._handle
        if handle is None:
            raise RuntimeError("the journal is not open")
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self.records += 1


@dataclass(frozen=True)
class PriorRun:
    """What the gate needs to know about the stage before this one."""

    stage: str
    cycles_completed: int
    stop_reason: str
    seconds_per_cycle: float | None


def read_prior(path: Path) -> tuple[PriorRun | None, str]:
    """Read a stage's journal back, or say why it cannot be read.

    Only the header and the summary are needed, but a truncated journal is read the
    same way a complete one is: a run that crashed has no summary line, and the gate
    below turns that absence into a refusal rather than into a guess.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{path} could not be read: {exc}"
    stage = ""
    summary: Mapping[str, Any] | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping):
            continue
        if record.get("record") == "header":
            stage = str(record.get("stage") or "")
        elif record.get("record") == "summary":
            summary = record
    if not stage:
        return None, f"{path} carries no header record, so its stage is unknown"
    if summary is None:
        return None, f"{path} carries no summary record: that run did not finish"
    per_cycle = summary.get("seconds_per_cycle_median")
    return (
        PriorRun(
            stage=stage,
            cycles_completed=int(summary.get("cycles_completed") or 0),
            stop_reason=str(summary.get("stop_reason") or ""),
            seconds_per_cycle=float(per_cycle) if isinstance(per_cycle, (int, float)) else None,
        ),
        "",
    )


def project_hours(seconds_per_cycle: float, cycles: int) -> float:
    """How long a stage of this many cycles takes at this measured pace."""
    return seconds_per_cycle * cycles / 3600.0


def gate_stage(stage: StageSpec, prior: PriorRun | None, *, max_hours: float) -> Check:
    """Decide whether this stage may start, on the evidence of the one before it.

    A stage that would run longer than ``--max-hours`` at the pace measured one rung
    down is refused, with that projection in the message, so the length of a run is
    decided against a measurement rather than against an estimate.
    """
    if stage.prior is None:
        return Check("stage gate", True, f"stage {stage.name!r} needs no prior stage")
    if prior is None:
        return Check(
            "stage gate",
            False,
            f"stage {stage.name!r} needs the journal of stage {stage.prior!r}, passed "
            "with --prior. Run the smaller stage first.",
        )
    if prior.stage != stage.prior:
        return Check(
            "stage gate",
            False,
            f"the journal passed to --prior is from stage {prior.stage!r}, and stage "
            f"{stage.name!r} follows stage {stage.prior!r}.",
            observed=prior.stage,
        )
    if prior.stop_reason != "completed":
        return Check(
            "stage gate",
            False,
            f"stage {prior.stage!r} stopped with {prior.stop_reason!r} rather than "
            "completing. Fix what stopped it before running a longer stage.",
            observed=prior.stop_reason,
        )
    expected = STAGES[stage.prior].cycles
    if prior.cycles_completed < expected:
        return Check(
            "stage gate",
            False,
            f"stage {prior.stage!r} completed {prior.cycles_completed} of {expected} cycles",
            observed=prior.cycles_completed,
        )
    if prior.seconds_per_cycle is None:
        return Check(
            "stage gate",
            False,
            f"stage {prior.stage!r} measured no per-cycle time, so this stage's length "
            "is still an estimate.",
        )
    hours = project_hours(prior.seconds_per_cycle, stage.cycles)
    if hours > max_hours:
        return Check(
            "stage gate",
            False,
            f"at the {prior.seconds_per_cycle:.2f} s/cycle measured in stage "
            f"{prior.stage!r}, stage {stage.name!r} would take {hours:.2f} h, over the "
            f"{max_hours:.2f} h limit. Raise --max-hours deliberately or shorten the stage.",
            observed=round(hours, 3),
        )
    return Check(
        "stage gate",
        True,
        f"stage {prior.stage!r} measured {prior.seconds_per_cycle:.2f} s/cycle, so stage "
        f"{stage.name!r} projects to {hours:.2f} h",
        observed=round(hours, 3),
    )


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

LoadFn = Callable[..., dict[str, Any]]
DeleteFn = Callable[..., dict[str, Any]]


def refuse_load(**_kwargs: Any) -> dict[str, Any]:
    """Stand in for load_device in the dry stage, and raise if anything calls it."""
    raise WriteAttempted("the dry stage tried to call load_device")


def refuse_delete(**_kwargs: Any) -> dict[str, Any]:
    """Stand in for delete_device in the dry stage, and raise if anything calls it."""
    raise WriteAttempted("the dry stage tried to call delete_device")


@dataclass(frozen=True)
class CycleOutcome:
    """One cycle's verdict and the record that went into the journal."""

    index: int
    accepted: bool
    code: str
    detail: str
    seconds: float
    record: dict[str, Any]


@dataclass
class RunSummary:
    """What a stage measured, and where it stopped."""

    stage: str
    cycles_planned: int
    cycles_completed: int = 0
    stop_reason: str = "completed"
    stop_detail: str = ""
    seconds: list[float] = field(default_factory=list)

    def as_record(self) -> dict[str, Any]:
        """The summary as it goes into the journal, and as the next stage reads it."""
        ordered = sorted(self.seconds)
        return {
            "record": "summary",
            "stage": self.stage,
            "cycles_planned": self.cycles_planned,
            "cycles_completed": self.cycles_completed,
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "seconds_per_cycle_median": round(median(ordered), 4) if ordered else None,
            "seconds_per_cycle_min": round(ordered[0], 4) if ordered else None,
            "seconds_per_cycle_max": round(ordered[-1], 4) if ordered else None,
            "projected_hours_1000": (
                round(project_hours(median(ordered), 1000), 3) if ordered else None
            ),
            "attribution_note": (
                "A memory delta over this run cannot be attributed. Nothing in the Live "
                "Object Model clears or reports the undo stack, and a thousand cycles push "
                "roughly two thousand undo steps. Read the curve's shape, a bounded plateau "
                "against unbounded growth, and subtract an idle control arm of the same "
                "wall-clock length. A before/after pair says nothing."
            ),
        }


class StressRun:
    """Drives the cycles and writes the journal.

    Every collaborator is injected, so the guards and the cycle shape can be exercised
    against fakes with no Live present.
    """

    def __init__(
        self,
        *,
        reader: Reader,
        target: Target,
        journal: Journal,
        load: LoadFn,
        delete: DeleteFn,
        sampler: MemorySampler | None,
        dry: bool,
        pace: float = DEFAULT_PACE,
        footprint_every: int = DEFAULT_FOOTPRINT_EVERY,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reader = reader
        self.target = target
        self.journal = journal
        self.load = load
        self.delete = delete
        self.sampler = sampler
        self.dry = dry
        self.pace = pace
        self.footprint_every = footprint_every
        self.sleep = sleep
        self.clock = clock

    def guard(self) -> list[Check]:
        """The per-cycle guard, one round trip, all reads."""
        paths = [
            f"{self.target.item_path}.name",
            "song.view.selected_track.name",
            f"song.tracks[{self.target.track}].devices",
            "song.is_playing",
        ]
        item_name, selected, devices, is_playing = self.reader.read(paths)
        return [
            check_item_name(item_name, self.target.expect_item_name),
            check_selection(selected, self.target.sentinel),
            check_chain_empty(devices, self.target.track),
            Check(
                "transport idle",
                is_playing.ok and is_playing.value is not True,
                "transport stopped" if is_playing.ok else f"unreadable: {is_playing.error}",
                observed=is_playing.value,
            ),
        ]

    def samples(self, index: int) -> list[dict[str, Any]]:
        """The memory samples this cycle takes."""
        if self.sampler is None:
            return []
        taken = [self.sampler.ps().as_record(), self.sampler.threads().as_record()]
        if self.footprint_every > 0 and index % self.footprint_every == 0:
            taken.append(self.sampler.footprint().as_record())
        return taken

    def _pause(self) -> None:
        """Wait between writes. ``--pace 0`` really is free rather than a no-op sleep."""
        if self.pace > 0:
            self.sleep(self.pace)

    def cycle(self, index: int) -> CycleOutcome:
        """One guard, one load, one delete, one journal line."""
        started = self.clock()
        record: dict[str, Any] = {"record": "cycle", "cycle": index, "dry": self.dry}

        guards = self.guard()
        record["guards"] = [c.as_record() for c in guards]
        failed = [c for c in guards if not c.ok]
        if failed:
            record["accepted"] = False
            record["code"] = "guard_failed"
            record["detail"] = "; ".join(f"{c.name}: {c.detail}" for c in failed)
            elapsed = self.clock() - started
            record["seconds"] = round(elapsed, 4)
            record["memory"] = self.samples(index)
            self.journal.write(record)
            return CycleOutcome(index, False, "guard_failed", record["detail"], elapsed, record)

        if self.dry:
            record["would_load"] = {
                "item_path": self.target.item_path,
                "root": self.target.root,
                "confirm": True,
            }
            record["would_delete"] = {
                "track": self.target.track,
                "device": 0,
                "confirm": True,
            }
            record["accepted"] = True
            record["code"] = "dry"
            record["detail"] = "the guards passed and no write was issued"
            elapsed = self.clock() - started
            record["seconds"] = round(elapsed, 4)
            record["memory"] = self.samples(index)
            self.journal.write(record)
            return CycleOutcome(index, True, "dry", record["detail"], elapsed, record)

        load_started = self.clock()
        loaded = self.load(
            item_path=self.target.item_path,
            root=self.target.root,
            confirm=True,
        )
        record["load_seconds"] = round(self.clock() - load_started, 4)
        verdict = judge_load(loaded, expect_device=self.target.expect_device)
        record["load"] = _load_evidence(loaded)
        record["load_verdict"] = {"accepted": verdict.accepted, "code": verdict.code,
                                  "detail": verdict.detail}
        if not verdict.accepted:
            return self._stop(index, started, record, verdict)
        self._pause()

        delete_started = self.clock()
        removed = self.delete(track=self.target.track, device=0, confirm=True)
        record["delete_seconds"] = round(self.clock() - delete_started, 4)
        delete_verdict = judge_delete(removed)
        record["delete"] = _delete_evidence(removed)
        record["delete_verdict"] = {
            "accepted": delete_verdict.accepted,
            "code": delete_verdict.code,
            "detail": delete_verdict.detail,
        }
        if not delete_verdict.accepted:
            return self._stop(index, started, record, delete_verdict)
        self._pause()

        record["accepted"] = True
        record["code"] = "cycle"
        record["detail"] = "loaded and deleted, both on read-back evidence"
        elapsed = self.clock() - started
        record["seconds"] = round(elapsed, 4)
        record["memory"] = self.samples(index)
        self.journal.write(record)
        return CycleOutcome(index, True, "cycle", record["detail"], elapsed, record)

    def _stop(
        self, index: int, started: float, record: dict[str, Any], verdict: Judgement
    ) -> CycleOutcome:
        """Journal a rejected cycle and hand back the reason the run stops."""
        record["accepted"] = False
        record["code"] = verdict.code
        record["detail"] = verdict.detail
        elapsed = self.clock() - started
        record["seconds"] = round(elapsed, 4)
        record["memory"] = self.samples(index)
        self.journal.write(record)
        return CycleOutcome(index, False, verdict.code, verdict.detail, elapsed, record)

    def run(self, cycles: int, *, out: TextIO = sys.stdout) -> RunSummary:
        """Run cycles until one is not accepted, and summarise either way."""
        summary = RunSummary(stage="", cycles_planned=cycles)
        for index in range(1, cycles + 1):
            outcome = self.cycle(index)
            if not outcome.accepted:
                summary.stop_reason = outcome.code
                summary.stop_detail = outcome.detail
                print(f"[{index}/{cycles}] stopped: {outcome.code}: {outcome.detail}",
                      file=out, flush=True)
                break
            summary.cycles_completed += 1
            summary.seconds.append(outcome.seconds)
            print(f"[{index}/{cycles}] {outcome.code} in {outcome.seconds:.2f} s",
                  file=out, flush=True)
        return summary


def _load_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a load_device result a later reader will want, and no more."""
    keys = (
        "ok",
        "item_path",
        "selected_track_same",
        "selected_track_before",
        "selected_track_after",
        "track_count_before",
        "track_count_after",
        "chain_before",
        "chain_after",
        "displaced",
        "displaced_unknown_reason",
        "loaded_device_index",
        "configure_needed",
        "configure_needed_unknown_reason",
        "error",
    )
    return {key: result[key] for key in keys if key in result}


def _delete_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a delete_device result a later reader will want, and no more."""
    keys = (
        "ok",
        "verified",
        "verified_note",
        "deleted_index",
        "device_count_before",
        "device_count_after",
        "devices_after",
        "track_name",
        "error",
    )
    return {key: result[key] for key in keys if key in result}


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """The command line. Everything that names the target is required."""
    parser = argparse.ArgumentParser(
        prog="stress_load_delete.py",
        description=(
            "Load one device and delete it again, many times, against a set "
            "made for this and nothing else. Stage 'dry' writes nothing."
        ),
    )
    parser.add_argument("--stage", choices=sorted(STAGES), default="dry",
                        help="dry (no writes, the default), a=25, b=100, c=1000 cycles")
    parser.add_argument("--cycles", type=int, default=None,
                        help="override the stage's cycle count, downwards or upwards")
    parser.add_argument("--go", action="store_true",
                        help="required for any stage that writes to Live")
    parser.add_argument("--expect-set", required=True,
                        help="the .als path song.file_path must equal, exactly")
    parser.add_argument("--sentinel", required=True,
                        help="the target track's name, which must not look like a Live default")
    parser.add_argument("--track", type=int, required=True,
                        help="index of the target track in song.tracks")
    parser.add_argument("--item-path", required=True,
                        help="browser item_path of the device to load, from a load_device search")
    parser.add_argument("--expect-item-name", required=True,
                        help="the name that item_path must still resolve to, re-proved each cycle")
    parser.add_argument("--expect-device", required=True,
                        help="the device name that must appear in the chain after a load")
    parser.add_argument("--root", default="",
                        help="browser category to resolve item_path in, e.g. user_library")
    parser.add_argument("--journal", required=True, type=Path,
                        help="JSONL journal, appended to, flushed and fsynced per cycle")
    parser.add_argument("--prior", type=Path, default=None,
                        help="journal of the preceding stage, which gates this one")
    parser.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS,
                        help="longest projected run the gate approves "
                             f"(default {DEFAULT_MAX_HOURS})")
    parser.add_argument("--pace", type=float, default=DEFAULT_PACE,
                        help=f"pause after each write (default {DEFAULT_PACE})")
    parser.add_argument("--footprint-every", type=int, default=DEFAULT_FOOTPRINT_EVERY,
                        help="cycles between footprint samples "
                             f"(default {DEFAULT_FOOTPRINT_EVERY})")
    parser.add_argument("--live-pid", type=int, default=None,
                        help="pid of the Live process to sample, when pgrep finds more than one")
    parser.add_argument("--i-know-what-im-doing", action="store_true",
                        help="wave through the population heuristic, nothing else")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def print_checks(checks: Sequence[Check], *, out: TextIO) -> None:
    """Print one line per guard, in the order they ran."""
    for check in checks:
        mark = "ok" if check.ok else "!!"
        print(f"  {mark}  {check.name:<16} {check.detail}", file=out)


def main(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout) -> int:
    """Run one stage and return the process exit code."""
    args = build_parser().parse_args(argv)
    stage = STAGES[args.stage]
    cycles = stage.cycles if args.cycles is None else args.cycles
    print(_BANNER, file=out)

    if stage.writes and not args.go:
        print(f"Refusing: stage {stage.name!r} writes to Live and --go was not passed.", file=out)
        return CODE_REFUSED

    prior: PriorRun | None = None
    if args.prior is not None:
        prior, why = read_prior(args.prior)
        if prior is None:
            print(f"Refusing: {why}", file=out)
            return CODE_REFUSED
    gate = gate_stage(stage, prior, max_hours=args.max_hours)
    print_checks([gate], out=out)
    if not gate.ok:
        return CODE_REFUSED

    target = Target(
        expect_set=args.expect_set,
        sentinel=args.sentinel,
        track=args.track,
        item_path=args.item_path,
        expect_item_name=args.expect_item_name,
        expect_device=args.expect_device,
        root=args.root,
    )

    pid = args.live_pid
    sampler: MemorySampler | None = None
    if pid is None:
        pid, why = find_live_pid()
        if pid is None:
            print(f"No memory samples will be taken: {why}", file=out)
    if pid is not None:
        sampler = MemorySampler(pid)

    client = LiveClient(args.host, args.port)
    load: LoadFn = refuse_load
    delete: DeleteFn = refuse_delete
    transport: LomClient = ReadOnlyGate(client) if not stage.writes else client
    if stage.writes:
        # One socket, not two. The protocol is strictly serial per connection and a
        # second connection from the same process is unmeasured, so the server's tool
        # functions are pointed at the client this harness already holds.
        from live_maestro import server

        server._client = client
        load = server.load_device
        delete = server.delete_device

    reader = Reader(transport)
    try:
        checks = preflight(reader, target, limits=POPULATION_LIMITS)
    except LiveError as exc:
        print(f"Refusing: Live did not answer the preflight reads: {exc}", file=out)
        return CODE_REFUSED
    print("Preflight:", file=out)
    print_checks(checks, out=out)
    stoppers = refusals(checks, override=args.i_know_what_im_doing)
    if stoppers:
        print(f"\nRefusing to run: {len(stoppers)} guard(s) failed.", file=out)
        return CODE_REFUSED

    started_at = datetime.now(UTC).isoformat()
    with Journal(args.journal) as journal:
        journal.write(
            {
                "record": "header",
                "stage": stage.name,
                "cycles_planned": cycles,
                "dry": not stage.writes,
                "started_at": started_at,
                "expect_set": target.expect_set,
                "sentinel": target.sentinel,
                "track": target.track,
                "item_path": target.item_path,
                "expect_item_name": target.expect_item_name,
                "expect_device": target.expect_device,
                "root": target.root,
                "pace": args.pace,
                "footprint_every": args.footprint_every,
                "live_pid": pid,
                "host": args.host,
                "port": args.port,
            }
        )
        journal.write({"record": "preflight", "checks": [c.as_record() for c in checks],
                       "overridden": args.i_know_what_im_doing})

        if stage.writes:
            aimed = aim_selection(client, target.track)
            journal.write({"record": "aim", "result": dict(aimed)})
            if aimed.get("read_back") != "applied":
                print(
                    "Refusing: pointing song.view.selected_track at "
                    f"song.tracks[{target.track}] read back "
                    f"{aimed.get('read_back')!r} rather than 'applied'.",
                    file=out,
                )
                journal.write({"record": "summary", "stage": stage.name,
                               "cycles_planned": cycles, "cycles_completed": 0,
                               "stop_reason": "aim_failed", "stop_detail": str(aimed)})
                return CODE_REFUSED
        else:
            journal.write(
                {
                    "record": "aim",
                    "would": {
                        "handler": "lom_set",
                        "path": "song.view.selected_track",
                        "value": {"__path__": f"song.tracks[{target.track}]"},
                    },
                }
            )

        run = StressRun(
            reader=reader,
            target=target,
            journal=journal,
            load=load,
            delete=delete,
            sampler=sampler,
            dry=not stage.writes,
            pace=args.pace,
            footprint_every=args.footprint_every,
        )
        try:
            summary = run.run(cycles, out=out)
        except WriteAttempted as exc:
            journal.write({"record": "summary", "stage": stage.name, "cycles_planned": cycles,
                           "cycles_completed": 0, "stop_reason": "write_in_dry_mode",
                           "stop_detail": str(exc)})
            print(f"\nA write escaped the dry stage: {exc}", file=out)
            return CODE_WRITE_IN_DRY_MODE
        except LiveError as exc:
            journal.write({"record": "summary", "stage": stage.name, "cycles_planned": cycles,
                           "cycles_completed": 0, "stop_reason": "live_error",
                           "stop_detail": str(exc)})
            print(f"\nLive stopped answering: {exc}", file=out)
            return CODE_STOPPED
        summary.stage = stage.name
        record = summary.as_record()
        journal.write(record)

    print(
        f"\nStage {stage.name!r}: {summary.cycles_completed}/{cycles} cycles, "
        f"stopped with {summary.stop_reason!r}.",
        file=out,
    )
    if record["seconds_per_cycle_median"] is not None:
        print(
            f"Measured {record['seconds_per_cycle_median']} s/cycle (median), which puts "
            f"1000 cycles at {record['projected_hours_1000']} h.",
            file=out,
        )
    print(f"Journal: {args.journal}", file=out)
    if not isinstance(transport, ReadOnlyGate):
        return 0 if summary.stop_reason == "completed" else CODE_STOPPED
    print(f"Handlers the dry stage sent: {sorted(set(transport.handlers))}", file=out)
    return 0 if summary.stop_reason == "completed" else CODE_STOPPED


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
