"""Tests for the endurance harness in ``scripts/stress_load_delete.py``.

The guards stand between a thousand-cycle destructive loop and somebody's project and
fail silently until it is expensive; everything else in that script is arithmetic.

Acceptance is judged on results from the real ``server.load_device`` and
``server.delete_device``, driven through the real executor and catalog against
:class:`FakeLive`, an in-memory Live answering the transport protocol by path. A
hand-written load result would only prove the harness agrees with itself, and the case
that matters is not hand-made: a load answering ``ok: true`` while
``selected_track_same`` is null, which the real tool produces on a renamed track.

"The dry stage writes nothing" is asserted at the transport: the fake records every
operation it is handed, and the test reads that record rather than the harness's own
idea of what it did. Nothing here needs Ableton Live.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import stress_load_delete as harness

from live_maestro import server

ITEM_PATH = "app.browser.user_library.children[5].children[0].children[32].children[0]"
ITEM_NAME = "LiveMaestro Probe"
#: A home directory for the fixtures, written with a placeholder rather than a real
#: account name. The harness only ever compares this path against itself, so any one
#: path used consistently is the whole requirement.
HOME = "/Users/<producer>"
SET_PATH = f"{HOME}/Music/Ableton/LM-Stress Project/LM-Stress.als"
SENTINEL = "LM-STRESS"


# --------------------------------------------------------------------------- #
# A Live that is not there
# --------------------------------------------------------------------------- #

_TRACK_INDEX = re.compile(r"^song\.tracks\[(\d+)\]")
_DEVICE_INDEX = re.compile(r"\.devices\[(\d+)\]")
_PARAM_INDEX = re.compile(r"\.parameters\[(\d+)\]")


def device(name: str, parameters: int = 8) -> dict[str, Any]:
    """One device in a chain, as the fake holds it."""
    return {"name": name, "class_name": name, "parameters": parameters}


def track(
    name: str, *, devices: list[dict[str, Any]] | None = None, midi: bool = True,
    group: bool = False,
) -> dict[str, Any]:
    """One track in the fake set."""
    return {"name": name, "devices": list(devices or []), "midi": midi, "group": group}


class FakeLive:
    """An in-memory Live answering both transport protocols this harness uses.

    The executor speaks ``get``/``set``/``call``/``batch`` to it and the harness's own
    reader speaks ``send``. Both land on the same state, so a load performed through
    the real ``server.load_device`` is visible to the next guard read, which is what
    makes a whole cycle testable end to end with no DAW anywhere.
    """

    def __init__(
        self,
        tracks: list[dict[str, Any]],
        *,
        file_path: str = SET_PATH,
        selected: int = 0,
        browser_item: str = ITEM_NAME,
        playing: bool = False,
        recording: bool = False,
        scene_names: list[str] | None = None,
        rename_on_load: str | None = None,
    ) -> None:
        self.tracks = [dict(t) for t in tracks]
        self.file_path = file_path
        self.selected = selected
        self.browser_item = browser_item
        self.playing = playing
        self.recording = recording
        # A new Live set opens with eight scenes and none of them named, which is the
        # case the population guard has to let through.
        self.scene_names = list(scene_names if scene_names is not None else [""] * 8)
        self.rename_on_load = rename_on_load
        #: Every operation the fake was handed, as ``(kind, detail)``. The dry-stage
        #: test reads this rather than trusting the harness's own account of itself.
        self.operations: list[tuple[str, str]] = []

    # ---------------------------------------------------------------- reading
    def _track_for(self, path: str) -> dict[str, Any] | None:
        if path.startswith("song.view.selected_track"):
            return self.tracks[self.selected]
        found = _TRACK_INDEX.match(path)
        if found is None:
            return None
        index = int(found.group(1))
        return self.tracks[index] if index < len(self.tracks) else None

    def _read(self, path: str) -> dict[str, Any]:
        if path == "song.file_path":
            return _ok(self.file_path)
        if path == "song.is_playing":
            return _ok(self.playing)
        if path in ("song.record_mode", "song.session_record"):
            return _ok(self.recording)
        if path == "song.tracks":
            return _ok(
                [
                    {"__lom__": "Track", "path": f"song.tracks[{i}]", "name": t["name"]}
                    for i, t in enumerate(self.tracks)
                ]
            )
        if path == "song.scenes":
            return _ok([{"__lom__": "Scene"} for _ in self.scene_names])
        scene = re.match(r"^song\.scenes\[(\d+)\]\.name$", path)
        if scene is not None:
            index = int(scene.group(1))
            if index >= len(self.scene_names):
                return _absent("no such scene")
            return _ok(self.scene_names[index])
        if path.startswith("app.browser"):
            return _ok(self.browser_item if path.endswith(".name") else {"__lom__": "BrowserItem"})
        if path == "song.view.selected_track":
            holder = self.tracks[self.selected]
            return _ok({"__lom__": "Track", "path": path, "name": holder["name"]})

        holder = self._track_for(path)
        if holder is None:
            return _absent("no such track")
        if ".parameters[" in path and path.endswith(".name"):
            found = _DEVICE_INDEX.search(path)
            slot = _PARAM_INDEX.search(path)
            if found is None or slot is None:
                return _absent("unparseable parameter path")
            index, which = int(found.group(1)), int(slot.group(1))
            if index >= len(holder["devices"]) or which >= holder["devices"][index]["parameters"]:
                return _absent("no such parameter")
            return _ok(f"p{which}")
        if ".devices[" in path:
            found = _DEVICE_INDEX.search(path)
            if found is None:
                return _absent("unparseable device path")
            index = int(found.group(1))
            if index >= len(holder["devices"]):
                return _absent("no such device")
            entry = holder["devices"][index]
            if path.endswith(".name"):
                return _ok(entry["name"])
            if path.endswith(".class_name"):
                return _ok(entry["class_name"])
            if path.endswith(".parameters"):
                return _ok([{"__lom__": "DeviceParameter"}] * entry["parameters"])
            return _absent(f"unhandled device path {path}")
        if path.endswith(".devices"):
            return _ok(
                [
                    {"__lom__": "Device", "path": f"{path}[{i}]", "name": d["name"]}
                    for i, d in enumerate(holder["devices"])
                ]
            )
        if path.endswith(".name"):
            return _ok(holder["name"])
        if path.endswith(".has_midi_input"):
            return _ok(bool(holder["midi"]))
        if path.endswith(".is_foldable"):
            return _ok(bool(holder["group"]))
        return _absent(f"unhandled path {path}")

    # ------------------------------------------------------- the two protocols
    def get(self, path: str) -> dict[str, Any]:
        """One read, the way the executor asks for it."""
        self.operations.append(("get", path))
        reply = self._read(path)
        if reply.get("status") != "success":
            raise AssertionError(f"the fake was asked for {path!r}: {reply}")
        return {"path": path, **reply}

    def set(self, path: str, value: object) -> dict[str, Any]:
        """One write."""
        self.operations.append(("set", f"{path}={value!r}"))
        if path == "song.view.selected_track" and isinstance(value, dict):
            handle = _TRACK_INDEX.match(str(value.get("__path__")))
            assert handle is not None, f"the selection was aimed with {value!r}"
            before = self.tracks[self.selected]["name"]
            self.selected = int(handle.group(1))
            return {
                "path": path,
                "before": before,
                "after": self.tracks[self.selected]["name"],
                "read_back": "applied",
                "changed": True,
            }
        raise AssertionError(f"the fake was asked to write {path!r}")

    def call(self, path: str, method: str, args: list[object]) -> dict[str, Any]:
        """One allowlisted method call."""
        self.operations.append(("call", f"{path}.{method}({args!r})"))
        if method == "load_item":
            holder = self.tracks[self.selected]
            holder["devices"] = [device(self.browser_item)]
            if self.rename_on_load is not None:
                holder["name"] = self.rename_on_load
            return {"ok": True, "result": None}
        if method == "delete_device":
            holder = self._track_for(path)
            assert holder is not None, f"delete aimed at {path!r}"
            del holder["devices"][int(str(args[0]))]
            return {"ok": True, "result": None}
        raise AssertionError(f"the fake was asked to call {method!r}")

    def batch(self, ops: list[dict[str, Any]], *, atomic: bool = False) -> dict[str, Any]:
        """Several operations in one round trip, in the wire shape of docs/protocol.md."""
        results: list[dict[str, Any]] = []
        for op in ops:
            if op["op"] == "get":
                results.append({"path": op["path"], **self._read(op["path"])})
                self.operations.append(("get", op["path"]))
            elif op["op"] == "call":
                self.call(op["path"], op["method"], list(op.get("args") or []))
                results.append({"path": op["path"], "status": "success", "value": None})
            else:
                written = self.set(op["path"], op.get("value"))
                results.append({"status": "success", **written})
        return {"results": results, "ok_count": len(results), "error_count": 0}

    def send(
        self, handler: str, params: Any = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """The raw handler interface, which is what the harness's reader speaks."""
        payload = dict(params or {})
        if handler == "lom_get":
            self.operations.append(("get", payload["path"]))
            return {"path": payload["path"], **self._read(payload["path"])}
        if handler == "lom_set":
            return self.set(payload["path"], payload.get("value"))
        if handler == "lom_batch":
            return self.batch(list(payload["ops"]), atomic=bool(payload.get("atomic")))
        raise AssertionError(f"the fake was sent handler {handler!r}")

    # ------------------------------------------------------------------ probes
    def writes(self) -> list[tuple[str, str]]:
        """Every operation that changed something, in order."""
        return [entry for entry in self.operations if entry[0] in ("set", "call")]


def _ok(value: object) -> dict[str, Any]:
    """A successful read, in the shape lom_batch answers in."""
    reply: dict[str, Any] = {"status": "success", "value": value}
    if isinstance(value, list):
        reply["count"] = len(value)
    return reply


def _absent(why: str) -> dict[str, Any]:
    """The reply Live gives for an index past the end of a collection."""
    return {"status": "error", "code": "index_out_of_range", "error": why}


def empty_set(**kwargs: Any) -> FakeLive:
    """The set the stress run is meant to be pointed at: one named, empty MIDI track."""
    return FakeLive([track(SENTINEL)], **kwargs)


def target(**overrides: Any) -> harness.Target:
    """The target the operator named, with room to break one field at a time."""
    fields: dict[str, Any] = {
        "expect_set": SET_PATH,
        "sentinel": SENTINEL,
        "track": 0,
        "item_path": ITEM_PATH,
        "expect_item_name": ITEM_NAME,
        "expect_device": ITEM_NAME,
        "root": "user_library",
    }
    fields.update(overrides)
    return harness.Target(**fields)


def failed(checks: list[harness.Check], *, override: bool = False) -> dict[str, str]:
    """The names and reasons of the checks that would stop the run."""
    return {c.name: c.detail for c in harness.refusals(checks, override=override)}


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #


def test_the_command_line_names_the_target_or_there_is_no_run() -> None:
    """No flag, no run. Every field that identifies the set is required.

    A harness with a default track index, or a default set, runs against whatever
    happens to be open the first time somebody types the command without reading it.
    """
    for missing in ("--expect-set", "--sentinel", "--track", "--item-path", "--journal"):
        argv = [
            "--expect-set", SET_PATH,
            "--sentinel", SENTINEL,
            "--track", "0",
            "--item-path", ITEM_PATH,
            "--expect-item-name", ITEM_NAME,
            "--expect-device", ITEM_NAME,
            "--journal", "/tmp/j.jsonl",
        ]
        where = argv.index(missing)
        del argv[where : where + 2]
        with pytest.raises(SystemExit) as exit_info:
            harness.build_parser().parse_args(argv)
        assert exit_info.value.code == 2, f"{missing} was not required"


def test_a_writing_stage_without_go_refuses_before_it_opens_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--go is checked before anything connects, so a typo costs nothing."""

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a socket was opened before --go was checked")

    monkeypatch.setattr(harness, "LiveClient", never)
    out = io.StringIO()
    code = harness.main(_argv(tmp_path, stage="a"), out=out)

    assert code == harness.CODE_REFUSED
    assert "--go was not passed" in out.getvalue()


def test_the_open_set_must_be_the_one_the_operator_named() -> None:
    """The first guard, and the one that is never overridable.

    ``song.file_path`` is read before the first write. A set that is not the named one
    is refused whatever else is true about it, and a set that has never been saved is
    refused too: it has no path, so there is nothing to identify it by.
    """
    other = harness.Reader(empty_set(file_path=f"{HOME}/Music/Songs/Northlight/temp.als"))
    stoppers = failed(harness.preflight(other, target(), limits=harness.POPULATION_LIMITS))
    assert "set identity" in stoppers
    assert "temp.als" in stoppers["set identity"]

    unsaved = harness.Reader(empty_set(file_path=""))
    stoppers = failed(harness.preflight(unsaved, target(), limits=harness.POPULATION_LIMITS))
    assert "set identity" in stoppers
    assert "never been saved" in stoppers["set identity"]

    named = harness.Reader(empty_set())
    assert failed(harness.preflight(named, target(), limits=harness.POPULATION_LIMITS)) == {}


def test_the_set_identity_guard_cannot_be_waved_through() -> None:
    """--i-know-what-im-doing reaches the population heuristic and nothing else.

    A heuristic about how full a set looks is a guess, and a person who knows the set
    may overrule it. "This is not the file you named" is not a guess.
    """
    wrong = harness.Reader(empty_set(file_path="/somewhere/else.als"))
    checks = harness.preflight(wrong, target(), limits=harness.POPULATION_LIMITS)
    assert "set identity" in failed(checks, override=True)


def test_a_sentinel_live_could_have_chosen_itself_is_refused() -> None:
    """A default-named track is refused before a single read is taken.

    An instrument load renames a track that still carries Live's default name. From
    outside, that rename is indistinguishable from the selection having moved, so
    ``selected_track_same`` would come back null on every cycle and a thousand cycles
    would measure nothing at all.
    """
    for default_name in ("1-MIDI", "3 Audio", "12_midi", "2-Audio"):
        check = harness.check_sentinel_shape(default_name)
        assert not check.ok, f"{default_name!r} was accepted as a sentinel"
        assert "default track name" in check.detail
        assert not check.overridable

    assert harness.check_sentinel_shape(SENTINEL).ok
    assert not harness.check_sentinel_shape("   ").ok


def test_the_track_must_carry_the_sentinel_name_right_now() -> None:
    """Naming a sentinel is not enough; the track has to be wearing it."""
    live = FakeLive([track("2-MIDI")], selected=0)
    stoppers = failed(harness.preflight(harness.Reader(live), target(),
                                        limits=harness.POPULATION_LIMITS))
    assert "sentinel track" in stoppers
    assert "'2-MIDI'" in stoppers["sentinel track"]


def test_a_group_track_and_an_audio_track_are_both_refused() -> None:
    """An instrument needs an ordinary MIDI track, and a group track has no chain."""
    group = FakeLive([track(SENTINEL, group=True)])
    assert "track kind" in failed(
        harness.preflight(harness.Reader(group), target(), limits=harness.POPULATION_LIMITS)
    )

    audio = FakeLive([track(SENTINEL, midi=False)])
    stoppers = failed(harness.preflight(harness.Reader(audio), target(),
                                        limits=harness.POPULATION_LIMITS))
    assert "no MIDI input" in stoppers["track kind"]


def test_a_chain_that_is_not_empty_is_refused() -> None:
    """The harness deletes device 0 every cycle, so it builds the chain or it stops."""
    occupied = FakeLive([track(SENTINEL, devices=[device("Operator")])])
    stoppers = failed(harness.preflight(harness.Reader(occupied), target(),
                                        limits=harness.POPULATION_LIMITS))
    assert "already holds 1 device" in stoppers["empty chain"]


def test_a_rolling_transport_is_refused() -> None:
    """Nothing may be playing or recording while devices come and go."""
    rolling = empty_set(playing=True)
    assert "transport idle" in failed(
        harness.preflight(harness.Reader(rolling), target(), limits=harness.POPULATION_LIMITS)
    )

    armed = empty_set(recording=True)
    assert "transport idle" in failed(
        harness.preflight(harness.Reader(armed), target(), limits=harness.POPULATION_LIMITS)
    )


def test_a_set_that_looks_like_somebody_else_s_work_is_refused_but_overridable() -> None:
    """The population heuristic is the one guard a person may overrule."""
    busy = FakeLive(
        [track(SENTINEL), track("Drums", devices=[device("Kit")]), track("Bass"),
         track("Keys"), track("Vox")],
        scene_names=["Intro", "Verse", "Chorus", ""],
    )
    checks = harness.preflight(harness.Reader(busy), target(), limits=harness.POPULATION_LIMITS)
    stoppers = failed(checks)
    assert "population" in stoppers
    assert failed(checks, override=True) == {}, "the override reached nothing else"


def test_an_empty_set_s_eight_unnamed_scenes_are_not_somebody_else_s_work() -> None:
    """Scenes are counted by name, not by how many there are.

    A new Live set opens with eight empty scenes. Counting scenes rather than named
    ones refuses exactly the set this harness is built for, and a guard that refuses
    the intended case is a guard somebody turns off.
    """
    fresh = empty_set()
    assert failed(harness.preflight(harness.Reader(fresh), target(),
                                    limits=harness.POPULATION_LIMITS)) == {}

    worked_in = FakeLive([track(SENTINEL)], scene_names=["Intro", "Verse", ""])
    stoppers = failed(harness.preflight(harness.Reader(worked_in), target(),
                                        limits=harness.POPULATION_LIMITS))
    assert "named scenes=2" in stoppers["population"]


def test_the_browser_item_path_is_reproved_and_a_moved_index_stops_the_run() -> None:
    """``item_path`` is positional, and nothing pins it across a browser reindex.

    Re-proving it costs one op in a batch the cycle takes anyway. Without it, a
    reindex that moved another item into that slot loads that item instead, silently,
    for the rest of the run.
    """
    moved = empty_set(browser_item="Some Other Preset")
    stoppers = failed(harness.preflight(harness.Reader(moved), target(),
                                        limits=harness.POPULATION_LIMITS))
    assert "item path" in stoppers
    assert "Some Other Preset" in stoppers["item path"]


def test_a_track_index_outside_the_set_is_refused_before_anything_else_is_asked() -> None:
    """An index past the end of song.tracks stops the preflight rather than reading on."""
    checks = harness.preflight(
        harness.Reader(empty_set()), target(track=7), limits=harness.POPULATION_LIMITS
    )
    stoppers = failed(checks)
    assert "track index" in stoppers
    assert "sentinel track" not in {c.name for c in checks}


# --------------------------------------------------------------------------- #
# The dry stage
# --------------------------------------------------------------------------- #


def _argv(tmp_path: Path, *, stage: str = "dry", **extra: str) -> list[str]:
    """A complete command line for a run against the fake set."""
    argv = [
        "--stage", stage,
        "--expect-set", SET_PATH,
        "--sentinel", SENTINEL,
        "--track", "0",
        "--item-path", ITEM_PATH,
        "--expect-item-name", ITEM_NAME,
        "--expect-device", ITEM_NAME,
        "--root", "user_library",
        "--journal", str(tmp_path / "journal.jsonl"),
        "--pace", "0",
        "--live-pid", "4242",
    ]
    for flag, value in extra.items():
        argv += [f"--{flag.replace('_', '-')}", value]
    return argv


def fake_commands(_command: Any) -> Any:
    """A command runner that answers ps and footprint without running anything."""

    class Done:
        returncode = 0
        stdout = "1330384 437764224  7.9\n"
        stderr = ""

    return Done()


def test_the_dry_stage_issues_no_write_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted at the transport, not at the harness's own account of itself.

    The fake records every operation it is handed. After a full dry run, that record
    must contain reads and nothing else: no ``lom_set`` aiming the selection, no
    ``load_item``, no ``delete_device``. The harness's load and delete in this stage
    are callables that raise, and its client is wrapped in a gate that raises, so a
    write here is a crash rather than a device appearing in somebody's set.
    """
    live = empty_set()
    monkeypatch.setattr(harness, "LiveClient", lambda *_a, **_k: live)
    monkeypatch.setattr(harness, "run_command", fake_commands)

    code = harness.main(_argv(tmp_path), out=io.StringIO())

    assert code == 0
    assert live.writes() == [], f"the dry stage wrote: {live.writes()}"
    assert live.tracks[0]["devices"] == [], "the dry stage left a device behind"


def test_the_dry_stage_says_what_it_would_have_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rehearsal is only useful if it records the write it did not make."""
    live = empty_set()
    monkeypatch.setattr(harness, "LiveClient", lambda *_a, **_k: live)
    monkeypatch.setattr(harness, "run_command", fake_commands)

    harness.main(_argv(tmp_path), out=io.StringIO())
    records = _records(tmp_path / "journal.jsonl")

    aim = [r for r in records if r["record"] == "aim"]
    assert aim and aim[0]["would"]["value"] == {"__path__": "song.tracks[0]"}
    assert aim[0]["would"]["handler"] == "lom_set"

    cycles = [r for r in records if r["record"] == "cycle"]
    assert len(cycles) == harness.STAGES["dry"].cycles
    assert all(r["dry"] is True for r in cycles)
    assert cycles[0]["would_load"] == {"item_path": ITEM_PATH, "root": "user_library",
                                       "confirm": True}
    assert cycles[0]["would_delete"] == {"track": 0, "device": 0, "confirm": True}
    assert "load" not in cycles[0] and "delete" not in cycles[0]


def test_the_gate_refuses_a_batch_that_carries_a_write() -> None:
    """``lom_batch`` is not a read-only handler, because it can carry a set or a call.

    Letting a batch through on the handler name alone is exactly how a write reaches
    Live from a stage that promised not to make one.
    """
    live = empty_set()
    gate = harness.ReadOnlyGate(live)

    gate.send("lom_batch", {"ops": [{"op": "get", "path": "song.file_path"}]})
    assert live.operations, "a batch of reads did not reach the client"

    with pytest.raises(harness.WriteAttempted, match="is 'set', not 'get'"):
        gate.send("lom_batch", {"ops": [{"op": "set", "path": "song.tempo", "value": 100}]})
    with pytest.raises(harness.WriteAttempted, match="is 'call', not 'get'"):
        gate.send("lom_batch", {"ops": [{"op": "get", "path": "song.tempo"},
                                        {"op": "call", "path": "app.browser",
                                         "method": "load_item"}]})
    with pytest.raises(harness.WriteAttempted, match="not read-only"):
        gate.send("lom_set", {"path": "song.view.selected_track", "value": 0})
    assert live.writes() == []


def test_the_write_rule_reads_the_ops_and_not_the_handler_name() -> None:
    """``write_refusal`` is the whole of the gate, so it is tested on its own."""
    assert harness.write_refusal("lom_get", {"path": "song.tempo"}) is None
    assert harness.write_refusal("ping", None) is None
    assert harness.write_refusal("lom_batch", {"ops": []}) is None
    assert harness.write_refusal("lom_call", {}) is not None
    assert harness.write_refusal("lom_batch", None) is not None
    assert harness.write_refusal("lom_batch", {"ops": "get"}) is not None
    assert harness.write_refusal("lom_batch", {"ops": ["get"]}) is not None


def test_the_dry_stage_load_and_delete_raise_rather_than_return_a_pretend_result() -> None:
    """A stand-in that answered would let a bug in the stage flag pass as a cycle."""
    with pytest.raises(harness.WriteAttempted):
        harness.refuse_load(item_path=ITEM_PATH, confirm=True)
    with pytest.raises(harness.WriteAttempted):
        harness.refuse_delete(track=0, device=0, confirm=True)


# --------------------------------------------------------------------------- #
# Accepting a cycle, against results the real tools produced
# --------------------------------------------------------------------------- #


def real_load(live: FakeLive, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the real ``server.load_device`` against the fake and return its result."""
    monkeypatch.setattr(server, "_client", live)
    return server.load_device(item_path=ITEM_PATH, root="user_library", confirm=True)


def real_delete(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, *, index: int = 0
) -> dict[str, Any]:
    """Run the real ``server.delete_device`` against the fake and return its result."""
    monkeypatch.setattr(server, "_client", live)
    return server.delete_device(track=0, device=index, confirm=True)


def test_a_clean_load_is_accepted_on_the_evidence_the_real_tool_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: what a load that went where it was aimed looks like."""
    live = empty_set()
    result = real_load(live, monkeypatch)

    assert result["selected_track_same"] is True
    verdict = harness.judge_load(result, expect_device=ITEM_NAME)
    assert verdict.accepted, verdict.detail
    assert verdict.code == "loaded"


def test_a_load_that_answers_ok_but_cannot_name_the_track_is_not_a_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case this harness exists to catch, produced by the real tool.

    An instrument load renames a track that still carries Live's default name. The
    tool then reports ``ok: true`` and ``selected_track_same: null``, because a
    reading that moved proves that something moved and not which thing. Counting that
    as a cycle would leave the next load aimed at a track nobody has identified, and
    on a MIDI track a load replaces the instrument that is there.
    """
    renaming = FakeLive([track("1-MIDI")], rename_on_load=ITEM_NAME)
    result = real_load(renaming, monkeypatch)

    assert result["ok"] is True, "the precondition of this test is a load that succeeded"
    assert result["selected_track_same"] is None
    assert result["displaced_unknown_reason"] == "selection_moved_during_load"

    verdict = harness.judge_load(result, expect_device=ITEM_NAME)
    assert not verdict.accepted
    assert verdict.code == "identity_unknown"
    assert "selection_moved_during_load" in verdict.detail


def test_the_identity_is_judged_before_ok_is_even_looked_at() -> None:
    """A null identity stops the run whatever else the result says.

    ``selected_track_same`` is true or null and never false, so judging ``ok`` first
    would let a failed load report a tidier reason than the one that matters.
    """
    verdict = harness.judge_load(
        {"ok": False, "selected_track_same": None, "displaced_unknown_reason": "chain_unreadable"},
        expect_device=ITEM_NAME,
    )
    assert verdict.code == "identity_unknown"

    assert harness.judge_load(
        {"ok": False, "selected_track_same": True, "chain_before": [], "displaced": [],
         "loaded_device_index": 0, "chain_after": [ITEM_NAME]},
        expect_device=ITEM_NAME,
    ).code == "load_failed"


def test_a_load_that_displaced_something_or_landed_elsewhere_is_not_a_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that replaced another one is a cycle that did not start from empty."""
    occupied = FakeLive([track(SENTINEL, devices=[device("Operator")])])
    result = real_load(occupied, monkeypatch)

    assert result["displaced"] == ["Operator"]
    verdict = harness.judge_load(result, expect_device=ITEM_NAME)
    assert not verdict.accepted
    assert verdict.code == "chain_not_empty"

    wrong_name = harness.judge_load(result | {"chain_before": [], "displaced": []},
                                    expect_device="Something Else")
    assert wrong_name.code == "wrong_device"


def test_a_clean_delete_is_accepted_only_where_the_count_went_one_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control for the delete half of a cycle."""
    live = FakeLive([track(SENTINEL, devices=[device(ITEM_NAME)])])
    result = real_delete(live, monkeypatch)

    assert result["verified"] is True
    verdict = harness.judge_delete(result)
    assert verdict.accepted, verdict.detail
    assert live.tracks[0]["devices"] == []


def test_a_delete_that_left_a_device_behind_is_not_a_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``verified`` says the count shrank, which is not the same as the chain being empty.

    The real tool reports ``ok: true`` and ``verified: true`` for a chain that went
    from two devices to one. That is a correct report of a correct delete and a wrong
    answer to the question this harness asks, which is whether the cycle returned the
    track to the state the next cycle needs.
    """
    live = FakeLive([track(SENTINEL, devices=[device("A"), device("B")])])
    result = real_delete(live, monkeypatch)

    assert result["ok"] is True and result["verified"] is True
    verdict = harness.judge_delete(result)
    assert not verdict.accepted
    assert verdict.code == "wrong_count"
    assert "1 -> 0" in verdict.detail


def test_a_delete_that_could_not_be_verified_is_not_a_cycle() -> None:
    """A null ``verified`` is "not checked", and an unchecked delete is not evidence."""
    verdict = harness.judge_delete(
        {"ok": True, "verified": None, "verified_note": "not checked: the count could not "
         "be measured.", "device_count_before": 1, "device_count_after": 0}
    )
    assert not verdict.accepted
    assert verdict.code == "delete_unverified"


# --------------------------------------------------------------------------- #
# A whole cycle, with the real tools on both ends
# --------------------------------------------------------------------------- #


def build_run(
    live: FakeLive, journal: harness.Journal, monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> harness.StressRun:
    """A writing run wired to the real load and delete against the fake set."""
    monkeypatch.setattr(server, "_client", live)
    fields: dict[str, Any] = {
        "reader": harness.Reader(live),
        "target": target(),
        "journal": journal,
        "load": server.load_device,
        "delete": server.delete_device,
        "sampler": None,
        "dry": False,
        "pace": 0.0,
        "footprint_every": 0,
        "sleep": lambda _seconds: None,
    }
    fields.update(overrides)
    return harness.StressRun(**fields)


def test_a_whole_cycle_loads_and_deletes_and_leaves_the_track_as_it_found_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real tools: guard, load, delete, journal.

    The cycle's evidence is not shaped by this harness, so if either tool's result
    fields are renamed or stop being filled in, this test fails rather than a run in
    Live.
    """
    live = empty_set()
    with harness.Journal(tmp_path / "j.jsonl") as journal:
        run = build_run(live, journal, monkeypatch)
        outcomes = [run.cycle(index) for index in (1, 2, 3)]

    assert [o.accepted for o in outcomes] == [True, True, True]
    assert live.tracks[0]["devices"] == [], "the chain did not come back to empty"

    records = [r for r in _records(tmp_path / "j.jsonl") if r["record"] == "cycle"]
    assert len(records) == 3
    assert records[0]["load"]["selected_track_same"] is True
    assert records[0]["load"]["chain_after"] == [ITEM_NAME]
    assert records[0]["delete"]["device_count_before"] == 1
    assert records[0]["delete"]["device_count_after"] == 0
    assert records[0]["load_seconds"] >= 0 and records[0]["delete_seconds"] >= 0


def test_the_run_stops_at_the_first_cycle_it_cannot_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected cycle ends the run, and no retry is attempted.

    A write that failed may still have landed, so the only safe next move is a read.
    The stop is what leaves the operator to make it.
    """
    live = empty_set()
    with harness.Journal(tmp_path / "j.jsonl") as journal:
        run = build_run(live, journal, monkeypatch)
        assert run.cycle(1).accepted
        # Somebody clicks another track between cycles.
        live.tracks.append(track("Scratch"))
        live.selected = 1
        summary = run.run(5, out=io.StringIO())

    assert summary.cycles_completed == 0
    assert summary.stop_reason == "guard_failed"
    assert "selection" in summary.stop_detail
    assert live.tracks[1]["devices"] == [], "a device was loaded onto the clicked track"


def test_a_guard_failure_mid_run_is_journalled_before_the_run_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cycle that stopped the run is on disk with the reason it stopped."""
    live = empty_set()
    with harness.Journal(tmp_path / "j.jsonl") as journal:
        run = build_run(live, journal, monkeypatch)
        live.browser_item = "Another Preset"
        outcome = run.cycle(1)

    assert not outcome.accepted
    record = _records(tmp_path / "j.jsonl")[-1]
    assert record["code"] == "guard_failed"
    assert any(not c["ok"] and c["name"] == "item path" for c in record["guards"])


# --------------------------------------------------------------------------- #
# The journal
# --------------------------------------------------------------------------- #


def _records(path: Path) -> list[dict[str, Any]]:
    """Every JSON record in a journal, read back from disk."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_record_is_on_disk_before_the_next_one_is_built(tmp_path: Path) -> None:
    """The whole point of the journal: a crash at cycle 617 leaves 616 cycles.

    Read through a second, independent handle after each write, which is what a
    separate process reading the file after a crash would see. A buffered writer
    passes a test that only reads the file at the end.
    """
    path = tmp_path / "j.jsonl"
    with harness.Journal(path) as journal:
        for index in range(1, 6):
            journal.write({"record": "cycle", "cycle": index})
            on_disk = _records(path)
            assert len(on_disk) == index, "the last record had not reached disk"
            assert on_disk[-1]["cycle"] == index


def test_every_record_is_fsynced_and_not_merely_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flush leaves the line in the operating system, fsync puts it on the disk."""
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        harness.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1]
    )

    with harness.Journal(tmp_path / "j.jsonl") as journal:
        journal.write({"record": "header"})
        journal.write({"record": "cycle", "cycle": 1})

    assert len(synced) == 2, "a record reached the file without being fsynced"


def test_a_journal_that_cannot_be_written_to_is_not_silently_skipped(tmp_path: Path) -> None:
    """Writing to a closed journal raises rather than dropping a measured cycle."""
    journal = harness.Journal(tmp_path / "j.jsonl")
    with pytest.raises(RuntimeError, match="not open"):
        journal.write({"record": "cycle"})


# --------------------------------------------------------------------------- #
# Staging
# --------------------------------------------------------------------------- #


def write_journal(path: Path, stage: str, **summary: Any) -> Path:
    """A journal the stage gate can read, with a header and a summary."""
    fields: dict[str, Any] = {
        "record": "summary",
        "stage": stage,
        "cycles_completed": harness.STAGES[stage].cycles,
        "stop_reason": "completed",
        "seconds_per_cycle_median": 7.5,
    }
    fields.update(summary)
    path.write_text(
        json.dumps({"record": "header", "stage": stage}) + "\n" + json.dumps(fields) + "\n",
        encoding="utf-8",
    )
    return path


def test_a_bigger_stage_does_not_start_without_the_smaller_one_s_journal(tmp_path: Path) -> None:
    """Every writing stage is gated on the stage below it having completed."""
    assert harness.gate_stage(harness.STAGES["dry"], None, max_hours=3.0).ok

    for name in ("a", "b", "c"):
        check = harness.gate_stage(harness.STAGES[name], None, max_hours=3.0)
        assert not check.ok
        assert "--prior" in check.detail

    wrong_rung = harness.read_prior(write_journal(tmp_path / "dry.jsonl", "dry"))[0]
    check = harness.gate_stage(harness.STAGES["c"], wrong_rung, max_hours=3.0)
    assert not check.ok
    assert "follows stage 'b'" in check.detail


def test_a_prior_stage_that_stopped_or_fell_short_does_not_gate_the_next_one(
    tmp_path: Path,
) -> None:
    """A stage that stopped measured how far it got, not how long a longer one takes."""
    stopped = harness.read_prior(
        write_journal(tmp_path / "a.jsonl", "a", stop_reason="identity_unknown")
    )[0]
    check = harness.gate_stage(harness.STAGES["b"], stopped, max_hours=3.0)
    assert not check.ok
    assert "identity_unknown" in check.detail

    short = harness.read_prior(write_journal(tmp_path / "a2.jsonl", "a", cycles_completed=9))[0]
    assert not harness.gate_stage(harness.STAGES["b"], short, max_hours=3.0).ok

    crashed = tmp_path / "a3.jsonl"
    crashed.write_text(json.dumps({"record": "header", "stage": "a"}) + "\n", encoding="utf-8")
    prior, why = harness.read_prior(crashed)
    assert prior is None
    assert "did not finish" in why


def test_the_measured_per_cycle_time_replaces_the_estimate_before_the_long_run(
    tmp_path: Path,
) -> None:
    """The gate turns a measured cycle time into permission to spend an afternoon.

    The duration is never assumed: at 7.5 s a cycle the thousand cycle stage is 2.08
    hours and passes a three hour limit; at 12 s a cycle it is 3.33 hours and is
    refused with that number in the message, rather than being discovered two hours in.
    """
    quick = harness.read_prior(write_journal(tmp_path / "b.jsonl", "b",
                                             seconds_per_cycle_median=7.5))[0]
    assert quick is not None and quick.seconds_per_cycle == 7.5
    approved = harness.gate_stage(harness.STAGES["c"], quick, max_hours=3.0)
    assert approved.ok
    assert approved.observed == pytest.approx(2.083, abs=0.01)

    slow = harness.read_prior(write_journal(tmp_path / "b2.jsonl", "b",
                                            seconds_per_cycle_median=12.0))[0]
    refused = harness.gate_stage(harness.STAGES["c"], slow, max_hours=3.0)
    assert not refused.ok
    assert "3.33 h" in refused.detail
    assert "12.00 s/cycle" in refused.detail

    unmeasured = harness.read_prior(
        write_journal(tmp_path / "b3.jsonl", "b", seconds_per_cycle_median=None)
    )[0]
    assert not harness.gate_stage(harness.STAGES["c"], unmeasured, max_hours=3.0).ok


def test_a_run_writes_the_number_the_next_stage_is_gated_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary a stage writes is exactly what the next stage's gate reads back."""
    live = empty_set()
    monkeypatch.setattr(harness, "LiveClient", lambda *_a, **_k: live)
    monkeypatch.setattr(harness, "run_command", fake_commands)

    assert harness.main(_argv(tmp_path), out=io.StringIO()) == 0
    prior, why = harness.read_prior(tmp_path / "journal.jsonl")

    assert prior is not None, why
    assert prior.stage == "dry"
    assert prior.stop_reason == "completed"
    assert prior.cycles_completed == harness.STAGES["dry"].cycles
    assert prior.seconds_per_cycle is not None


def test_the_summary_refuses_to_attribute_a_memory_delta() -> None:
    """A result never claims what it cannot read back, and the undo stack is unreadable."""
    summary = harness.RunSummary(stage="c", cycles_planned=1000, cycles_completed=1000,
                                 seconds=[7.0] * 1000)
    record = summary.as_record()

    assert record["seconds_per_cycle_median"] == 7.0
    assert record["projected_hours_1000"] == pytest.approx(1.944, abs=0.01)
    assert "undo stack" in record["attribution_note"]
    assert "before/after pair" in record["attribution_note"]


# --------------------------------------------------------------------------- #
# What macOS says about a running process
# --------------------------------------------------------------------------- #

#: Verbatim output of ``ps -M -p <pid>`` on this machine, trimmed to four threads.
PS_M_OUTPUT = """USER    PID   TT   %CPU STAT PRI     STIME     UTIME COMMAND
roman  1645   ??    6.9 S    46T   0:23.00   2:49.95 /Applications/Live
       1645         0.0 S    46T   0:00.24   0:00.23
       1645         0.0 S    31T   0:00.00   0:00.00
       1645         0.0 S    31T   0:00.00   0:00.00
       1645         0.0 S    31T   0:00.00   0:00.00
"""

#: Verbatim head and tail of ``footprint -p <pid>`` on this machine.
FOOTPRINT_OUTPUT = """======================================================================
Live [1645]: 64-bit    Footprint: 1330 MB (16384 bytes per page)
======================================================================

  Dirty      Clean  Reclaimable    Regions    Category
 672 MB        0 B       415 MB        337    MALLOC_SMALL
1330 MB     178 MB       415 MB       9852    TOTAL

Auxiliary data:
    phys_footprint: 1330 MB
    phys_footprint_peak: 2059 MB
"""


def test_the_thread_count_counts_threads_and_not_lines() -> None:
    """Parsed from the real shape: a header, the process, then one line per thread.

    Subtracting two from the line count gives the same answer today and a wrong one
    the day the header wraps or a blank line appears.
    """
    assert harness.parse_ps_threads(PS_M_OUTPUT, 1645) == {"threads": 4.0}
    assert harness.parse_ps_threads(PS_M_OUTPUT, 9999) == {"threads": 0.0}


def test_the_memory_figures_are_parsed_from_the_real_output() -> None:
    """RSS in kibibytes from ps, footprint in mebibytes with its unit read, not assumed."""
    assert harness.parse_ps_memory("1330384 437764224  7.9\n") == {
        "rss_kib": 1330384.0,
        "vsz_kib": 437764224.0,
        "percent_memory": 7.9,
    }
    with pytest.raises(ValueError, match="three fields"):
        harness.parse_ps_memory("1330384\n")

    parsed = harness.parse_footprint(FOOTPRINT_OUTPUT)
    assert parsed["footprint_mib"] == 1330.0
    assert parsed["footprint_peak_mib"] == 2059.0
    assert harness.parse_footprint("Footprint: 2 GB (x)")["footprint_mib"] == 2048.0
    with pytest.raises(ValueError, match="no footprint figure"):
        harness.parse_footprint("nothing here")


def test_a_sample_carries_the_command_it_ran_and_what_that_cost() -> None:
    """The cost is measured when the sample is taken, never written down in advance.

    A number in the source would be a claim about a machine. The command travels with
    the sample so that a reader of the journal can run it again.
    """
    ticks = iter([0.0, 0.0032, 1.0, 1.0035])

    def runner(command: Any) -> Any:
        class Done:
            returncode = 0
            stdout = PS_M_OUTPUT if "-M" in command else "1330384 437764224  7.9"
            stderr = ""

        return Done()

    sampler = harness.MemorySampler(1645, runner=runner, clock=lambda: next(ticks))
    memory = sampler.ps().as_record()
    threads = sampler.threads().as_record()

    assert memory["command"] == ["/bin/ps", "-o", "rss=,vsz=,pmem=", "-p", "1645"]
    assert memory["cost_ms"] == pytest.approx(3.2, abs=0.01)
    assert memory["fields"]["rss_kib"] == 1330384.0
    assert threads["command"] == ["/bin/ps", "-M", "-p", "1645"]
    assert threads["fields"]["threads"] == 4.0


def test_a_sampler_that_fails_reports_the_failure_and_does_not_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory is context for the run, not its verdict, so a failed sample is recorded.

    A harness that died because ``footprint`` was unavailable would throw away the
    cycles it had already measured.
    """

    def broken(_command: Any) -> Any:
        raise OSError("no such tool")

    sample = harness.MemorySampler(1645, runner=broken).footprint()
    assert not sample.ok
    assert "no such tool" in sample.error
    assert sample.fields == {}

    live = empty_set()
    with harness.Journal(tmp_path / "j.jsonl") as journal:
        run = build_run(
            live,
            journal,
            monkeypatch,
            sampler=harness.MemorySampler(1645, runner=broken),
            footprint_every=1,
        )
        assert run.cycle(1).accepted

    record = _records(tmp_path / "j.jsonl")[-1]
    assert [s["kind"] for s in record["memory"]] == ["ps", "ps_threads", "footprint"]
    assert all(s["ok"] is False for s in record["memory"])
    assert all(s["command"] for s in record["memory"])


def test_a_machine_without_exactly_one_live_is_reported_rather_than_guessed() -> None:
    """Sampling the wrong process is worse than sampling none."""

    def answering(text: str) -> Any:
        def runner(_command: Any) -> Any:
            class Done:
                returncode = 0
                stdout = text
                stderr = ""

            return Done()

        return runner

    assert harness.find_live_pid(answering("1645\n")) == (1645, "")
    assert harness.find_live_pid(answering(""))[0] is None
    pid, why = harness.find_live_pid(answering("1645\n1646\n"))
    assert pid is None
    assert "2 processes" in why


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def test_a_set_path_is_compared_without_resolving_it() -> None:
    """Normalised, never resolved: a symlink named like the stress set is not it.

    Case is not folded either. A path that differs in case is a path the operator did
    not type, and on a case-insensitive volume the file system will accept it anyway;
    here the question is whether the operator named this set, not whether the bytes
    can be opened.
    """
    assert harness.same_set_path(SET_PATH, SET_PATH)
    assert harness.same_set_path("/a/b/../b/c.als", "/a/b/c.als")
    assert not harness.same_set_path(SET_PATH.upper(), SET_PATH)
    assert not harness.same_set_path("", SET_PATH)
    assert not harness.same_set_path(SET_PATH, "")
