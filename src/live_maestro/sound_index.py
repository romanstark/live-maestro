"""An index of how the installed library sounds, built from Ableton's own preview audio.

Live renders a short audio preview for browser items and keeps it beside the content, under
``Ableton Folder Info/Previews``, mirroring the folder the item sits in. It is ordinary Ogg
Vorbis: not encrypted, readable without Live, and already a recording of the preset making
a sound. The samples themselves stay closed, carrying an ``able`` compression type that
neither Python nor Max for Live inside Live's process can read (``measurements/spikes/``).

What a preview cannot give is behaviour per note. It is the phrase Ableton chose to
demonstrate the item with, so it says nothing about playable range, velocity response
or the attack of a single note, and a preset demonstrated by a busy riff describes
itself differently from one demonstrated by a held chord.

The index is not committed: it describes one machine's installed packs, and
``live-maestro-index`` rebuilds it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from live_maestro.audio.descriptor import (
    BANDS,
    DEFAULT_WEIGHTS,
    Descriptor,
    Weights,
    distance,
)

__all__ = [
    "CAVEAT",
    "FAMILIES",
    "FAMILY_NAMES",
    "INDEX_VERSION",
    "PREVIEW_FOLDER",
    "PREVIEW_SUFFIX",
    "Family",
    "Hit",
    "Item",
    "SoundIndex",
    "SoundIndexError",
    "default_index_path",
    "default_roots",
    "discover_previews",
    "family_of",
    "load_index",
]

#: Bumped when the descriptor or the entry shape changes. An index built under an older
#: version is refused rather than read, because a descriptor is only comparable with
#: others taken the same way, and a silently mixed index ranks nonsense.
INDEX_VERSION = 2

#: Where Live keeps the preview audio inside a library root.
PREVIEW_FOLDER = Path("Ableton Folder Info") / "Previews"

PREVIEW_SUFFIX = ".ogg"

#: Item kinds worth indexing, and what each one is. Clips and grooves are indexed too:
#: a clip preview answers "what does this loop sound like", which is the same question.
KINDS: dict[str, str] = {
    ".adg": "rack or preset group",
    ".adv": "device preset",
    ".alc": "clip",
    ".agr": "groove",
}


@dataclass(frozen=True)
class Family:
    """One coarse instrument family, and the category words that name it.

    Coarse on purpose. Whether a preset is filed under Operator or Wavetable says which
    device makes it, not what it sounds like, and a caller asking for a bass does not
    care. What the families do have to separate is things a caller would never swap for
    each other, which is why a drum kit and a single cymbal are two of them: a stem of a
    whole kit answered with five separate hi-hats is a useless answer, and that is what
    one undivided drum family produces.
    """

    name: str
    describes: str
    matches: tuple[str, ...]
    excludes: tuple[str, ...] = ()

    def claims(self, category: str) -> bool:
        lowered = category.lower()
        if any(word in lowered for word in self.excludes):
            return False
        return any(word in lowered for word in self.matches)


#: Families in the order they are tried. Order is the whole rule: a path like
#: "Drums/Drum Hits/Bass Drum" holds both "bass" and "drum" and is a drum, and a kit has
#: to be ruled out before a hit can claim the path it sits under.
FAMILIES: tuple[Family, ...] = (
    Family(
        "drum_hit",
        "one drum or percussion instrument, a single hit",
        ("drum hits", "single instruments"),
    ),
    Family(
        "drum_kit",
        "a whole drum kit or drum rack",
        ("drum", "kit", "percussi", "hihat", "snare", "cymbal", "conga", "bongo",
         "timbal", "shaker", "clave", "cowbell", "tambourine"),
    ),
    Family("mallets", "tuned percussion, struck or plucked metal and wood",
           ("mallet", "marimba", "vibraphone", "xylophon", "glocken", "kalimba",
            "bell", "chime")),
    Family("voice", "sung or spoken material", ("voice", "vocal", "choir", "chant")),
    Family("guitar", "guitar and other plucked strings",
           ("guitar", "plucked", "pluck", "harp", "banjo", "ukulele", "sitar")),
    Family("strings", "bowed strings",
           ("string", "violin", "cello", "viola", "orchestral")),
    Family("brass", "brass", ("brass", "horn", "trumpet", "trombone", "tuba")),
    Family("winds", "woodwind and reeds",
           ("flute", "clarinet", "oboe", "bassoon", "woodwind", "sax", "reed")),
    Family("keys", "piano, electric piano, organ and keyboard sounds",
           ("keys", "piano", "organ", "rhodes", "clav", "suitcase", "wurl")),
    Family("bass", "bass, synthetic or played", ("bass", "sub")),
    Family("pad", "sustained and evolving texture",
           ("pad", "ambient", "evolving", "atmosphere", "texture", "drone")),
    Family("lead", "a melodic line meant to sit in front", ("lead", "arp")),
    Family("fx", "noise, risers, impacts and other non pitched material",
           ("effect", "fx", "noise", "riser", "impact", "foley")),
)

#: Every family name, for a tool that has to offer them as a choice.
FAMILY_NAMES: tuple[str, ...] = tuple(f.name for f in FAMILIES)


def family_of(category: str) -> str:
    """The coarse family a browser category path names, or an empty string for none.

    >>> family_of("Drums/Drum Hits/Standard Hits/Cymbal")
    'drum_hit'
    >>> family_of("Racks/Drum Racks/Electronic")
    'drum_kit'
    >>> family_of("Sounds/Bass")
    'bass'
    >>> family_of("Devices/Instruments/Operator/Bass")
    'bass'
    >>> family_of("Sounds/Guitar and Bass Effects")
    'guitar'
    >>> family_of("Presets/Something Unfiled")
    ''
    """
    for family in FAMILIES:
        if family.claims(category):
            return family.name
    return ""


#: Fewest analysis windows an item's descriptor may rest on and still be ranked. A
#: preview of 0.3 seconds yields about eight windows, and its sustain fraction and onset
#: rate then describe one gesture rather than a sound. One such item was measured taking
#: rank 1 against a four minute bass stem.
MIN_FRAMES = 20

#: Said inside every answer built on this index: a ranked list with scores reads like a
#: measurement of fit, and this one measures Ableton's demo recordings. A caller who
#: never sees the tool's prose still has to be told.
CAVEAT = (
    "Ranked against the audio Ableton renders for its own browser previews, not against "
    "the item played on your notes. A preview is the phrase Ableton chose to demonstrate "
    "the item with, so the comparison carries that phrase as well as the timbre. Level "
    "is removed and pitch is not matched. Nothing here was played or listened to: load a "
    "candidate and listen before believing it fits."
)


def default_roots() -> tuple[Path, ...]:
    """Library roots to search, those of them that exist on this machine.

    A root is any folder holding an ``Ableton Folder Info``. Every factory pack is its
    own root, so the packs folder is walked for them rather than treated as one.
    """
    home = Path.home()
    candidates = [
        home / "Music" / "Ableton" / "Factory Packs",
        home / "Music" / "Ableton" / "User Library",
        Path("/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library"),
        Path("/Applications/Ableton Live 12 Standard.app/Contents/App-Resources/Core Library"),
        Path("/Applications/Ableton Live 12 Intro.app/Contents/App-Resources/Core Library"),
    ]
    return tuple(path for path in candidates if path.is_dir())


def default_index_path() -> Path:
    """Where the index lives unless told otherwise.

    Under the user's own data folder rather than in the repository: the index describes
    one machine's installed content, and two machines do not share one.
    """
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "live-maestro" / "sound-index.json"


@dataclass(frozen=True)
class Item:
    """One browser item with a preview, and what its preview sounds like.

    Attributes:
        name: The item's name as Live shows it, which is the file stem. This is what
            ``load_device(query=...)`` searches for: a filesystem path is not a browser
            handle, and no handle can be derived from one.
        kind: File extension of the item, a key of :data:`KINDS`.
        category: The folders between the library root and the item, which is the
            browser's own grouping.
        library: Name of the library root the item came from, such as a pack name.
        target: Absolute path of the item itself, for reference. Not a load handle.
        preview: Absolute path of the preview audio the descriptor was taken from.
        descriptor: What the preview sounds like.
    """

    name: str
    kind: str
    category: str
    library: str
    target: str
    preview: str
    descriptor: Descriptor = field(default_factory=Descriptor)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "category": self.category,
            "library": self.library,
            "target": self.target,
            "preview": self.preview,
            "descriptor": self.descriptor.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Item:
        raw = data.get("descriptor")
        return cls(
            name=str(data.get("name") or ""),
            kind=str(data.get("kind") or ""),
            category=str(data.get("category") or ""),
            library=str(data.get("library") or ""),
            target=str(data.get("target") or ""),
            preview=str(data.get("preview") or ""),
            descriptor=Descriptor.from_dict(raw) if isinstance(raw, dict) else Descriptor(),
        )


@dataclass(frozen=True)
class Hit:
    """One ranked candidate. ``score`` orders this answer and nothing else."""

    item: Item
    score: float
    rank: int


@dataclass
class SoundIndex:
    """Every indexed item, and the readings that say what the index is worth."""

    items: list[Item] = field(default_factory=list)
    version: int = INDEX_VERSION
    bands: int = BANDS
    built_on: str = ""
    roots: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def rank(
        self,
        query: Descriptor,
        *,
        limit: int = 8,
        kinds: tuple[str, ...] = (),
        category_contains: str = "",
        families: tuple[str, ...] = (),
        min_frames: int = MIN_FRAMES,
        weights: Weights = DEFAULT_WEIGHTS,
        exclude: frozenset[str] = frozenset(),
    ) -> list[Hit]:
        """Rank the index against one descriptor, closest first.

        Args:
            query: The descriptor to match, taken with the same code at the same rate.
            limit: How many candidates to return.
            kinds: Restrict to these item kinds, such as ``(".adg", ".adv")`` for
                things that can be loaded onto a track. Empty allows all.
            category_contains: Restrict to items whose category contains this text,
                case insensitive. Empty allows all.
            min_frames: Skip items whose descriptor rests on fewer analysis windows
                than this. A preview of a few tenths of a second carries a sustain
                fraction and an onset rate built from almost nothing, and one such item
                was measured taking rank 1 against a four minute stem.
            families: Restrict to these coarse families. Empty allows all, and
                measured against real stems that is much the worse answer: the family
                is usually known, and guessing it is the step that goes wrong.
            weights: How much each part of the comparison counts.
            exclude: Item targets to leave out, which is how a leave one out
                evaluation keeps a probe from retrieving itself.

        Returns:
            Up to ``limit`` hits. An empty index answers with an empty list rather than
            an error, because "nothing is indexed yet" is a state, not a failure.
        """
        wanted = category_contains.lower()
        scored: list[tuple[float, Item]] = []
        for item in self.items:
            if kinds and item.kind not in kinds:
                continue
            if wanted and wanted not in item.category.lower():
                continue
            if item.descriptor.frames < min_frames:
                continue
            if families and family_of(item.category) not in families:
                continue
            if item.target in exclude:
                continue
            score = distance(query, item.descriptor, weights)
            if score == float("inf"):
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].name))
        return [
            Hit(item=item, score=score, rank=index)
            for index, (score, item) in enumerate(scored[:limit])
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bands": self.bands,
            "built_on": self.built_on,
            "roots": list(self.roots),
            "skipped": dict(self.skipped),
            "count": len(self.items),
            "items": [item.as_dict() for item in self.items],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=1) + "\n")
        return path


class SoundIndexError(RuntimeError):
    """Raised when an index cannot be used. ``code`` machine, ``error`` human."""

    def __init__(self, code: str, error: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error


def load_index(path: Path | None = None) -> SoundIndex:
    """Read an index off disk.

    Raises:
        SoundIndexError: There is no index, it is not readable, or it was built by a
            different descriptor version. A mixed index is refused rather than read:
            two descriptors taken differently rank against each other as noise.
    """
    target = path or default_index_path()
    if not target.is_file():
        raise SoundIndexError(
            "no_index",
            f"No sound index at {target}. Build one by running live-maestro-index, "
            f"which takes about a quarter of an hour and needs no network.",
        )
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        raise SoundIndexError("unreadable_index", f"{target} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise SoundIndexError("unreadable_index", f"{target} does not hold an index.")

    version = int(data.get("version") or 0)
    if version != INDEX_VERSION:
        raise SoundIndexError(
            "stale_index",
            f"{target} was built by descriptor version {version} and this is "
            f"{INDEX_VERSION}. Rebuild it: a descriptor taken another way does not "
            f"rank against this one.",
        )
    raw_items = data.get("items")
    items = [Item.from_dict(entry) for entry in raw_items] if isinstance(raw_items, list) else []
    return SoundIndex(
        items=items,
        version=version,
        bands=int(data.get("bands") or BANDS),
        built_on=str(data.get("built_on") or ""),
        roots=[str(v) for v in (data.get("roots") or [])],
        skipped={str(k): int(v) for k, v in (data.get("skipped") or {}).items()},
    )


def discover_previews(roots: tuple[Path, ...] = ()) -> list[Item]:
    """Every previewed item under these roots, without decoding anything.

    A root passed here may hold libraries rather than be one: the factory packs folder
    contains a root per pack. Both are handled by looking for the preview folder itself
    and taking its grandparent as the library.

    Returns:
        Items with an empty descriptor, in a stable order. An item whose preview has no
        matching content file is left out, because a name that cannot be loaded is not
        a candidate.
    """
    found: dict[str, Item] = {}
    for given in roots or default_roots():
        for previews in sorted(given.rglob(str(PREVIEW_FOLDER))):
            if not previews.is_dir():
                continue
            library = previews.parent.parent
            for preview in sorted(previews.rglob(f"*{PREVIEW_SUFFIX}")):
                relative = preview.relative_to(previews)
                item_relative = Path(str(relative)[: -len(PREVIEW_SUFFIX)])
                target = library / item_relative
                if target.suffix not in KINDS or not target.is_file():
                    continue
                key = str(target)
                if key in found:
                    continue
                found[key] = Item(
                    name=target.stem,
                    kind=target.suffix,
                    category=str(item_relative.parent).strip("."),
                    library=library.name,
                    target=key,
                    preview=str(preview),
                )
    return list(found.values())
