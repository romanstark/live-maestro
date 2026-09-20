"""Clean up what a transcriber heard, before any of it is written into Live.

A transcriber reports what is in the signal, and a played note puts more than one thing
there: a bass note at 40 Hz carries an octave, a twelfth and a double octave above it, each
loud enough to look like a note of its own. Measured against a generator's own MIDI export
of the same stem, an untouched transcription of a bass agreed on 93.7 percent of the pitch
classes and still sat nine semitones high at the median, because half of it was overtones.

Nothing here is a transcriber and nothing here looks at audio: it takes events in
seconds, removes what a physical note explains, and hands back events in seconds or
:class:`~live_maestro.models.Note` objects in beats.

The pitch tolerance is wide because the third harmonic sits 19.02 semitones above its
fundamental and the fifth 27.86, and a transcriber rounds both to the nearest semitone.
A ghost is recognised by being close to a harmonic rather than exactly on it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from live_maestro.models import Note

__all__ = [
    "DEFAULT_GAP_S",
    "HARMONIC_SEMITONES",
    "Event",
    "clean",
    "drop_harmonic_ghosts",
    "keep_lowest",
    "merge_repeats",
    "to_notes",
]

#: Where the harmonics of a note sit above it, in semitones. The second harmonic is an
#: octave, the third a twelfth, and from there they stop landing on the grid at all.
HARMONIC_SEMITONES: tuple[float, ...] = (12.0, 19.02, 24.0, 27.86, 31.02, 34.0, 36.0)

#: How far from a harmonic a pitch may sit and still be called one. Just over a
#: semitone, because a transcriber rounds 19.02 to 19 and the error compounds with its
#: own pitch estimate.
GHOST_TOLERANCE_SEMITONES = 0.6

#: How much of a candidate's length has to lie inside the lower note for the lower note
#: to explain it. A harmonic sounds while its fundamental does.
GHOST_OVERLAP = 0.5

#: A harmonic is quieter than what produces it. A candidate louder than the note below
#: it by this much is its own note, whatever interval it sits at.
GHOST_LOUDER_BY = 1.25

#: The shortest stretch of a note that survives monophonic reduction. What is left of
#: a note after a lower one has taken the time it overlapped is a fragment, and below
#: this it is not something anybody played: it is the tail of an overtone the ghost
#: filter did not catch. Measured on a transcribed bass, dropping fragments under this
#: takes the result from 420 notes to 377 against an export holding 350, and the median
#: pitch from seven semitones above the export's to five.
MIN_FRAGMENT_S = 0.09

#: Two notes of the same pitch separated by less than this, and not overlapping, are one
#: note the transcriber cut in half. At 120 BPM this is well under a sixty fourth.
#:
#: Overlapping notes of the same pitch are left alone, which is the whole difference
#: between this working and not. A transcriber reports a repeated note as two events
#: that overlap slightly, and treating a negative gap as a small one merges a part
#: playing sixteenths into a handful of long notes: measured against a generator's own
#: export, merging across overlaps took a keys part from 2,272 notes to 701 where the
#: export held 2,319, and the agreement on pitch content fell from 96.2 to 89.9 percent.
DEFAULT_GAP_S = 0.06

#: No merge may produce a note longer than this. A held note that a transcriber reported
#: in pieces is still one note; a chain of separate notes at one pitch is not.
MAX_MERGED_S = 8.0


@dataclass(frozen=True)
class Event:
    """One transcribed note, in seconds.

    Attributes:
        start_s: When it begins, in seconds from the start of the audio.
        end_s: When it ends.
        pitch: MIDI note number.
        amplitude: How loud the transcriber thought it was, 0.0 to 1.0. Used to tell a
            harmonic from a note, so a transcriber that does not report one should pass
            the same value everywhere rather than zero.
    """

    start_s: float
    end_s: float
    pitch: int
    amplitude: float = 1.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def overlap_s(self, other: Event) -> float:
        """Seconds the two sound at the same time."""
        return max(0.0, min(self.end_s, other.end_s) - max(self.start_s, other.start_s))


def _is_harmonic_of(candidate: Event, lower: Event) -> bool:
    """Return whether ``lower`` sounding would produce ``candidate``'s pitch."""
    distance = candidate.pitch - lower.pitch
    if distance <= 0:
        return False
    return any(abs(distance - h) <= GHOST_TOLERANCE_SEMITONES for h in HARMONIC_SEMITONES)


def drop_harmonic_ghosts(events: list[Event]) -> list[Event]:
    """Remove events a lower, louder, simultaneous event already explains.

    An event survives unless some other event is below it at a harmonic interval, sounds
    through at least :data:`GHOST_OVERLAP` of it, and is not quieter than it by
    :data:`GHOST_LOUDER_BY`.

    Only one pass, and only against events that themselves survived, so a fundamental
    removed as somebody else's harmonic cannot take its own harmonics down with it.

    >>> low = Event(0.0, 1.0, 40, 0.5)
    >>> octave = Event(0.0, 1.0, 52, 0.4)
    >>> [e.pitch for e in drop_harmonic_ghosts([low, octave])]
    [40]
    >>> louder = Event(0.0, 1.0, 52, 0.9)
    >>> [e.pitch for e in drop_harmonic_ghosts([low, louder])]
    [40, 52]
    >>> apart = Event(2.0, 3.0, 52, 0.4)
    >>> [e.pitch for e in drop_harmonic_ghosts([low, apart])]
    [40, 52]
    >>> seventh = Event(0.0, 1.0, 50, 0.4)
    >>> [e.pitch for e in drop_harmonic_ghosts([low, seventh])]
    [40, 50]
    """
    ordered = sorted(events, key=lambda e: (e.pitch, e.start_s))
    kept: list[Event] = []
    for event in ordered:
        explained = False
        for lower in kept:
            if lower.pitch >= event.pitch:
                continue
            if not _is_harmonic_of(event, lower):
                continue
            if event.duration_s <= 0.0:
                continue
            if event.overlap_s(lower) < GHOST_OVERLAP * event.duration_s:
                continue
            if event.amplitude > lower.amplitude * GHOST_LOUDER_BY:
                continue
            explained = True
            break
        if not explained:
            kept.append(event)
    return sorted(kept, key=lambda e: (e.start_s, e.pitch))


def keep_lowest(events: list[Event]) -> list[Event]:
    """Reduce to one line: at no moment does more than one note sound.

    For a bass or a sung line, played one note at a time and transcribed as several.
    Anything genuinely polyphonic loses its upper voices, which is why this is not part
    of :func:`clean` unless it is asked for.

    A note keeps only the stretches where nothing lower is sounding. Where a lower note
    covers its beginning it starts later, where a lower note begins during it it ends
    there, and where a lower note covers it entirely it goes. The result is monophonic
    by construction rather than by a threshold: measuring the overlap as a share of the
    higher note's own length lets a long high note survive a run of short low ones,
    because each covers only a fraction of it.

    Only the first surviving stretch of a note is kept. A note interrupted in the middle
    by a lower one was one event in the transcription and stays one here. A stretch
    shorter than :data:`MIN_FRAGMENT_S` is dropped rather than kept, unless the whole
    note was that short to begin with.

    >>> together = [Event(0.0, 1.0, 40), Event(0.2, 0.8, 55), Event(1.5, 2.0, 43)]
    >>> [e.pitch for e in keep_lowest(together)]
    [40, 43]
    >>> long_high = [Event(0.0, 4.0, 52), Event(1.0, 1.2, 40)]
    >>> [(e.pitch, round(e.end_s, 2)) for e in keep_lowest(long_high)]
    [(52, 1.0), (40, 1.2)]
    >>> grazed = keep_lowest([Event(0.0, 1.0, 40), Event(0.95, 2.0, 55)])
    >>> [(e.pitch, round(e.start_s, 2), round(e.end_s, 2)) for e in grazed]
    [(40, 0.0, 1.0), (55, 1.0, 2.0)]
    >>> overlapping = keep_lowest([Event(0.0, 3.0, 50), Event(0.1, 2.0, 38)])
    >>> all(a.overlap_s(b) == 0.0 for a in overlapping for b in overlapping if a is not b)
    True
    """
    ordered = sorted(events, key=lambda e: (e.pitch, e.start_s))
    taken: list[tuple[float, float]] = []
    kept: list[Event] = []
    for event in ordered:
        free = _first_free(event.start_s, event.end_s, taken)
        if free is None:
            continue
        begin, finish = free
        if finish - begin < MIN_FRAGMENT_S and finish - begin < event.duration_s:
            continue
        kept.append(replace(event, start_s=begin, end_s=finish))
        taken.append((begin, finish))
        taken.sort()
    return sorted(kept, key=lambda e: (e.start_s, e.pitch))


def _first_free(
    start: float, end: float, taken: list[tuple[float, float]]
) -> tuple[float, float] | None:
    """The first stretch of ``[start, end)`` that no taken span covers.

    Lower pitches are placed first, so a span already taken belongs to a note below
    this one and this one has to give way to it.
    """
    at = start
    for begin, finish in sorted(taken):
        if finish <= at:
            continue
        if begin >= end:
            break
        if begin > at:
            return (at, min(begin, end))
        at = max(at, finish)
        if at >= end:
            return None
    return (at, end) if end - at > 1e-9 else None


def merge_repeats(events: list[Event], gap_s: float = DEFAULT_GAP_S) -> list[Event]:
    """Join same-pitch events separated by less than ``gap_s`` into one.

    A held note that wavers in level is reported as two, and two notes written where one
    was played is audible as a stutter.

    Overlapping events are left alone: a transcriber reports a re-articulated note as
    two events that overlap, and merging those turns a part playing sixteenths into a
    few long notes.

    >>> halves = [Event(0.0, 1.0, 60), Event(1.02, 2.0, 60), Event(3.0, 3.5, 60)]
    >>> [(e.start_s, e.end_s) for e in merge_repeats(halves)]
    [(0.0, 2.0), (3.0, 3.5)]
    >>> played_twice = [Event(0.0, 1.0, 60), Event(0.9, 2.0, 60)]
    >>> len(merge_repeats(played_twice))
    2
    """
    out: list[Event] = []
    for event in sorted(events, key=lambda e: (e.pitch, e.start_s)):
        if out and out[-1].pitch == event.pitch:
            previous = out[-1]
            gap = event.start_s - previous.end_s
            grown = max(previous.end_s, event.end_s) - previous.start_s
            if 0.0 <= gap <= gap_s and grown <= MAX_MERGED_S:
                out[-1] = replace(
                    previous,
                    end_s=max(previous.end_s, event.end_s),
                    amplitude=max(previous.amplitude, event.amplitude),
                )
                continue
        out.append(event)
    return sorted(out, key=lambda e: (e.start_s, e.pitch))


def clean(
    events: list[Event], *, monophonic: bool = False, merge: bool = False
) -> list[Event]:
    """Remove what a played note explains, and nothing that measured better left in.

    Args:
        events: What the transcriber reported.
        monophonic: True for material played one note at a time, such as a bass or a
            sung line. Also reduces the result to the lowest note sounding, which is
            wrong for anything that plays chords.
        merge: Join same-pitch events separated by a small gap. Off by default:
            measured against a generator's own export of five stems it cost agreement
            on pitch content every time, between one and ten points, because a part
            playing repeated notes has small gaps by construction.

    Returns:
        The events that survive, in time order.

    Note:
        Ghost removal runs first, so a fundamental reported in two pieces cannot shelter
        a harmonic across the gap between them.
    """
    kept = drop_harmonic_ghosts(events)
    if monophonic:
        kept = keep_lowest(kept)
    return merge_repeats(kept) if merge else kept



def to_notes(
    events: list[Event], tempo: float, *, velocity: float = 100.0
) -> tuple[Note, ...]:
    """Convert seconds to clip beats at one tempo.

    Args:
        events: Cleaned events, in seconds from the start of the audio.
        tempo: Beats per minute. A drifting tempo needs a map rather than a number, and
            this takes a number: a transcription placed with one tempo against audio
            that drifts will separate from it over the length of a song.
        velocity: Used where an event carries no amplitude. An amplitude between 0 and 1
            is scaled onto 1 to 127.

    Returns:
        Notes in clip beats, in time order.

    Raises:
        ValueError: The tempo is not positive.

    >>> notes = to_notes([Event(0.0, 0.5, 60, 0.5), Event(1.0, 1.5, 64, 1.0)], 120.0)
    >>> [(n.pitch, n.start_time, n.duration) for n in notes]
    [(60, 0.0, 1.0), (64, 2.0, 1.0)]
    """
    if tempo <= 0.0:
        raise ValueError(f"tempo {tempo} is not positive")
    per_beat = 60.0 / tempo
    out: list[Note] = []
    for event in sorted(events, key=lambda e: (e.start_s, e.pitch)):
        beats = event.duration_s / per_beat
        if beats <= 0.0:
            continue
        loudness = (
            max(1.0, min(127.0, round(event.amplitude * 127.0)))
            if 0.0 < event.amplitude <= 1.0
            else velocity
        )
        out.append(
            Note(
                pitch=event.pitch,
                start_time=round(event.start_s / per_beat, 6),
                duration=round(beats, 6),
                velocity=loudness,
            )
        )
    return tuple(out)
