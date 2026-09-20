"""What ``load_device`` concludes from a ``browser_walk`` reply, on the server side.

The reply says how much of the browser the walk covered, in two fields: ``status``,
which is ``complete`` only when nothing truncated the walk and no root was left with
work pending, and ``unreached_roots``, the roots whose subtree was not fully searched,
a half searched root included. Two cases the consumer has to get right: an older
Remote Script sends neither field, and that silence must not be read as ``complete``
(Live loads Remote Scripts only at startup, so an old one stays on the wire until the
application is restarted); and a walk can exhaust its budget inside the first root it
enters, naming no root at all, so a warning that fires only on a non-empty
``unreached_roots`` is silent there.

Every tool is driven through ``server.mcp.call_tool``, so it sees the validated models
FastMCP builds rather than dictionaries a test made up, and every fake refuses a
handler or a row it was not set up for.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from typing import Any

import pytest
from mcp.types import TextContent

from live_maestro import server

REMOTE_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "live-remote-script" / "__init__.py"


def _text_payload(result: Any) -> str:
    """Return the text a call_tool answer carries, whichever shape it arrived in.

    ``call_tool`` answers either a sequence of content blocks or a
    ``(blocks, structured)`` pair depending on the version in use, and only the text
    block holds the JSON this file reads.
    """
    blocks = result[0] if isinstance(result, tuple) else result
    first = blocks[0]
    assert isinstance(first, TextContent), (
        f"expected a text content block, got {type(first).__name__}"
    )
    return first.text


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call one tool the way an MCP client does and return its decoded answer."""
    return json.loads(_text_payload(asyncio.run(server.mcp.call_tool(name, args))))


class _WalkClient:
    """A client that answers one prepared ``browser_walk`` and refuses anything else."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send(self, handler: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        self.sent.append((handler, payload))
        if handler != "browser_walk":
            raise AssertionError(f"unplanned handler {handler!r} with {payload!r}")
        return self.reply


def _install(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any]) -> _WalkClient:
    """Point the search step at ``reply`` and give it a selected track to report."""
    client = _WalkClient(reply)

    def fake_run(spec_id: str, **_kw: Any) -> dict[str, Any]:
        if spec_id != "view.selected_track":
            raise AssertionError(f"unplanned catalog row {spec_id!r} on the search step")
        return {
            "ok": True,
            "value": {"__lom__": "Track", "path": "song.view.selected_track", "name": "1-MIDI"},
        }

    monkeypatch.setattr(server, "_run", fake_run)
    monkeypatch.setattr(server, "_client_instance", lambda: client)
    return client


# --------------------------------------------------------------------------------
# The search step reports its own coverage
# --------------------------------------------------------------------------------


def test_an_exhausted_search_reports_incomplete_and_names_what_it_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty candidate list is only evidence of absence when the walk finished.

    Both fields that say whether it finished reach the caller, and the guidance names
    the remedy: search one of the unreached roots on a budget of its own.
    """
    _install(
        monkeypatch,
        {
            "mode": "query",
            "matches": [],
            "count": 0,
            "truncated": True,
            "truncated_by": "node_budget",
            "unreached_roots": ["user_library", "packs"],
            "status": "incomplete",
        },
    )
    answer = _call("load_device", {"query": "Serum"})
    assert answer["status"] == "incomplete"
    assert answer["unreached_roots"] == ["user_library", "packs"]
    assert "user_library" in answer["next_step"]
    assert "root=" in answer["next_step"], "the way out has to be named, not implied"
    assert "lower bound" in answer["next_step"]


def test_a_complete_search_with_no_matches_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished walk with no matches carries no coverage warning.

    Nothing truncated this walk and every root was searched to the end, so an empty
    candidate list means the item is not there.
    """
    _install(
        monkeypatch,
        {
            "mode": "query",
            "matches": [],
            "count": 0,
            "truncated": False,
            "truncated_by": None,
            "unreached_roots": [],
            "status": "complete",
        },
    )
    answer = _call("load_device", {"query": "Serum"})
    assert answer["status"] == "complete"
    assert answer["unreached_roots"] == []
    assert "WARNING" not in answer["next_step"]
    assert "not fully searched" not in answer["next_step"]


# --------------------------------------------------------------------------------
# The other end of the wire sends those fields
# --------------------------------------------------------------------------------


def _script_tree() -> ast.Module:
    """Parse the shipped Remote Script.

    Read rather than imported: the script runs inside Live's interpreter and imports
    ``Live`` and ``_Framework``, neither of which exists out here.
    """
    return ast.parse(REMOTE_SCRIPT.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return one function definition from a parsed module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in the Remote Script")


def _reply_keys_by_mode(func: ast.FunctionDef) -> dict[str, list[set[str]]]:
    """Map each reply mode to the key sets of the dictionaries returned under it.

    A list per mode rather than one merged set, so that a key missing from one of two
    branches cannot be supplied by the other.
    """
    out: dict[str, list[set[str]]] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        pairs = [
            (key.value, value)
            for key, value in zip(node.value.keys, node.value.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        modes = [
            value.value
            for name, value in pairs
            if name == "mode" and isinstance(value, ast.Constant)
        ]
        if not modes:
            continue
        out.setdefault(str(modes[0]), []).append({name for name, _value in pairs})
    return out


def test_the_remote_script_emits_status_and_unreached_roots() -> None:
    """``_handle_browser_walk`` puts both coverage fields on the wire.

    Server and Remote Script are deployed separately, so they drift: the server can be
    updated in place while the script Live has loaded is whatever was on disk at
    startup.
    """
    query_replies = _reply_keys_by_mode(_function(_script_tree(), "_handle_browser_walk")).get(
        "query"
    )
    assert query_replies, "no reply dictionary with mode 'query' in _handle_browser_walk"
    for keys in query_replies:
        assert "status" in keys, sorted(keys)
        assert "unreached_roots" in keys, sorted(keys)


def test_every_caller_of_browser_search_unpacks_three_values() -> None:
    """``_browser_search`` returns three values, and every call site unpacks three.

    A call site left at another arity raises inside Live, on the main thread, where
    nothing out here sees it: the reply that never arrives reads from this side as a
    timeout. So the arity is checked statically, against the script as shipped.
    """
    tree = _script_tree()
    unpacks: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not isinstance(func, ast.Attribute) or func.attr != "_browser_search":
            continue
        target = node.targets[0]
        assert isinstance(target, ast.Tuple), (
            f"line {node.lineno}: _browser_search is assigned to a single name, so a "
            "change in its arity would pass unnoticed"
        )
        unpacks.append(len(target.elts))
    assert unpacks, "no call site of _browser_search found in the Remote Script"
    assert set(unpacks) == {3}, f"call sites unpack {sorted(set(unpacks))}, expected 3"

    references = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_browser_search"
    )
    assert references == len(unpacks), (
        f"{references} references to _browser_search but only {len(unpacks)} unpacking "
        "call sites, so one of them reaches the result in a shape this test cannot see"
    )
