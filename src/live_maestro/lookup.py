"""Intent to path lookup over the catalog: producer words in, the right mechanism out.

The catalog exists so that this server can expose a handful of tools instead of one per
Live property (docs/architecture.md). That only works if a caller can find a row without
reading the catalog: fetching the full catalog resource costs around 134,000 tokens. Scored
against 444 producer-language queries, a naive substring search over the same catalog chose
the right mechanism only 17 percent of the time. This module maps studio intent in natural
language to the right LOM mechanism in around 1.5k tokens.

Three things do the work here, in this order.

**Routing before ranking.** Not every intent is a row. Exporting audio is impossible
(docs/limits.md). A filter cutoff is a runtime ``DeviceParameter`` the catalog cannot
enumerate. Writing notes has a tool that verifies what a raw row cannot. Ranking rows for
those is worse than useless, because a plausible wrong row reads like an answer. Each is
answered as its own route with the next call named.

**Family collapse.** ``track``, ``return``, ``master`` and ``chain`` carry the same
property under four ids, and every ``clip`` row has an ``arrangement_clip`` twin. Without
collapsing them, the top eight for "mute" is two facts shown four times. Rows sharing an
id tail collapse into one hit whose ``scopes`` list the places it applies.

**Zone weighting.** A row's own name is evidence. Its prose is context. The path leaf and
the id tail score high, the doc's first sentence low, the rest of the doc almost nothing.
A row that merely mentions a word in a caveat therefore does not answer a query about it.

Pure module: no socket, no Live, no I/O beyond the registry it is handed. That keeps it
testable without a DAW, which is the standing rule for this repository (CONTRIBUTING.md).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from live_maestro.models import Access
from live_maestro.vocabulary import (
    BLOCKED,
    DEVICE_PARAMETER,
    INTENT_TOOL,
    SYNONYMS,
    RouteEntry,
)

if TYPE_CHECKING:
    from live_maestro.registry import Registry
    from live_maestro.spec import PathSpec

__all__ = [
    "DEFAULT_LIMIT",
    "Hit",
    "Lookup",
    "Route",
    "find",
    "normalise_query",
]

#: How many hits a lookup answers with unless asked otherwise. Six is what fits in about
#: 1.5k tokens with full docs, which is the budget this whole module exists to defend.
DEFAULT_LIMIT = 6

#: Words that carry no target. Dropping them is what lets "turn the bass down" score on
#: nothing but "bass", instead of matching every row whose doc contains the word "set".
_STOPWORDS = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "get", "have", "how", "i", "in", "into", "is", "it", "its", "me", "my", "of", "on",
    "or", "please", "put", "set", "the", "their", "them", "then", "there", "this", "to",
    "want", "we", "what", "when", "where", "which", "who", "why", "will", "with", "you",
    "your",
))

#: Verbs and adverbs of intent. A producer says "turn it down", "make it louder", "bump
#: the level": the instruction is in these words and the target never is.
#:
#: Four words a general stop list would hold are deliberately absent, because in a studio
#: they are nouns and dropping them would delete the target of the sentence: "kick" is a
#: drum, "drop" is a section of the arrangement, "take" is a recording, and "roll" is
#: something hats do. "turn the kick down" has to keep its kick.
_INTENT_WORDS = frozenset((
    "adjust", "bit", "bring", "change", "crank", "dial", "down", "give", "go",
    "increase", "just", "less", "lower", "make", "more", "nudge", "pull", "push",
    "raise", "reduce", "slightly", "some", "turn", "tweak", "up",
))

_TOKEN = re.compile(r"[a-z0-9]+")
_INDEX = re.compile(r"\[[^\]]*\]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

#: Suffixes stripped to match a producer's word against the catalog's inflection. A
#: producer types "mute the clip" and the row is clip.muted. "quantize" against
#: quantization, "warp" against warping, "send" against sends. Longest suffix first, and
#: nothing shorter than five characters is stemmed, which keeps "on", "eq", "arm" and
#: "bass" intact.
_SUFFIXES = ("ization", "isation", "ations", "ation", "ings", "ing", "ized", "ised",
             "izes", "ises", "ize", "ise", "ers", "er", "ed", "es", "s")


def _stem(token: str) -> str:
    """Strip one inflectional suffix, or return the token unchanged.

    >>> _stem("muted"), _stem("warping"), _stem("quantization")
    ('mut', 'warp', 'quant')
    >>> _stem("arm"), _stem("on"), _stem("bass")
    ('arm', 'on', 'bass')
    """
    if len(token) < 5:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token

# --------------------------------------------------------------------------- #
# Scoring weights.
# --------------------------------------------------------------------------- #
# Ordered by how much a match in that zone tells you about the row. A term in the path
# leaf names the thing. A term in the back half of a doc is usually a cross-reference to
# a different row, so it counts for almost nothing.

_W_LEAF_EXACT = 100.0
_W_LEAF_TOKEN = 62.0
_W_TAIL_EXACT = 92.0
_W_TAIL_TOKEN = 48.0
_W_METHOD_TOKEN = 66.0
_W_AREA = 45.0
_W_PATH_MID = 16.0
_W_DOC_FIRST = 13.0
_W_DOC_REST = 2.5

#: Added per query term beyond the first that also matched. A row explaining two of the
#: caller's words beats a row shouting about one of them. Raising this value trades
#: first-hit accuracy for third-hit accuracy.
_W_COVERAGE = 50.0

#: A synonym match is real evidence but weaker than the producer using the row's own word.
_SYNONYM_DISCOUNT = 0.82

#: A match that only survives stemming is weaker than a match on the surface form:
#: "quantize" reaching clip.quantized should not outrank a row that says quantize.
_STEM_DISCOUNT = 0.55

#: How far a term's rarity may move its weight. A term in almost every row ("track",
#: "device", "value", "position") does not narrow the answer to one row. A term in three
#: rows ("crossfader", "choke", "swing") nearly identifies one. Without this, "crossfader
#: position" scores master.crossfader and arrangement_clip.position identically, because
#: each matches one leaf exactly, and the tie falls to alphabetical order.
_IDF_FLOOR = 0.30
_IDF_CEILING = 2.10
_IDF_SCALE = 3.0

#: Below this, a hit is noise: the score comes from one stopword-adjacent term matching
#: in a doc body and nothing else.
_FLOOR = 30.0


class Route(str):
    """Which mechanism answers an intent. A str subclass so it serialises as itself."""

    CATALOG = "catalog_row"
    DEVICE_PARAMETER = "device_parameter"
    INTENT_TOOL = "intent_tool"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


@dataclass
class Hit:
    """One collapsed family of catalog rows that answers the query.

    Attributes:
        id: The best row's catalog id.
        path: Its LOM path template.
        score: Ranking score, comparable only within one result set.
        access: Access verbs the row grants.
        proven: Access verbs a run against Live actually demonstrated. Narrower than
            ``access`` wherever nothing exercised a verb, and that gap is the point:
            ``access`` says what the object offers, ``proven`` says what was seen to
            work. ``automate`` is never in here, because nothing writes an envelope
            during a probe, so a row granting it has an untested route.
        doc: The row's documentation.
        status: Catalog status of the best row.
        scopes: Other areas carrying the same property, e.g. return and master for volume.
        family: Every collapsed row id this hit stands for, the best one first. A caller
            addressing a return track needs the return id, not the track one, and a test
            asking whether the lookup found the right thing has to look here rather than
            at ``id`` alone.
        writes: A sibling row to write when the best row is a read-only object.
        matched: Query terms this hit accounted for.
    """

    id: str
    path: str
    score: float
    access: list[str]
    doc: str
    status: str
    proven: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    family: list[str] = field(default_factory=list)
    writes: str | None = None
    matched: list[str] = field(default_factory=list)


@dataclass
class Lookup:
    """The answer to one intent: a route, whatever rows apply, and what to do next."""

    query: str
    route: str
    hits: list[Hit] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    reason: str = ""
    next_call: str = ""
    tool: str = ""
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Query normalisation
# --------------------------------------------------------------------------- #


def normalise_query(query: str) -> list[str]:
    """Reduce a producer's sentence to the terms worth scoring.

    >>> normalise_query("turn the bass down")
    ['bass']
    >>> normalise_query("Solo the drum track!")
    ['solo', 'drum', 'track']
    >>> normalise_query("make it louder")
    ['louder']
    """
    tokens = _TOKEN.findall(query.lower())
    kept = [t for t in tokens if t not in _STOPWORDS and t not in _INTENT_WORDS]
    return kept or [t for t in tokens if t not in _STOPWORDS] or tokens


# --------------------------------------------------------------------------- #
# Row zones, computed once per registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Zones:
    """The searchable text of one row, split by how much each part is worth."""

    spec: Any
    area: str
    tail: str
    tail_tokens: frozenset[str]
    tail_len: int
    leaf: str
    leaf_tokens: frozenset[str]
    method_tokens: frozenset[str]
    mid_tokens: frozenset[str]
    doc_first: frozenset[str]
    doc_rest: frozenset[str]
    stems: frozenset[str]
    tokens: frozenset[str]


def _zones_for(spec: PathSpec) -> _Zones:
    """Split one catalog row into its scoring zones."""
    area, _, tail = spec.id.partition(".")
    segments = [_INDEX.sub("", s) for s in spec.path.split(".")]
    leaf = segments[-1] if segments else ""
    doc = " ".join((spec.doc or "").split())
    sentences = _SENTENCE_END.split(doc, maxsplit=1)
    first = sentences[0] if sentences else ""
    rest = sentences[1] if len(sentences) > 1 else ""
    tail_tokens = frozenset(_TOKEN.findall(tail))
    leaf_tokens = frozenset(_TOKEN.findall(leaf))
    method_tokens = frozenset(_TOKEN.findall(spec.method or ""))
    mid_tokens = frozenset(_TOKEN.findall(" ".join(segments[:-1])))
    doc_first = frozenset(_TOKEN.findall(first.lower()))
    doc_rest = frozenset(_TOKEN.findall(rest.lower()))
    named = tail_tokens | leaf_tokens | method_tokens | {area}
    return _Zones(
        spec=spec,
        area=area,
        tail=tail,
        tail_tokens=tail_tokens,
        tail_len=max(1, len(tail_tokens)),
        leaf=leaf,
        leaf_tokens=leaf_tokens,
        method_tokens=method_tokens,
        mid_tokens=mid_tokens,
        doc_first=doc_first,
        doc_rest=doc_rest,
        # Stems only cover the naming zones. Stemming the whole doc would let a caveat
        # three sentences down answer a query, which is the failure zone weighting exists
        # to prevent.
        stems=frozenset(_stem(t) for t in named),
        tokens=named | mid_tokens | doc_first | doc_rest,
    )


@lru_cache(maxsize=4)
def _index(registry: Registry) -> tuple[tuple[_Zones, ...], dict[str, float]]:
    """Return the scoring zones for every row and the rarity weight of every term."""
    zones = tuple(_zones_for(spec) for spec in registry.all())
    frequency: dict[str, int] = {}
    for zone in zones:
        for token in zone.tokens:
            frequency[token] = frequency.get(token, 0) + 1
    total = len(zones) or 1
    rarity = {
        token: min(
            _IDF_CEILING,
            max(_IDF_FLOOR, math.log((total + 1) / (df + 1)) / _IDF_SCALE),
        )
        for token, df in frequency.items()
    }
    return zones, rarity


def _term_score(zone: _Zones, term: str) -> float:
    """Return the best score this term earns anywhere in the row. The highest zone wins."""
    direct = _surface_score(zone, term)
    if direct > 0.0:
        return direct
    stem = _stem(term)
    if stem != term and stem in zone.stems:
        return _W_TAIL_TOKEN * _STEM_DISCOUNT
    if any(s == stem for s in zone.stems):
        return _W_TAIL_TOKEN * _STEM_DISCOUNT
    return 0.0


def _surface_score(zone: _Zones, term: str) -> float:
    """Score a term against the row's own words, without stemming."""
    if term == zone.leaf:
        return _W_LEAF_EXACT
    if term == zone.tail:
        return _W_TAIL_EXACT
    if term in zone.method_tokens:
        return _W_METHOD_TOKEN
    if term in zone.leaf_tokens:
        return _W_LEAF_TOKEN
    if term in zone.tail_tokens:
        return _W_TAIL_TOKEN
    if term == zone.area:
        return _W_AREA
    if term in zone.mid_tokens:
        return _W_PATH_MID
    if term in zone.doc_first:
        return _W_DOC_FIRST
    if term in zone.doc_rest:
        return _W_DOC_REST
    return 0.0


def _score_row(
    zone: _Zones,
    terms: list[str],
    expanded: dict[str, float],
    rarity: dict[str, float],
) -> tuple[float, list[str]]:
    """Score one row against the query terms. Return the score and what it matched."""
    total = 0.0
    matched: list[str] = []
    for term in terms:
        best = _term_score(zone, term)
        weight = expanded.get(term, 1.0) * rarity.get(term, 1.0)
        if best > 0.0:
            total += best * weight
            matched.append(term)
    if not matched:
        return 0.0, []
    # Coverage measures how much of the query a row explains, so only the words the
    # producer typed count. Counting injected synonyms too would reward a row matching
    # several of the generic words a synonym entry drags in ("value", "mixer_device",
    # "db") over the row matching the word that was asked.
    spoken = sum(1 for term in matched if expanded.get(term, 1.0) >= 1.0)
    total += _W_COVERAGE * max(0, spoken - 1)
    # A shorter id tail is the more central row: track.volume before track.volume_name.
    total -= (zone.tail_len - 1) * 4.0
    if zone.spec.status.value != "verified":
        total -= 12.0
    return total, matched


def _expand(
    terms: list[str], phrase: str, synonyms: dict[str, list[str]]
) -> tuple[list[str], dict[str, float]]:
    """Add catalog vocabulary for producer words, discounted against a literal match.

    Both halves matter. Single words carry the plain lookup, and most of the producer
    vocabulary in the table is phrasal: "turn it down", "how many dB", "make it mono",
    "pull the fader down". Without the phrase half, "turn the bass down" carries nothing
    but the word "bass", which names a track rather than a control.
    """
    out = list(terms)
    weights: dict[str, float] = {t: 1.0 for t in terms}

    def add(words: list[str]) -> None:
        for added in words:
            for token in _TOKEN.findall(added.lower()):
                if token not in weights:
                    out.append(token)
                    weights[token] = _SYNONYM_DISCOUNT

    for term in terms:
        add(synonyms.get(term, []))
    spoken = _TOKEN.findall(phrase)
    for key, words in synonyms.items():
        if " " in key and _covers(spoken, key):
            add(words)
    return out, weights


def _covers(spoken: list[str], key: str) -> bool:
    """Return True when the query says what a phrasal synonym says, in that order.

    Exact substring matching is too rigid for speech. The table holds "turn it down" and
    the producer types "turn the bass down", and the two share no substring. Comparing
    only the phrase's content words as an ordered subsequence bridges that and keeps the
    order,
    so "down the send" does not fire an entry meaning "send it down".

    >>> _covers(["turn", "the", "bass", "down"], "turn it down")
    True
    >>> _covers(["down", "by", "the", "turn"], "turn it down")
    False
    """
    wanted = [w for w in _TOKEN.findall(key) if w not in _STOPWORDS]
    # One content word is not a phrase. "on this track" reduces to "track" and would then
    # fire on every query mentioning a track, dragging a mixing entry's whole vocabulary
    # into an unrelated question. Single words are handled by the plain lookup anyway.
    if len(wanted) < 2:
        return False
    position = 0
    for word in wanted:
        while position < len(spoken) and spoken[position] != word:
            position += 1
        if position == len(spoken):
            return False
        position += 1
    return True


# --------------------------------------------------------------------------- #
# The lookup
# --------------------------------------------------------------------------- #


#: Asked before any row is scored. An impossible intent stays impossible however well it
#: matches a row, so this one route outranks whatever the ranking would have said.
_ROUTES_BEFORE = ((Route.BLOCKED, BLOCKED),)

#: A hit at or above this is confident enough to answer with instead of falling back to
#: the ambiguous half of the device-parameter vocabulary. Below it the rows are a guess,
#: and a guess that reads like an answer is what this module exists to stop.
_CONFIDENT = 78.0


_SplitRoutes = tuple[tuple["RouteEntry", ...], tuple["RouteEntry", ...]]


@lru_cache(maxsize=4)
def _split_device_terms(registry: Registry) -> _SplitRoutes:
    """Split the device-parameter route by whether its triggers name a catalog row.

    The 181 triggers are not equal. "cutoff", "resonance" and "makeup" appear nowhere in
    any row's id, path or method, so a query carrying one cannot be about a catalog row
    and the route can answer immediately. "reverb", "filter" and "delay" do appear in row
    names (hybrid_reverb, browser.filter_type), so a query carrying one might well be
    about a row, and the route has to wait until scoring has had its turn. Deciding this
    from the catalog rather than from a hand-kept list means it stays right as the
    catalog grows.
    """
    zones, _rarity = _index(registry)
    named: set[str] = set()
    for zone in zones:
        # The area is tokenised, not taken whole: the area of the Hybrid Reverb rows is
        # "hybrid_reverb", and unsplit it hides the word "reverb" from this check, which
        # would then treat "reverb" as a word no row can be about.
        named |= zone.tail_tokens | zone.leaf_tokens | zone.method_tokens
        named |= set(_TOKEN.findall(zone.area))
    specific: list[RouteEntry] = []
    ambiguous: list[RouteEntry] = []
    for entry in DEVICE_PARAMETER:
        unnamed = tuple(
            t for t in entry.terms
            if not any(w in named or _stem(w) in named for w in _TOKEN.findall(t))
        )
        if unnamed:
            specific.append(RouteEntry(unnamed, entry.answer, entry.evidence, entry.next_call))
        rest = tuple(t for t in entry.terms if t not in unnamed)
        if rest:
            ambiguous.append(RouteEntry(rest, entry.answer, entry.evidence, entry.next_call))
    return tuple(specific), tuple(ambiguous)


def _route(terms: list[str], phrase: str, tables) -> Lookup | None:
    """Answer an intent that rows cannot answer, using one of the route tables."""
    for route, table in tables:
        for entry in table:
            if not _entry_matches(entry.terms, terms, phrase):
                continue
            return Lookup(
                query=phrase,
                route=route,
                terms=terms,
                reason=entry.answer,
                next_call=entry.next_call,
                notes=[entry.evidence] if entry.evidence else [],
            )
    return None


def _entry_matches(entry_terms: tuple[str, ...], terms: list[str], phrase: str) -> bool:
    """Return True when the caller's words carry one of this entry's triggers.

    A multi-word trigger has to appear as a phrase. A single word may match a term or its
    stem. Matching a multi-word trigger term by term would fire "export the audio" on any
    sentence containing "audio", which is most of them.
    """
    stems = {_stem(t) for t in terms}
    present = set(terms) | stems
    for trigger in entry_terms:
        if " " in trigger:
            if trigger in phrase:
                return True
        elif trigger in present or _stem(trigger) in present:
            return True
    return False


def find(
    registry: Registry,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    area: str | None = None,
    access: str | None = None,
    synonyms: dict[str, list[str]] | None = None,
) -> Lookup:
    """Answer one intent with a route and, where rows are the answer, ranked rows.

    Args:
        registry: The loaded catalog.
        query: The caller's intent in their own words.
        limit: Maximum hits to return.
        area: Restrict to one catalog area, e.g. 'clip'.
        access: Restrict to rows granting this verb, e.g. 'set'.
        synonyms: Producer word to catalog words, applied before scoring.

    Returns:
        A Lookup carrying the route, the hits, and the terms that found nothing.
    """
    terms = normalise_query(query)
    phrase = " ".join(_TOKEN.findall(query.lower()))
    routed = _route(terms, phrase, _ROUTES_BEFORE)
    if routed is not None:
        return routed
    specific, ambiguous = _split_device_terms(registry)
    routed = _route(terms, phrase, ((Route.DEVICE_PARAMETER, specific),))
    if routed is not None:
        return routed
    expanded, weights = _expand(terms, phrase, SYNONYMS if synonyms is None else synonyms)

    wanted = Access(access) if access else None
    zones, rarity = _index(registry)
    scored: list[tuple[float, _Zones, list[str]]] = []
    for zone in zones:
        if area is not None and zone.area != area:
            continue
        if wanted is not None and wanted not in zone.spec.access:
            continue
        score, matched = _score_row(zone, expanded, weights, rarity)
        if score >= _FLOOR:
            scored.append((score, zone, matched))

    scored.sort(key=lambda t: (-t[0], t[1].spec.id))
    hits = _collapse(scored, limit)
    matched_terms = {m for _s, _z, ms in scored for m in ms}

    if not hits or hits[0].score < _CONFIDENT:
        routed = _route(terms, phrase, ((Route.DEVICE_PARAMETER, ambiguous),))
        if routed is not None:
            routed.hits = hits
            routed.unmatched = [t for t in terms if t not in matched_terms]
            return routed

    tool = _route(terms, phrase, ((Route.INTENT_TOOL, INTENT_TOOL),))
    return Lookup(
        query=query,
        route=Route.CATALOG if hits else Route.UNRESOLVED,
        hits=hits,
        # An intent tool is a better way to do the same thing, not a different answer, so
        # it rides alongside the rows instead of hiding them. Routing to it terminally
        # would replace the path a third of all queries ask for with a tool name.
        tool=(tool.reason + (" " + tool.next_call if tool.next_call else "")) if tool else "",
        terms=terms,
        unmatched=[t for t in terms if t not in matched_terms],
        reason=(
            ""
            if hits
            else "No catalog row matches these words. The catalog covers Live's fixed "
            "object model. A knob on a device is not in it."
        ),
        next_call=(
            ""
            if hits
            else "get_devices on the track, then describe(path, with_parameters=True) to "
            "find the parameter index, then set_parameter."
        ),
    )


#: Areas that mirror one another. A property on one usually exists on the others, and
#: showing all of them is four ways of saying the same thing.
_MIRRORS = (
    frozenset({"track", "return", "master", "chain"}),
    frozenset({"clip", "arrangement_clip"}),
    frozenset({"device", "return_device", "master_device"}),
    frozenset({"drum_pad", "rack_drum_pad"}),
)


def _mirror_group(area: str) -> int:
    """Return the mirror family an area belongs to, or -1 when it stands alone."""
    for i, group in enumerate(_MIRRORS):
        if area in group:
            return i
    return -1


def _collapse(
    scored: list[tuple[float, _Zones, list[str]]], limit: int
) -> list[Hit]:
    """Fold mirrored rows into one hit, and fuse a read-only object with its value row."""
    best: dict[tuple[int, str], tuple[float, _Zones, list[str]]] = {}
    order: list[tuple[int, str]] = []
    extra_scopes: dict[tuple[int, str], list[str]] = {}
    members: dict[tuple[int, str], list[str]] = {}

    for score, zone, matched in scored:
        group = _mirror_group(zone.area)
        key = (group, zone.tail) if group >= 0 else (-1, zone.spec.id)
        if key not in best:
            best[key] = (score, zone, matched)
            order.append(key)
            extra_scopes[key] = []
            members[key] = [zone.spec.id]
        else:
            other = best[key][1].area
            members[key].append(zone.spec.id)
            if zone.area not in extra_scopes[key] and zone.area != other:
                extra_scopes[key].append(zone.area)

    hits: list[Hit] = []
    for key in order[: limit * 2]:
        score, zone, matched = best[key]
        spec = zone.spec
        hits.append(
            Hit(
                id=spec.id,
                path=spec.path,
                score=round(score, 1),
                access=[a.value for a in spec.access],
                proven=[a.value for a in spec.proven],
                doc=" ".join((spec.doc or "").split()),
                status=spec.status.value,
                scopes=sorted(extra_scopes[key]),
                family=members[key],
                matched=matched,
            )
        )
    _fuse_value_rows(hits)
    return hits[:limit]


def _fuse_value_rows(hits: list[Hit]) -> None:
    """Point a read-only parameter object at the sibling row that actually writes.

    ``track.volume`` is the DeviceParameter and answers ``not_settable``, and
    ``track.volume_value`` is the number. A caller handed the first without the second
    writes to a path that refuses, which the catalog documents but a ranked list does not.
    """
    by_id = {h.id: h for h in hits}
    for hit in list(hits):
        companion = by_id.get(hit.id + "_value")
        if companion is None:
            continue
        if "set" not in hit.access and "set" in companion.access:
            hit.writes = companion.id
            if companion in hits and len(hits) > 1:
                hits.remove(companion)
                hit.scopes = sorted(set(hit.scopes))
