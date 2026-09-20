"""What the cleanup has to get right before any of it is written into a clip.

Synthetic throughout. The real measurement is against a generator's own MIDI export of
five stems and lives in ``measurements/spikes/``; these are the properties that must
hold whatever material arrives.
"""

from __future__ import annotations

import pytest

from live_maestro.music.transcription import (
    DEFAULT_GAP_S,
    HARMONIC_SEMITONES,
    MAX_MERGED_S,
    Event,
    clean,
    drop_harmonic_ghosts,
    keep_lowest,
    merge_repeats,
    to_notes,
)


def ev(start: float, end: float, pitch: int, amp: float = 0.5) -> Event:
    return Event(start, end, pitch, amp)


class TestEvent:
    def test_duration_is_never_negative(self) -> None:
        assert ev(1.0, 0.5, 60).duration_s == 0.0

    def test_overlap_of_two_that_do_not_touch_is_zero(self) -> None:
        assert ev(0.0, 1.0, 60).overlap_s(ev(2.0, 3.0, 60)) == 0.0

    def test_overlap_is_the_shared_span(self) -> None:
        assert ev(0.0, 1.0, 60).overlap_s(ev(0.5, 2.0, 60)) == pytest.approx(0.5)

    def test_overlap_is_symmetric(self) -> None:
        a, b = ev(0.0, 1.0, 60), ev(0.5, 2.0, 64)
        assert a.overlap_s(b) == b.overlap_s(a)


class TestHarmonicGhosts:
    @pytest.mark.parametrize("interval", [12, 19, 24, 28, 31])
    def test_a_quieter_note_at_a_harmonic_is_removed(self, interval: int) -> None:
        low = ev(0.0, 1.0, 40, 0.8)
        ghost = ev(0.0, 1.0, 40 + interval, 0.3)
        assert [e.pitch for e in drop_harmonic_ghosts([low, ghost])] == [40]

    @pytest.mark.parametrize("interval", [1, 2, 3, 5, 7, 10, 14, 17])
    def test_a_note_at_no_harmonic_survives(self, interval: int) -> None:
        """A fifth and a third are chord tones, not overtones of the note below."""
        low = ev(0.0, 1.0, 40, 0.8)
        other = ev(0.0, 1.0, 40 + interval, 0.3)
        assert len(drop_harmonic_ghosts([low, other])) == 2

    def test_a_louder_note_at_a_harmonic_survives(self) -> None:
        """An octave played on purpose is louder than an octave that came for free."""
        low = ev(0.0, 1.0, 40, 0.3)
        played = ev(0.0, 1.0, 52, 0.9)
        assert len(drop_harmonic_ghosts([low, played])) == 2

    def test_a_note_that_does_not_overlap_survives(self) -> None:
        low = ev(0.0, 1.0, 40, 0.8)
        later = ev(5.0, 6.0, 52, 0.2)
        assert len(drop_harmonic_ghosts([low, later])) == 2

    def test_a_note_barely_overlapping_survives(self) -> None:
        low = ev(0.0, 1.0, 40, 0.8)
        grazing = ev(0.95, 2.0, 52, 0.2)
        assert len(drop_harmonic_ghosts([low, grazing])) == 2

    def test_a_note_below_is_never_removed_by_one_above(self) -> None:
        """Removing the fundamental is the failure this whole module exists to avoid."""
        low = ev(0.0, 1.0, 40, 0.2)
        high = ev(0.0, 1.0, 52, 0.9)
        assert 40 in [e.pitch for e in drop_harmonic_ghosts([low, high])]

    def test_a_removed_ghost_cannot_remove_its_own_harmonics(self) -> None:
        """Only survivors explain anything, or one removal cascades up the series.

        80 sits 28 semitones above 52, which is a harmonic of it, and 40 above the
        fundamental at 40, which is no harmonic of anything. It may only be removed on
        the strength of a note that is still there, and 52 is not.
        """
        low = ev(0.0, 1.0, 40, 0.9)
        octave = ev(0.0, 1.0, 52, 0.3)
        harmonic_of_the_ghost = ev(0.0, 1.0, 80, 0.2)
        kept = [e.pitch for e in drop_harmonic_ghosts([low, octave, harmonic_of_the_ghost])]
        assert 52 not in kept
        assert 80 in kept

    def test_tolerance_reaches_the_rounded_twelfth(self) -> None:
        """The third harmonic sits at 19.02 semitones and a transcriber says 19."""
        assert any(abs(19 - h) <= 0.6 for h in HARMONIC_SEMITONES)

    def test_the_result_is_in_time_order(self) -> None:
        events = [ev(3.0, 4.0, 60), ev(1.0, 2.0, 62), ev(2.0, 2.5, 55)]
        out = drop_harmonic_ghosts(events)
        assert [e.start_s for e in out] == sorted(e.start_s for e in out)

    def test_an_empty_list_stays_empty(self) -> None:
        assert drop_harmonic_ghosts([]) == []


class TestKeepLowest:
    def test_a_higher_simultaneous_note_goes(self) -> None:
        assert [e.pitch for e in keep_lowest([ev(0.0, 1.0, 40), ev(0.1, 0.9, 55)])] == [40]

    def test_a_higher_note_at_another_time_stays(self) -> None:
        out = keep_lowest([ev(0.0, 1.0, 40), ev(2.0, 3.0, 55)])
        assert [e.pitch for e in out] == [40, 55]

    def test_a_note_grazed_at_its_start_begins_where_the_lower_one_ends(self) -> None:
        """Dropping it whole would lose the stretch where it was the only thing sounding."""
        out = keep_lowest([ev(0.0, 1.0, 40), ev(0.95, 2.0, 55)])
        assert [(e.pitch, e.start_s, e.end_s) for e in out] == [(40, 0.0, 1.0), (55, 1.0, 2.0)]

    def test_a_long_high_note_never_survives_short_low_ones(self) -> None:
        """Measured against a real bass: 110 simultaneous pairs survived a share rule."""
        line = [ev(0.0, 6.0, 52)] + [ev(0.5 + i, 0.7 + i, 40) for i in range(4)]
        out = keep_lowest(line)
        assert all(a.overlap_s(b) == 0.0 for a in out for b in out if a is not b)

    def test_nothing_sounds_at_the_same_time_as_anything_else(self) -> None:
        import random as _r
        rng = _r.Random(9)
        line = [ev(rng.uniform(0, 20), 0.0, rng.randint(30, 70)) for _ in range(40)]
        line = [ev(e.start_s, e.start_s + rng.uniform(0.1, 3.0), e.pitch) for e in line]
        out = keep_lowest(line)
        assert all(a.overlap_s(b) == 0.0 for a in out for b in out if a is not b)

    def test_a_line_of_single_notes_is_untouched(self) -> None:
        line = [ev(float(i), i + 0.9, 40 + i) for i in range(5)]
        assert len(keep_lowest(line)) == 5


class TestMergeRepeats:
    def test_two_halves_of_one_note_become_one(self) -> None:
        out = merge_repeats([ev(0.0, 1.0, 60), ev(1.0 + DEFAULT_GAP_S / 2, 2.0, 60)])
        assert len(out) == 1
        assert out[0].end_s == pytest.approx(2.0)

    def test_a_wide_gap_is_two_notes(self) -> None:
        assert len(merge_repeats([ev(0.0, 1.0, 60), ev(3.0, 4.0, 60)])) == 2

    def test_overlapping_notes_are_left_alone(self) -> None:
        """A re-articulated note arrives as two events that overlap, and it is two."""
        assert len(merge_repeats([ev(0.0, 1.0, 60), ev(0.9, 2.0, 60)])) == 2

    def test_different_pitches_never_merge(self) -> None:
        assert len(merge_repeats([ev(0.0, 1.0, 60), ev(1.0, 2.0, 62)])) == 2

    def test_a_merge_may_not_grow_past_the_ceiling(self) -> None:
        pieces = [ev(i * 1.0, i * 1.0 + 0.98, 60) for i in range(20)]
        assert max(e.duration_s for e in merge_repeats(pieces)) <= MAX_MERGED_S + 1.0

    def test_the_louder_amplitude_survives_a_merge(self) -> None:
        out = merge_repeats([ev(0.0, 1.0, 60, 0.2), ev(1.01, 2.0, 60, 0.9)])
        assert out[0].amplitude == pytest.approx(0.9)


class TestClean:
    def test_the_default_does_not_merge(self) -> None:
        """Merging cost agreement on every stem it was measured against."""
        halves = [ev(0.0, 1.0, 60), ev(1.01, 2.0, 60)]
        assert len(clean(halves)) == 2
        assert len(clean(halves, merge=True)) == 1

    def test_monophonic_reduces_to_one_line(self) -> None:
        chord = [ev(0.0, 1.0, 40, 0.9), ev(0.0, 1.0, 45, 0.9), ev(0.0, 1.0, 47, 0.9)]
        assert len(clean(chord)) == 3
        assert len(clean(chord, monophonic=True)) == 1

    def test_a_chord_with_no_harmonic_relation_survives_intact(self) -> None:
        chord = [ev(0.0, 1.0, 60, 0.7), ev(0.0, 1.0, 63, 0.7), ev(0.0, 1.0, 67, 0.7)]
        assert len(clean(chord)) == 3


class TestToNotes:
    def test_seconds_become_beats_at_the_tempo(self) -> None:
        notes = to_notes([ev(0.0, 0.5, 60), ev(1.0, 1.5, 64)], 120.0)
        assert [(n.start_time, n.duration) for n in notes] == [(0.0, 1.0), (2.0, 1.0)]

    def test_a_faster_tempo_gives_more_beats(self) -> None:
        slow = to_notes([ev(0.0, 1.0, 60)], 60.0)[0]
        fast = to_notes([ev(0.0, 1.0, 60)], 240.0)[0]
        assert fast.duration == pytest.approx(slow.duration * 4)

    def test_amplitude_becomes_velocity_in_range(self) -> None:
        notes = to_notes([ev(0.0, 1.0, 60, 0.01), ev(2.0, 3.0, 62, 1.0)], 120.0)
        assert all(1.0 <= n.velocity <= 127.0 for n in notes)
        assert notes[1].velocity > notes[0].velocity

    def test_a_zero_length_event_is_dropped(self) -> None:
        assert to_notes([ev(1.0, 1.0, 60)], 120.0) == ()

    def test_a_tempo_of_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not positive"):
            to_notes([ev(0.0, 1.0, 60)], 0.0)

    def test_the_notes_come_back_in_time_order(self) -> None:
        notes = to_notes([ev(2.0, 3.0, 60), ev(0.0, 1.0, 62)], 120.0)
        assert [n.start_time for n in notes] == sorted(n.start_time for n in notes)
