"""How ``load_device`` turns a search candidate back into a loadable item.

The search step and the load step are two separate calls, so the load step has
to find the item again from whatever handle it was given. A ``uri`` is found by
walking the browser a second time, and that walk shares the same node budget as
the search - which it can exhaust several folders short of a User Library preset,
answering ``found: false`` for an item that is plainly there.

Two things keep that from being a dead end, and these tests hold both down: the
``root`` the caller already searched is passed to the second walk so it is
narrowed the same way the first one was, and the ``item_path`` on every candidate
is not walked for at all. What the tool *says* is part of the fix: guidance that
recommends the fragile handle is how a caller ends up holding it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.types import TextContent

from live_maestro import server

ITEM_PATH = "app.browser.user_library.children[4].children[0].children[29].children[0]"


def _text_payload(result: Any) -> str:
    """Return the text a call_tool answer carries, whichever shape it arrived in."""
    blocks = result[0] if isinstance(result, tuple) else result
    first = blocks[0]
    assert isinstance(first, TextContent), (
        f"expected a text content block, got {type(first).__name__}"
    )
    return first.text


class _Browser:
    """A browser that answers a walk only when it is pointed at the right root."""

    def __init__(self, needs_root: str | None) -> None:
        self.needs_root = needs_root
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send(self, handler: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        self.sent.append((handler, payload))
        if handler != "browser_walk":
            raise AssertionError(f"unplanned handler {handler!r} with {payload!r}")
        if "uri" not in payload:
            return {
                "matches": [{"name": "LiveMaestro Analyzer", "uri": "query:x", "path": ITEM_PATH}],
                "count": 1,
                "truncated": False,
            }
        if self.needs_root is not None and payload.get("root") != self.needs_root:
            # The shape the budget failure really has: not found, and truncated
            # so the caller can tell "gave up" from "is not installed".
            return {
                "mode": "uri",
                "found": False,
                "item_path": None,
                "truncated": True,
                "truncated_by": "node_budget",
            }
        return {"mode": "uri", "found": True, "item_path": ITEM_PATH, "truncated": False}


class _SearchBrowser:
    """A browser that answers the search step with one prepared reply."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send(self, handler: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        self.sent.append((handler, payload))
        if handler != "browser_walk":
            raise AssertionError(f"unplanned handler {handler!r} with {payload!r}")
        assert "uri" not in payload, "these tests are about the search step, not a resolve"
        return self.reply


#: The handle a selected-track read answers with, in the shape that comes off the wire:
#: the key is ``path`` and it carries the property path, never ``song.tracks[N]``.
_SELECTION = {"__lom__": "Track", "path": "song.view.selected_track", "name": "Lead"}

#: One device on that track, read the same on both sides of the load. These tests are
#: about resolving the item, so the chain is held still: a chain that does not move
#: leaves ``_loaded_device_index`` at None, which is also why no parameter read is sent
#: and why the browser fakes here never have to answer a ``lom_get``.
_CHAIN = [{"__lom__": "Device", "path": "song.view.selected_track.devices[0]", "name": "Utility"}]


def _install(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser | _SearchBrowser
) -> list[str]:
    """Wire the tool to ``browser`` and record the catalog rows it calls."""
    called: list[str] = []

    def fake_run(spec_id: str, **_kw: Any) -> dict[str, Any]:
        called.append(spec_id)
        if spec_id == "view.selected_track":
            return {"ok": True, "value": dict(_SELECTION)}
        return {"ok": True}

    def fake_run_batch(
        ops: Any, **_kw: Any
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        ids = [op["id"] for op in ops]
        assert ids == ["view.selected_track", "view.selected_track_devices"], ids
        called.extend(ids)
        return [{"ok": True, "value": dict(_SELECTION)}, {"ok": True, "value": list(_CHAIN)}], None

    def fake_count(
        spec_id: str, index_arg: str, base: Any, cap: int | None = None
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        assert spec_id == "track.name", spec_id
        return {"count": 1, "exact": True, "values": ["Lead"]}, None

    monkeypatch.setattr(server, "_client_instance", lambda: browser)
    monkeypatch.setattr(server, "_run", fake_run)
    monkeypatch.setattr(server, "_run_batch", fake_run_batch)
    monkeypatch.setattr(server, "_count", fake_count)
    return called


def _call(**kw: Any) -> dict[str, Any]:
    return json.loads(_text_payload(asyncio.run(server.mcp.call_tool("load_device", kw))))


def _walks(browser: _Browser) -> list[dict[str, Any]]:
    return [payload for handler, payload in browser.sent if handler == "browser_walk"]


# ------------------------------------------------------------------ the search step


def test_the_search_step_points_the_caller_at_item_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guidance naming ``uri`` first is what hands the caller the fragile handle."""
    browser = _Browser(needs_root="user_library")
    _install(monkeypatch, browser)
    answer = _call(query="LiveMaestro", root="user_library")
    assert answer["loaded"] is False
    assert "item_path" in answer["next_step"]
    assert answer["next_step"].index("item_path") < answer["next_step"].index("uri"), (
        "the reliable handle has to be named before the one that can fail"
    )


# -------------------------------------------------------------------- the load step


def test_a_uri_is_resolved_inside_the_root_that_was_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing that made the search work must reach the second walk too."""
    browser = _Browser(needs_root="user_library")
    called = _install(monkeypatch, browser)
    answer = _call(uri="query:x", root="user_library", confirm=True)
    assert answer["ok"] is True
    assert _walks(browser)[0] == {"uri": "query:x", "root": "user_library"}
    assert "browser.load_item" in called


def test_an_unrooted_uri_walk_sends_no_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a root there is nothing to narrow with, and none may be invented."""
    browser = _Browser(needs_root=None)
    _install(monkeypatch, browser)
    answer = _call(uri="query:x", confirm=True)
    assert answer["ok"] is True
    assert _walks(browser)[0] == {"uri": "query:x"}


def test_an_item_path_is_not_walked_for_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reason to prefer item_path: no walk, so no budget to run out of."""
    browser = _Browser(needs_root="never-matches")
    called = _install(monkeypatch, browser)
    answer = _call(item_path=ITEM_PATH, confirm=True)
    assert answer["ok"] is True
    assert _walks(browser) == [], "item_path must reach the load without any walk"
    assert "browser.load_item" in called


def test_an_invalid_item_path_is_refused_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _Browser(needs_root=None)
    called = _install(monkeypatch, browser)
    answer = _call(item_path="not a lom path at all", confirm=True)
    assert answer["ok"] is False
    assert "invalid item_path" in answer["error"]
    assert "browser.load_item" not in called


# ------------------------------------------------------- what a failure has to say


def test_a_budget_failure_names_item_path_and_root_as_the_way_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error that only says what went wrong leaves the caller where they were."""
    browser = _Browser(needs_root="user_library")
    called = _install(monkeypatch, browser)
    answer = _call(uri="query:x", confirm=True)
    assert answer["ok"] is False
    assert "node_budget" in answer["error"]
    assert "item_path" in answer["error"]
    assert "root=" in answer["error"], "the remedy the caller already has must be named"
    assert answer["searched_root"] is None
    assert "browser.load_item" not in called, "a failed resolve must not load anything"


def test_a_budget_failure_with_a_root_already_given_does_not_suggest_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telling a caller to pass what they just passed reads as a broken tool."""
    browser = _Browser(needs_root="somewhere_else")
    _install(monkeypatch, browser)
    answer = _call(uri="query:x", root="user_library", confirm=True)
    assert answer["ok"] is False
    assert "item_path" in answer["error"]
    assert "root=" not in answer["error"]
    assert answer["searched_root"] == "user_library"


# ------------------------------------------- what a half finished search has to say


def _incomplete(**overrides: Any) -> dict[str, Any]:
    """A query reply that stopped short, with the parts each test varies."""
    reply: dict[str, Any] = {
        "mode": "query",
        "matches": [{"name": "LiveMaestro Analyzer", "uri": "query:x", "path": ITEM_PATH}],
        "count": 1,
        "truncated": True,
        "truncated_by": "limit",
        "unreached_roots": [],
        "status": "incomplete",
    }
    reply.update(overrides)
    return reply


def test_an_incomplete_search_warns_even_when_it_names_no_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The silent case: the budget went inside the first root, so no root is named.

    ``unreached_roots`` is empty here not because everything was searched but because
    what stopped the walk was the match limit. A warning branch that fires only on a
    non-empty list says nothing at all in exactly the case where the candidate list is
    most obviously short of the truth.
    """
    browser = _SearchBrowser(_incomplete())
    _install(monkeypatch, browser)
    answer = _call(query="LiveMaestro")
    assert answer["status"] == "incomplete"
    assert answer["unreached_roots"] == []
    assert "lower bound" in answer["next_step"]


def test_a_reply_without_a_status_key_is_not_called_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older Remote Script says nothing about coverage, and silence is not a claim.

    Live loads Remote Scripts only at startup, so an installation that predates the
    ``status`` field stays on the wire until the application is restarted. Defaulting to
    ``complete`` would let that silence license "the item is not installed".
    """
    reply = _incomplete(truncated_by="node_budget", matches=[], count=0)
    del reply["status"]
    del reply["unreached_roots"]
    browser = _SearchBrowser(reply)
    _install(monkeypatch, browser)
    answer = _call(query="LiveMaestro")
    assert answer["status"] == "incomplete"
    assert answer["unreached_roots"] == []
    assert "lower bound" in answer["next_step"]


def test_the_warning_does_not_claim_a_category_was_skipped_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named root may have been entered and left half done, and that is the point.

    ``unreached_roots`` means "not fully searched", not "never opened". A caller told a
    category was skipped entirely would reasonably search it and stop there; the truth
    is that the rest of it is still unsearched, which is why it is worth searching on a
    budget of its own.
    """
    browser = _SearchBrowser(_incomplete(unreached_roots=["packs"]))
    _install(monkeypatch, browser)
    answer = _call(query="LiveMaestro")
    assert "not fully searched" in answer["next_step"]
    assert "NOT searched" not in answer["next_step"]
    assert "packs" in answer["next_step"]
    assert "root=" in answer["next_step"]
