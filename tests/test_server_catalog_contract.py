"""The contract between ``server.py`` and the catalog: enforced, not assumed.

``server.py`` does not build paths. It names catalog rows by id and lets
:func:`live_maestro.executor.execute` do the rest (docs/architecture.md, 'Layers'). That
indirection is the right design, and it has one failure mode: the two halves can
drift apart silently. A row can be renamed, or lose the access the server needs,
and nothing complains until a tool fails against a running Live.

The concrete shape of that drift. ``track.volume``'s path stops at the
``DeviceParameter`` *object*, which Live refuses to write, so that row grants
only ``get`` while the companion row ``track.volume_value`` carries the write.
Point ``set_mix`` at ``track.volume`` and nothing on this side of the socket
objects: every unit test passes, ``ruff`` passes. The first run against a real
Live answers ``access_not_allowed: track.volume does not allow set``: by which
time the tool is in someone's hands. A green suite beside a tool that cannot
write is the combination these tests exist to make impossible.

These tests close that gap. They need no Live: the catalog is data on disk and
the server's mapping tables are module constants, so the check is free and runs
in CI.
"""

from __future__ import annotations

import json

import pytest

from live_maestro import server
from live_maestro.client import LiveError
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

    This is the exact check that catches the ``set_mix`` drift described in the
    module docstring: a mapping key that writes through a row granting only
    ``get``.
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

    The pair carries a real distinction and both halves are load-bearing: the
    object row is what ``automation_write`` needs for its ``parameter`` argument
    (it rejects the ``.value`` spelling: *"float is not a DeviceParameter"*,
    measured), and the ``.value`` row is the only one that can be written. Losing
    either half silently breaks one of the two uses.
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
    """Selectivity is the whole design, so it is the thing worth testing.

    A hint on every result becomes noise, and a note that is always there stops
    being read. So the interesting assertions here are the ones that expect
    nothing.
    """
    # A verified row, read normally: nothing to say.
    assert server._catalog_hint("song.tracks[0].mixer_device.volume", writing=False) is None
    assert server._catalog_hint("song.tempo", writing=False) is None
    # Off-catalog: also nothing. "No row" is the expected answer for a plug-in.
    assert server._catalog_hint("song.tracks[0].devices[1].parameters[7].x", writing=True) is None


def test_the_catalog_hint_speaks_for_a_broken_row() -> None:
    """``song.master_track.mute`` is measured broken.

    It reads like the most ordinary write in Live, and it is one of exactly three
    broken property paths.
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
    # And not a "write X instead": the script says that with the concrete index
    # (measured 2026-08-31), so repeating it from here could only be worse.
    assert "instead" not in hint["note"]


def test_a_failing_read_still_carries_the_catalog_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failing read is where the hint is worth the most.

    ``lom_get song.master_track.mute`` answers, from Live, "Main track has no
    'mute' property!", and the catalog has carried ``status: broken`` for that
    row since 2026-08-29. The hint therefore rides on the failure as well as on
    the success, and this test goes through ``lom_get`` rather than calling
    ``_catalog_hint`` directly.
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

    ``lom_get`` carries no row id, so the decoding is driven by the path. Measured
    2026-08-31: ``song.clip_trigger_quantization`` returns ``{"value": 4,
    "type": "int"}``: one integer of fourteen, meaning nothing on its own.
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
# load_device reports what it displaced
# --------------------------------------------------------------------------------


class _FakeBrowser:
    """A client whose browser always resolves the uri it is handed."""

    def send(self, handler: str, params: dict) -> dict:
        assert handler == "browser_walk"
        return {"found": True, "item_path": "app.browser.instruments.children[0]",
                "item": {"name": "Something"}}


def _load_with_chains(monkeypatch, before: list[str], after: list[str], **kwargs) -> dict:
    """Run load_device against a selection whose chain goes ``before`` -> ``after``."""
    calls: list[str] = []

    def fake_run(spec_id: str, **kw):
        calls.append(spec_id)
        if spec_id == "view.selected_track":
            return {"ok": True, "value": {"__lom__": "Track",
                                          "path": "song.view.selected_track",
                                          "name": "15 Pad Mystic"}}
        if spec_id == "view.selected_track_devices":
            names = before if calls.count("view.selected_track_devices") == 1 else after
            return {"ok": True, "value": [{"name": n} for n in names]}
        if spec_id == "browser.load_item":
            return {"ok": True}
        raise AssertionError(f"unexpected row {spec_id!r}")

    monkeypatch.setattr(server, "_run", fake_run)
    monkeypatch.setattr(server, "_client_instance", lambda: _FakeBrowser())
    result = server.load_device(uri="x", confirm=True, **kwargs)
    result["_rows_used"] = calls
    return result


def test_the_chain_is_read_through_the_selection_not_a_track_index(monkeypatch) -> None:
    """The handle Live sends carries no track index, and assuming one breaks silently.

    Measured 2026-09-01 against a running Live: ``view.selected_track`` answers
    with ``path: 'song.view.selected_track'``, the property path, and never with a
    handle shaped ``song.tracks[2]``. Parsing ``tracks[N]`` out of it therefore
    matches nothing and reports no displacement at all.

    So the row used here is the assertion: the chain is read by following the
    selection, which is also what a browser load targets.
    """
    result = _load_with_chains(monkeypatch, ["Wavetable"], ["Soft Pad"])
    assert "view.selected_track_devices" in result["_rows_used"]
    assert "device.list" not in result["_rows_used"]
    assert result["displaced"] == ["Wavetable"]


def test_a_load_that_replaces_an_instrument_says_so_in_its_own_result(monkeypatch) -> None:
    """The warning has to reach the call that does the damage, not the one before it.

    A caller may search once, do other work, and confirm later with a uri, so a
    replacement warning on the search branch alone reaches nobody at the moment
    the instrument is displaced. The confirming branch carries its own.
    """
    result = _load_with_chains(monkeypatch, ["Kit-OP 808"], ["DS Clap"])
    assert result["displaced"] == ["Kit-OP 808"]
    assert result["chain_before"] == ["Kit-OP 808"]
    assert result["chain_after"] == ["DS Clap"]
    assert result["notes"][0].startswith("Replaced:"), result["notes"][0]
    assert "Kit-OP 808" in result["notes"][0]


def test_an_effect_that_displaces_nothing_says_that_too(monkeypatch) -> None:
    """Silence would be ambiguous: nothing lost, or nothing looked at?"""
    result = _load_with_chains(monkeypatch, ["Operator"], ["Operator", "Auto Filter"])
    assert result["displaced"] == []
    assert "Nothing was displaced" in result["notes"][0]


def test_an_unreadable_chain_claims_no_displacement(monkeypatch) -> None:
    """An empty chain list means the chain was not read.

    Reporting a loss from it would invent one.
    """
    result = _load_with_chains(monkeypatch, [], [])
    assert result["displaced"] == []
    assert not any(note.startswith("Replaced:") for note in result["notes"])
    assert not any("Nothing was displaced" in note for note in result["notes"])


def test_the_item_path_route_reports_displacement_too(monkeypatch) -> None:
    """The way around the budget failure must not be a way around the report.

    Measured 2026-09-01: a deeply nested preset makes the uri lookup run out of
    node budget, and the error sends the caller to ``lom_call(app.browser,
    load_item)``, which loads the same item and says nothing about what it
    replaced. The displacement report therefore lives on this route too.
    """
    calls: list[str] = []

    def fake_run(spec_id: str, **kw):
        calls.append(spec_id)
        if spec_id == "view.selected_track":
            return {"ok": True, "value": {"path": "song.view.selected_track"}}
        if spec_id == "view.selected_track_devices":
            names = (["Wavetable", "Utility"]
                     if calls.count("view.selected_track_devices") == 1
                     else ["Soft Pad", "Utility"])
            return {"ok": True, "value": [{"name": n} for n in names]}
        if spec_id == "browser.load_item":
            return {"ok": True}
        raise AssertionError(f"unexpected row {spec_id!r}")

    class _NoWalk:
        def send(self, handler: str, params: dict) -> dict:
            raise AssertionError("item_path must not trigger a browser walk")

    monkeypatch.setattr(server, "_run", fake_run)
    monkeypatch.setattr(server, "_client_instance", lambda: _NoWalk())
    result = server.load_device(
        item_path="app.browser.instruments.children[3].children[7]", confirm=True
    )
    assert result["displaced"] == ["Wavetable"]
    assert result["notes"][0].startswith("Replaced:")


def test_a_malformed_item_path_is_refused_before_anything_is_sent(monkeypatch) -> None:
    """A path this project would not build is a caller mistake, not a load."""
    monkeypatch.setattr(server, "_run", lambda spec_id, **kw: {"ok": True, "value": None})
    monkeypatch.setattr(server, "_client_instance", lambda: None)
    result = server.load_device(item_path="not a path at all", confirm=True)
    assert result["ok"] is False
    assert "item_path" in result["error"]


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
