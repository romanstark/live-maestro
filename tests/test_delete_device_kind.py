"""``delete_device`` reaching the return and main chains, not only ``song.tracks``.

The tool used to take a bare track index, which addresses ``song.tracks`` and
nothing else. A return chain and the main chain are ordinary places to put a
device, and the only way to remove one from either was ``lom_call`` on
``delete_device`` directly - which walks straight past the ``confirm=True`` gate
and past the count check that proves the chain actually shrank. These tests hold
down that ``kind`` reaches all three, that each one names its own catalog rows,
and that the guard is not decorative on any of them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent

from live_maestro import server
from live_maestro.models import Access
from live_maestro.registry import default_registry

KINDS = ("track", "return", "master")


def _text_payload(result: Any) -> str:
    """Return the text a call_tool answer carries, whichever shape it arrived in."""
    blocks = result[0] if isinstance(result, tuple) else result
    first = blocks[0]
    assert isinstance(first, TextContent), (
        f"expected a text content block, got {type(first).__name__}"
    )
    return first.text


def _install(monkeypatch: pytest.MonkeyPatch, chain: list[str]) -> dict[str, Any]:
    """Wire the tool to a synthetic chain, recording every row id it names."""
    seen: dict[str, Any] = {"read_fields": [], "counts": [], "run": []}

    def fake_read_fields(specs: Any) -> tuple[dict[str, Any], None]:
        seen["read_fields"].append([(row_id, dict(args)) for row_id, args in specs])
        out: dict[str, Any] = {}
        for row_id, _args in specs:
            leaf = row_id.rsplit(".", 1)[1]
            if leaf in ("name", "parameter_name"):
                out["name"] = {"ok": True, "value": "Analyzer"}
            elif leaf == "class_name":
                out["class_name"] = {"ok": True, "value": "MxDeviceAudioEffect"}
        return out, None

    def fake_count(row_id: str, loop: str, args: Any) -> tuple[dict[str, Any], None]:
        seen["counts"].append((row_id, loop, dict(args)))
        if loop == "param":
            return {"count": 1, "exact": True, "values": ["Device On"]}, None
        remaining = chain if not seen["run"] else chain[:-1]
        return {"count": len(remaining), "exact": True, "values": list(remaining)}, None

    def fake_run(spec_id: str, **kw: Any) -> dict[str, Any]:
        seen["run"].append((spec_id, kw))
        return {"ok": True}

    monkeypatch.setattr(server, "_read_fields", fake_read_fields)
    monkeypatch.setattr(server, "_count", fake_count)
    monkeypatch.setattr(server, "_run", fake_run)
    return seen


def _call(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"track": 0, "device": 2}
    args.update(kw)
    return json.loads(
        _text_payload(asyncio.run(server.mcp.call_tool("delete_device", args)))
    )


# ----------------------------------------------------------------- the catalog


@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_names_a_delete_row_that_is_callable_and_destructive(kind: str) -> None:
    """The confirm gate is only worth having if it guards a destructive call.

    A ``device_delete`` pointed at a row that is not marked destructive would
    still delete the device in Live while the executor stopped demanding
    ``confirm=True`` for it, which turns the loudest guard in this server off
    without a single test going red.
    """
    row_id = server._TRACK_KINDS[kind]["device_delete"]
    spec = default_registry().get(row_id)
    assert Access.CALL in spec.access, f"{row_id!r} must grant 'call', grants {spec.access}"
    assert spec.destructive, f"{row_id!r} deletes a device and must be marked destructive"
    assert spec.method == "delete_device"


def test_the_three_kinds_name_three_different_chains() -> None:
    """One row id shared between two kinds would delete from the wrong chain."""
    rows = [server._TRACK_KINDS[kind]["device_delete"] for kind in KINDS]
    assert len(set(rows)) == 3, f"kinds must not share a delete row: {rows}"
    paths = {default_registry().get(row).path for row in rows}
    assert len(paths) == 3, f"kinds must not share a path: {sorted(paths)}"


# -------------------------------------------------------------------- the tool


def test_an_unknown_kind_never_reaches_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KindArg`` is a Literal, so the schema refuses a bad kind at the boundary.

    Worth pinning: it means a caller cannot reach a delete call with a kind the
    server has no chain for, and the refusal costs no round trip to Live.
    """
    seen = _install(monkeypatch, ["EQ Eight", "Utility", "Analyzer"])
    with pytest.raises(ToolError) as excinfo:
        _call(kind="chain", confirm=True)
    assert "'track', 'return' or 'master'" in str(excinfo.value)
    assert seen["run"] == [], "a bad kind must not reach a delete call"


def test_the_body_guards_the_kind_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Called directly, past the schema, the kind is still checked.

    ``delete_device`` is reachable as a Python function as well as a tool, and
    the schema is the only thing between the Literal and the profile lookup that
    would otherwise raise KeyError on an unknown kind.
    """
    seen = _install(monkeypatch, ["EQ Eight", "Utility", "Analyzer"])
    # Splatted so the Literal annotation does not reject the bad kind statically:
    # the point of the test is what the body does when one arrives anyway.
    bad: dict[str, Any] = {"track": 0, "device": 2, "kind": "chain", "confirm": True}
    answer = server.delete_device(**bad)
    assert answer["ok"] is False
    assert "kind must be one of" in answer["error"]
    assert seen["run"] == [], "a bad kind must not reach a delete call"


@pytest.mark.parametrize("kind", KINDS)
def test_confirm_false_deletes_nothing_on_any_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    seen = _install(monkeypatch, ["EQ Eight", "Utility", "Analyzer"])
    answer = _call(kind=kind, confirm=False)
    assert answer["ok"] is False
    assert answer["confirm_required"] is True
    assert answer["kind"] == kind
    assert answer["would_lose"]["name"] == "Analyzer"
    assert seen["run"] == [], "confirm=False must not call delete_device"


def test_a_return_delete_addresses_the_return_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, ["Delay", "Utility", "Analyzer"])
    answer = _call(track=1, kind="return", confirm=True)
    assert answer["ok"] is True
    assert answer["kind"] == "return"
    assert answer["index"] == 1
    spec_id, kwargs = seen["run"][0]
    assert spec_id == "return_device.delete"
    # The device rows take {return}; only the track-level name row takes {ret}.
    assert kwargs["return"] == 1
    assert kwargs["call_args"] == [2]
    assert "track" not in kwargs


def test_a_master_delete_carries_no_index_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main track has no index, so none may be sent and none may be reported."""
    seen = _install(monkeypatch, ["Limiter", "Utility", "Analyzer"])
    answer = _call(track=7, kind="master", confirm=True)
    assert answer["ok"] is True
    assert answer["kind"] == "master"
    assert answer["index"] is None
    assert answer["track"] is None, "a reported index the caller cannot act on is a lie"
    spec_id, kwargs = seen["run"][0]
    assert spec_id == "master_device.delete"
    assert kwargs["call_args"] == [2]
    assert "track" not in kwargs and "return" not in kwargs


def test_a_track_delete_still_behaves_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must not have moved: kind is additive, not a redesign."""
    seen = _install(monkeypatch, ["EQ Eight", "Utility", "Analyzer"])
    answer = _call(track=3, confirm=True)
    assert answer["ok"] is True
    assert answer["kind"] == "track"
    assert answer["index"] == 3
    assert answer["deleted_index"] == 2
    assert answer["device_count_before"] == 3
    assert answer["device_count_after"] == 2
    spec_id, kwargs = seen["run"][0]
    assert spec_id == "track.delete_device"
    assert kwargs["track"] == 3
    assert kwargs["call_args"] == [2]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("track", "track 0"), ("return", "return track 0"), ("master", "the main track")],
)
def test_the_loss_report_names_the_chain_in_words(
    monkeypatch: pytest.MonkeyPatch, kind: str, expected: str
) -> None:
    """'from track 0' is wrong for a return and meaningless for the main track.

    The report exists so a caller can check the target before confirming, which
    it cannot do if every kind is described as a numbered ``song.tracks`` entry.
    """
    _install(monkeypatch, ["EQ Eight", "Utility", "Analyzer"])
    answer = _call(kind=kind, confirm=False)
    assert f"from {expected} " in answer["error"], answer["error"]
