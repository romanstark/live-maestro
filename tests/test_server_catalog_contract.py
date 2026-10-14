"""The contract between ``server.py`` and the catalog: enforced, not assumed.

``server.py`` does not build paths. It names catalog rows by id and lets
:func:`live_maestro.executor.execute` do the rest (docs/architecture.md, 'Layers'),
so the two halves can drift apart silently: a row can be renamed, or lose the access
the server needs, and nothing complains until a tool fails against a running Live.

``track.volume`` is the shape of that drift. Its path stops at the
``DeviceParameter`` *object*, which Live refuses to write, so the row grants only
``get`` while the companion row ``track.volume_value`` carries the write. Point
``set_mix`` at ``track.volume`` and every unit test still passes; the first run
against a real Live answers ``access_not_allowed: track.volume does not allow set``.

No Live is needed here: the catalog is data on disk and the mapping tables are constants.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from live_maestro import instruments, server
from live_maestro.client import LiveCommandError, LiveError
from live_maestro.models import Access
from live_maestro.registry import default_registry

#: Mapping keys whose value is a catalog id the server writes through. Kept
#: explicit rather than inferred: a key that starts being written should have to
#: be added here deliberately, which is the moment to think about whether the row
#: it points at can actually take a write.
WRITTEN_KEYS = frozenset({"volume", "panning", "send", "mute", "solo", "parameter"})

#: Keys that are not row ids at all: they name a parameter placeholder or another
#: kind's table, so they must not be looked up in the catalog.
NOT_ROW_IDS = frozenset({"param", "fields", "device_param"})


def _mapping_rows() -> list[tuple[str, str, str]]:
    """Every (kind, key, row_id) the server can reach, from its own tables."""
    out: list[tuple[str, str, str]] = []
    for kind, table in server._TRACK_KINDS.items():
        for key, value in table.items():
            if key in NOT_ROW_IDS:
                continue
            if key == "fields":
                continue
            if isinstance(value, str):
                out.append((kind, key, value))
        for row_id in table.get("fields", ()):
            out.append((kind, "fields", row_id))
    return out


def test_every_row_the_server_names_exists() -> None:
    """A renamed or deleted row must fail here, not in front of a user."""
    registry = default_registry()
    known = {spec.id for spec in registry.all()}
    missing = sorted(
        f"{kind}.{key} -> {row_id!r}"
        for kind, key, row_id in _mapping_rows()
        if row_id not in known
    )
    assert not missing, (
        "server.py names catalog rows that do not exist:\n  "
        + "\n  ".join(missing)
        + "\n\nEither the row was renamed in catalog/*.yaml and server.py was not "
        "followed up, or the id is a typo. server.py must never build a path of "
        "its own to work around this."
    )


@pytest.mark.parametrize("kind", sorted(server._TRACK_KINDS))
def test_rows_the_server_writes_through_grant_set(kind: str) -> None:
    """A write must land on a row that actually allows ``set``.

    This catches a mapping key that writes through a row granting only ``get``.
    """
    registry = default_registry()
    table = server._TRACK_KINDS[kind]
    offenders: list[str] = []
    for key, row_id in table.items():
        if key not in WRITTEN_KEYS or not isinstance(row_id, str):
            continue
        spec = registry.get(row_id)
        if Access.SET not in spec.access:
            granted = ", ".join(a.value for a in spec.access)
            offenders.append(
                f"{kind}[{key!r}] -> {row_id!r} grants only [{granted}]; "
                f"its path is {spec.path!r}"
            )
    assert not offenders, (
        "server.py writes through rows that do not allow 'set':\n  "
        + "\n  ".join(offenders)
        + "\n\nA path that stops at a DeviceParameter object cannot be written: Live "
        "answers not_settable. Point the mapping at the '.value' companion row "
        "instead, and leave the object row for reads and for automation_write's "
        "'parameter' argument, which requires the object (measured 2026-08-29)."
    )


def test_rows_the_server_reads_grant_get() -> None:
    """Every field the server reads must actually be readable."""
    registry = default_registry()
    offenders: list[str] = []
    for kind, key, row_id in _mapping_rows():
        spec = registry.get(row_id)
        if Access.GET not in spec.access and Access.CALL not in spec.access:
            granted = ", ".join(a.value for a in spec.access)
            offenders.append(f"{kind}[{key!r}] -> {row_id!r} grants only [{granted}]")
    assert not offenders, "server.py reads rows that grant neither get nor call:\n  " + "\n  ".join(
        offenders
    )


def test_value_companions_and_their_object_rows_stay_paired() -> None:
    """Every ``*_value`` row must sit beside the object row it was split from.

    Both halves are load-bearing: ``automation_write`` needs the object row for its
    ``parameter`` argument and rejects the ``.value`` spelling with *"float is not a
    DeviceParameter"*, while the ``.value`` row is the only one that can be written.
    """
    registry = default_registry()
    known = {spec.id: spec for spec in registry.all()}
    problems: list[str] = []
    for spec in registry.all():
        if not spec.id.endswith("_value") or not spec.path.endswith(".value"):
            continue
        object_id = spec.id[: -len("_value")]
        parent = known.get(object_id)
        if parent is None:
            problems.append(f"{spec.id!r} has no object row {object_id!r} beside it")
            continue
        if parent.path + ".value" != spec.path:
            problems.append(
                f"{spec.id!r} path {spec.path!r} is not {object_id!r}'s path plus '.value'"
            )
        if Access.SET in parent.access:
            problems.append(
                f"{object_id!r} still grants 'set' although {spec.id!r} exists to carry the write"
            )
        if Access.SET not in spec.access:
            problems.append(f"{spec.id!r} exists to carry a write but does not grant 'set'")
    assert not problems, "value companions and object rows have drifted:\n  " + "\n  ".join(
        problems
    )


# --------------------------------------------------------------------------------
# The way back: a raw path, and what the catalog knows about it
# --------------------------------------------------------------------------------


def test_a_raw_path_finds_its_catalog_rows() -> None:
    """``lom_get`` has a path. The catalog is keyed by id. This is the bridge.

    ``spec.build()`` goes template → path, and ``rows_for_path`` goes the other
    way, so a raw path can reach the rows that describe it.
    """
    registry = default_registry()
    assert [s.id for s in registry.rows_for_path("song.tracks[5].playing_slot_index")] == [
        "track.playing_slot_index"
    ]
    # Methods are addressed by path AND name, so a path alone must not return them.
    assert registry.rows_for_path("song") == ()
    # The dynamic surface is not catalogued, and that is not a failure.
    assert registry.rows_for_path("song.tracks[0].devices[1].parameters[7].plugin_field") == ()


def test_the_catalog_hint_stays_silent_unless_it_disagrees() -> None:
    """The hint is selective: one on every result would be noise.

    Most of the assertions here expect nothing at all.
    """
    # A verified row, read normally: nothing to say.
    assert server._catalog_hint("song.tracks[0].mixer_device.volume", writing=False) is None
    assert server._catalog_hint("song.tempo", writing=False) is None
    # Off-catalog: also nothing. "No row" is the expected answer for a plug-in.
    assert server._catalog_hint("song.tracks[0].devices[1].parameters[7].x", writing=True) is None


def test_the_catalog_hint_speaks_for_a_broken_row() -> None:
    """``song.master_track.mute`` is measured broken.

    It reads like an ordinary write and is one of exactly three broken property
    paths.
    """
    hint = server._catalog_hint("song.master_track.mute", writing=False)
    assert hint is not None
    assert hint["status"] == {"master.mute": "broken"}
    assert "not `verified`" in hint["note"]
    assert hint["doc"]["master.mute"], "the row's own doc has to travel with the hint"


def test_the_catalog_hint_names_the_writable_companion() -> None:
    """The DeviceParameter trap, in the one place a caller can still walk into it.

    ``…mixer_device.volume`` is a DeviceParameter object: the catalog grants it
    ``get``/``observe``/``automate`` and puts the write on ``…volume.value``.
    ``lom_set`` takes no row, so nothing else stops or informs that write.
    """
    hint = server._catalog_hint("song.tracks[0].mixer_device.volume", writing=True)
    assert hint is not None
    assert hint["not_settable_per_catalog"] == ["track.volume"]
    assert "the access was measured" in hint["note"]
    # The doc is the part the layer below cannot supply: the fader curve, where
    # 0.85 is 0 dB and three different numbers describe one control.
    assert "0.85 is 0 dB" in hint["doc"]["track.volume"]
    # And not a "write X instead": the script already says that, with the concrete
    # index, so repeating it from here could only be worse.
    assert "instead" not in hint["note"]


def test_a_failing_read_still_carries_the_catalog_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hint rides on a failed read as well as on a successful one.

    ``lom_get song.master_track.mute`` answers, from Live, "Main track has no
    'mute' property!", and the catalog carries ``status: broken`` for that row. The
    test goes through ``lom_get`` rather than calling ``_catalog_hint`` directly.
    """

    class Refusing:
        def get(self, path: str) -> dict[str, object]:
            raise LiveError("Main track has no 'mute' property!")

    monkeypatch.setattr(server, "_client_instance", lambda: Refusing())
    result = server.lom_get("song.master_track.mute")
    assert result["ok"] is False
    assert result["catalog"]["status"] == {"master.mute": "broken"}
    assert result["catalog"]["doc"]["master.mute"]


def test_a_raw_path_gets_its_value_decoded_and_only_then() -> None:
    """The sentinel arrives on the read that produced it, or not at all.

    ``lom_get`` carries no row id, so the decoding is driven by the path.
    ``song.clip_trigger_quantization`` returns ``{"value": 4, "type": "int"}``: one
    integer of fourteen, meaning nothing on its own.
    """
    assert server._means_for_path("song.tracks[3].playing_slot_index", -1) is not None
    assert server._means_for_path("song.tracks[3].playing_slot_index", -2) is not None
    assert server._means_for_path("song.scenes[2].tempo", -1.0) is not None
    # And the case that must stay quiet: an ordinary value on the same path.
    assert server._means_for_path("song.tracks[3].playing_slot_index", 8) is None
    assert server._means_for_path("song.scenes[2].tempo", 124.0) is None
    # Nothing catalogued, nothing said.
    assert server._means_for_path("song.tracks[0].devices[1].parameters[7].value", -1) is None


def test_a_row_based_result_decodes_value_and_read_back() -> None:
    """``_result_dict`` is the other end: the tools that name a row, not a path."""
    from live_maestro.executor import Result

    spec = default_registry().get("track.playing_slot_index")
    out = server._result_dict(
        Result(ok=True, id=spec.id, path="song.tracks[0].playing_slot_index", value=-1), spec
    )
    assert "nothing is playing" in out["means"]

    quiet = server._result_dict(
        Result(ok=True, id=spec.id, path="song.tracks[0].playing_slot_index", value=8), spec
    )
    assert "means" not in quiet, "an ordinary index must not acquire a gloss"


# --------------------------------------------------------------------------------
# load_device reports what it displaced, and says on what evidence
# --------------------------------------------------------------------------------

#: The only raw read ``load_device`` is allowed to send: the parameter collection of
#: the one device the two chains single out. Anything else reaching the fake below is
#: a read the tool has no business making.
_PARAMETERS_READ = re.compile(r"^song\.[\w.]+(?:\[\d+\])?(?:\.[\w]+\[\d+\])*\.parameters$")


def _track_handle(name: str | None) -> dict[str, Any]:
    """The handle a selected-track read answers with, in the shape Live sends.

    On Live 12.4.5, ``lom_get song.view.selected_track`` answers ``{"__lom__":
    "Track", "path": "song.view.selected_track", "name": ...}``. The key is ``path``
    and it carries the property path, never ``song.tracks[N]``. ``__path__`` is the
    spelling of the *request* side and the wire never sends it.

    A handle without a name is what an unreadable name looks like from here, so the
    name is left off rather than faked when the caller asks for None.
    """
    handle: dict[str, Any] = {"__lom__": "Track", "path": "song.view.selected_track"}
    if name is not None:
        handle["name"] = name
    return handle


def _device_handles(names: list[str]) -> list[dict[str, Any]]:
    """The device chain of the selected track, as a collection read answers it."""
    return [
        {"__lom__": "Device", "path": f"song.view.selected_track.devices[{i}]", "name": name}
        for i, name in enumerate(names)
    ]


class _FakeBrowser:
    """A client shaped like the wire, recording every raw read the tool sends.

    It refuses rather than answers: an unplanned path raises, a ``_live_ptr`` read
    raises what Live raises, and a parameters read answers with a real collection
    whose size the test chose. One plausible constant covering every question, such
    as ``{"ok": True, "value": 1, "count": 10}`` for any ``lom_get``, is how a dead
    branch stays green.
    """

    def __init__(self, *, parameter_count: int | None = None, walks: bool = True) -> None:
        self.parameter_count = parameter_count
        self.walks = walks
        self.reads: list[str] = []

    def send(self, handler: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        if handler == "lom_get":
            return self._read(str(payload.get("path", "")), payload)
        if handler != "browser_walk":
            raise AssertionError(f"unplanned handler {handler!r} with {payload!r}")
        if not self.walks:
            raise AssertionError("item_path must not trigger a browser walk")
        return {
            "mode": "uri",
            "found": True,
            "item_path": "app.browser.instruments.children[0]",
            "item": {"name": "Something"},
            "truncated": False,
            "truncated_by": None,
            "unreached_roots": [],
            "status": "complete",
        }

    def _read(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.reads.append(path)
        if "_live_ptr" in path:
            # What the wire answers on Live 12.4.5: bad_path with this message, and
            # no value. A pointer comparison has nothing to compare.
            raise LiveCommandError(
                "lom_get",
                payload,
                {
                    "code": "bad_path",
                    "message": "segment '_live_ptr' addresses a private attribute",
                    "path": path,
                },
            )
        if self.parameter_count is None or not _PARAMETERS_READ.match(path):
            raise AssertionError(f"unplanned lom_get path {path!r}")
        names = ["Device On"] + [f"Macro {i}" for i in range(1, self.parameter_count)]
        return {
            "path": path,
            "type": "list",
            "class": "DeviceParameterVector",
            "count": len(names),
            "value": [
                {"__lom__": "DeviceParameter", "path": f"{path}[{i}]", "name": name}
                for i, name in enumerate(names)
            ],
        }


def _load_with_chains(
    monkeypatch: pytest.MonkeyPatch,
    before: list[str],
    after: list[str],
    *,
    name_before: str | None = "15 Pad Mystic",
    name_after: str | None = "15 Pad Mystic",
    tracks_before: int | None = 4,
    tracks_after: int | None = 4,
    parameter_count: int | None = None,
    chains_read: bool = True,
    fail_second_snapshot: bool = False,
    load_args: dict[str, Any] | None = None,
    device_class: str = "Device",
) -> dict[str, Any]:
    """Run load_device against a selection whose chain goes ``before`` -> ``after``.

    Every reading the tool compares is a parameter here, because the tool has to be
    able to report each of them as unknown: the selected track's name on each side,
    the number of tracks on each side, whether the chain read succeeded at all, and
    how many parameters the new device exposes. ``parameter_count`` defaults to None,
    which makes the fake refuse a parameters read, so a test that does not expect a
    Configure probe fails loudly if one is sent.

    Returns:
        The tool's result, with the catalog rows it used under ``_rows_used`` and the
        raw paths it read under ``_reads``.
    """
    calls: list[str] = []
    snapshots = 0
    counts = iter([tracks_before, tracks_after])

    def fake_run(spec_id: str, **_kw: Any) -> dict[str, Any]:
        calls.append(spec_id)
        if spec_id == "browser.load_item":
            return {"ok": True}
        raise AssertionError(
            f"unplanned catalog row {spec_id!r}: the selection and its chain belong to "
            "one batch, because a chain read a round trip after the handle it belongs "
            "to can already be another track's chain"
        )

    def fake_run_batch(
        ops: Any, **_kw: Any
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        nonlocal snapshots
        ids = [op["id"] for op in ops]
        assert ids == ["view.selected_track", "view.selected_track_devices"], ids
        calls.extend(ids)
        snapshots += 1
        if fail_second_snapshot and snapshots == 2:
            return None, {"ok": False, "code": "timeout", "error": "the batch timed out."}
        first = snapshots == 1
        chain = _device_handles(before if first else after)
        if not first:
            for handle in chain:
                handle["__lom__"] = device_class
        return (
            [
                {"ok": True, "value": _track_handle(name_before if first else name_after)},
                {"ok": chains_read, "value": chain if chains_read else None},
            ],
            None,
        )

    def fake_count(
        spec_id: str, index_arg: str, base: Any, cap: int | None = None
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        assert spec_id == "track.name", spec_id
        total = next(counts)
        if total is None:
            # A probe that did not reach the end of the collection. It settles nothing,
            # and the tool has to say so rather than compare the ceiling it stopped at.
            return {"count": 0, "exact": False, "values": []}, None
        return {"count": total, "exact": True, "values": [f"t{i}" for i in range(total)]}, None

    args = dict(load_args or {"uri": "x"})
    browser = _FakeBrowser(parameter_count=parameter_count, walks="item_path" not in args)
    monkeypatch.setattr(server, "_run", fake_run)
    monkeypatch.setattr(server, "_run_batch", fake_run_batch)
    monkeypatch.setattr(server, "_count", fake_count)
    monkeypatch.setattr(server, "_client_instance", lambda: browser)
    result = server.load_device(confirm=True, **args)
    result["_rows_used"] = calls
    result["_reads"] = browser.reads
    return result


def test_the_chain_is_read_through_the_selection_not_a_track_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handle Live sends carries no track index, and assuming one breaks silently.

    ``view.selected_track`` answers with ``path: 'song.view.selected_track'``, the
    property path, and never with a handle shaped ``song.tracks[2]``. Parsing
    ``tracks[N]`` out of it matches nothing and reports no displacement at all.

    So the row used here is the assertion: the chain is read by following the
    selection, which is also what a browser load targets.
    """
    result = _load_with_chains(monkeypatch, ["Wavetable"], ["Soft Pad"], parameter_count=16)
    assert "view.selected_track_devices" in result["_rows_used"]
    assert "device.list" not in result["_rows_used"]
    assert result["displaced"] == ["Wavetable"]


def test_a_load_that_replaces_an_instrument_says_so_in_its_own_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning has to reach the call that does the damage, not the one before it.

    A caller may search once, do other work, and confirm later with a uri, so a
    replacement warning on the search branch alone reaches nobody at the moment
    the instrument is displaced. The confirming branch carries its own.
    """
    result = _load_with_chains(monkeypatch, ["Kit-OP 808"], ["DS Clap"], parameter_count=16)
    assert result["displaced"] == ["Kit-OP 808"]
    assert result["chain_before"] == ["Kit-OP 808"]
    assert result["chain_after"] == ["DS Clap"]
    replaced = [note for note in result["notes"] if note.startswith("Replaced:")]
    assert replaced, result["notes"]
    assert "Kit-OP 808" in replaced[0]


def test_an_effect_that_displaces_nothing_says_that_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence would be ambiguous: nothing lost, or nothing looked at?"""
    result = _load_with_chains(
        monkeypatch, ["Operator"], ["Operator", "Auto Filter"], parameter_count=16
    )
    assert result["displaced"] == []
    assert any("Nothing was displaced" in note for note in result["notes"])


def test_an_unreadable_chain_claims_no_displacement(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain that was not read must not produce a loss report, or deny one either.

    The read failed, so there is nothing to compare. ``displaced`` is null with
    ``chain_unreadable`` beside it, which is a different answer from the empty list a
    chain that was read and lost nothing earns.
    """
    result = _load_with_chains(
        monkeypatch, ["Wavetable"], ["Soft Pad"], chains_read=False
    )
    assert result["displaced"] is None
    assert result["displaced_unknown_reason"] == "chain_unreadable"
    assert not any(note.startswith("Replaced:") for note in result["notes"])
    assert not any("Nothing was displaced" in note for note in result["notes"])


def test_the_item_path_route_reports_displacement_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The way around the budget failure must not be a way around the report.

    A deeply nested preset makes the uri lookup run out of node budget, and the
    error sends the caller to ``lom_call(app.browser, load_item)``, which loads the
    same item and says nothing about what it replaced. The displacement report
    therefore lives on this route too.
    """
    result = _load_with_chains(
        monkeypatch,
        ["Wavetable", "Utility"],
        ["Soft Pad", "Utility"],
        parameter_count=16,
        load_args={"item_path": "app.browser.instruments.children[3].children[7]"},
    )
    assert result["displaced"] == ["Wavetable"]
    assert any(note.startswith("Replaced:") for note in result["notes"])
    assert result["_reads"] == ["song.view.selected_track.devices[0].parameters"]


def test_a_malformed_item_path_is_refused_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path this project would not build is a caller mistake, not a load."""
    monkeypatch.setattr(server, "_run", lambda spec_id, **kw: {"ok": True, "value": None})
    monkeypatch.setattr(server, "_client_instance", lambda: None)
    result = server.load_device(item_path="not a path at all", confirm=True)
    assert result["ok"] is False
    assert "item_path" in result["error"]


# --------------------------------------------------------------------------------
# What "the same track" means when the LOM hands out no track id
# --------------------------------------------------------------------------------


def test_a_track_added_during_the_load_is_not_read_as_a_displacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instrument loaded onto an audio track.

    Live creates a MIDI track for it and selects that, so the chain read afterwards
    belongs to a different track than the one read before. Comparing the two enumerates
    the first track's devices as losses that never happened. Every field derived from
    that comparison is therefore null with a reason, and the reason is the machine code
    for a selection that moved.
    """
    result = _load_with_chains(
        monkeypatch, ["Wavetable"], ["Soft Pad"], tracks_before=4, tracks_after=5
    )
    assert result["selected_track_same"] is None
    assert result["track_count_before"] == 4
    assert result["track_count_after"] == 5
    assert result["displaced"] is None
    assert result["displaced_unknown_reason"] == "selection_moved_during_load"
    assert result["configure_needed"] is None
    assert result["configure_needed_unknown_reason"] == "selection_moved_during_load"
    assert result["loaded_device_index"] is None
    evidence = [note for note in result["notes"] if "track count" in note]
    assert evidence, result["notes"]
    assert "track count 4 then 5" in evidence[0], evidence[0]
    assert result["_reads"] == [], "nothing is worth probing once identity is unsettled"


def test_no_private_attribute_is_read_during_a_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """``song.view.selected_track._live_ptr`` is not reachable and must not be tried.

    On Live 12.4.5 that path answers ``bad_path``, *segment '_live_ptr' addresses a
    private attribute*, and no value, so a comparison built on it establishes
    nothing.

    The recorded paths are what holds the line here rather than the error: the tool
    catches a failed read and reports it as unreadable, so sending the read again would
    look like a missing measurement instead of a test failure.
    """
    result = _load_with_chains(
        monkeypatch, ["Utility"], ["Utility", "Reverb"], parameter_count=33
    )
    assert result["_reads"] == ["song.view.selected_track.devices[1].parameters"]
    assert not any("_live_ptr" in path for path in result["_reads"])
    assert all(path.endswith(".parameters") for path in result["_reads"])


def test_a_renamed_selection_is_reported_as_not_known_rather_than_as_a_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename and a moved selection are the same reading from here, so both are null.

    On Live 12.4.5 an instrument load renames a track that still carries Live's
    default name. The name is the only identity this bridge has,
    so a load that renamed the track reads exactly like one that selected another
    track. Reporting ``false`` would claim to know which of the two happened.
    """
    result = _load_with_chains(
        monkeypatch, ["Wavetable"], ["Operator"], name_before="2-MIDI", name_after="Operator"
    )
    # None, and never False: False would be a claim about which of the two happened.
    assert result["selected_track_same"] is None
    assert result["displaced"] is None
    assert result["displaced_unknown_reason"] == "selection_moved_during_load"
    assert not any(note.startswith("Replaced:") for note in result["notes"])


def test_a_plugin_with_one_parameter_asks_for_configure_by_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Configure probe reads the device that arrived, not device 0 on a guess.

    A plug-in that has not been through Live's Configure mode exposes exactly one
    parameter, 'Device On'. Establishing that is worth one read and no more, and the
    read has to address the position the two chains single out.
    """
    result = _load_with_chains(
        monkeypatch, [], ["Serum"], parameter_count=1, device_class="PluginDevice"
    )
    assert result["loaded_device_index"] == 0
    assert result["configure_needed"] is True
    assert result["configure_needed_unknown_reason"] is None
    assert result["_reads"] == ["song.view.selected_track.devices[0].parameters"]
    assert "Configure" in result["next_step"]
    assert server.CONFIGURE_NOTE in result["notes"]


def test_a_device_that_came_back_configured_is_not_told_to_configure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The threshold is exactly one parameter, so two is already a configured device.

    Two is the value that matters: it is one above the line, so a rule that widened to
    'two or fewer' would send a Configure instruction, its note and its next_step to a
    device this tool read as configured, which is a claim it did not measure.
    """
    result = _load_with_chains(
        monkeypatch, [], ["Serum"], parameter_count=2, device_class="PluginDevice"
    )
    assert result["loaded_device_index"] == 0
    assert result["configure_needed"] is False
    assert result["configure_needed_unknown_reason"] is None
    assert server.CONFIGURE_NOTE not in result["notes"]
    assert "next_step" not in result
    assert result["_reads"] == ["song.view.selected_track.devices[0].parameters"]


def test_a_load_that_brought_two_devices_probes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two new devices single out none of them, and a probe would have to pick one.

    A preset can load a rack, or an instrument plus its own effect. Nothing in the two
    chains says which position is the one the answer is about, so the position is null,
    the Configure verdict is null with its own code, and no read is sent at all.
    """
    result = _load_with_chains(monkeypatch, ["Utility"], ["Utility", "Serum", "Reverb"])
    assert result["loaded_device_index"] is None
    assert result["configure_needed"] is None
    assert result["configure_needed_unknown_reason"] == "new_device_not_identified"
    assert result["_reads"] == []


@pytest.mark.parametrize("device_class", ["MaxDevice", "Device", "Eq8Device"])
def test_native_single_parameter_device_needs_no_vst_configure(
    monkeypatch: pytest.MonkeyPatch, device_class: str
) -> None:
    result = _load_with_chains(
        monkeypatch, [], ["LiveMaestro Analyzer"],
        parameter_count=1, device_class=device_class,
    )
    assert result["configure_needed"] is False
    assert "next_step" not in result
    assert server.CONFIGURE_NOTE not in result["notes"]


def test_two_devices_of_one_name_still_report_the_one_that_went(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A membership test loses this case: the survivor answers for the one that is gone.

    A track carrying two Reverbs that comes back carrying one has lost a Reverb, and
    ``'Reverb' in after`` is True either way. The comparison is a multiset.
    """
    result = _load_with_chains(monkeypatch, ["Reverb", "Reverb"], ["Reverb"])
    assert result["displaced"] == ["Reverb"]
    assert result["chain_before"] == ["Reverb", "Reverb"]
    assert result["chain_after"] == ["Reverb"]
    assert result["_reads"] == []


def test_a_track_count_that_could_not_be_settled_is_not_a_moved_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different nulls, and the code is what tells them apart.

    A count the probe could not settle is a reading this bridge failed to take. A count
    that moved is a reading it took twice and got two answers. Collapsing them into one
    reason would send a caller looking for a track that was never added.
    """
    result = _load_with_chains(
        monkeypatch, ["Wavetable"], ["Soft Pad"], tracks_after=None
    )
    assert result["track_count_after"] is None
    assert result["selected_track_same"] is None
    assert result["displaced_unknown_reason"] == "track_identity_unreadable"
    assert result["configure_needed_unknown_reason"] == "track_identity_unreadable"


def test_the_result_says_which_identity_it_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comparison a caller cannot see the terms of is a claim, not a measurement."""
    result = _load_with_chains(
        monkeypatch, ["Utility"], ["Utility", "Reverb"], parameter_count=33
    )
    assert server.TRACK_IDENTITY_NOTE in result["notes"]
    assert "no id for a track" in server.TRACK_IDENTITY_NOTE
    assert "A name is not an identity" in server.TRACK_IDENTITY_NOTE


def test_a_load_is_still_reported_when_the_state_after_it_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load that already ran may not be reported as a read failure.

    The call went to Live and Live answered it. If the read that follows times out, the
    tool still has to hand back that answer, because a caller told only "the read
    failed" has no way to know whether to load again, and loading again loads twice.
    """
    result = _load_with_chains(
        monkeypatch, ["Utility"], ["Utility"], fail_second_snapshot=True
    )
    assert result["call"]["ok"] is True
    assert result["loaded"] is True
    assert "code" not in result, "the transport error must not replace the result"
    assert result["chain_after"] == []
    assert result["displaced"] is None
    assert result["displaced_unknown_reason"] == "track_identity_unreadable"
    assert any("could not be read back after the load" in note for note in result["notes"])


def test_the_limits_resource_carries_the_class_of_each_entry() -> None:
    """``live://limits`` reports the class docs/limits.md tags an entry with.

    The document writes the tag in parentheses after the title, as in
    ``### Track reordering (LOM)``. A parser expecting another form reports every
    entry as unclassified, and the resource still answers with a full list of
    sections and titles, so nothing looks wrong from the outside.
    """
    payload = json.loads(server.limits_resource())
    entries = [entry for section in payload["sections"] for entry in section["entries"]]
    assert entries, "the limits document parsed to no entries at all"

    classified = [entry for entry in entries if entry["class"] != "unclassified"]
    assert classified, "no entry carries a class, so the heading form is not being read"

    known = set(payload["classes"])
    assert {entry["class"] for entry in classified} <= known

    titles = {entry["title"]: entry["class"] for entry in entries}
    assert titles.get("Track reordering") == "LOM"
    assert titles.get("Auditory judgment") == "physical"


# --------------------------------------------------------------------------- #
# The browser: where the catalog and the walk genuinely part company
# --------------------------------------------------------------------------- #


def test_every_root_the_tools_search_is_a_catalog_row() -> None:
    """The roots ``find_sound`` and ``load_device`` offer all exist in the catalog.

    The catalog is what says ``app.browser.sounds`` is a real path. A root offered by a
    tool and absent from the catalog would be a path the executor could not resolve.
    """
    registry = default_registry()
    known = {spec.id for spec in registry.all()}
    missing = [root for root in instruments.ROOTS if f"browser.{root}" not in known]
    assert not missing, (
        "these browser roots are offered by a tool and have no catalog row:\n  "
        + "\n  ".join(missing)
    )


def test_the_two_roots_the_catalog_has_and_the_walk_lacks_stay_out_of_the_tools() -> None:
    """``user_folders`` and ``legacy_libraries`` are verified rows the walk refuses.

    On Live 12.4.6, ``browser_walk`` with ``root="user_folders"`` answers
    ``no_such_path``, "unknown browser root 'user_folders'". Both rows are real paths
    and are reachable with ``lom_get``; what they are not is searchable. A tool
    listing them as search roots promises a search that cannot run.
    """
    registry = default_registry()
    known = {spec.id for spec in registry.all()}
    for row in ("browser.user_folders", "browser.legacy_libraries"):
        assert row in known, f"{row} was expected to stay a catalog row"
        assert row.split(".", 1)[1] not in instruments.ROOTS


def test_finding_a_sound_names_no_catalog_row_and_touches_no_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``find_sound`` reads the browser and nothing else.

    It is annotated READS, and the only way to keep that true as the tool grows is to
    make a single row read or write fail here. ``_run`` and ``_run_batch`` are how every
    other tool reaches the set, so both are made to raise.
    """
    walks: list[dict[str, Any]] = []

    class _OnlyWalks:
        def send(self, handler: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            assert handler == "browser_walk", f"find_sound sent {handler!r}"
            walks.append(dict(params or {}))
            return {
                "mode": "query",
                "status": "complete",
                "matched": 0,
                "considered": 4961,
                "truncated": False,
                "truncated_by": None,
                "unreached_roots": [],
                "count": 0,
                "matches": [],
            }

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("find_sound must not read or write the set")

    monkeypatch.setattr(server, "_client_instance", _OnlyWalks)
    monkeypatch.setattr(server, "_run", refuse)
    monkeypatch.setattr(server, "_run_batch", refuse)
    result = server.find_sound("warm bass")
    assert result["ok"] is True
    assert walks and all(walk["root"] == "sounds" for walk in walks)
