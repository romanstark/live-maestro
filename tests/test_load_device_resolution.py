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


def _install(monkeypatch: pytest.MonkeyPatch, browser: _Browser) -> list[str]:
    """Wire the tool to ``browser`` and record the catalog rows it calls."""
    called: list[str] = []

    def fake_run(spec_id: str, **_kw: Any) -> dict[str, Any]:
        called.append(spec_id)
        if spec_id == "view.selected_track":
            return {"ok": True, "value": {"__lom__": "Track", "name": "Lead"}}
        return {"ok": True}

    monkeypatch.setattr(server, "_client_instance", lambda: browser)
    monkeypatch.setattr(server, "_run", fake_run)
    monkeypatch.setattr(server, "_selected_chain_names", lambda: ["Utility"])
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
