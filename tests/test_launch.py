"""Where a fired launch lands, which `fire` itself never says.

All synthetic. The arithmetic is the part that can be wrong quietly, and it is the part
that decides whether a caller aiming a scene at a bar hits it or the one after.
"""

from __future__ import annotations

import pytest

from live_maestro.music.launch import (
    QUANTIZATION_BEATS,
    QUANTIZATION_NAMES,
    bar_beats,
    grid_beats,
    landing,
)


class TestBarBeats:
    @pytest.mark.parametrize(
        ("numerator", "denominator", "expected"),
        [(4, 4, 4.0), (3, 4, 3.0), (6, 8, 3.0), (7, 8, 3.5), (5, 4, 5.0), (2, 2, 4.0)],
    )
    def test_a_bar_is_as_long_as_its_signature(
        self, numerator: int, denominator: int, expected: float
    ) -> None:
        assert bar_beats(numerator, denominator) == pytest.approx(expected)

    @pytest.mark.parametrize(("numerator", "denominator"), [(0, 4), (4, 0), (-1, 4)])
    def test_an_impossible_signature_is_refused(
        self, numerator: int, denominator: int
    ) -> None:
        with pytest.raises(ValueError, match="not usable"):
            bar_beats(numerator, denominator)


class TestGrid:
    @pytest.mark.parametrize(
        ("index", "expected"),
        [(0, 0.0), (1, 32.0), (2, 16.0), (3, 8.0), (4, 4.0), (5, 2.0), (7, 1.0),
         (9, 0.5), (11, 0.25), (13, 0.125)],
    )
    def test_the_grid_in_four_four(self, index: int, expected: float) -> None:
        assert grid_beats(index) == pytest.approx(expected)

    @pytest.mark.parametrize("index", [6, 8, 10, 12])
    def test_a_triplet_grid_is_not_a_plain_division(self, index: int) -> None:
        """A triplet grid lands between the straight ones, which is the point of it."""
        assert grid_beats(index) not in (0.125, 0.25, 0.5, 1.0, 2.0, 4.0)

    def test_the_bar_grids_follow_the_time_signature(self) -> None:
        assert grid_beats(4, 3, 4) == pytest.approx(3.0)
        assert grid_beats(1, 3, 4) == pytest.approx(24.0)

    def test_every_documented_index_resolves(self) -> None:
        for index in QUANTIZATION_BEATS:
            assert grid_beats(index) >= 0.0
            assert index in QUANTIZATION_NAMES

    @pytest.mark.parametrize("index", [-1, 14, 99])
    def test_an_index_live_does_not_have_is_refused(self, index: int) -> None:
        with pytest.raises(ValueError, match="not a Live launch quantisation"):
            grid_beats(index)


class TestLanding:
    def test_a_launch_inside_a_bar_lands_on_the_next_one(self) -> None:
        assert landing(55.6, 55.7, 4).earliest == pytest.approx(56.0)

    def test_a_launch_exactly_on_a_boundary_lands_there(self) -> None:
        """Not on the one after: firing on the beat is not firing a beat late."""
        assert landing(64.0, 64.0, 4).earliest == pytest.approx(64.0)

    def test_a_window_that_crosses_a_boundary_is_not_certain(self) -> None:
        answer = landing(63.9, 64.1, 4)
        assert not answer.certain
        assert (answer.earliest, answer.latest) == (64.0, 68.0)

    def test_a_window_inside_one_grid_step_is_certain(self) -> None:
        answer = landing(64.1, 64.3, 4)
        assert answer.certain
        assert answer.earliest == pytest.approx(68.0)

    def test_the_eight_bar_grid_is_the_case_that_hurt(self) -> None:
        """Measured against Live: fired at 55.6, landed at 96 rather than 64."""
        answer = landing(55.6, 55.7, 1)
        assert answer.earliest == pytest.approx(64.0)
        assert answer.grid_beats == pytest.approx(32.0)

    def test_readings_in_either_order_give_the_same_window(self) -> None:
        assert landing(64.1, 63.9, 4) == landing(63.9, 64.1, 4)

    def test_with_quantisation_off_the_window_is_the_answer(self) -> None:
        answer = landing(55.6, 55.7, 0)
        assert (answer.earliest, answer.latest, answer.grid_beats) == (55.6, 55.7, 0.0)

    def test_a_stopped_transport_reads_the_same_beat_twice(self) -> None:
        answer = landing(0.0, 0.0, 4)
        assert answer.certain
        assert answer.window_beats == 0.0

    def test_the_window_width_is_reported(self) -> None:
        assert landing(10.0, 10.25, 7).window_beats == pytest.approx(0.25)

    def test_a_launch_never_lands_before_it_was_fired(self) -> None:
        for position in (0.0, 0.1, 3.99, 4.0, 4.01, 55.6, 1000.7):
            assert landing(position, position, 4).earliest >= position - 1e-9
