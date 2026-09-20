"""What the descriptor has to get right for a ranking built on it to mean anything.

Every case here is synthetic and deterministic. A test that needs a sound library
passes or fails on which packs are installed, which is a property of a machine and not
of this code; the library is exercised by the index builder instead.

The assertions are relations rather than values. A centroid of 219.8 Hz for a 220 Hz
tone is a fact about this window and this transform size, and pinning it turns a change
of frame size into a test failure that says nothing. That a tone reads lower than noise
is the property the ranking rests on.
"""

from __future__ import annotations

import cmath
import itertools
import math
import random

import pytest

from live_maestro.audio.descriptor import (
    BANDS,
    FRAME,
    SAMPLE_RATE,
    Descriptor,
    band_edges,
    describe,
    distance,
    fft,
    loudest_window,
)


def tone(hz: float, seconds: float = 0.5, amplitude: float = 0.8) -> list[float]:
    count = int(SAMPLE_RATE * seconds)
    return [amplitude * math.sin(2 * math.pi * hz * n / SAMPLE_RATE) for n in range(count)]


def noise(seconds: float = 0.5, seed: int = 0) -> list[float]:
    rng = random.Random(seed)
    return [rng.uniform(-0.8, 0.8) for _ in range(int(SAMPLE_RATE * seconds))]


def clicks(every_s: float, seconds: float = 2.0) -> list[float]:
    count = int(SAMPLE_RATE * seconds)
    step = max(1, int(SAMPLE_RATE * every_s))
    out = [0.0] * count
    for start in range(0, count, step):
        for i in range(min(200, count - start)):
            out[start + i] = math.exp(-i / 30.0) * math.sin(2 * math.pi * 900 * i / SAMPLE_RATE)
    return out


class TestFft:
    def test_a_single_impulse_has_a_flat_magnitude_spectrum(self) -> None:
        spectrum = fft([1 + 0j] + [0j] * 7)
        assert all(abs(abs(v) - 1.0) < 1e-9 for v in spectrum)

    def test_a_constant_puts_everything_in_the_first_bin(self) -> None:
        spectrum = fft([1 + 0j] * 8)
        assert abs(spectrum[0].real - 8.0) < 1e-9
        assert all(abs(v) < 1e-9 for v in spectrum[1:])

    def test_a_sine_peaks_at_its_own_bin(self) -> None:
        size = 64
        cycles = 7
        wave = [complex(math.sin(2 * math.pi * cycles * n / size), 0.0) for n in range(size)]
        magnitudes = [abs(v) for v in fft(wave)[: size // 2]]
        assert magnitudes.index(max(magnitudes)) == cycles

    def test_it_matches_the_direct_transform(self) -> None:
        rng = random.Random(4)
        values = [complex(rng.uniform(-1, 1), 0.0) for _ in range(32)]
        fast = fft(values)
        for k in range(len(values)):
            slow = sum(
                v * cmath.exp(-2j * math.pi * k * n / len(values))
                for n, v in enumerate(values)
            )
            assert abs(fast[k] - slow) < 1e-9

    @pytest.mark.parametrize("size", [0, 3, 6, 100])
    def test_a_length_that_is_not_a_power_of_two_is_refused(self, size: int) -> None:
        with pytest.raises(ValueError, match="power of two"):
            fft([0j] * size)


class TestBandEdges:
    def test_there_is_one_more_edge_than_there_are_bands(self) -> None:
        assert len(band_edges()) == BANDS + 1

    def test_edges_never_repeat(self) -> None:
        """A repeated edge is an empty band, which would always read as zero energy."""
        edges = band_edges()
        assert all(b > a for a, b in itertools.pairwise(edges))

    def test_the_bands_are_logarithmic_and_not_linear(self) -> None:
        """A linear split leaves every bass octave in one band, which is the failure."""
        edges = band_edges()
        widths = [b - a for a, b in itertools.pairwise(edges)]
        assert widths[-1] > widths[0] * 4


class TestDescribe:
    def test_audio_shorter_than_one_frame_says_so_instead_of_guessing(self) -> None:
        answer = describe([0.1] * (FRAME - 1))
        assert answer.frames == 0
        assert answer.bands == ()
        assert answer.duration_s > 0.0

    def test_a_low_tone_reads_lower_than_a_high_one(self) -> None:
        assert describe(tone(110)).centroid_hz < describe(tone(3520)).centroid_hz

    def test_the_centroid_of_a_pure_tone_lands_near_the_tone(self) -> None:
        answer = describe(tone(440))
        assert 380.0 < answer.centroid_hz < 520.0

    def test_a_tone_is_less_flat_than_noise(self) -> None:
        assert describe(tone(440)).flatness < describe(noise()).flatness

    def test_a_tone_is_peakier_than_noise(self) -> None:
        """The reading that separates a sung note from a cymbal, which flatness does not."""
        assert describe(tone(440)).peakiness > describe(noise()).peakiness

    def test_peakiness_separates_a_harmonic_series_from_noise(self) -> None:
        harmonics = [
            sum(
                (0.8 / h) * math.sin(2 * math.pi * 220 * h * n / SAMPLE_RATE)
                for h in range(1, 12)
            )
            for n in range(int(SAMPLE_RATE * 0.5))
        ]
        assert describe(harmonics).peakiness > describe(noise()).peakiness * 2

    def test_a_held_tone_sustains_and_a_sparse_click_train_does_not(self) -> None:
        assert describe(tone(440, seconds=2.0)).sustain_fraction > 0.95
        assert describe(clicks(0.5)).sustain_fraction < 0.6

    def test_more_clicks_read_as_more_onsets(self) -> None:
        sparse = describe(clicks(0.5)).onset_rate_hz
        dense = describe(clicks(0.125)).onset_rate_hz
        assert dense > sparse

    def test_a_held_tone_has_almost_no_onsets(self) -> None:
        assert describe(tone(440, seconds=2.0)).onset_rate_hz < 1.0

    def test_the_band_vector_is_a_shape_and_not_a_level(self) -> None:
        """Two recordings of the same sound at different gains must match exactly."""
        quiet = describe(tone(440, amplitude=0.05))
        loud = describe(tone(440, amplitude=0.9))
        assert quiet.bands == pytest.approx(loud.bands, abs=1e-9)
        assert distance(quiet, loud) == pytest.approx(0.0, abs=1e-9)

    def test_the_bands_sum_to_one(self) -> None:
        assert sum(describe(noise()).bands) == pytest.approx(1.0, abs=1e-9)

    def test_the_descriptor_survives_a_round_trip_through_json(self) -> None:
        original = describe(clicks(0.25))
        restored = Descriptor.from_dict(original.as_dict())
        assert restored.bands == pytest.approx(original.bands, abs=1e-5)
        assert restored.centroid_hz == pytest.approx(original.centroid_hz, abs=1e-3)
        assert distance(original, restored) < 1e-4

    def test_a_malformed_entry_reads_as_zero_rather_than_raising(self) -> None:
        answer = Descriptor.from_dict({"bands": "not a list", "centroid_hz": None})
        assert answer.bands == ()
        assert answer.centroid_hz == 0.0


class TestLoudestWindow:
    def test_it_finds_the_loud_half(self) -> None:
        """A stem is silent wherever its instrument rests, which is most of a file."""
        samples = [0.0] * SAMPLE_RATE + tone(440, seconds=1.0)
        start, end = loudest_window(samples, 1.0)
        assert start >= SAMPLE_RATE - SAMPLE_RATE // 4
        assert end - start == SAMPLE_RATE

    def test_it_finds_the_loud_middle(self) -> None:
        samples = [0.0] * SAMPLE_RATE + tone(440, seconds=1.0) + [0.0] * SAMPLE_RATE
        start, _ = loudest_window(samples, 1.0)
        assert SAMPLE_RATE // 2 < start < int(SAMPLE_RATE * 1.5)

    def test_a_window_longer_than_the_audio_takes_all_of_it(self) -> None:
        samples = tone(440, seconds=0.5)
        assert loudest_window(samples, 10.0) == (0, len(samples))

    def test_silence_answers_a_window_rather_than_failing(self) -> None:
        """Where to look and whether anything is there are two questions."""
        start, end = loudest_window([0.0] * (SAMPLE_RATE * 3), 1.0)
        assert end - start == SAMPLE_RATE

    def test_the_window_never_runs_past_the_end(self) -> None:
        samples = [0.0] * SAMPLE_RATE + tone(440, seconds=1.0)
        start, end = loudest_window(samples, 1.0)
        assert 0 <= start and end <= len(samples)

    def test_avoid_moves_the_window_off_the_loudest_stretch(self) -> None:
        """A second window of one file has to land somewhere else to say anything."""
        samples = tone(440, seconds=1.0) + [0.0] * SAMPLE_RATE + tone(440, seconds=1.0)
        first = loudest_window(samples, 1.0)
        second = loudest_window(samples, 1.0, avoid=first)
        assert second[1] <= first[0] or second[0] >= first[1]

    def test_avoid_with_no_room_left_answers_the_loudest_window(self) -> None:
        """Refusing is the wrong answer: one window is still a description."""
        samples = tone(440, seconds=2.0)
        whole = (0, len(samples))
        assert loudest_window(samples, 1.0, avoid=whole) == loudest_window(samples, 1.0)


class TestDistance:
    def test_a_descriptor_matches_itself_exactly(self) -> None:
        answer = describe(tone(440))
        assert distance(answer, answer) == pytest.approx(0.0, abs=1e-12)

    def test_two_nearby_tones_are_closer_than_two_distant_ones(self) -> None:
        low = describe(tone(220))
        near = describe(tone(247))
        far = describe(tone(3520))
        assert distance(low, near) < distance(low, far)

    def test_a_tone_is_closer_to_a_tone_than_to_noise(self) -> None:
        assert distance(describe(tone(440)), describe(tone(494))) < distance(
            describe(tone(440)), describe(noise())
        )

    def test_it_is_symmetric(self) -> None:
        left, right = describe(tone(440)), describe(noise())
        assert distance(left, right) == pytest.approx(distance(right, left))

    def test_an_empty_descriptor_never_wins(self) -> None:
        """An unmeasured item must not rank above a measured one by scoring zero."""
        assert distance(describe(tone(440)), Descriptor()) == math.inf

    def test_vectors_of_different_widths_do_not_compare(self) -> None:
        assert distance(Descriptor(bands=(0.5, 0.5)), Descriptor(bands=(0.3,) * 3)) == math.inf
