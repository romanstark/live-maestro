"""Tests for the intent-to-path lookup and the vocabulary tables behind it.

Two kinds of test live here and they fail for different reasons.

The behaviour tests pin decisions that were measured rather than chosen: that a query
naming no scope still finds the mixer fader, that a device knob is answered as a device
knob instead of with the nearest-looking row, that an impossible intent is refused. Each
states the rule it holds, so a weight can be read against the behaviour it produces.

The table tests are the ones that will actually fail one day. ``vocabulary.py`` points at
410 catalog row ids and claims 1312 producer words reach them. Rename a row and the
anchors rot silently: the lookup keeps working, just worse, and nothing says so. These
tests turn that into a red build.
"""

from __future__ import annotations

import pytest

from live_maestro import lookup
from live_maestro.registry import default_registry
from live_maestro.vocabulary import (
    BLOCKED,
    DEVICE_PARAMETER,
    INTENT_TOOL,
    SYNONYMS,
    all_anchor_ids,
)


@pytest.fixture(scope="module")
def registry():
    return default_registry()


def ids(found: lookup.Lookup) -> list[str]:
    """Every row id the answer stands for, families flattened."""
    return [row for hit in found.hits for row in hit.family]


# --------------------------------------------------------------------------- #
# The tables describe a catalog that exists
# --------------------------------------------------------------------------- #


def test_every_anchor_id_is_a_real_catalog_row(registry) -> None:
    """An anchor naming a row that is gone points the lookup at nothing.

    This is the test that catches a renamed row. The lookup would keep answering, just
    with the anchor silently dead, which is the failure mode the whole module exists to
    prevent one level up.
    """
    known = {spec.id for spec in registry.all()}
    missing = sorted(anchor for anchor in all_anchor_ids() if anchor not in known)
    assert not missing, f"vocabulary.py anchors {len(missing)} row(s) that do not exist: {missing}"


def test_no_anchor_points_at_a_row_that_does_not_work(registry) -> None:
    """A 'broken' row is one Live refuses, and sending a caller there wastes a round trip.

    ``clip.set_notes`` is the case to hold in mind: this server refuses it by design,
    because calling it opens a modal dialog inside Live.
    """
    by_id = {spec.id: spec for spec in registry.all()}
    broken = sorted(
        anchor for anchor in all_anchor_ids()
        if anchor in by_id and by_id[anchor].status.value != "verified"
    )
    assert not broken, f"anchors on rows that are not verified: {broken}"


def test_every_synonym_reaches_words_the_catalog_actually_uses(registry) -> None:
    """A catalog term that occurs in no row is dead weight in the scorer.

    The table's job is to translate producer words into words that can be matched. A
    'translation' into a word the catalog never uses matches nothing and only costs time.
    """
    zones, _rarity = lookup._index(registry)
    vocabulary: set[str] = set()
    for zone in zones:
        vocabulary |= zone.tokens
    ungrounded = sorted(
        {
            term
            for terms in SYNONYMS.values()
            for word in terms
            for term in lookup._TOKEN.findall(word.lower())
            if term not in vocabulary
        }
    )
    assert not ungrounded, f"synonym targets that occur in no catalog row: {ungrounded}"


def test_route_entries_carry_an_answer_and_evidence() -> None:
    """A route that refuses without saying why or where is worse than no route."""
    thin = [
        entry.terms[:2]
        for table in (BLOCKED, INTENT_TOOL, DEVICE_PARAMETER)
        for entry in table
        if not entry.answer.strip() or not entry.evidence.strip()
    ]
    assert not thin, f"route entries missing an answer or its evidence: {thin}"


# --------------------------------------------------------------------------- #
# Query normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("turn the bass down", ["bass"]),
        ("Solo the drum track!", ["solo", "drum", "track"]),
        ("turn the kick down", ["kick"]),
        ("the drop", ["drop"]),
        ("nudge the notes", ["notes"]),
    ],
)
def test_normalise_keeps_the_target_and_drops_the_instruction(query, expected) -> None:
    """kick, drop, take and roll are nouns in a studio and must survive.

    A general stop list holds all four as verbs. Dropping them here would delete the
    target of the sentence: "turn the kick down" would arrive carrying nothing.
    """
    assert lookup.normalise_query(query) == expected


def test_a_query_of_nothing_but_instruction_words_still_has_terms() -> None:
    """Stripping everything would search for nothing at all, so the strip backs off."""
    assert lookup.normalise_query("turn it up") != []


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_an_impossible_intent_is_refused_rather_than_approximated(registry) -> None:
    """Exporting audio has no LOM route (docs/limits.md section 1).

    Answering it with the nearest-looking rows is the worst failure available here,
    because six plausible rows read like a way to do it.
    """
    found = lookup.find(registry, "export the track to a wav")
    assert found.route == lookup.Route.BLOCKED
    assert found.reason
    assert not found.hits


def test_a_device_knob_is_answered_as_a_device_knob(registry) -> None:
    """Measured: the catalog has no row containing the word cutoff.

    It is a runtime DeviceParameter, so the honest answer names the procedure to reach
    it rather than ranking rows that cannot help.
    """
    found = lookup.find(registry, "filter cutoff on the lead")
    assert found.route == lookup.Route.DEVICE_PARAMETER
    assert "describe" in found.next_call


def test_a_broad_device_word_still_yields_to_a_row_that_fits(registry) -> None:
    """"reverb" triggers the device-parameter route and also names a real row.

    A send level is a mixer row. Only the reverb's own decay is a device parameter. The
    ambiguous half of that vocabulary is therefore consulted only after scoring, which is
    what keeps this query on the rows.
    """
    found = lookup.find(registry, "reverb send amount")
    assert found.route == lookup.Route.CATALOG
    assert any("send" in row for row in ids(found))


def test_an_intent_tool_rides_alongside_the_rows_instead_of_replacing_them(registry) -> None:
    """A tool match adds a suggestion rather than replacing the rows.

    An intent tool is a better way to do the same thing, not a different answer, so a
    query that matches one still gets the paths it asked for.
    """
    found = lookup.find(registry, "quantize the clip to sixteenths")
    assert found.route == lookup.Route.CATALOG
    assert found.hits


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_the_mirror_scopes_collapse_into_one_answer(registry) -> None:
    """track, return, master and chain carry the same property under four ids.

    Without collapsing them the first four answers are one fact shown four times, and the
    second real answer never fits on the page.
    """
    found = lookup.find(registry, "mute")
    assert found.hits
    top = found.hits[0]
    assert top.scopes, "the mute row exists on more than one scope and should say so"
    # A repeated tail is only a duplicate when both rows sit in the same mirror family.
    # drum_pad.mute and chain.mute share a name and are different things: one silences a
    # pad, the other a rack chain, and collapsing them would hide one of them.
    keys = [
        (lookup._mirror_group(hit.id.split(".", 1)[0]), hit.id.split(".", 1)[1])
        for hit in found.hits
    ]
    assert len(set(keys)) == len(keys), f"the same family appears twice: {keys}"


def test_a_rare_word_outranks_a_common_one(registry) -> None:
    """Rarity breaks a tie that an exact leaf match leaves open.

    arrangement_clip.position and master.crossfader each explain one word of
    "crossfader position", so an exact match alone cannot separate them. The
    crossfader is in a handful of rows and position is everywhere, which is what
    ranks the crossfader first.
    """
    found = lookup.find(registry, "crossfader position")
    assert any("crossfader" in row for row in ids(found)[:2])


def test_an_inflected_query_reaches_the_row_it_names(registry) -> None:
    """Producers type "mute the clip" and the row is clip.muted."""
    found = lookup.find(registry, "mute the clip")
    assert any(row.endswith((".muted", ".mute")) for row in ids(found))


def test_a_read_only_parameter_object_names_the_row_that_writes(registry) -> None:
    """track.volume answers not_settable, and track.volume_value is the number.

    Handing back the first without the second sends a caller to a path that refuses.
    """
    found = lookup.find(registry, "track volume", access="get")
    written = [hit.writes for hit in found.hits if hit.writes is not None]
    assert all(companion.endswith("_value") for companion in written)


def test_access_narrows_the_answer_to_rows_that_take_a_value(registry) -> None:
    """A caller who intends to write should not be offered read-only rows."""
    found = lookup.find(registry, "volume", access="set")
    assert found.hits
    assert all("set" in hit.access for hit in found.hits)


def test_the_answer_says_which_words_found_nothing(registry) -> None:
    """An unmatched word is usually where the query went wrong, so it is reported."""
    found = lookup.find(registry, "xyzzy volume")
    assert "xyzzy" in found.unmatched


def test_a_phrase_synonym_survives_words_in_between(registry) -> None:
    """The table says "turn it down" and a producer says "turn the bass down".

    Exact substring matching finds nothing in common between those two, so a phrase
    match has to tolerate words in between.
    """
    assert lookup._covers(["turn", "the", "bass", "down"], "turn it down")
    assert not lookup._covers(["down", "by", "the", "turn"], "turn it down")


def test_a_one_word_phrase_does_not_fire_on_every_query(registry) -> None:
    """"on this track" reduces to "track" and would otherwise fire on half the set."""
    assert not lookup._covers(["solo", "the", "track"], "on this track")


def test_a_hit_reports_what_was_proven_beside_what_is_granted(registry) -> None:
    """``access`` is what the object offers. ``proven`` is what a run demonstrated.

    A row can read ``status: verified`` and grant ``automate`` while nothing has
    ever written an envelope on it, and a caller who sees only the first two
    concludes the route works. Both belong in the answer.
    """
    found = lookup.find(registry, "master volume")
    hit = next(h for h in found.hits if h.id == "master.volume")
    assert "automate" in hit.access
    assert hit.proven == ["get"], "nothing in a probe writes an envelope"
    for other in found.hits:
        assert set(other.proven) <= set(other.access), other.id
        assert "automate" not in other.proven


def test_automating_the_main_fader_is_blocked_rather_than_pointed_at_a_row(
    registry,
) -> None:
    """The main and return tracks have no clip slots, so no envelope can live there.

    The catalog grants ``automate`` on those faders because Live can automate them,
    and this channel writes automation only as a clip envelope. Answering with the
    row sends a caller to write one where none can exist.
    """
    found = lookup.find(registry, "automate the master fader over the whole arrangement")
    assert found.route == "blocked"
    reason = found.reason or ""
    assert "no clip slots" in reason
    # A blocked answer that does not say what does work is half an answer.
    assert "write_automation" in reason
    assert "arrange" in reason


def test_blocking_that_does_not_swallow_a_clip_envelope_question(registry) -> None:
    """A blocked term is a phrase, and the matcher tolerates words in between.

    That is what makes it useful and what makes it dangerous: "master fade" as a
    term fires on "master fader level", an ordinary question about the level. Every
    query here has to reach the rows.
    """
    for query in (
        "automate the bass volume in the clip",
        "fade in the bass over four bars",
        "write a filter sweep into the clip",
        "master fader level",
        "master fader",
        "master volume",
        "turn the master down",
        "how loud is the master",
    ):
        assert lookup.find(registry, query).route != "blocked", query


def test_the_blocked_terms_that_have_to_fire(registry) -> None:
    for query in (
        "automate the master fader over the whole arrangement",
        "automate the main fader",
        "arrangement automation for the master",
        "automate a return track",
        "fade out the master over the last bars",
    ):
        assert lookup.find(registry, query).route == "blocked", query


class TestWhatChoseTheRoute:
    """A route picked by one incidental word has to say so.

    Measured case: "merge repeated notes of the same pitch into one long sustained
    chord, legato" is a question about a clip, and the word "sustained" alone sends it
    to a device envelope. The routing is unchanged; what it reports is not.
    """

    def test_a_route_names_the_words_that_selected_it(self) -> None:
        found = lookup.find(default_registry(), "make the attack snappier")
        assert found.route == "device_parameter"
        assert found.matched_on

    def test_one_word_out_of_a_long_question_is_called_thin(self) -> None:
        found = lookup.find(
            default_registry(),
            "merge repeated notes of the same pitch into one long sustained chord, legato",
        )
        assert found.matched_on == ["sustain"]
        assert any("chosen by the word" in note for note in found.notes)

    def test_a_short_question_on_its_own_subject_is_not_called_thin(self) -> None:
        """The caution is about a word lost in a sentence, not about brevity."""
        found = lookup.find(default_registry(), "filter cutoff")
        assert not any("chosen by the word" in note for note in found.notes)
