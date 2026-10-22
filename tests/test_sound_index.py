"""What the sound index has to get right, without needing a sound library to say it.

Discovery is tested against a folder tree built here, laid out the way Live lays one
out. That keeps the test honest on a machine with no Ableton installed and keeps it
from passing by accident on one where the real library happens to look right.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from live_maestro.audio.descriptor import BANDS, Descriptor, describe
from live_maestro.sound_index import (
    FAMILIES,
    FAMILY_NAMES,
    INDEX_VERSION,
    PREVIEW_FOLDER,
    Item,
    SoundIndex,
    SoundIndexError,
    default_index_path,
    discover_previews,
    family_of,
    load_index,
)


def make_library(root: Path, entries: list[tuple[str, str]], *, previews: bool = True) -> Path:
    """Build a library root: each entry is a path under the root and its content.

    A real root carries the item and, beside it under ``Ableton Folder Info/Previews``,
    an audio file whose name is the item's name plus an extra suffix.
    """
    for relative, _kind in entries:
        item = root / relative
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text("item")
        if previews:
            preview = root / PREVIEW_FOLDER / f"{relative}.ogg"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"OggS fake")
    return root


def band_vector(weight_low: float) -> tuple[float, ...]:
    """A normalised band vector tilted low or high, for ranking tests."""
    values = [weight_low ** (i / BANDS) for i in range(BANDS)]
    total = sum(values)
    return tuple(v / total for v in values)


class TestDiscovery:
    def test_it_finds_an_item_and_names_it_after_the_file(self, tmp_path: Path) -> None:
        root = make_library(tmp_path / "Core", [("Drums/Kit A.adg", "adg")])
        found = discover_previews((root,))
        assert [(i.name, i.kind, i.category) for i in found] == [("Kit A", ".adg", "Drums")]

    def test_the_library_is_the_folder_holding_the_preview_folder(self, tmp_path: Path) -> None:
        root = make_library(tmp_path / "My Pack", [("Bass/Deep.adv", "adv")])
        assert discover_previews((root,))[0].library == "My Pack"

    def test_a_folder_of_libraries_is_walked_for_each_of_them(self, tmp_path: Path) -> None:
        """Ableton's factory packs folder is not a library; every pack inside it is."""
        packs = tmp_path / "Factory Packs"
        make_library(packs / "Pack One", [("Bass/One.adv", "adv")])
        make_library(packs / "Pack Two", [("Lead/Two.adv", "adv")])
        assert sorted(i.library for i in discover_previews((packs,))) == ["Pack One", "Pack Two"]

    def test_a_preview_with_no_item_beside_it_is_left_out(self, tmp_path: Path) -> None:
        """A name that cannot be loaded is not a candidate, however good it sounds."""
        root = tmp_path / "Core"
        orphan = root / PREVIEW_FOLDER / "Drums" / "Ghost.adg.ogg"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"OggS fake")
        assert discover_previews((root,)) == []

    def test_an_item_kind_the_index_does_not_carry_is_left_out(self, tmp_path: Path) -> None:
        root = make_library(tmp_path / "Core", [("Odd/Thing.txt", "txt")])
        assert discover_previews((root,)) == []

    def test_an_item_with_no_preview_is_left_out(self, tmp_path: Path) -> None:
        root = make_library(tmp_path / "Core", [("Bass/Silent.adv", "adv")], previews=False)
        assert discover_previews((root,)) == []

    def test_the_same_item_reached_twice_is_carried_once(self, tmp_path: Path) -> None:
        root = make_library(tmp_path / "Core", [("Bass/One.adv", "adv")])
        assert len(discover_previews((root, root))) == 1

    def test_a_root_that_does_not_exist_is_not_an_error(self, tmp_path: Path) -> None:
        assert discover_previews((tmp_path / "nowhere",)) == []

    def test_the_order_is_stable(self, tmp_path: Path) -> None:
        root = make_library(
            tmp_path / "Core",
            [("B/Two.adv", "adv"), ("A/One.adv", "adv"), ("C/Three.adv", "adv")],
        )
        assert [i.name for i in discover_previews((root,))] == [
            i.name for i in discover_previews((root,))
        ]


def item(name: str, low: float, *, kind: str = ".adv", category: str = "Bass") -> Item:
    return Item(
        name=name,
        kind=kind,
        category=category,
        library="Test",
        target=f"/x/{name}{kind}",
        preview=f"/x/{name}{kind}.ogg",
        descriptor=Descriptor(bands=band_vector(low), centroid_hz=100.0 * low, frames=40),
    )


class TestRanking:
    def test_the_closest_item_comes_first(self) -> None:
        index = SoundIndex(items=[item("High", 0.02), item("Low", 30.0), item("Mid", 1.0)])
        query = Descriptor(bands=band_vector(28.0), centroid_hz=2900.0, frames=40)
        assert index.rank(query, limit=1)[0].item.name == "Low"

    def test_the_limit_is_honoured_and_ranks_start_at_zero(self) -> None:
        index = SoundIndex(items=[item(f"N{i}", 1.0 + i) for i in range(10)])
        hits = index.rank(Descriptor(bands=band_vector(3.0), centroid_hz=300.0), limit=3)
        assert [h.rank for h in hits] == [0, 1, 2]

    def test_scores_never_decrease_down_the_list(self) -> None:
        index = SoundIndex(items=[item(f"N{i}", 0.5 + i) for i in range(8)])
        hits = index.rank(Descriptor(bands=band_vector(2.0), centroid_hz=200.0), limit=8)
        assert all(a.score <= b.score for a, b in itertools.pairwise(hits))

    def test_a_kind_filter_excludes_everything_else(self) -> None:
        index = SoundIndex(items=[item("Rack", 1.0, kind=".adg"), item("Preset", 1.0)])
        hits = index.rank(Descriptor(bands=band_vector(1.0)), kinds=(".adg",))
        assert [h.item.name for h in hits] == ["Rack"]

    def test_a_category_filter_ignores_case(self) -> None:
        index = SoundIndex(items=[item("A", 1.0, category="Synth Lead"), item("B", 1.0)])
        hits = index.rank(Descriptor(bands=band_vector(1.0)), category_contains="synth")
        assert [h.item.name for h in hits] == ["A"]

    def test_an_unmeasured_item_is_left_out_rather_than_ranked_first(self) -> None:
        """A missing descriptor must not score better than a real one."""
        index = SoundIndex(items=[Item("Empty", ".adv", "", "", "", ""), item("Real", 1.0)])
        hits = index.rank(Descriptor(bands=band_vector(1.0)), limit=8)
        assert [h.item.name for h in hits] == ["Real"]

    def test_an_empty_index_answers_empty_rather_than_failing(self) -> None:
        assert SoundIndex().rank(Descriptor(bands=band_vector(1.0))) == []

    def test_ranking_is_deterministic_for_a_tie(self) -> None:
        index = SoundIndex(items=[item("Zeta", 1.0), item("Alpha", 1.0)])
        hits = index.rank(Descriptor(bands=band_vector(1.0)), limit=2)
        assert [h.item.name for h in hits] == ["Alpha", "Zeta"]


class TestFile:
    def test_an_index_survives_a_write_and_a_read(self, tmp_path: Path) -> None:
        index = SoundIndex(items=[item("One", 1.0)], built_on="2026-01-01", roots=["/x"])
        path = index.write(tmp_path / "i.json")
        back = load_index(path)
        assert [i.name for i in back.items] == ["One"]
        assert back.roots == ["/x"]
        assert back.items[0].descriptor.bands == pytest.approx(
            index.items[0].descriptor.bands, abs=1e-5
        )

    def test_a_missing_index_says_how_to_build_one(self, tmp_path: Path) -> None:
        with pytest.raises(SoundIndexError) as caught:
            load_index(tmp_path / "absent.json")
        assert caught.value.code == "no_index"
        assert "live-maestro-index" in caught.value.error

    def test_an_index_from_another_descriptor_version_is_refused(self, tmp_path: Path) -> None:
        """Descriptors taken two ways rank against each other as noise."""
        path = tmp_path / "old.json"
        path.write_text(json.dumps({"version": INDEX_VERSION + 1, "items": []}))
        with pytest.raises(SoundIndexError) as caught:
            load_index(path)
        assert caught.value.code == "stale_index"

    def test_a_file_that_is_not_an_index_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(SoundIndexError) as caught:
            load_index(path)
        assert caught.value.code == "unreadable_index"

    def test_broken_json_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json")
        with pytest.raises(SoundIndexError) as caught:
            load_index(path)
        assert caught.value.code == "unreadable_index"

    def test_the_default_path_is_outside_the_repository(self) -> None:
        """The index describes one machine's installed content and is not committed."""
        assert "live-maestro" in str(default_index_path())
        assert default_index_path().name.endswith(".json")

    def test_the_written_index_carries_what_was_skipped(self, tmp_path: Path) -> None:
        """A run that silently dropped a third of the library must say so."""
        index = SoundIndex(items=[item("One", 1.0)], skipped={"decoder_refused": 7})
        assert load_index(index.write(tmp_path / "i.json")).skipped == {"decoder_refused": 7}


class TestAgainstRealAudio:
    """Synthetic audio through the whole chain, to prove the parts fit together."""

    def test_a_synthetic_bass_ranks_a_bass_above_a_hiss(self) -> None:
        import math
        import random

        rate = 32000
        bass = [0.8 * math.sin(2 * math.pi * 80 * n / rate) for n in range(rate)]
        rng = random.Random(1)
        hiss = [rng.uniform(-0.8, 0.8) for _ in range(rate)]
        index = SoundIndex(
            items=[
                Item("Bassy", ".adv", "Bass", "T", "/x", "/x.ogg", describe(bass)),
                Item("Hissy", ".adv", "Perc", "T", "/y", "/y.ogg", describe(hiss)),
            ]
        )
        query = [0.3 * math.sin(2 * math.pi * 95 * n / rate) for n in range(rate)]
        assert index.rank(describe(query), limit=1)[0].item.name == "Bassy"


class TestFamilies:
    """The coarse families a caller picks from, and the one split that had to happen."""

    @pytest.mark.parametrize(
        ("category", "expected"),
        [
            ("Drums", "drum_kit"),
            ("Drums/Hybrid", "drum_kit"),
            ("Drums/MPE Kits", "drum_kit"),
            ("Racks/Drum Racks/Electronic", "drum_kit"),
            ("Drums/Drum Hits/Standard Hits/Cymbal", "drum_hit"),
            ("Drums/Drum Hits/Shaker", "drum_hit"),
            ("Single Instruments/Kick", "drum_hit"),
            ("Sounds/Bass", "bass"),
            ("Devices/Instruments/Operator/Bass", "bass"),
            ("Racks/Instrument Racks/Plucked", "guitar"),
            ("Sounds/Synth Pad", "pad"),
            ("Sounds/Synth Lead", "lead"),
            ("Sounds/Piano & Keys", "keys"),
            ("Sounds/Effects", "fx"),
            ("Presets/Nothing Familiar Here", ""),
        ],
    )
    def test_a_category_lands_in_its_family(self, category: str, expected: str) -> None:
        assert family_of(category) == expected

    def test_a_kit_and_a_single_hit_are_never_the_same_family(self) -> None:
        """A stem of a whole kit answered with five separate hi-hats is a useless answer."""
        assert family_of("Drums") != family_of("Drums/Drum Hits/Standard Hits/Hihat")

    def test_a_bass_drum_is_a_drum_and_not_a_bass(self) -> None:
        """Order is the whole rule: the path holds both words."""
        assert family_of("Drums/Drum Hits/Standard Hits/Bass Drum") == "drum_hit"

    def test_every_family_is_reachable(self) -> None:
        """A family no category can land in is a choice the tool offers and never fills."""
        for family in FAMILIES:
            probe = f"Sounds/{family.matches[0].title()}"
            assert family_of(probe) != "", f"{family.name} cannot be reached by {probe}"

    def test_the_names_are_unique_and_match_the_table(self) -> None:
        assert FAMILY_NAMES == tuple(f.name for f in FAMILIES)
        assert len(set(FAMILY_NAMES)) == len(FAMILY_NAMES)


class TestFamilyFilter:
    def test_a_family_filter_keeps_only_that_family(self) -> None:
        index = SoundIndex(
            items=[
                item("A Kit", 1.0, kind=".adg", category="Drums"),
                item("A Cymbal", 1.0, category="Drums/Drum Hits/Standard Hits/Cymbal"),
                item("A Bass", 1.0, category="Sounds/Bass"),
            ]
        )
        query = Descriptor(bands=band_vector(1.0))
        assert [h.item.name for h in index.rank(query, families=("drum_kit",))] == ["A Kit"]
        assert [h.item.name for h in index.rank(query, families=("bass",))] == ["A Bass"]

    def test_several_families_are_allowed_at_once(self) -> None:
        index = SoundIndex(
            items=[
                item("A Kit", 1.0, category="Drums"),
                item("A Bass", 1.0, category="Sounds/Bass"),
                item("A Pad", 1.0, category="Sounds/Synth Pad"),
            ]
        )
        hits = index.rank(band_query := Descriptor(bands=band_vector(1.0)),
                          families=("bass", "pad"))
        assert sorted(h.item.name for h in hits) == ["A Bass", "A Pad"]
        assert band_query.bands

    def test_no_family_filter_keeps_everything(self) -> None:
        index = SoundIndex(items=[item("A", 1.0, category="Drums"),
                                  item("B", 1.0, category="Sounds/Bass")])
        assert len(index.rank(Descriptor(bands=band_vector(1.0)))) == 2

    def test_an_item_with_no_family_is_dropped_by_any_filter(self) -> None:
        index = SoundIndex(items=[item("Odd", 1.0, category="Presets/Unfiled")])
        assert index.rank(Descriptor(bands=band_vector(1.0)), families=("bass",)) == []


def test_preferred_packs_path_reads_factory_packs_location_from_library_cfg(
    tmp_path: Path,
) -> None:
    """A custom Factory Packs directory in Library.cfg is read as a library root."""
    from live_maestro.sound_index import _preferred_packs_path

    cfg = tmp_path / "Library.cfg"
    packs_dir = tmp_path / "Custom Packs"
    cfg.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Ableton><ContentLibrary>"
        f'<PreferredFactoryPacksInstallationPath Value="{packs_dir}" />'
        "</ContentLibrary></Ableton>\n",
        encoding="utf-8",
    )
    assert _preferred_packs_path(cfg) == packs_dir
    assert _preferred_packs_path(tmp_path / "missing.cfg") is None
