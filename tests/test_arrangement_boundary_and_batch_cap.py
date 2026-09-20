"""Arrangement writes and the batch ceiling.

Three subjects. The Session and Arrangement boundary, with the ``end_marker`` write
refused on the direct-call path and the MCP tool-call path alike. ``arrange`` in single
mode, whose list mode and collective verification are in
``tests/test_arrange_placements.py``. And the ``lom_batch`` ceiling of 1000 ops, which
is documented, enforced, and rejects rather than truncates.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from mcp.types import TextContent
from test_arrange_placements import FakeLive

from live_maestro import server

END_MARKER_PATH = "song.tracks[1].arrangement_clips[3].end_marker"

LIMITS_DOC = Path(__file__).resolve().parent.parent / "docs" / "limits.md"

#: A batch ceiling written as 200 anywhere in the published surface. The measurement
#: sentence legitimately says '200 ops took 500 ms' and '3200 cheap reads', so a bare
#: search for '200' cannot tell a stale cap from a timing figure.
STALE_CEILING = re.compile(
    r"(?:at most|capped at|max[_ ]?ops\s*[=:]?\s*|limit(?:ed)? (?:of|to)|ceiling of)\s*200\b"
)


def _text_payload(result: Any) -> str:
    """Return the text a call_tool answer carries, whichever shape it arrived in."""
    blocks = result[0] if isinstance(result, tuple) else result
    first = blocks[0]
    assert isinstance(first, TextContent), (
        f"expected a text content block, got {type(first).__name__}"
    )
    return first.text


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool the way an MCP client does, through FastMCP's own validation."""
    return json.loads(_text_payload(asyncio.run(server.mcp.call_tool(tool, arguments))))


class _NeverReached:
    """A client that fails the test if the socket is touched at all."""

    def batch(
        self,
        ops: Sequence[Mapping[str, Any]],
        *,
        atomic: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        raise AssertionError("the guard let the batch through to Live")

    def set(self, path: str, value: Any) -> dict[str, Any]:
        raise AssertionError("the guard let the write through to Live")


class _RecordingClient:
    """A client that records exactly what it was handed and answers plainly."""

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def batch(
        self,
        ops: Sequence[Mapping[str, Any]],
        *,
        atomic: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        recorded = list(ops)
        self.calls.append(recorded)
        return {
            "results": [
                {"status": "success", "path": op.get("path"), "value": 120.0}
                for op in recorded
            ],
            "ok_count": len(recorded),
            "error_count": 0,
        }


# ------------------------------------------------------------------------- C1
#: Each boundary refusal, as a callable that produces it. Every one of them has to
#: refuse before the socket, so the whole set runs behind _NeverReached: a guard that
#: regressed would otherwise write to the open Live set rather than fail a test.
BOUNDARY_REFUSALS = {
    "lom_set": lambda: server.lom_set(path=END_MARKER_PATH, value=16.0),
    "lom_batch": lambda: server.lom_batch(
        ops=[{"op": "set", "path": END_MARKER_PATH, "value": 32.0}]
    ),
    "resolve_clip_args": lambda: server._resolve_clip_args(
        None, None, "song.tracks[0].arrangement_clips[0]", "get_clip"
    ),
}


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        ("lom_set", "arrangement_clip_not_writable"),
        ("lom_batch", "arrangement_clip_not_writable"),
        ("resolve_clip_args", "region_not_addressable"),
    ],
)
def test_a_boundary_refusal_splits_the_code_from_the_sentence(
    monkeypatch: pytest.MonkeyPatch, refusal: str, code: str
) -> None:
    """The machine code lives in 'code', the human sentence in 'error', and 'message' is gone."""
    monkeypatch.setattr(server, "_client_instance", _NeverReached)
    res = BOUNDARY_REFUSALS[refusal]()

    assert isinstance(res, dict)
    assert res["ok"] is False
    assert res["code"] == code
    assert res["error"] != code
    assert code not in res["error"]
    assert res["error"].endswith(".")
    assert "message" not in res
    assert server.SESSION_ARRANGEMENT_BOUNDARY_RULE in res["error"]
    assert res["rule"] == server.SESSION_ARRANGEMENT_BOUNDARY_RULE


def test_lom_set_end_marker_on_arrangement_clip_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1: lom_set on arrangement_clips[i].end_marker is refused before the socket."""
    monkeypatch.setattr(server, "_client_instance", _NeverReached)
    res = server.lom_set(path=END_MARKER_PATH, value=16.0)

    assert res["ok"] is False
    assert res["code"] == "arrangement_clip_not_writable"
    # The sighting was one producer session, not a probe, and the refusal says so.
    assert "unproven" in res["error"]
    assert "Observed once" in res["error"]
    assert "message" not in res


def test_lom_batch_end_marker_rejected_on_the_direct_call_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 over the direct-call path, where ops arrive as plain dictionaries."""
    monkeypatch.setattr(server, "_client_instance", _NeverReached)
    res = server.lom_batch(ops=[{"op": "set", "path": END_MARKER_PATH, "value": 32.0}])

    assert res["ok"] is False
    assert res["code"] == "arrangement_clip_not_writable"
    assert res["error"].startswith(f"Op 0 ({END_MARKER_PATH}):")
    assert "unproven" in res["error"]
    assert res["rule"] == server.SESSION_ARRANGEMENT_BOUNDARY_RULE
    assert "message" not in res


def test_lom_batch_end_marker_rejected_through_the_mcp_call_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 over the surface a real client uses, where ops arrive as validated models.

    FastMCP hands the tool a list of :class:`toolargs.BatchOp`, and
    ``isinstance(BatchOp(...), Mapping)`` is False. A guard that reads the argument
    without normalising it first inspects nothing an MCP client ever sends, which is how
    this write reached Live while the direct-call test above passed.
    """
    monkeypatch.setattr(server, "_client_instance", _NeverReached)
    res = _call(
        "lom_batch",
        {"ops": [{"op": "set", "path": END_MARKER_PATH, "value": 32.0}]},
    )

    assert res["ok"] is False
    assert res["code"] == "arrangement_clip_not_writable"
    assert server.SESSION_ARRANGEMENT_BOUNDARY_RULE in res["error"]
    assert res["rule"] == server.SESSION_ARRANGEMENT_BOUNDARY_RULE
    assert "unproven" in res["error"]
    assert "message" not in res


def test_the_end_marker_sighting_claims_no_read_back_anywhere() -> None:
    """One sighting, one account of it: accepted, end_time unmoved, read_back unknown.

    docs/protocol.md records that what ``read_back`` answered for that write was never
    recorded. A docstring that says the write stored a value claims a read-back the
    session did not take, which is the one thing a result here may never do.
    """
    doc = server.lom_batch.__doc__ or ""
    assert "2026-09-10 against Live 12.4.5" in doc
    assert "end_time did not move" in doc
    assert "read back was never" in doc
    assert "stored a value" not in doc
    assert "stored a value" not in server.ARRANGEMENT_END_MARKER_REFUSAL
    assert "read_back" not in server.ARRANGEMENT_END_MARKER_REFUSAL
    assert "end_time did not move" in server.ARRANGEMENT_END_MARKER_REFUSAL


def test_lom_batch_hands_validated_models_to_the_client_as_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the guard inspected is what the socket receives, in one round trip."""
    client = _RecordingClient()
    monkeypatch.setattr(server, "_client_instance", lambda: client)
    ops = [{"op": "get", "path": "song.tempo"}, {"op": "get", "path": "song.tracks[0].name"}]

    res = _call("lom_batch", {"ops": ops})

    assert res["ok"] is True
    assert len(client.calls) == 1, "the batch was sent more than once"
    sent = client.calls[0]
    for op in sent:
        assert type(op) is dict, f"the client was handed {type(op).__name__}, not a dict"
    assert sent == ops


# ------------------------------------------------------------------------- C2
def test_arrange_single_mode_stays_backward_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """C2: single-clip arrange call works with track, slot, at_beat.

    The fake keeps the destination track's clips sorted by start beat, the way Live's
    own collection is ordered, and the track already holds a clip later than the
    placement. A single call that reported the collection's last entry as the clip it
    placed would answer about that other clip instead.
    """
    fake = FakeLive({0: [(96.0, 100.0)]}, clip_length=8.0)
    monkeypatch.setattr(server, "_client_instance", lambda: fake)

    res = server.arrange(track=0, slot=1, at_beat=64.0)
    assert res["ok"] is True
    assert res["verified"] is True
    assert res["at_beat"] == 64.0
    assert res["actual_beat"] == 64.0
    assert res["end_beat"] == 72.0
    assert res["destination_track"] == 0
    assert res["arrangement_clip_count_before"] == 1
    assert res["arrangement_clip_count_after"] == 2
    assert res["placed_clip"]["start_time"]["value"] == 64.0
    assert res["placed_clip"]["length"]["value"] == 8.0
    assert fake.clips[0] == [(64.0, 72.0), (96.0, 100.0)]


# ------------------------------------------------------------------------- C3
def test_lom_batch_rejects_above_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """C3: a batch over the ceiling is refused whole, and nothing reaches Live."""
    sentinel = _NeverReached()
    monkeypatch.setattr(server, "_client_instance", lambda: sentinel)
    # The sentinel only proves something if it really would have failed the test.
    with pytest.raises(AssertionError):
        sentinel.batch([{"op": "get", "path": "song.tempo"}])

    count = server.MAX_BATCH_OPS + 1
    res = server.lom_batch(ops=[{"op": "get", "path": "song.tempo"} for _ in range(count)])

    assert res["ok"] is False
    assert res["code"] == "batch_too_large"
    assert res["max_ops"] == server.MAX_BATCH_OPS == 1000
    assert res["count"] == count
    assert "1000" in res["error"]
    assert "truncat" in res["error"]
    assert "message" not in res


def test_a_batch_at_exactly_the_cap_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary the old ceiling made unreachable: exactly max_ops is sent, not refused.

    The session that motivated the list form needed 236 placements, which the previous
    ceiling of 200 refused outright.
    """
    client = _RecordingClient()
    monkeypatch.setattr(server, "_client_instance", lambda: client)
    ops = [{"op": "get", "path": "song.tempo"} for _ in range(server.MAX_BATCH_OPS)]

    res = server.lom_batch(ops=ops)

    assert res.get("code") != "batch_too_large"
    assert res["ok"] is True
    assert len(client.calls) == 1
    assert len(client.calls[0]) == server.MAX_BATCH_OPS


def test_live_limits_resource_reports_batch_ops_and_rule() -> None:
    """C3: live://limits answers the op-count question without an experiment."""
    payload = json.loads(server.limits_resource())

    assert server.MAX_BATCH_OPS == 1000
    assert payload.get("max_batch_ops") == server.MAX_BATCH_OPS
    assert server.SESSION_ARRANGEMENT_BOUNDARY_RULE in payload.get("always_true", [])


def test_the_published_cap_matches_the_constant_everywhere() -> None:
    """One number, four places: the constant, the argument schema, the docstring, the docs."""
    tools = asyncio.run(server.mcp.list_tools())
    batch_tool = next(tool for tool in tools if tool.name == "lom_batch")
    ops_description = batch_tool.inputSchema["properties"]["ops"]["description"]
    cap = str(server.MAX_BATCH_OPS)

    assert cap in ops_description
    assert cap in (batch_tool.description or "")
    assert "200" not in ops_description
    assert STALE_CEILING.search(ops_description) is None
    assert STALE_CEILING.search(batch_tool.description or "") is None

    limits = LIMITS_DOC.read_text(encoding="utf-8")
    assert f"max_ops={server.MAX_BATCH_OPS}" in limits
    assert "max_ops=200" not in limits


def test_the_batch_staleness_warning_survives_the_docstring() -> None:
    """The one paragraph that says what a stale read inside a batch looks like.

    It was deleted by commit aa1c17d with nothing failing, which is why it is pinned
    here rather than protected by a comment. Whitespace is collapsed on both sides
    because the docstring wraps the sentence across three lines.
    """
    warning = (
        "That is the failure shape to expect here. It does not raise. It returns a "
        "clean set of numbers that look like a measurement of a parameter which never "
        "changes, and the conclusion drawn from them is wrong."
    )
    tools = asyncio.run(server.mcp.list_tools())
    batch_tool = next(tool for tool in tools if tool.name == "lom_batch")

    assert " ".join(warning.split()) in " ".join((batch_tool.description or "").split())
