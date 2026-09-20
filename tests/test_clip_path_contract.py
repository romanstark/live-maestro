"""Pin the A2 clip address contract across all nine Session clip tools.

A2 gave every Session clip tool a third way to name its clip, a ``path``, and made
``track`` and ``slot`` optional beside it. These tests hold that contract on the
surface the defect was found on: the emitted MCP schema a client reads, and the
tool functions themselves.

Nothing in this file may reach Live. ``_client_instance`` is replaced for every test
with a guard that fails the test rather than opening a socket, so a refusal that is
really a round trip cannot pass unnoticed.
"""

from __future__ import annotations

import asyncio
from typing import Any, NoReturn

import pytest

from live_maestro import server

#: The nine Session clip tools A2 covers.
CLIP_TOOLS = (
    "get_clip",
    "read_clip_notes",
    "write_clip_notes",
    "read_automation",
    "write_automation",
    "clear_automation",
    "quantize_clip",
    "transpose_clip",
    "delete_clip",
)

#: A DeviceParameter path, present only so the automation tools have their own
#: required argument. It is never read, because the address refusal comes first.
MIXER_VOLUME = "song.tracks[2].mixer_device.volume"

#: An Arrangement clip address, which none of the nine may answer.
ARRANGEMENT_PATH = "song.tracks[2].arrangement_clips[0]"

#: Each clip tool with the arguments it needs beyond the address itself.
ARRANGEMENT_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("get_clip", {}),
    ("read_clip_notes", {}),
    ("write_clip_notes", {"notes": []}),
    ("read_automation", {"parameter": MIXER_VOLUME}),
    ("write_automation", {"parameter": MIXER_VOLUME, "points": [[0.0, 0.0]]}),
    ("clear_automation", {"all_envelopes": True}),
    ("quantize_clip", {}),
    ("transpose_clip", {"semitones": 1}),
    ("delete_clip", {}),
)


@pytest.fixture(autouse=True)
def no_live_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a clip tool reaches for the Live client at all."""

    def _refuse() -> NoReturn:
        pytest.fail("a clip tool reached for the Live client instead of resolving its address")

    monkeypatch.setattr(server, "_client_instance", _refuse)


def test_every_clip_tool_publishes_path_and_requires_no_session_coordinates() -> None:
    """Each of the nine offers ``path`` and makes ``track`` and ``slot`` optional.

    The assertion walks the emitted MCP schema rather than the Python signature,
    because the schema is what a client validates against and where the missing
    argument was found.
    """
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    offenders: list[str] = []
    for name in CLIP_TOOLS:
        tool = tools.get(name)
        if tool is None:
            offenders.append(f"{name}: not published as an MCP tool at all")
            continue
        schema: dict[str, Any] = tool.inputSchema
        properties: dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", []))
        if "path" not in properties:
            offenders.append(f"{name}: publishes no 'path' argument")
        for coordinate in ("track", "slot"):
            if coordinate in required:
                offenders.append(f"{name}: lists '{coordinate}' as required")
            declared = properties.get(coordinate)
            if declared is None:
                offenders.append(f"{name}: publishes no '{coordinate}' argument")
            elif declared.get("default", "<no default>") is not None:
                offenders.append(
                    f"{name}: '{coordinate}' defaults to "
                    f"{declared.get('default', '<no default>')!r} rather than None"
                )
    assert offenders == [], "clip tools that do not publish the A2 address contract: " + "; ".join(
        offenders
    )


def test_get_clip_with_no_arguments_refuses_instead_of_answering_about_track_zero() -> None:
    """A call that names no clip is refused, not resolved to track 0 slot 0."""
    res = server.get_clip()

    assert res["ok"] is False
    assert res["code"] == "missing_arguments"
    assert "Session coordinates" in res["error"]
    assert "get_clip" in res["error"]
    assert "missing_arguments" not in res["error"], (
        "the machine code belongs in 'code' and must stay out of the human sentence"
    )


def test_every_clip_tool_refuses_an_arrangement_path() -> None:
    """All nine answer an Arrangement address with region_not_addressable and a route.

    An arrangement path never resolves to a
    Session clip, and the refusal says where to read and where to write instead.
    """
    assert {name for name, _ in ARRANGEMENT_CALLS} == set(CLIP_TOOLS)

    for name, extra in ARRANGEMENT_CALLS:
        tool = getattr(server, name)
        res = tool(path=ARRANGEMENT_PATH, **extra)

        assert res["ok"] is False, name
        assert res["code"] == "region_not_addressable", name
        assert res["error"] != "region_not_addressable", (
            f"{name}: the machine code is not a human sentence"
        )
        assert server.SESSION_ARRANGEMENT_BOUNDARY_RULE in res["error"], name
        assert res["rule"] == server.SESSION_ARRANGEMENT_BOUNDARY_RULE, name
        assert "lom_get" in res["read_instead"], name
        assert "lom_batch" in res["read_instead"], name
        assert "arrange(" in res["write_instead"], name
        assert "clip_slots" in res["write_instead"], name


def test_a_session_clip_path_resolves_with_or_without_the_trailing_clip() -> None:
    """The path is matched in full, so a property path and a view path are refused.

    ``song.view.selected_track`` is the case a search rather than a match answered
    as track 3 slot 5, because the coordinates came from whatever the search found.
    """
    with_clip = server._resolve_clip_args(
        None, None, "song.tracks[3].clip_slots[5].clip", "get_clip"
    )
    assert with_clip == (3, 5)
    without_clip = server._resolve_clip_args(None, None, "song.tracks[3].clip_slots[5]", "get_clip")
    assert without_clip == (3, 5)

    on_a_property = server._resolve_clip_args(
        None, None, "song.tracks[3].clip_slots[5].clip.name", "get_clip"
    )
    assert isinstance(on_a_property, dict)
    assert on_a_property["ok"] is False
    assert on_a_property["code"] == "bad_path"

    on_a_view = server._resolve_clip_args(None, None, "song.view.selected_track", "get_clip")
    assert isinstance(on_a_view, dict)
    assert on_a_view["ok"] is False
    assert on_a_view["code"] == "bad_path"
    assert "bad_path" not in on_a_view["error"]


def test_coordinates_that_contradict_the_path_are_refused_and_agreeing_ones_are_not() -> None:
    """Coordinates beside a path must agree with it; a partial one is filled in."""
    agreeing = server._resolve_clip_args(1, 2, "song.tracks[1].clip_slots[2].clip", "get_clip")
    assert agreeing == (1, 2)

    clash = server._resolve_clip_args(0, 0, "song.tracks[1].clip_slots[2]", "get_clip")
    assert isinstance(clash, dict)
    assert clash["ok"] is False
    assert clash["code"] == "conflicting_arguments"
    assert clash["path_names"] == {"track": 1, "slot": 2}
    assert "track=0" in clash["error"]

    assert server._resolve_clip_args(1, None, "song.tracks[1].clip_slots[2]", "get_clip") == (1, 2)


def test_a_partial_clash_names_only_the_coordinate_the_call_carried() -> None:
    """A refusal never tells the caller they passed an argument they left out."""
    partial = server._resolve_clip_args(0, None, "song.tracks[1].clip_slots[2]", "get_clip")
    assert isinstance(partial, dict)
    assert partial["ok"] is False
    assert partial["code"] == "conflicting_arguments"
    assert partial["path_names"] == {"track": 1, "slot": 2}
    assert "track=0" in partial["error"]
    assert "slot=" not in partial["error"]
    assert "None" not in partial["error"]

    mirrored = server._resolve_clip_args(None, 0, "song.tracks[1].clip_slots[2]", "read_clip_notes")
    assert isinstance(mirrored, dict)
    assert mirrored["code"] == "conflicting_arguments"
    assert "slot=0" in mirrored["error"]
    assert "track=" not in mirrored["error"]
    assert "None" not in mirrored["error"]


def test_a_partial_or_absent_coordinate_is_refused_as_missing_arguments() -> None:
    """One coordinate on its own is not an address, and neither is no coordinate."""
    for track, slot in ((0, None), (None, 3), (None, None)):
        res = server._resolve_clip_args(track, slot, "", "read_clip_notes")

        assert isinstance(res, dict), (track, slot)
        assert res["ok"] is False, (track, slot)
        assert res["code"] == "missing_arguments", (track, slot)
        assert "Session coordinates" in res["error"], (track, slot)
        assert "clip_slots" in res["error"], (track, slot)
        assert "read_clip_notes" in res["error"], (track, slot)


def test_track_and_slot_still_address_the_clip_without_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The A2 change is additive: a caller that passes only coordinates still works."""
    assert server._resolve_clip_args(0, 1, "", "delete_clip") == (0, 1)

    recorded: list[tuple[str, dict[str, Any]]] = []

    def _record(spec_id: str, **kwargs: Any) -> dict[str, Any]:
        recorded.append((spec_id, kwargs))
        return {"ok": True, "value": False}

    monkeypatch.setattr(server, "_run", _record)
    res = server.get_clip(track=0, slot=1)

    assert res["ok"] is True
    assert res["has_clip"] is False
    assert res["slot"] == 1
    assert recorded, "get_clip resolved the address but read nothing"
    spec_id, kwargs = recorded[0]
    assert spec_id == "clip_slot.has_clip"
    assert kwargs["track"] == 0
    assert kwargs["slot"] == 1
