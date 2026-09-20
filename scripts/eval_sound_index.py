"""Score the sound index against the category every indexed item already carries.

Reading nine stems by eye gives nine opinions, and weights tuned until those nine look
right are weights fitted to nine examples. The browser category Ableton filed each item
under is a label nobody wrote for this purpose and therefore cannot be tuned to.

The measure is leave one out retrieval: a probe is matched with itself removed, and the
answer counts as right when the top candidate shares its family. Families are coarse on
purpose. Whether a preset is filed under Operator or Wavetable is a fact about which
device makes it, not about what it sounds like.

This cannot measure whether a family is the right answer for a recording with no
counterpart in the library, such as a sung line. That is scored separately, as the share
of probes whose best distance is above a given cut.

Usage:
    .venv/bin/python scripts/eval_sound_index.py
    .venv/bin/python scripts/eval_sound_index.py --probes 1500
    .venv/bin/python scripts/eval_sound_index.py --search 60
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from live_maestro.audio.descriptor import DEFAULT_WEIGHTS, Weights
from live_maestro.sound_index import Item, SoundIndex, load_index
from live_maestro.sound_index import family_of as sound_family


def family_of(item: Item) -> str:
    """The coarse family an item's category names, or an empty string for none.

    The table itself lives in :mod:`live_maestro.sound_index`, because the tool offers
    these families as a choice and a scorer that knew a different set would be scoring
    something the tool cannot do.
    """
    return sound_family(item.category)


def score(
    index: SoundIndex,
    probes: list[Item],
    labels: dict[str, str],
    weights: Weights,
    *,
    top_n: int = 5,
) -> dict[str, float]:
    """Leave one out retrieval over ``probes``, against the whole index."""
    right_1 = right_n = 0
    best_distances: list[float] = []
    for probe in probes:
        hits = index.rank(
            probe.descriptor,
            limit=top_n,
            kinds=(".adg", ".adv"),
            weights=weights,
            exclude=frozenset({probe.target}),
        )
        families = [labels.get(hit.item.target, "") for hit in hits]
        wanted = labels[probe.target]
        if families and families[0] == wanted:
            right_1 += 1
        if wanted in families:
            right_n += 1
        if hits:
            best_distances.append(hits[0].score)
    total = len(probes) or 1
    best_distances.sort()
    return {
        "top_1": right_1 / total,
        f"top_{top_n}": right_n / total,
        "median_best_distance": best_distances[len(best_distances) // 2]
        if best_distances
        else 0.0,
        "probes": float(total),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/eval_sound_index.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--probes", type=int, default=600,
                        help="how many probes to draw")
    parser.add_argument("--seed", type=int, default=11, help="draw seed")
    parser.add_argument("--search", type=int, default=0, metavar="N",
                        help="try N random weightings and report the best")
    args = parser.parse_args(argv)

    index = load_index()
    loadable = [i for i in index.items if i.kind in (".adg", ".adv")]
    labels = {i.target: family_of(i) for i in loadable}
    labelled = [i for i in loadable if labels[i.target]]
    print(f"{len(loadable)} loadable items, {len(labelled)} carry a family")

    counts: dict[str, int] = {}
    for item in labelled:
        counts[labels[item.target]] = counts.get(labels[item.target], 0) + 1
    biggest = max(counts.values()) / len(labelled)
    for name, count in sorted(counts.items(), key=lambda p: -p[1]):
        print(f"  {count:5d}  {name}")
    print(f"\nalways answering the largest family scores {biggest:.3f}")

    rng = random.Random(args.seed)
    probes = rng.sample(labelled, min(args.probes, len(labelled)))

    started = time.monotonic()
    base = score(index, probes, labels, DEFAULT_WEIGHTS)
    print(f"\ncurrent weights: top_1 {base['top_1']:.3f}  top_5 {base['top_5']:.3f}  "
          f"({time.monotonic() - started:.0f}s)")

    if args.search <= 0:
        return 0

    best_weights, best = DEFAULT_WEIGHTS, base["top_1"]
    for round_index in range(args.search):
        candidate = Weights(
            spectrum=1.0,
            centroid=round(rng.uniform(0.0, 1.2), 2),
            peakiness=round(rng.uniform(0.0, 1.5), 2),
            sustain=round(rng.uniform(0.0, 1.0), 2),
            onset=round(rng.uniform(0.0, 1.0), 2),
            crest=round(rng.uniform(0.0, 0.8), 2),
        )
        got = score(index, probes, labels, candidate)["top_1"]
        if got > best:
            best, best_weights = got, candidate
            print(f"  [{round_index:3d}] top_1 {got:.3f}  {candidate}")
    print(f"\nbest top_1 {best:.3f} against {base['top_1']:.3f} for the current weights")
    print(f"  {best_weights}")
    print(
        "\nA weighting picked by searching this set is fitted to this set. Re-run with "
        "another --seed before believing the gain."
    )
    if best_weights != DEFAULT_WEIGHTS:
        held_out = random.Random(args.seed + 1).sample(labelled, len(probes))
        a = score(index, held_out, labels, DEFAULT_WEIGHTS)["top_1"]
        b = score(index, held_out, labels, best_weights)["top_1"]
        print(f"on a fresh draw: current {a:.3f}, searched {b:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
