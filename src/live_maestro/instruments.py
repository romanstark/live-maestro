"""Intent to instrument lookup over Live's browser: a sound in producer words, presets out.

``browser_walk`` matches one case-insensitive substring against one name, spaces
included, so a phrase like "warm bass" is a literal needle that no preset name contains
and the walk answers ``status: "complete"`` with nothing. This module splits the phrase,
issues one rooted walk per word and ranks the union here, where splitting is free.

A sounds preset's uri carries its category (``query:Sounds#Bass:FileId_16547``), so a
word naming a category is matched against that rather than against the name; no taxonomy
is hardcoded, the categories arrive attached to the hits. Routing happens before ranking,
as in :mod:`live_maestro.lookup`: a drum word is answered from the drums root. Identity
for de-duplication is the uri's FileId, and nothing is cached: a stored list of preset
names is a second source of truth that can disagree with Live. Pure module, no socket and
no I/O, so it is testable against recorded replies; the caller sends the walks.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

# Shared with lookup.py on purpose: a second stemmer would give "muted" a different reach
# to "Mute" here than there, and the two doors would answer differently.
from live_maestro.lookup import _SUFFIXES, _stem, normalise_query

__all__ = [
    "CAVEAT",
    "DEFAULT_LIMIT",
    "DRUM_WORDS",
    "MAX_WALKS",
    "ROOTS",
    "ROOT_DEPTH",
    "WALK_CEILING",
    "SoundHit",
    "SoundPlan",
    "SoundResult",
    "category_of",
    "identity_of",
    "plan",
    "rank",
]

#: The browser roots the Remote Script accepts, in its own order. The script's
#: ``_BROWSER_ROOTS`` is the authority: ``user_folders`` is a verified catalog row and
#: still answers ``no_such_path`` from the walk. A test reads the script and fails when
#: the two lists part company.
ROOTS = (
    "instruments",
    "sounds",
    "drums",
    "audio_effects",
    "midi_effects",
    "plugins",
    "max_for_live",
    "packs",
    "user_library",
    "clips",
    "samples",
    "current_project",
)

#: Depth to walk a root at, for roots where the script's default is wrong. The default
#: runs at the script's maximum and exhausts the node budget on a wide root: drums
#: finishes at depth 3 and its kits are unreachable unless that depth is sent. Only roots
#: measured against a running Live appear here; the rest run at the script's default.
ROOT_DEPTH: dict[str, int] = {"drums": 3}

#: Words whose answer is a drum kit, which the sounds root does not hold. "punchy" in the
#: sounds root answers "Punchy Bass"; in the drums root at depth 3 it answers
#: "Kit-BigPunchy". Without the routing a "punchy kick" is answered with a bass preset.
DRUM_WORDS = frozenset((
    "kick", "snare", "hat", "hats", "hihat", "clap", "rim", "tom", "kit", "kits",
    "drum", "drums", "breakbeat", "808", "909", "707", "606",
))

#: Words a producer says about wanting a sound that name nothing in a preset name. Not in
#: ``lookup``'s stop list, where they are harmless; here each costs a walk and drags in a
#: category ("Sounds" is half of "MPE Sounds").
_FILLER = frozenset((
    "anything", "best", "better", "good", "instrument", "instruments", "kind", "like",
    "need", "nice", "patch", "preset", "presets", "really", "similar", "something",
    "sound", "sounds", "thing", "tone", "type", "very", "vibe",
))

#: A word, in any alphabet. An ASCII-only class splits every non-ASCII word around its
#: accented letters ("Fläche" into 'fl' and 'che'), and the fragments then match presets
#: the caller never asked for.
_TOKEN = re.compile(r"[^\W_]+")
_FILE_ID = re.compile(r"FileId_[0-9]+\Z")

#: How many candidates an answer carries unless asked otherwise.
DEFAULT_LIMIT = 8

#: How many walks may pay off before the search stops. Each walk can block Live's main
#: thread for up to the script's 7.5 second budget on a strictly serial protocol; a rooted
#: preset search measures well under a second, so three is about 1.5 s of Live's attention.
MAX_WALKS = 3

#: The hard ceiling on walks, productive or not. A word that matched nothing does not
#: consume the productive budget, so "a huge moog-style saw lead" can spend two walks
#: finding that nothing is named "huge" or "moog" and still reach "saw" and "lead".
#: Without the refund the first three words would win by position alone.
WALK_CEILING = 5

# Ordered by how much a match tells you. The producer's word inside the preset's own name
# is the strongest evidence there is; the same word inside the category is real but
# weaker, because a category holds hundreds of presets and says nothing about this one.
_W_NAME_TOKEN = 100.0
_W_NAME_PART = 74.0
_W_NAME_STEM = 56.0
_W_CATEGORY = 58.0

#: The shortest truncation of a word that may be looked for inside a name. Below four
#: characters a truncation stops being about the word: "strings" truncates to "str", which
#: sits inside "Strobe", "Structure" and "Astro".
_MIN_TRUNCATION = 4

#: How much of a name's word has to sit before a match for the word to read as a compound
#: the match is part of. "Darkstring" is "dark" and "string"; "Swarmer" is "s" and
#: "warmer", and one letter is not a word. Three is the shortest head this library
#: actually builds compounds from ("sub", "pad", "arp"), and it is what keeps
#: ``warmer`` out of ``Swarmer`` without losing ``string`` in ``Darkstring``.
_MIN_COMPOUND_HEAD = 3

#: Added per matched word beyond the first, so a preset answering two of the caller's
#: words outranks one answering a single word strongly.
_W_COVERAGE = 62.0

#: The walk's own name rank, 0 best, folded in as a tiebreak rather than as evidence. It
#: separates "Sub Bass Drone" from "Particle Sub Bass" and must never separate a two-word
#: match from a one-word match, so it stays smaller than ``_W_COVERAGE``.
_W_RANK = 6.0

#: Charged per word of the name that answered nothing. "Deep Bass" before "Deep Sea
#: Monster Bass Thing" when both matched the same two words.
_W_NOISE = 3.0

#: How far a word's rarity may move its weight, and the divisor that scales the log into
#: that range. The same three numbers as ``lookup.py``'s ``_IDF_FLOOR`` / ``_IDF_CEILING``
#: / ``_IDF_SCALE``, for the same reason and deliberately not a second set of habits.
#:
#: The rarity is not a guess: every walk reports how many names in the root matched its
#: word and how many it examined, which separates "glassy" from "keys" by two orders of
#: magnitude. Without the weighting "glassy keys" answers "Exo Keys" over "GlassyChord".
_IDF_FLOOR = 0.30
_IDF_CEILING = 2.10
_IDF_SCALE = 3.0

#: Carried in every answer: an ordered list with scores reads like a measurement, and this
#: one is a reading of words. A caller who never sees the tool's prose still needs it.
CAVEAT = (
    "Ranked by the preset's name and by the browser category its uri carries. Nothing "
    "here was loaded, played or measured, so this is a reading of the words Ableton gave "
    "these presets and not of how any of them sounds. Two presets sharing a word can be "
    "nothing alike, and the one you want may say nothing in its name. Load one and listen."
)


@dataclass(frozen=True)
class SoundPlan:
    """What one intent asks of the browser: which root, which words, which walks.

    Attributes:
        intent: The caller's phrase, unchanged.
        terms: Every word worth searching for, in the order they were said. How many
            of them actually get a walk is decided as the replies come in, by
            :meth:`may_walk`.
        root: The browser root every walk is scoped to. Never empty: an unrooted walk
            spends its whole node budget before reaching any root at all.
        root_reason: Why that root, in one sentence for the caller.
        kind: The ``kind`` filter every walk carries.
        depth: The depth every walk carries, or None to let the script choose.
        limit: How many entries each walk may return.
        max_walks: How many walks that found something may be spent.
        ceiling: The hard ceiling on walks of any kind.
    """

    intent: str
    terms: tuple[str, ...]
    root: str
    root_reason: str
    kind: str
    depth: int | None
    limit: int
    max_walks: int = MAX_WALKS
    ceiling: int = WALK_CEILING

    def walk_for(self, term: str) -> dict[str, Any]:
        """Return the ``browser_walk`` parameters that search this root for one word."""
        params: dict[str, Any] = {"root": self.root, "query": term, "limit": self.limit}
        if self.kind:
            params["kind"] = self.kind
        if self.depth is not None:
            params["depth"] = self.depth
        return params

    def may_walk(self, spent: int, productive: int) -> bool:
        """Return True when one more walk may be sent.

        A walk that matched nothing is refunded against ``max_walks`` but still counts
        against ``ceiling``, so a query full of words this library never used costs a
        bounded number of walks instead of stopping on the first three of them.
        """
        return spent < self.ceiling and productive < self.max_walks


@dataclass
class SoundHit:
    """One browser item that answers, keyed by the identity its uri carries.

    Attributes:
        name: The browser's own name, verbatim, extension and all.
        item_path: The item as a LOM path. Named after ``load_device``'s argument,
            because the chain from finding to loading must not rename a field. The walk
            calls this key ``path``.
        uri: The item's uri, for ``load_device(uri=...)`` where the path has gone stale.
        category: The browser category read out of the uri fragment, or None where the
            uri carried none. The drums root's uris carry no category.
        kind: The walk's own classification: device, preset, sample, folder or other.
        is_loadable: The walk's own answer. None means this Live did not say, not False.
        match_rank: The walk's name rank, 0 best.
        score: Ranking score, comparable only inside one answer.
        matched: Which of the caller's words this hit is here for.
        also_in: Other categories the same item was found under in this same answer.
    """

    name: str
    item_path: str
    uri: str | None
    category: str | None
    kind: str | None
    is_loadable: bool | None
    match_rank: int | None
    score: float
    matched: list[str] = field(default_factory=list)
    also_in: list[str] = field(default_factory=list)


@dataclass
class SoundResult:
    """The answer to one intent: the candidates and exactly how far the walks got."""

    plan: SoundPlan
    candidates: list[SoundHit] = field(default_factory=list)
    status: str = "complete"
    unreached_roots: list[str] = field(default_factory=list)
    truncated_by: list[str] = field(default_factory=list)
    considered: int = 0
    walks: int = 0
    terms_searched: list[str] = field(default_factory=list)
    terms_without_hits: list[str] = field(default_factory=list)
    covered_all_terms: bool = False


def category_of(uri: str | None) -> str | None:
    """Return the browser category a uri carries, or None where it carries none.

    The fragment after ``#`` is percent-encoded and colon-separated, and the last segment
    is the FileId. Read against a running Live 12.4.6: a sounds preset reads
    ``query:Sounds#Bass:FileId_16547`` and ``query:Sounds#Guitar%20&%20Plucked:FileId_4864``,
    an instruments preset reads ``query:Synths#Drift:Pad:FileId_5052`` where the leading
    segment is the device, and a drums preset reads ``query:Drums#FileId_36888`` with no
    category at all.

    >>> category_of("query:Sounds#Bass:FileId_16547")
    'Bass'
    >>> category_of("query:Sounds#Guitar%20&%20Plucked:FileId_4864")
    'Guitar & Plucked'
    >>> category_of("query:Drums#FileId_36888") is None
    True
    >>> category_of("query:Synths#Operator") is None
    True
    """
    if not uri or "#" not in uri:
        return None
    segments = [s for s in unquote(uri.split("#", 1)[1]).split(":") if s]
    # The FileId is what makes the fragment an item rather than a folder. A uri without
    # one names the folder itself ("query:Synths#Operator"), and calling that a category
    # would file every Operator preset under "Operator".
    if not segments or not _FILE_ID.match(segments[-1]):
        return None
    return segments[-2] if len(segments) >= 2 else None


def identity_of(entry: dict[str, Any]) -> str:
    """Return what makes two browser entries the same item.

    The FileId in the uri is stable across roots and across a restart: "Warm Analog Pad"
    is FileId_5052 under both ``Synths#Drift:Pad`` and ``Synths#Instrument%20Rack:Pad``.
    Where a uri carries no FileId the uri stands for itself, and failing that the
    positional path, which is the only one of the three that moves when the library does.
    """
    uri = entry.get("uri")
    if isinstance(uri, str) and uri:
        match = re.search(r"FileId_[0-9]+", uri)
        return match.group(0) if match else uri
    path = entry.get("path")
    return str(path) if path else str(entry.get("name") or "")


def plan(
    intent: str,
    *,
    root: str = "",
    kind: str = "preset",
    limit: int = 60,
    max_walks: int = MAX_WALKS,
) -> SoundPlan:
    """Turn a producer's phrase into the walks that answer it.

    Args:
        intent: The sound wanted, in the caller's own words.
        root: Browser root to search. Empty picks one from the words.
        kind: The walk's kind filter. ``preset`` is the default because a query that does
            not filter puts category folders at rank 0 above every preset: the top
            eight for "bass" in the instruments root were seven folders named "Bass".
        limit: How many entries each walk may return.
        max_walks: How many productive walks to allow, which bounds how long Live's
            main thread is held.

    Returns:
        A SoundPlan carrying every searchable word, the root, and the walk budget.
    """
    terms = [t for t in normalise_query(intent) if t not in _FILLER]
    if not terms:
        terms = normalise_query(intent)

    if root:
        picked, reason = root, f"root={root!r} was asked for."
    elif any(t in DRUM_WORDS or _stem(t) in DRUM_WORDS for t in terms):
        picked = "drums"
        reason = (
            "These words name a drum, and drum kits live in the 'drums' root rather than "
            "in 'sounds'. Pass root='sounds' to search the melodic library instead."
        )
    else:
        picked = "sounds"
        reason = (
            "No root was given, so the 'sounds' root was searched: it holds the melodic "
            "presets and it is the one root a query can cover completely. An unrooted "
            "walk spends its whole node budget before reaching any root at all."
        )
    return SoundPlan(
        intent=intent,
        terms=tuple(terms),
        root=picked,
        root_reason=reason,
        kind=kind,
        depth=ROOT_DEPTH.get(picked),
        limit=limit,
        max_walks=max(1, max_walks),
    )


def _truncations(term: str) -> list[str]:
    """Every truncation of a word the shared suffix list licenses, longest first.

    ``_stem`` returns the shortest of these, because it strips the longest suffix that
    applies. That suits property names, where nothing between "warp" and "warpings" is a
    word. A producer's nouns differ: "strings" truncates to "string" by its plural and to
    "str" by a suffix that is not one here, and only the first is still about strings.

    >>> _truncations("strings")
    ['string', 'str']
    >>> _truncations("muted")
    ['mut']
    >>> _truncations("keys")
    []
    """
    if len(term) < 5:
        return []
    cuts = [
        term[: -len(suffix)]
        for suffix in _SUFFIXES
        if term.endswith(suffix) and len(term) - len(suffix) >= 3
    ]
    return sorted(dict.fromkeys(cuts), key=len, reverse=True)


def _name_score(term: str, lowered: str, tokens: set[str], stems: set[str]) -> float:
    """Score one term against a preset name. The strongest tier that applies wins.

    Every entry the walk returned contains the needle as a substring by construction, so
    a ranker comparing whole tokens only would score real hits at zero: "glassy" is in
    "GlassyChord.adg", which tokenises as one word.

    The last tier looks for less than the caller said, so it tries the longest truncation
    first and stops at ``_MIN_TRUNCATION``: "strings" is looked for as "string", which is
    inside "Darkstring.adg" and not inside "Dark Strobe.adv". A word with nothing longer
    to offer is still tried at the stemmer's floor, which is how "muted" reaches
    "Trumpet Section Mute Legato.adg".
    """
    stem = _stem(term)
    if term in tokens or stem in stems:
        return _W_NAME_TOKEN
    if _inside_a_word(term, lowered):
        return _W_NAME_PART
    cuts = _truncations(term)
    long_enough = [cut for cut in cuts if len(cut) >= _MIN_TRUNCATION]
    for cut in long_enough or cuts:
        if _inside_a_word(cut, lowered):
            return _W_NAME_STEM
    return 0.0


def _inside_a_word(needle: str, lowered: str) -> bool:
    """Is ``needle`` a part of a name's word that the word is actually built from?

    A bare ``needle in lowered`` scores a hit wherever the letters line up: "warmer" sits
    inside "Swarmer", which then outranks every preset in the Bass category.

    What decides it is what precedes the match inside its own word. "Darkstring.adg" is
    "dark" and "string", and reaching "string" there is the point; "Swarmer" is "s" and
    "warmer", and one letter is not a word. So a match at the start of a word counts, and
    a match further in counts only when what sits before it is long enough to be a word.
    """
    for token in _TOKEN.finditer(lowered):
        word = token.group()
        start = word.find(needle)
        if start == 0 or (start >= _MIN_COMPOUND_HEAD and start > 0):
            return True
    return False


def _category_matches(term: str, categories: set[str]) -> bool:
    """Return True when a term names one of the categories this item was found under."""
    stem = _stem(term)
    for category in categories:
        words = set(_TOKEN.findall(category.lower()))
        if term in words or stem in {_stem(w) for w in words}:
            return True
    return False


def rank(
    sound_plan: SoundPlan,
    replies: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_LIMIT,
) -> SoundResult:
    """Rank the union of the walk replies, and report exactly how far they got.

    Args:
        sound_plan: The plan the replies answer, whose terms are what gets scored.
        replies: One ``browser_walk`` reply per walk, in the plan's term order.
        limit: How many candidates to keep.

    Returns:
        A SoundResult whose ``status`` is "complete" only when every walk was.
    """
    searched = list(sound_plan.terms[: len(replies)])
    merged: dict[str, dict[str, Any]] = {}
    categories: dict[str, list[str]] = {}
    found_for: dict[str, bool] = {t: False for t in searched}
    rarity: dict[str, float] = {}

    status = "complete"
    unreached: list[str] = []
    truncated_by: list[str] = []
    considered = 0

    for index, reply in enumerate(replies):
        term = searched[index] if index < len(searched) else ""
        if term:
            rarity[term] = _rarity(reply)
        # A script that predates the status field says nothing about coverage, and
        # reading that silence as "complete" is the one claim this answer must never
        # invent: it is the difference between "not in the library" and "not looked at".
        reply_status = reply.get("status") or (
            "incomplete" if reply.get("truncated") else "complete"
        )
        if reply_status != "complete":
            status = "incomplete"
        for name in reply.get("unreached_roots") or []:
            if str(name) not in unreached:
                unreached.append(str(name))
        stopped = reply.get("truncated_by")
        if stopped and str(stopped) not in truncated_by:
            truncated_by.append(str(stopped))
        count = reply.get("considered")
        if isinstance(count, int) and not isinstance(count, bool):
            considered += count

        matches = reply.get("matches") or []
        if term and matches:
            found_for[term] = True
        for entry in matches:
            if not isinstance(entry, dict):
                continue
            key = identity_of(entry)
            category = category_of(entry.get("uri"))
            seen = categories.setdefault(key, [])
            if category and category not in seen:
                seen.append(category)
            best = merged.get(key)
            if best is None or _rank_of(entry) < _rank_of(best):
                merged[key] = entry

    hits: list[SoundHit] = []
    for key, entry in merged.items():
        found = categories.get(key, [])
        # Scored against every word the caller said, not only the walked ones. A word the
        # budget never reached still costs nothing to check against the categories that
        # came back, and dropping it would throw away free evidence: "lead" answers
        # "Synth Lead" whether or not a walk was spent on it.
        hit = _score(entry, list(sound_plan.terms), found, rarity)
        if hit is not None:
            hits.append(hit)

    hits.sort(key=lambda h: (-h.score, len(h.name), h.name))
    kept = hits[: max(1, limit)]
    return SoundResult(
        plan=sound_plan,
        candidates=kept,
        status=status,
        unreached_roots=unreached,
        truncated_by=truncated_by,
        considered=considered,
        walks=len(replies),
        terms_searched=searched,
        terms_without_hits=[t for t, ok in found_for.items() if not ok],
        covered_all_terms=any(len(h.matched) == len(sound_plan.terms) for h in kept),
    )


def _rank_of(entry: dict[str, Any]) -> int:
    """Return an entry's name rank, treating a missing or malformed one as the worst."""
    value = entry.get("match_rank")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 9


def _rarity(reply: dict[str, Any]) -> float:
    """Return how much one walk's word narrows this root, from the walk's own counts.

    ``matched`` is how many names in the root carry the word and ``considered`` is how
    many items the walk examined, so the reply that answers a word also says how common
    that word is. A reply that reports neither weighs 1.0, which is the same as saying
    nothing about it.
    """
    matched = reply.get("matched")
    total = reply.get("considered")
    if not isinstance(matched, int) or isinstance(matched, bool):
        return 1.0
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        return 1.0
    weight = math.log((total + 1) / (max(0, matched) + 1)) / _IDF_SCALE
    return min(_IDF_CEILING, max(_IDF_FLOOR, weight))


def _score(
    entry: dict[str, Any],
    terms: list[str],
    found_in: list[str],
    rarity: dict[str, float],
) -> SoundHit | None:
    """Score one merged entry, or return None when it answers none of the words."""
    name = str(entry.get("name") or "")
    lowered = name.lower()
    tokens = set(_TOKEN.findall(lowered))
    stems = {_stem(t) for t in tokens}
    category_set = set(found_in)

    total = 0.0
    matched: list[str] = []
    for term in terms:
        weight = _name_score(term, lowered, tokens, stems)
        if weight == 0.0 and _category_matches(term, category_set):
            weight = _W_CATEGORY
        if weight > 0.0:
            total += weight * rarity.get(term, 1.0)
            matched.append(term)
    if not matched:
        return None
    total += _W_COVERAGE * (len(matched) - 1)
    total += _W_RANK * (4 - min(_rank_of(entry), 4))
    total -= _W_NOISE * max(0, len(tokens) - len(matched))

    loadable = entry.get("is_loadable")
    primary = found_in[0] if found_in else None
    return SoundHit(
        name=name,
        item_path=str(entry.get("path") or ""),
        uri=str(entry["uri"]) if entry.get("uri") else None,
        category=primary,
        kind=str(entry["kind"]) if entry.get("kind") else None,
        is_loadable=loadable if isinstance(loadable, bool) else None,
        match_rank=_rank_of(entry) if entry.get("match_rank") is not None else None,
        score=round(total, 1),
        matched=matched,
        also_in=found_in[1:],
    )
