"""Build the sound index from Ableton's own preview audio.

One pass over every previewed item in the installed libraries: decode the preview,
reduce it to a descriptor, write one index file. The index is what ``match_sound``
searches; it describes one machine's installed content and is not committed.

Decoding and the transform are CPU bound and independent per file, so the work runs one
process per core, and progress prints because a run takes minutes rather than seconds.
``scripts/build_sound_index.py`` wraps this module, which ships in the wheel for a
caller who has no checkout to run a script from.

Usage:
    live-maestro-index
    live-maestro-index --limit 200 --out /tmp/try.json
    live-maestro-index --kinds .adg .adv
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from live_maestro.audio.decode import (
    TOOL,
    DecodeError,
    decode_to_samples,
    tool_available,
)
from live_maestro.audio.descriptor import describe
from live_maestro.sound_index import (
    INDEX_VERSION,
    KINDS,
    Item,
    SoundIndex,
    default_index_path,
    default_roots,
    discover_previews,
)


def _measure(item: Item) -> tuple[Item, str]:
    """Decode one preview and attach its descriptor. Runs in a worker process.

    Returns the item and an empty string, or the unchanged item and a reason code. A
    reason is counted rather than raised: one unreadable preview out of thousands must
    not end a run that takes half an hour.
    """
    try:
        samples = decode_to_samples(item.preview)
    except DecodeError as exc:
        return item, exc.code
    except (OSError, ValueError, subprocess.SubprocessError):
        return item, "decode_crashed"
    descriptor = describe(samples)
    if descriptor.frames == 0:
        return item, "too_short"
    return (
        Item(
            name=item.name,
            kind=item.kind,
            category=item.category,
            library=item.library,
            target=item.target,
            preview=item.preview,
            descriptor=descriptor,
        ),
        "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="live-maestro-index",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", type=Path, default=None, metavar="PATH",
                        help=f"where to write (default {default_index_path()})")
    parser.add_argument("--root", type=Path, action="append", default=[], metavar="DIR",
                        help="library root to search; repeatable, replaces the defaults")
    parser.add_argument("--kinds", nargs="*", default=[], metavar="EXT",
                        help=f"only these item kinds (of {' '.join(sorted(KINDS))})")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="stop after N items, for a trial run")
    parser.add_argument("--workers", type=int, default=0, metavar="N",
                        help="worker processes (default: one per core)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be indexed and decode nothing")
    args = parser.parse_args(argv)

    if not tool_available():
        print(
            f"No decoder available ({TOOL}). Install FFmpeg or live-maestro[audio].",
            file=sys.stderr,
        )
        return 2

    roots = tuple(args.root) if args.root else default_roots()
    if not roots:
        print(
            "No Ableton library root was found. A root is a folder holding an "
            "'Ableton Folder Info'; the usual places are under ~/Music/Ableton and "
            "inside the Live application bundle. Name one with --root if the library "
            "is somewhere else.",
            file=sys.stderr,
        )
        return 2
    print(f"roots: {', '.join(str(r) for r in roots)}")

    found = discover_previews(roots)
    if args.kinds:
        wanted = {k if k.startswith(".") else f".{k}" for k in args.kinds}
        found = [item for item in found if item.kind in wanted]
    if args.limit > 0:
        found = found[: args.limit]
    print(f"{len(found)} previewed items")
    if args.dry_run:
        by_kind: dict[str, int] = {}
        for item in found:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        for kind, count in sorted(by_kind.items()):
            print(f"  {count:6d}  {kind}  {KINDS.get(kind, '')}")
        return 0

    workers = args.workers or (os.cpu_count() or 4)
    measured: list[Item] = []
    skipped: dict[str, int] = {}
    started = time.monotonic()
    done = 0

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for item, reason in pool.map(_measure, found, chunksize=8):
            done += 1
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                measured.append(item)
            if done % 250 == 0 or done == len(found):
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed > 0 else 0.0
                left = (len(found) - done) / rate if rate > 0 else 0.0
                print(f"  {done}/{len(found)}  {rate:.1f}/s  {left/60:.1f} min left",
                      flush=True)

    index = SoundIndex(
        items=measured,
        version=INDEX_VERSION,
        built_on=datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        roots=[str(r) for r in roots],
        skipped=skipped,
    )
    target = index.write(args.out or default_index_path())
    elapsed = time.monotonic() - started

    print(f"\n{len(measured)} indexed, {sum(skipped.values())} skipped, "
          f"{elapsed/60:.1f} min on {workers} workers")
    for reason, count in sorted(skipped.items()):
        print(f"  skipped {count:6d}  {reason}")
    print(f"-> {target}  {target.stat().st_size/1_000_000:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
