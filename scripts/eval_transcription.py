#!/usr/bin/env python
"""Score the transcription cleanup against a generator's own MIDI export.

Recomputes the figures quoted in ``transcribe_stem`` and in
``measurements/spikes/spike-transcription-cleanup.json``, so a change to the cleanup
that moves them has to move them here first. Pitch content only: the export's timing
drifts against its own audio.

Usage::

    .venv/bin/python scripts/eval_transcription.py
    .venv/bin/python scripts/eval_transcription.py --stems DIR --midi DIR

The directories default to the ones the record was taken from, and each stem is matched
to the export of the same name. Transcribing four stems takes a few minutes.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from live_maestro import transcribe as bridge
from live_maestro.music.transcription import clean, to_notes

#: Where the record was taken from. Neither directory ships with the project.
DEFAULT_STEMS = Path.home() / "Music/Songs/Northlight-Suno-Exports/STEMS"
DEFAULT_MIDI = Path.home() / "Music/Songs/Northlight-Suno-Exports/MIDI"

#: Stem file to export file. The generator names them differently on each side.
PAIRS: tuple[tuple[str, str], ...] = (
    ("4 Bass.wav", "Bass.mid"),
    ("5 Acoustic Guitar.wav", "Acoustic Guitar.mid"),
    ("6 Synth Pad.wav", "Synth Pad.mid"),
    ("7 Synth Keys.wav", "Synth Keys.mid"),
)

#: Tempo the song was generated at, for turning seconds into beats.
TEMPO = 128.0


def read_pitches(path: Path) -> list[int]:
    """Every note-on pitch of a standard MIDI file, in file order.

    A parser rather than a dependency, for the same reason the FFT in
    :mod:`live_maestro.audio.descriptor` is one: the whole need is note-on pitches, and
    the alternative is a package. Running status is honoured, because exporters use it.

    Args:
        path: A type 0 or type 1 standard MIDI file.

    Returns:
        The pitches. A note-on of velocity 0 is a note-off and is not counted.

    Raises:
        ValueError: The file does not begin with a header chunk.
    """
    data = path.read_bytes()
    if data[:4] != b"MThd":
        raise ValueError(f"{path} is not a standard MIDI file")
    tracks = int.from_bytes(data[10:12], "big")
    at = 8 + int.from_bytes(data[4:8], "big")
    pitches: list[int] = []

    def length_at(index: int) -> tuple[int, int]:
        """Read a variable length quantity, returning (value, index after it)."""
        value = 0
        while True:
            value = (value << 7) | (data[index] & 0x7F)
            more = data[index] & 0x80
            index += 1
            if not more:
                return value, index

    for _ in range(tracks):
        if data[at : at + 4] != b"MTrk":
            raise ValueError(f"{path}: expected a track chunk at byte {at}")
        end = at + 8 + int.from_bytes(data[at + 4 : at + 8], "big")
        cursor = at + 8
        status = 0
        while cursor < end:
            while data[cursor] & 0x80:
                cursor += 1
            cursor += 1
            byte = data[cursor]
            if byte == 0xFF:
                cursor += 2
                size, cursor = length_at(cursor)
                cursor += size
                continue
            if byte in (0xF0, 0xF7):
                cursor += 1
                size, cursor = length_at(cursor)
                cursor += size
                continue
            if byte & 0x80:
                status = byte
                cursor += 1
            kind = status & 0xF0
            if kind in (0x80, 0x90):
                pitch, velocity = data[cursor], data[cursor + 1]
                cursor += 2
                if kind == 0x90 and velocity > 0:
                    pitches.append(pitch)
            elif kind in (0xA0, 0xB0, 0xE0):
                cursor += 2
            else:
                cursor += 1
        at = end
    return pitches


def overlap_percent(got: list[int], want: list[int]) -> float:
    """How much of two pitch class distributions coincides, 0 to 100.

    Pitch classes rather than pitches, because a transcriber reporting a bass an octave
    high has the part right and the register wrong, and those are different mistakes.
    """
    if not got or not want:
        return 0.0
    here = [0.0] * 12
    there = [0.0] * 12
    for pitch in got:
        here[pitch % 12] += 1.0 / len(got)
    for pitch in want:
        there[pitch % 12] += 1.0 / len(want)
    return 100.0 * sum(min(a, b) for a, b in zip(here, there))


def configurations(events: list) -> list[tuple[str, list]]:
    """Each cleanup setting, named as the record names it."""
    return [
        ("raw", events),
        ("ghosts_only", clean(events)),
        ("ghosts_and_lowest", clean(events, monophonic=True)),
        ("with_merge", clean(events, merge=True)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stems", type=Path, default=DEFAULT_STEMS)
    parser.add_argument("--midi", type=Path, default=DEFAULT_MIDI)
    args = parser.parse_args()

    if not bridge.is_available():
        print("The basic-pitch environment is missing; nothing to measure.")
        return 2

    missing = [d for d in (args.stems, args.midi) if not d.is_dir()]
    if missing:
        for directory in missing:
            print(f"not a directory: {directory}")
        print("These are the private files the record was taken from; pass your own.")
        return 2

    print(f"{'stem':18}{'config':20}{'notes':>7}{'median':>8}{'offset':>8}{'overlap':>9}")
    for wav, mid in PAIRS:
        stem = args.stems / wav
        export_file = args.midi / mid
        if not stem.is_file() or not export_file.is_file():
            print(f"{wav:18}missing, skipped")
            continue
        export = read_pitches(export_file)
        reference = statistics.median(export)
        print(f"{wav[:17]:18}{'export':20}{len(export):>7}{reference:>8.0f}"
              f"{0:>8}{100.0:>9.1f}")
        events = bridge.transcribe(stem)
        for label, kept in configurations(events):
            pitches = [n.pitch for n in to_notes(kept, TEMPO)]
            if not pitches:
                print(f"{'':18}{label:20}{0:>7}")
                continue
            median = statistics.median(pitches)
            print(f"{'':18}{label:20}{len(pitches):>7}{median:>8.0f}"
                  f"{median - reference:>8.0f}{overlap_percent(pitches, export):>9.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
