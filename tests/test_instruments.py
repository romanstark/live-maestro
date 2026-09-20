"""The instrument finder against the shape the wire really has.

Every ``browser_walk`` reply below was recorded verbatim from the running Live 12.4.6 on
2026-09-17 through ``LiveClient``, entry for entry, with the match lists trimmed to the
rows a test reads and nothing else edited. A reply modelled on the implementation rather
than on the protocol would hide the shape this module must not get wrong: the key is
``path`` and not ``item_path``, ``is_loadable`` can be absent, a preset can arrive twice
under two categories, and ``status`` is a claim about coverage rather than a result.

The counts (``matched``, ``considered``) are this installation's on that day and are here
because the ranking reads them, not as facts about Live.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from live_maestro import instruments, server

# --------------------------------------------------------------------------- #
# Recorded replies
# --------------------------------------------------------------------------- #

#: One substring searched for "warm bass": a walk that examined every one of the 4961
#: items in the sounds root, finished, and found nothing, because no preset is named
#: "warm bass". This is why the phrase has to be split.
PHRASE_REPLY: dict[str, Any] = {
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


def _entry(path: str, name: str, uri: str, rank: int) -> dict[str, Any]:
    """One match, with every key the script's ``_browser_entry`` really emits."""
    return {
        "path": path,
        "name": name,
        "uri": uri,
        "is_folder": False,
        "is_device": False,
        "is_loadable": True,
        "is_selected": False,
        "child_count": 0,
        "kind": "preset",
        "match_rank": rank,
    }


WARM_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "complete",
    "matched": 16,
    "considered": 4961,
    "truncated": False,
    "truncated_by": None,
    "unreached_roots": [],
    "count": 6,
    "matches": [
        _entry("app.browser.sounds.children[14].children[382]", "Warm Saw Keys.adg",
               "query:Sounds#Synth%20Keys:FileId_4810", 2),
        _entry("app.browser.sounds.children[10].children[592]", "Warm Analog Pad.adg",
               "query:Sounds#Pad:FileId_5052", 2),
        _entry("app.browser.sounds.children[1].children[651]", "Warm Ladder Bass.adg",
               "query:Sounds#Bass:FileId_16547", 2),
        _entry("app.browser.sounds.children[12].children[239]", "Warm Glass Piano.adg",
               "query:Sounds#Piano%20&%20Keys:FileId_30712", 2),
        _entry("app.browser.sounds.children[3].children[112]", "Soft Warm Brass.adv",
               "query:Sounds#Brass:FileId_6969", 3),
        _entry("app.browser.sounds.children[0].children[211]", "Schwarm.adg",
               "query:Sounds#Ambient%20&%20Evolving:FileId_39823", 4),
    ],
}

BASS_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "complete",
    "matched": 583,
    "considered": 4961,
    "truncated": True,
    "truncated_by": "limit",
    "unreached_roots": [],
    "count": 3,
    "matches": [
        _entry("app.browser.sounds.children[1].children[75]", "Bassic.adv",
               "query:Sounds#Bass:FileId_7107", 2),
        _entry("app.browser.sounds.children[1].children[69]", "Bass Raw.adg",
               "query:Sounds#Bass:FileId_20881", 2),
        _entry("app.browser.sounds.children[10].children[47]", "Basston.adg",
               "query:Sounds#Pad:FileId_39896", 2),
    ],
}

#: "MPE Airy Pad & Sub" arrives twice, same FileId, two categories. Live files one preset
#: under two, so de-duplication has to be on identity and not on the name.
AIRY_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "complete",
    "matched": 5,
    "considered": 4961,
    "truncated": False,
    "truncated_by": None,
    "unreached_roots": [],
    "count": 3,
    "matches": [
        _entry("app.browser.sounds.children[5].children[4]", "Airy (Press Keys).adv",
               "query:Sounds#Experimental:FileId_11135", 2),
        _entry("app.browser.sounds.children[8].children[2]", "MPE Airy Pad & Sub.adg",
               "query:Sounds#MPE%20Sounds:FileId_5056", 3),
        _entry("app.browser.sounds.children[10].children[310]", "MPE Airy Pad & Sub.adg",
               "query:Sounds#Pad:FileId_5056", 3),
    ],
}

#: The instruments root, where the same walk cannot finish: 5000 nodes examined, status
#: "incomplete", and the root itself named in unreached_roots.
INSTRUMENTS_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "incomplete",
    "matched": 17,
    "considered": 5000,
    "truncated": True,
    "truncated_by": "limit",
    "unreached_roots": ["instruments"],
    "count": 2,
    "matches": [
        _entry("app.browser.instruments.children[5].children[7].children[53]",
               "Warm Analog Pad.adg", "query:Synths#Drift:Pad:FileId_5052", 2),
        _entry("app.browser.instruments.children[21].children[11].children[397]",
               "Warm Analog Pad.adg", "query:Synths#Instrument%20Rack:Pad:FileId_5052", 2),
    ],
}

EMPTY_REPLY: dict[str, Any] = {
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


def _names(result: instruments.SoundResult) -> list[str]:
    return [hit.name for hit in result.candidates]


# --------------------------------------------------------------------------- #
# The failure this package exists for
# --------------------------------------------------------------------------- #


def test_the_phrase_the_browser_cannot_answer_is_recorded_as_answering_nothing() -> None:
    """The recorded baseline says "complete" and zero, which is what looks like absence."""
    assert PHRASE_REPLY["status"] == "complete"
    assert PHRASE_REPLY["matched"] == 0
    assert PHRASE_REPLY["considered"] == 4961
    assert PHRASE_REPLY["matches"] == []


def test_splitting_the_same_phrase_finds_the_preset_the_whole_phrase_missed() -> None:
    """"warm bass" reaches "Warm Ladder Bass", which one substring never could."""
    made = instruments.plan("warm bass")
    assert list(made.terms) == ["warm", "bass"]
    result = instruments.rank(made, [WARM_REPLY, BASS_REPLY])
    assert _names(result)[0] == "Warm Ladder Bass.adg"
    top = result.candidates[0]
    assert top.matched == ["warm", "bass"]
    assert top.category == "Bass"


def test_the_word_in_the_category_counts_even_though_no_walk_searched_it() -> None:
    """"bass" answers through the uri's category on a preset the "bass" walk never saw."""
    made = instruments.plan("warm bass")
    result = instruments.rank(made, [WARM_REPLY])
    top = result.candidates[0]
    assert top.name == "Warm Ladder Bass.adg"
    assert "bass" in top.matched


def test_the_mechanism_word_in_the_users_own_question_is_not_searched_for() -> None:
    """"which instrument for a warm bass" spends its walks on "warm" and "bass"."""
    made = instruments.plan("which instrument for a warm bass")
    assert list(made.terms) == ["warm", "bass"]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_the_rarer_word_outweighs_the_common_one() -> None:
    """A preset carrying the word that narrows the search beats one carrying the common one.

    "warm" matched 16 names of 4961 and "bass" matched 583, both read off the walks
    themselves rather than written down here. Without that weighting "Bassic.adv" ties
    with "Warm Glass Piano.adg" and the order falls to the name, which is not an answer
    to "warm bass".
    """
    made = instruments.plan("warm bass")
    result = instruments.rank(made, [WARM_REPLY, BASS_REPLY], limit=20)
    scores = {hit.name: hit.score for hit in result.candidates}
    assert scores["Warm Glass Piano.adg"] > scores["Bassic.adv"]


def test_a_reply_that_counts_nothing_is_weighed_as_saying_nothing() -> None:
    """A walk without ``matched``/``considered`` must not silently weigh a term at zero."""
    stripped = dict(WARM_REPLY)
    stripped.pop("matched")
    stripped.pop("considered")
    result = instruments.rank(instruments.plan("warm bass"), [stripped, BASS_REPLY])
    assert _names(result)[0] == "Warm Ladder Bass.adg"


def test_a_name_that_is_one_word_still_answers_the_word_inside_it() -> None:
    """Every entry contains the needle by construction, so the ranker must not be stricter.

    "GlassyChord.adg" is a single token; a ranker comparing whole words only would score
    a hit the walk went and found at zero.
    """
    reply = dict(EMPTY_REPLY)
    reply["matched"] = 2
    reply["count"] = 1
    reply["matches"] = [
        _entry("app.browser.sounds.children[15].children[191]", "GlassyChord.adg",
               "query:Sounds#Synth%20Lead:FileId_39359", 2)
    ]
    result = instruments.rank(instruments.plan("glassy"), [reply])
    assert _names(result) == ["GlassyChord.adg"]


def test_an_item_that_answers_none_of_the_words_is_not_a_candidate() -> None:
    """"Schwarm" contains "warm" as letters and is ranked, "Bassic" under "glassy" is not."""
    result = instruments.rank(instruments.plan("glassy"), [WARM_REPLY])
    assert result.candidates == []


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("query:Sounds#Bass:FileId_16547", "Bass"),
        ("query:Sounds#Guitar%20&%20Plucked:FileId_4864", "Guitar & Plucked"),
        ("query:Sounds#Piano%20&%20Keys:FileId_30712", "Piano & Keys"),
        ("query:Synths#Drift:Pad:FileId_5052", "Pad"),
        ("query:Synths#Instrument%20Rack:Pad:FileId_5052", "Pad"),
        ("query:Drums#FileId_36888", None),
        ("query:Synths#Operator", None),
        ("", None),
        (None, None),
    ],
)
def test_the_category_is_read_out_of_the_uri_and_never_guessed(
    uri: str | None, expected: str | None
) -> None:
    """Percent-encoding, an ampersand, a device segment and a uri carrying no category."""
    assert instruments.category_of(uri) == expected


def test_one_preset_filed_under_two_categories_is_one_candidate() -> None:
    """Same FileId, two paths, two categories: one row, with the second under also_in."""
    result = instruments.rank(instruments.plan("airy"), [AIRY_REPLY], limit=20)
    mpe = [hit for hit in result.candidates if hit.name == "MPE Airy Pad & Sub.adg"]
    assert len(mpe) == 1
    assert sorted([mpe[0].category or ""] + mpe[0].also_in) == ["MPE Sounds", "Pad"]


def test_the_same_preset_under_two_devices_collapses_by_file_id() -> None:
    """The instruments root returns one preset twice; the FileId says it is one preset."""
    result = instruments.rank(instruments.plan("warm", root="instruments"), [INSTRUMENTS_REPLY])
    assert _names(result) == ["Warm Analog Pad.adg"]


def test_an_entry_without_a_uri_still_has_an_identity() -> None:
    """A uri-less entry falls back to its path rather than colliding with every other one."""
    first = _entry("app.browser.samples.children[1]", "Warm Thing.wav", "", 2)
    second = _entry("app.browser.samples.children[2]", "Warm Other.wav", "", 2)
    reply = dict(EMPTY_REPLY)
    reply["matched"], reply["count"], reply["matches"] = 2, 2, [first, second]
    result = instruments.rank(instruments.plan("warm", root="samples"), [reply])
    assert len(result.candidates) == 2


# --------------------------------------------------------------------------- #
# Coverage: what the answer may and may not claim
# --------------------------------------------------------------------------- #


def test_one_incomplete_walk_makes_the_whole_answer_incomplete() -> None:
    """Status is the weakest of the walks, and unreached roots are their union."""
    made = instruments.plan("warm bass")
    result = instruments.rank(made, [WARM_REPLY, INSTRUMENTS_REPLY])
    assert result.status == "incomplete"
    assert result.unreached_roots == ["instruments"]


def test_a_reply_that_names_no_status_is_not_read_as_complete() -> None:
    """A script predating the field says nothing about coverage, which is not a claim."""
    old = {k: v for k, v in WARM_REPLY.items() if k != "status"}
    old["truncated"] = True
    result = instruments.rank(instruments.plan("warm"), [old])
    assert result.status == "incomplete"


def test_every_walk_complete_is_the_only_way_to_answer_complete() -> None:
    result = instruments.rank(instruments.plan("warm bass"), [WARM_REPLY, BASS_REPLY])
    assert result.status == "complete"
    assert result.unreached_roots == []


def test_a_word_that_found_nothing_is_named_rather_than_dropped() -> None:
    made = instruments.plan("warm rhodes")
    result = instruments.rank(made, [WARM_REPLY, EMPTY_REPLY])
    assert result.terms_without_hits == ["rhodes"]


def test_an_answer_covering_only_some_words_says_so() -> None:
    result = instruments.rank(instruments.plan("warm rhodes"), [WARM_REPLY, EMPTY_REPLY])
    assert result.covered_all_terms is False
    assert result.candidates[0].matched == ["warm"]


def test_only_the_words_a_walk_was_spent_on_can_be_reported_as_finding_nothing() -> None:
    """A plan carries every word; a word that never got a walk proved nothing either way."""
    made = instruments.plan("warm bass pad lead")
    result = instruments.rank(made, [WARM_REPLY])
    assert result.terms_searched == ["warm"]
    assert result.terms_without_hits == []


# --------------------------------------------------------------------------- #
# The walk budget
# --------------------------------------------------------------------------- #


def test_a_walk_that_found_nothing_does_not_consume_the_productive_budget() -> None:
    """Otherwise the first three words win by being first, and "saw lead" never gets one."""
    made = instruments.plan("huge moog style saw lead")
    assert made.may_walk(spent=2, productive=0) is True
    assert made.may_walk(spent=3, productive=3) is False


def test_the_refund_is_bounded() -> None:
    made = instruments.plan("one two three four five six")
    assert made.may_walk(spent=instruments.WALK_CEILING, productive=0) is False


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_a_drum_word_is_answered_from_the_drums_root() -> None:
    """Recorded 2026-09-17 from Live 12.4.6: "punchy" answers "Punchy Bass" in sounds and
    "Kit-BigPunchy" at depth 3 in drums, and only one of those is a kick."""
    made = instruments.plan("punchy kick")
    assert made.root == "drums"
    assert made.depth == 3


def test_an_explicit_root_is_never_overridden() -> None:
    made = instruments.plan("punchy kick", root="sounds")
    assert made.root == "sounds"
    assert made.depth is None


def test_no_walk_is_ever_sent_unrooted() -> None:
    """An unrooted walk spends its whole node budget before reaching any root."""
    for intent in ("warm bass", "punchy kick", "anything at all"):
        assert instruments.plan(intent).walk_for("x")["root"]


def test_the_roots_offered_are_the_roots_the_script_walks() -> None:
    """A tool offering a root the walk refuses is a promise the library cannot keep.

    Recorded 2026-09-17 from Live 12.4.6: ``root="user_folders"`` answers
    ``no_such_path``.
    """
    import pathlib
    import re

    script = pathlib.Path(__file__).resolve().parents[1] / "live-remote-script" / "__init__.py"
    source = script.read_text(encoding="utf-8")
    block = re.search(r"_BROWSER_ROOTS = \((.*?)\)", source, re.DOTALL)
    assert block is not None, "the script no longer declares _BROWSER_ROOTS"
    declared = tuple(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert declared == instruments.ROOTS


# --------------------------------------------------------------------------- #
# The tool, over a client that refuses anything but a read
# --------------------------------------------------------------------------- #


class _FakeWalks:
    """A client that answers browser_walk and raises on anything else.

    An unplanned handler raises, so a single write attempt by a tool annotated READS
    fails the test rather than passing quietly.
    """

    def __init__(self, replies: dict[str, dict[str, Any]]) -> None:
        self.replies = replies
        self.sent: list[dict[str, Any]] = []

    def send(self, handler: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if handler != "browser_walk":
            raise AssertionError(f"find_sound must only walk the browser, not {handler!r}")
        payload = dict(params or {})
        self.sent.append(payload)
        return self.replies.get(str(payload.get("query")), EMPTY_REPLY)


def _tool(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    replies: dict[str, dict[str, Any]],
    **kwargs: Any,
) -> tuple[dict[str, Any], _FakeWalks]:
    fake = _FakeWalks(replies)
    monkeypatch.setattr(server, "_client_instance", lambda: fake)
    return server.find_sound(intent, **kwargs), fake


def test_the_tool_answers_the_reported_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    out, fake = _tool(
        monkeypatch, "which instrument for a warm bass",
        {"warm": WARM_REPLY, "bass": BASS_REPLY},
    )
    assert out["ok"] is True
    assert out["candidates"][0]["name"] == "Warm Ladder Bass.adg"
    assert out["words_searched"] == ["warm", "bass"]
    assert [w["query"] for w in fake.sent] == ["warm", "bass"]
    assert all(w["root"] == "sounds" for w in fake.sent)


def test_every_candidate_is_named_after_the_argument_it_feeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain from finding to loading must not break on a renamed key.

    The walk calls it ``path`` and ``load_device`` calls it ``item_path``; the candidate
    uses the name of the argument, and ``load_with`` goes further by being the argument
    dictionary itself.
    """
    out, _ = _tool(monkeypatch, "warm bass", {"warm": WARM_REPLY, "bass": BASS_REPLY})
    accepted = set(inspect.signature(server.load_device).parameters)
    for candidate in out["candidates"]:
        assert "path" not in candidate
        assert candidate["item_path"].startswith("app.browser.")
        assert set(candidate["load_with"]) <= accepted
        assert candidate["load_with"]["item_path"] == candidate["item_path"]
        assert candidate["load_with"]["confirm"] is False
        assert candidate["load_with"]["track"] == -1


def test_a_pasted_load_block_cannot_load_anything_on_its_own() -> None:
    """A block handed out ready to run is a block that runs on the wrong track.

    Both sound tools return a dictionary meant to be passed straight to ``load_device``.
    With ``confirm`` pre-set it loads on the first call, onto whatever track happened to
    be selected, replacing the instrument there. The block carries the safe values and
    the caller raises them.
    """
    signature = inspect.signature(server.load_device)
    assert signature.parameters["confirm"].default is False
    assert signature.parameters["track"].default == -1


def test_the_answer_carries_the_caveat_and_the_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ranked list with scores reads like a measurement and is not one, so the reply
    carries the caveat itself rather than leaving it in the tool's prose."""
    out, _ = _tool(monkeypatch, "warm bass", {"warm": WARM_REPLY, "bass": BASS_REPLY})
    assert out["basis"] == "name_and_category"
    assert out["caveat"] == instruments.CAVEAT
    assert "loaded, played or measured" in out["caveat"]


def test_an_incomplete_walk_is_reported_as_no_evidence_of_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _ = _tool(
        monkeypatch, "warm", {"warm": INSTRUMENTS_REPLY}, root="instruments",
    )
    assert out["status"] == "incomplete"
    assert out["unreached_roots"] == ["instruments"]
    assert "not proof" in out["status_note"]


def test_a_complete_walk_with_nothing_in_it_says_that_it_is_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _ = _tool(monkeypatch, "notaword", {})
    assert out["status"] == "complete"
    assert out["candidates"] == []
    assert "completely" in out["status_note"]


def test_an_empty_intent_is_refused_before_a_walk_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, fake = _tool(monkeypatch, "   ", {})
    assert out["ok"] is False
    assert out["code"] == "empty_intent"
    assert fake.sent == []


def test_a_word_that_found_nothing_buys_another_word_a_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, fake = _tool(
        monkeypatch, "huge moog style saw lead",
        {"saw": WARM_REPLY},
    )
    sent = [w["query"] for w in fake.sent]
    assert "saw" in sent
    assert len(sent) <= instruments.WALK_CEILING
    assert out["words_without_hits"] == ["huge", "moog", "style", "lead"]


def test_the_search_is_filtered_to_presets_unless_asked_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unfiltered, a query puts category folders at rank 0 above every preset."""
    _out, fake = _tool(monkeypatch, "warm bass", {"warm": WARM_REPLY, "bass": BASS_REPLY})
    assert all(w["kind"] == "preset" for w in fake.sent)
    _out, any_kind = _tool(
        monkeypatch, "warm", {"warm": WARM_REPLY}, kind="any",
    )
    assert all("kind" not in w for w in any_kind.sent)


def test_the_drums_root_is_walked_at_the_depth_it_completes_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the script's own default the same query exhausts the node budget."""
    _out, fake = _tool(monkeypatch, "punchy kick", {})
    assert all(w["root"] == "drums" and w["depth"] == 3 for w in fake.sent)


# --------------------------------------------------------------------------- #
# What a shortened word may and may not reach
# --------------------------------------------------------------------------- #

#: Recorded verbatim 2026-09-17, root "sounds", kind "preset", limit 60. "dark" matched
#: 41 of 4961 and the walk finished; the three rows kept here are the ones the ranking
#: has to tell apart. "strings" shortens to "str" under the catalog's own suffix list,
#: and "str" is inside "Strobe".
DARK_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "complete",
    "matched": 41,
    "considered": 4961,
    "truncated": False,
    "truncated_by": None,
    "unreached_roots": [],
    "count": 3,
    "matches": [
        _entry("app.browser.sounds.children[10].children[134]", "Darkstring.adg",
               "query:Sounds#Pad:FileId_39330", 2),
        _entry("app.browser.sounds.children[14].children[100]", "Dark Strobe.adv",
               "query:Sounds#Synth%20Keys:FileId_9802", 2),
        _entry("app.browser.sounds.children[13].children[27]", "Darkened Strings.adv",
               "query:Sounds#Strings:FileId_7180", 2),
    ],
}

#: The same walk for "strings": 62 names carry it and the list came back cut at the
#: limit, which is why "Darkstring.adg" is not in it. The browser matches one substring,
#: and "darkstring" does not contain "strings".
STRINGS_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "complete",
    "matched": 62,
    "considered": 4961,
    "truncated": True,
    "truncated_by": "limit",
    "unreached_roots": [],
    "count": 1,
    "matches": [
        _entry("app.browser.sounds.children[13].children[27]", "Darkened Strings.adv",
               "query:Sounds#Strings:FileId_7180", 3),
    ],
}

#: "trumpet" matched 18 and the walk finished. The preset a producer asking for a muted
#: trumpet wants is here and carries "Mute", not "muted", so only a shortened "muted"
#: reaches it. Live files it under two categories, same FileId.
TRUMPET_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "complete",
    "matched": 18,
    "considered": 4961,
    "truncated": False,
    "truncated_by": None,
    "unreached_roots": [],
    "count": 2,
    "matches": [
        _entry("app.browser.sounds.children[3].children[124]", "Trumpet Section Mute Legato.adg",
               "query:Sounds#Brass:FileId_28864", 2),
        _entry("app.browser.sounds.children[9].children[167]", "Trumpet Section Mute Legato.adg",
               "query:Sounds#Orchestral:FileId_28864", 2),
    ],
}

#: "piano" matched 119 and the list came back cut at the limit. "E-Piano Rhodish.adg" is
#: the row the "rhodes" walk cannot return, because no name in this root contains
#: "rhodes": the walk for it matched 0 of 4961 and finished.
PIANO_REPLY: dict[str, Any] = {
    "mode": "query",
    "status": "complete",
    "matched": 119,
    "considered": 4961,
    "truncated": True,
    "truncated_by": "limit",
    "unreached_roots": [],
    "count": 1,
    "matches": [
        _entry("app.browser.sounds.children[12].children[68]", "E-Piano Rhodish.adg",
               "query:Sounds#Piano%20&%20Keys:FileId_4921", 3),
    ],
}


def test_a_shortened_word_may_not_reach_a_name_the_whole_word_has_nothing_to_do_with(
) -> None:
    """"strings" is looked for as "string", never as "str".

    The catalog's suffix list strips "ings" off "warpings" and is right to; applied to a
    producer's noun it cuts "strings" down to "str", which sits inside "Strobe". Without
    the floor, "dark cinematic strings" answers "Dark Strobe.adv" in second place and
    reports it as matching "strings" (recorded 2026-09-17 from Live 12.4.6).
    """
    made = instruments.plan("dark strings")
    result = instruments.rank(made, [DARK_REPLY, STRINGS_REPLY], limit=20)
    by_name = {hit.name: hit for hit in result.candidates}
    assert "strings" not in by_name["Dark Strobe.adv"].matched
    # The browser's own search for "strings" cannot return the compound name
    # "Darkstring.adg", and the shortened "string" inside it can.
    assert by_name["Darkstring.adg"].matched == ["dark", "strings"]
    assert by_name["Darkened Strings.adv"].matched == ["dark", "strings"]
    assert _names(result)[0] == "Darkened Strings.adv"
    assert _names(result).index("Darkstring.adg") < _names(result).index("Dark Strobe.adv")


def test_a_word_with_nothing_longer_to_offer_is_still_shortened_to_the_stem() -> None:
    """"muted" truncates only to "mut", and "mut" reaching "Mute" is the point.

    A floor applied without this exception would answer "muted trumpet" with every
    trumpet in the library and none of the muted ones first.
    """
    made = instruments.plan("muted trumpet")
    result = instruments.rank(made, [EMPTY_REPLY, TRUMPET_REPLY], limit=20)
    assert _names(result)[0] == "Trumpet Section Mute Legato.adg"
    assert result.candidates[0].matched == ["muted", "trumpet"]


def test_a_word_the_walk_could_not_match_can_still_carry_a_candidate() -> None:
    """The reply must not deny in one field what it asserts in another.

    The walk for "rhodes" matched 0 of 4961 names and finished, so the word is listed in
    ``terms_without_hits``. "E-Piano Rhodish.adg" came back from the "piano" walk and the
    ranking reads "rhod" inside it, so the same answer shows the word matching a name.
    Both are true and the tool's own wording has to survive holding them at once.
    """
    made = instruments.plan("rhodes piano")
    result = instruments.rank(made, [EMPTY_REPLY, PIANO_REPLY], limit=20)
    assert result.terms_without_hits == ["rhodes"]
    rhodish = [hit for hit in result.candidates if hit.name == "E-Piano Rhodish.adg"]
    assert rhodish and "rhodes" in rhodish[0].matched


def test_the_answer_says_which_kind_and_which_depth_the_walks_carried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A word that matched nothing proves nothing about the kinds that were not searched.

    Recorded 2026-09-17 from Live 12.4.6: "kick" matches no preset in the drums root and
    does match a folder named "Kick" under kind="any". So the reply has to carry the
    filter it searched under, and the note about words that found nothing has to stop
    short of the whole root.
    """
    out, _ = _tool(monkeypatch, "punchy kick", {})
    assert out["kind"] == "preset"
    assert out["depth"] == 3
    assert out["words_without_hits"] == ["punchy", "kick"]
    note = out["words_without_hits_note"]
    assert "'preset'" in note and "'drums'" in note
    assert "shortened form" in note
