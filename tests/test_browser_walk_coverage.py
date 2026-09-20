"""What a ``browser_walk`` reply says about the part of the browser it did not search.

These tests execute the Remote Script rather than parsing it, which is the only way to
check the walk's own bookkeeping. Loading it out here needs Live's ``_Framework``
stubbed into ``sys.modules``, and a change that adds another Live-only import at module
level has to grow ``_LIVE_ONLY_MODULES`` below with it.

``status`` is ``"complete"`` only when nothing truncated the walk and no root was left
with work pending, and ``unreached_roots`` names the roots not fully searched, a root
the walk entered and left half done included. ``status`` reports coverage of the
search space, so the cuts that shorten a query's reply list rather than its walk (the
caller's ``limit``, and the ranking once collection is over) leave it ``"complete"``
and are reported by ``truncated``/``truncated_by``. A full ``match_pool`` stops the
collecting, so that walk did stop short and stays ``"incomplete"``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "live-remote-script" / "__init__.py"

#: The published wire contract this script has to be readable against.
PROTOCOL_MD = Path(__file__).resolve().parent.parent / "docs" / "protocol.md"

#: The uri of the one item both graphs hide away from a shallow walk.
TARGET_NAME = "LiveMaestro Analyzer"
TARGET_URI = "query:LiveMaestro Analyzer"

#: Everything Live provides that the script imports at module level. ``Live`` itself is
#: imported behind a ``try`` and falls back to ``None``, so only ``_Framework`` has to be
#: built here.
_LIVE_ONLY_MODULES = ("_Framework", "_Framework.ControlSurface")


# --------------------------------------------------------------------------------
# Loading the shipped script
# --------------------------------------------------------------------------------


class _ControlSurface:
    """Stand-in for Live's base class. The script subclasses it and, in these tests,
    never calls it: the handler is built with ``__new__`` so no ``c_instance`` is
    needed and no socket server is started."""

    def __init__(self, c_instance: Any) -> None:
        self._c_instance = c_instance


class _StubModule(ModuleType):
    """A stub module with its one attribute declared.

    ``ModuleType`` has no attributes of its own, so assigning one to a bare instance is
    something a type checker is right to refuse. Declaring it on a subclass keeps the
    plain assignment below readable and keeps ``pyright`` satisfied without a cast.
    """

    ControlSurface: Any


def _load_script() -> ModuleType:
    """Execute ``live-remote-script/__init__.py`` with Live's modules stubbed out.

    The stubs are removed again afterwards: this file is the only one in the suite
    that wants a ``_Framework`` on ``sys.modules``, and leaving a fake one there would
    change what an unrelated test means by "not importable outside Live".
    """
    framework = _StubModule("_Framework")
    control_surface = _StubModule("_Framework.ControlSurface")
    control_surface.ControlSurface = _ControlSurface
    framework.ControlSurface = control_surface
    installed = {"_Framework": framework, "_Framework.ControlSurface": control_surface}

    previous = {name: sys.modules.get(name) for name in _LIVE_ONLY_MODULES}
    sys.modules.update(installed)
    try:
        spec = importlib.util.spec_from_file_location("live_maestro_remote_script", SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"no import spec for {SCRIPT}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except ImportError as exc:
            raise RuntimeError(
                f"the Remote Script imports {exc.name!r} at module level and this test "
                "only stubs "
                f"{', '.join(_LIVE_ONLY_MODULES)}. Add the new module to "
                "_LIVE_ONLY_MODULES here, and check that "
                "tests/test_repository_claims.py still permits it."
            ) from exc
        return module
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


SCRIPT_MODULE = _load_script()


# --------------------------------------------------------------------------------
# A browser graph shaped like Live's own
# --------------------------------------------------------------------------------


class _Vector:
    """Stands in for a ``BrowserItemVector``: ``len()`` and ``[i]`` work, ``list()`` does
    not.

    The script's own note on :func:`sequence_items` records that a real
    ``BrowserItemVector`` answers ``len()`` and ``children[0]`` while ``list(children)``
    raises, and that one failed ``list()`` empties a whole browser level. A fake backed
    by a plain Python list would let that regression walk straight back in, so this one
    refuses iteration the same way.
    """

    def __init__(self, items: list[_Item]) -> None:
        self._items = list(items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> _Item:
        return self._items[index]

    def __iter__(self) -> Any:
        raise RuntimeError("a BrowserItemVector cannot be iterated; index it instead")


class _Item:
    """One browser item, carrying the six attributes ``_browser_entry`` reads."""

    def __init__(self, name: str, uri: str | None = None,
                 children: list[_Item] | None = None,
                 is_folder: bool = False, is_device: bool = False,
                 is_loadable: bool = False) -> None:
        self.name = name
        self.uri = uri if uri is not None else f"query:{name}"
        self.children = _Vector(children or [])
        self.is_folder = is_folder
        self.is_device = is_device
        self.is_loadable = is_loadable
        self.is_selected = False


class _Browser:
    """Live's browser with the two roots these graphs use.

    The other ten names in ``_BROWSER_ROOTS`` are simply absent, which is how the
    script discovers what an installation actually has: ``_browser_roots`` reads each
    name with a default of ``None`` and skips what is missing.
    """

    def __init__(self, instruments: _Item, user_library: _Item) -> None:
        self.instruments = instruments
        self.user_library = user_library


def _leaf(name: str, uri: str | None = None) -> _Item:
    """A loadable device with no children of its own."""
    return _Item(name, uri=uri, is_device=True, is_loadable=True)


def _folder(name: str, children: list[_Item]) -> _Item:
    return _Item(name, children=children, is_folder=True)


def _root(name: str, children: list[_Item]) -> _Item:
    """A browser root, which is an item and not a folder.

    The roots report ``is_folder=False`` and ``is_loadable=False`` and hang their
    content off ``.children``; the script's ``browser_kind`` docstring records that
    shape and has a container fallback for exactly this reason.
    """
    return _Item(name, children=children)


#: How wide the WIDE graph's first root is. Forty folders of forty items each is enough
#: that a fifty node budget cannot leave the first root, which is the case the whole
#: report exists for.
WIDE_FOLDERS = 40
WIDE_ITEMS = 40


def _wide_browser() -> _Browser:
    """A first root too big to finish, with the target in the second.

    ``instruments`` holds 40 folders of 40 loadable items. ``user_library`` holds
    ``LiveMaestro Analyzer`` four folders down. Under a 50 node budget the walk is still
    inside ``instruments`` when it stops, with ``user_library`` opened and its subtree
    barely begun, so both roots are unfinished and neither was skipped outright.
    """
    folders = [
        _folder(f"Category {folder:02d}",
                [_leaf(f"Device {folder:02d}-{item:02d}") for item in range(WIDE_ITEMS)])
        for folder in range(WIDE_FOLDERS)
    ]
    buried: _Item = _folder("Level 4", [_leaf(TARGET_NAME, uri=TARGET_URI)])
    for level in (3, 2, 1):
        buried = _folder(f"Level {level}", [buried])
    return _Browser(instruments=_root("instruments", folders),
                    user_library=_root("user_library", [buried]))


def _small_browser() -> _Browser:
    """Two shallow roots a 5000 node budget walks to the end several times over."""
    return _Browser(
        instruments=_root("instruments", [_leaf("Wavetable"), _leaf("Drift")]),
        user_library=_root("user_library",
                           [_leaf(TARGET_NAME, uri=TARGET_URI), _leaf("Operator")]),
    )


#: How many matching leaves ``_dense_browser`` hangs off its first root by default.
#: Well past ``limit`` and well short of ``_BROWSER_MATCH_POOL``, so a walk of it
#: finishes and only the reply list is cut.
DENSE_MATCHES = 30


def _dense_browser(matches: int = DENSE_MATCHES) -> _Browser:
    """One shallow root holding ``matches`` devices that all answer the same query.

    Every leaf is named ``Reverb NN``, so a search for "reverb" matches all of them
    while the graph stays small enough for a 5000 node budget to walk it several times
    over. That is the case the reply cut and the walk cut have to be told apart in: the
    caller's ``limit`` shortens the list, and nothing at all shortens the walk.
    """
    return _Browser(
        instruments=_root("instruments",
                          [_leaf(f"Reverb {index:02d}") for index in range(matches)]),
        user_library=_root("user_library", [_leaf("Operator")]),
    )


def _handler(browser: _Browser, max_nodes: int) -> Any:
    """The real handler, built without Live and pointed at ``browser``.

    ``__new__`` skips ``__init__``, which would open a socket and register listeners.
    ``_handle_browser_walk`` reaches Live through ``self._browser()`` and its budget
    through ``self._BROWSER_MAX_NODES``, and nothing else on the walk path touches the
    control surface, so those two instance attributes are the whole of the seam.
    """
    script = SCRIPT_MODULE.LiveMaestro.__new__(SCRIPT_MODULE.LiveMaestro)

    def _this_browser() -> _Browser:
        return browser

    script._browser = _this_browser
    script._BROWSER_MAX_NODES = max_nodes
    return script


def _walk(browser: _Browser, max_nodes: int, params: dict[str, Any]) -> dict[str, Any]:
    reply = _handler(browser, max_nodes)._handle_browser_walk(params)
    assert isinstance(reply, dict), f"browser_walk answered a {type(reply).__name__}"
    return reply


# --------------------------------------------------------------------------------
# A budget that ran out names what it did not search
# --------------------------------------------------------------------------------


def test_a_root_the_walk_never_searched_is_named() -> None:
    """A root the walk never searched is named among the unreached ones.

    A search without ``root`` for a device that lies only deep in ``user_library``, on a
    budget that cannot get out of ``instruments``, has to answer ``incomplete`` and name
    ``user_library`` among the roots it did not finish. Otherwise ``count: 0`` reads as
    "not installed" and the caller stops looking.

    ``considered >= 2`` is the load-bearing assertion: both roots were examined, so a
    walk that counted the roots it reached would call this complete, which is the
    failure the per node counter exists to prevent. ``instruments`` is named too, and
    correctly, because it was only half searched.
    """
    reply = _walk(_wide_browser(), 50, {"query": TARGET_NAME})

    assert reply["count"] == 0, (
        f"the target is four folders deep in user_library and this budget never leaves "
        f"instruments, so nothing should match: {reply['matches']}"
    )
    assert reply["considered"] >= 2, (
        f"considered={reply['considered']}: both roots are yielded in the first two "
        "nodes of a breadth-first walk, so anything less means the walk did not run"
    )
    assert reply["status"] == "incomplete", (
        f"status={reply['status']!r} with truncated_by={reply['truncated_by']!r}: a "
        "count of 0 from a walk that ran out of budget is a lower bound, not an answer"
    )
    assert "user_library" in reply["unreached_roots"], (
        f"unreached_roots={reply['unreached_roots']}: user_library was opened and its "
        "subtree left almost entirely unsearched, which is precisely what this field "
        "reports"
    )


def test_the_tree_mode_reports_the_roots_it_did_not_walk() -> None:
    """The tree mode answers the same question in its own shape.

    It recurses through ``_browser_children`` rather than the breadth-first frontier, so
    its rule is necessarily per root, but the reply keys and their meaning are the same:
    a caller reading one mode does not have to learn another vocabulary for the next.
    """
    reply = _walk(_wide_browser(), 50, {"depth": 4})

    assert reply["truncated"] is True, (
        "40 folders of 40 items cannot be walked four levels deep on 50 nodes"
    )
    assert reply["status"] == "incomplete", f"status={reply['status']!r}"
    assert "user_library" in reply["unreached_roots"], (
        f"unreached_roots={reply['unreached_roots']}: the budget ran out inside "
        "instruments, so user_library was never walked at all"
    )


def test_a_uri_miss_names_the_roots_it_never_finished() -> None:
    """A uri that was not reached is not a uri that does not exist.

    The item is there, four folders down. On this budget the lookup never gets near it,
    and the difference between "looked everywhere and it is absent" and "gave up" is the
    difference between a caller reporting a missing pack and a caller narrowing the
    search to one root.
    """
    reply = _walk(_wide_browser(), 50, {"uri": TARGET_URI})

    assert reply["found"] is False, "the target sits far outside a 50 node budget"
    assert reply["status"] == "incomplete", f"status={reply['status']!r}"
    assert "user_library" in reply["unreached_roots"], (
        f"unreached_roots={reply['unreached_roots']}: the uri is in user_library and "
        "that is the root the walk has to name before a caller will search it again"
    )


# --------------------------------------------------------------------------------
# A walk that finished says so
# --------------------------------------------------------------------------------


def test_a_tree_walk_that_finished_reports_complete() -> None:
    """Without this, "everything is incomplete" would satisfy the tests above."""
    reply = _walk(_small_browser(), 5000, {"depth": 3})

    assert reply["status"] == "complete", (
        f"status={reply['status']!r} with truncated_by={reply['truncated_by']!r}: six "
        "nodes on a 5000 node budget is a walk that finished"
    )
    assert reply["unreached_roots"] == [], (
        f"unreached_roots={reply['unreached_roots']}: every root was walked to the "
        "depth the caller asked for"
    )


def test_a_uri_that_was_found_reports_no_gap() -> None:
    """A hit reports ``complete`` and no unreached roots, deliberately.

    The walk stops on the item it was looking for, which leaves real work pending under
    the root that held it and under any root queued behind it. That pending work is not
    a gap in *this* answer: the question was "where is this uri", and it is answered.
    Reporting the leftovers here would make every successful lookup look partial.
    """
    reply = _walk(_small_browser(), 5000, {"uri": TARGET_URI})

    assert reply["found"] is True, f"the uri is a direct child of user_library: {reply}"
    assert reply["item"]["name"] == TARGET_NAME
    assert reply["status"] == "complete", f"status={reply['status']!r}"
    assert reply["unreached_roots"] == [], (
        f"unreached_roots={reply['unreached_roots']}: the walk stopped on its answer, "
        "so what it had not searched yet is not a shortfall"
    )


def test_a_search_that_covered_the_browser_says_so() -> None:
    """The guard on the whole report: a complete search still reports complete.

    Every other test here asks for ``incomplete`` in one situation or another, and a fix
    that answered ``incomplete`` unconditionally would pass all of them. This one fails
    the moment ``complete`` stops being reachable, and it is the only value that licenses
    a caller to conclude the device is not installed.
    """
    reply = _walk(_small_browser(), 5000, {"query": TARGET_NAME})

    assert reply["count"] == 1, f"one item carries that name: {reply['matches']}"
    assert reply["matches"][0]["name"] == TARGET_NAME
    assert reply["status"] == "complete", (
        f"status={reply['status']!r} with truncated_by={reply['truncated_by']!r}"
    )
    assert reply["unreached_roots"] == [], f"unreached_roots={reply['unreached_roots']}"


def test_a_depth_the_caller_asked_for_is_not_an_unfinished_walk() -> None:
    """Narrowing the search space is not truncating the search.

    ``depth: 1`` yields the roots and nothing below them, because that is what was
    asked for. Those nodes owe the walk nothing once they are examined, so the counter
    has to come back down for them, and the reply has to read ``complete``. Get this
    wrong and every bounded walk reports a gap that is not there, which is the fastest
    way to teach a caller to ignore the field.
    """
    reply = _walk(_small_browser(), 5000, {"query": "Operator", "depth": 1})

    assert reply["status"] == "complete", (
        f"status={reply['status']!r}: depth 1 was honoured in full, and a caller's own "
        "limit is not a shortfall"
    )
    assert reply["unreached_roots"] == [], f"unreached_roots={reply['unreached_roots']}"


def test_a_limit_that_cut_the_reply_list_did_not_cut_the_walk() -> None:
    """``limit`` shortens what comes back, never what was searched.

    Thirty devices match, the caller asked for five, and the walk examined every node
    the browser holds on a budget it barely dented. ``truncated``/``truncated_by`` are
    the fields that report the shortened list, and they do; ``status`` reports coverage
    of the search space, and this search covered all of it. Fold the one into the other
    and every ordinary ``load_device`` query - its default ``limit`` is 12 - comes back
    claiming the browser was not fully searched, which is the cry-wolf failure
    :func:`test_a_depth_the_caller_asked_for_is_not_an_unfinished_walk` exists to
    prevent. ``matched`` is the exact total here, not a lower bound.
    """
    reply = _walk(_dense_browser(), 5000, {"query": "reverb", "limit": 5})

    assert reply["count"] == 5, f"the caller asked for five: {reply['count']}"
    assert reply["matched"] == DENSE_MATCHES, (
        f"matched={reply['matched']}: the walk saw all {DENSE_MATCHES} of them and the "
        "cut happened afterwards"
    )
    assert reply["truncated"] is True, (
        "the reply list was cut, and that is what truncated reports"
    )
    assert reply["truncated_by"] == "limit", f"truncated_by={reply['truncated_by']!r}"
    assert reply["unreached_roots"] == [], (
        f"unreached_roots={reply['unreached_roots']}: both roots were searched to the "
        "end"
    )
    assert reply["budget_left"] > 0, (
        f"budget_left={reply['budget_left']}: nothing stopped this walk"
    )
    assert reply["status"] == "complete", (
        f"status={reply['status']!r} with unreached_roots={reply['unreached_roots']} "
        f"and budget_left={reply['budget_left']}: the walk covered the whole search "
        "space, so a cut reply list must not be reported as a gap in coverage"
    )


def test_a_match_pool_that_filled_is_a_walk_that_stopped() -> None:
    """The other reply cut is not the same thing, and must stay ``incomplete``.

    The pool really does end the walk: the search stops appending once it is full and
    leaves the rest of the root unexamined. So this one is a coverage shortfall, and it
    is reported as one through the root that still owes work - not through
    ``truncated_by``, which stays free to say which cut was applied. Without this test,
    "``status`` ignores ``truncated_by``" could be implemented as "``status`` ignores
    the pool too".
    """
    reply = _walk(_dense_browser(1100), 5000, {"query": "reverb", "limit": 1000})

    assert reply["matched"] == 1000, (
        f"matched={reply['matched']}: collection stops at the pool ceiling"
    )
    assert reply["truncated_by"] == "match_pool", (
        f"truncated_by={reply['truncated_by']!r}"
    )
    assert "instruments" in reply["unreached_roots"], (
        f"unreached_roots={reply['unreached_roots']}: the pool filled with a hundred "
        "leaves of that root still queued"
    )
    assert reply["status"] == "incomplete", (
        f"status={reply['status']!r}: the walk stopped short of the search space, so "
        "matched is a lower bound"
    )


# --------------------------------------------------------------------------------
# The mode that walks one folder rather than the roots
# --------------------------------------------------------------------------------


def test_the_path_mode_reports_status_without_inventing_roots() -> None:
    """``path`` mode carries ``status`` and no ``unreached_roots`` at all.

    It navigates below one named root, so there is no per-root question to ask and no
    honest list to answer it with. An empty list here would claim every root was
    searched; a full one would claim they were meant to be. ``status`` alone says the
    only thing this mode can say: whether the folder it was pointed at was walked to the
    end.
    """
    reply = _walk(_small_browser(), 5000, {"root": "user_library", "path": TARGET_NAME})

    assert reply["mode"] == "path"
    assert reply["item"]["name"] == TARGET_NAME
    assert reply["status"] == "complete", f"status={reply['status']!r}"
    assert "unreached_roots" not in reply, (
        f"unreached_roots={reply.get('unreached_roots')!r} on a walk of one named "
        "folder: this mode never looked at the roots, so it cannot report on them"
    )

    # And the other half of the mode: ``status`` is the only coverage signal it has, so
    # the budget branch of ``walk_status`` has to be exercised here or nowhere. A folder
    # of 40 items on a 5 node budget is a walk that stopped, and ``complete`` beside
    # ``truncated: True`` is the one pairing the field may never report.
    reply = _walk(_wide_browser(), 5, {"root": "instruments", "path": "Category 00"})

    assert reply["truncated"] is True, (
        f"budget_left={reply['budget_left']}: 40 items cannot be listed on 5 nodes"
    )
    assert reply["status"] == "incomplete", (
        f"status={reply['status']!r} with truncated_by={reply['truncated_by']!r}: the "
        "folder was not walked to the end, and status is all this mode can say so with"
    )
    assert "unreached_roots" not in reply, (
        f"unreached_roots={reply.get('unreached_roots')!r}: still no roots to report "
        "on, budget or no budget"
    )


# --------------------------------------------------------------------------------
# The published contract has to describe the reply this script actually sends
# --------------------------------------------------------------------------------


def _protocol_browser_walk_section() -> str:
    """The text of ``docs/protocol.md`` section 5.10, up to the next handler."""
    text = PROTOCOL_MD.read_text(encoding="utf-8")
    start = text.index("### 5.10 `browser_walk`")
    end = text.index("### 5.11", start)
    return text[start:end]


def test_the_protocol_tells_a_reader_that_a_limit_cut_leaves_status_complete() -> None:
    """A caller reading section 5.10 must not be surprised by the reply.

    :func:`test_a_limit_that_cut_the_reply_list_did_not_cut_the_walk` pins what the
    script sends: ``truncated: true`` with ``truncated_by: "limit"`` beside
    ``status: "complete"``. A document that says ``status`` is complete only when
    nothing truncated the walk describes that reply as impossible, and the caller who
    believes it reads every ordinary ``load_device`` search as self-contradictory.
    The section has to name the case, so the two fields can be told apart: ``truncated``
    is about the reply list, ``status`` is about the search space.
    """
    section = _protocol_browser_walk_section()
    sentences = [part for part in section.replace("\n", " ").split(". ") if "limit" in part]

    assert sentences, "section 5.10 never mentions limit at all"
    assert any("complete" in part for part in sentences), (
        "docs/protocol.md section 5.10 mentions limit in "
        f"{len(sentences)} sentence(s), none of which says what it does to status: "
        "the reply the script sends for a cut list pairs truncated_by 'limit' with "
        "status 'complete', and nothing here prepares a reader for that pairing"
    )


def test_the_protocol_keeps_match_pool_on_the_other_side_of_that_line() -> None:
    """The cut that looks alike and is not, named in the same place.

    :func:`test_a_match_pool_that_filled_is_a_walk_that_stopped` pins ``match_pool``
    as a walk that really stopped short. If section 5.10 excuses ``limit`` without
    separating ``match_pool`` from it, a reader generalizes the excuse to every value
    of ``truncated_by`` and stops believing ``status`` in the one case it matters.
    """
    section = _protocol_browser_walk_section()
    sentences = [
        part for part in section.replace("\n", " ").split(". ") if "match_pool" in part
    ]

    assert sentences, "section 5.10 never mentions match_pool"
    assert any("incomplete" in part for part in sentences), (
        "docs/protocol.md section 5.10 mentions match_pool but never says it leaves "
        "status incomplete, which is the half that keeps the limit exemption from "
        "reading as a blanket one"
    )


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__, "-q"]))
