"""The cross-check that says a transcription contradicts its own recording.

A pitch transcriber reports success on material it was not built for, in two opposite
ways: almost no notes on percussion, and a stutter of fragments on anything held. The
numbers here are the ones measured over the separated stems of one song, so a change in
the thresholds has to move real material rather than a synthetic case.
"""

from __future__ import annotations

from live_maestro.audio.descriptor import Descriptor
from live_maestro.server import _onset_disagreement

#: Twenty seconds, the stretch the tool looks at.
WINDOW = (0.0, 20.0)


def heard(onset_rate_hz: float, sustain_fraction: float) -> Descriptor:
    """The two fields the check reads, with the rest left at nothing."""
    return Descriptor(
        onset_rate_hz=onset_rate_hz, sustain_fraction=sustain_fraction, frames=600
    )


class TestMeasuredStems:
    """Each case is one stem of one song, with the rates measured on it.

    Rates are attacks per second in the stem's busiest 20 s, against the onsets the
    recording starts in the same stretch.
    """

    def test_a_bass_that_matches_its_recording_is_not_reported(self) -> None:
        answer = _onset_disagreement(heard(2.85, 0.63), written=67, window=WINDOW)
        assert answer is None

    def test_a_strummed_guitar_is_not_reported(self) -> None:
        """2.7 times what the audio strikes is a chord read as a few staggered notes."""
        answer = _onset_disagreement(heard(1.3, 0.84), written=70, window=WINDOW)
        assert answer is None

    def test_a_drum_kit_is_reported_as_too_few(self) -> None:
        answer = _onset_disagreement(heard(2.55, 0.71), written=0, window=WINDOW)
        assert answer is not None
        assert answer["kind"] == "too_few"

    def test_a_pad_is_reported_as_too_many(self) -> None:
        """The audio starts 0.1 events a second and the transcription writes 5.35."""
        answer = _onset_disagreement(heard(0.1, 1.0), written=107, window=WINDOW)
        assert answer is not None
        assert answer["kind"] == "too_many"
        assert answer["written_onsets_per_s"] == 5.35

    def test_merging_fragments_does_not_clear_a_pad(self) -> None:
        """sustained=true took the same pad to 2.25 a second, which is still not 0.1."""
        answer = _onset_disagreement(heard(0.1, 1.0), written=45, window=WINDOW)
        assert answer is not None
        assert answer["kind"] == "too_many"

    def test_a_keys_part_is_reported_as_too_many(self) -> None:
        answer = _onset_disagreement(heard(0.55, 1.0), written=106, window=WINDOW)
        assert answer is not None
        assert answer["kind"] == "too_many"


class TestBoundaries:
    def test_a_sparse_recording_transcribed_sparsely_says_nothing(self) -> None:
        """Below the percussion floor, few notes are what the recording holds."""
        answer = _onset_disagreement(heard(0.3, 0.3), written=1, window=WINDOW)
        assert answer is None

    def test_a_held_part_with_few_notes_written_says_nothing(self) -> None:
        """A slow line over a drone is a real thing to write and must not be flagged."""
        answer = _onset_disagreement(heard(0.0, 1.0), written=12, window=WINDOW)
        assert answer is None

    def test_a_struck_part_is_never_called_too_many(self) -> None:
        """Sustain is the gate: a busy percussive part writes densely and should."""
        answer = _onset_disagreement(heard(4.0, 0.4), written=200, window=WINDOW)
        assert answer is None

    def test_the_report_carries_both_rates_so_it_can_be_judged(self) -> None:
        answer = _onset_disagreement(heard(0.0, 1.0), written=195, window=WINDOW)
        assert answer is not None
        assert answer["heard_onsets_per_s"] == 0.0
        assert answer["notes_transcribed_there"] == 195
        assert answer["window_s"] == [0.0, 20.0]
