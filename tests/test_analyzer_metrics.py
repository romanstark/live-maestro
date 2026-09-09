"""Tests for the arithmetic behind every number ``analyze_audio`` reports.

None of this needs Live, which is the reason the arithmetic lives in a
module of its own. Every dB value, correlation, crest factor, centroid and
verdict is computed from the numbers the device reports, so each one can be
checked against theory here. A figure taken from the device and passed through
would have nothing to check.
"""

from __future__ import annotations

import math

import pytest

from live_maestro.analyzer.metrics import (
    DIGITAL_ZERO_DB,
    centroid_hz,
    combined_rms,
    correlation,
    crest_factor_db,
    to_dbfs,
)
from live_maestro.analyzer.protocol import BANDS


@pytest.mark.parametrize(
    ("amplitude", "expected"),
    [(1.0, 0.0), (0.5, -6.02), (0.1, -20.0), (0.001, -60.0)],
)
def test_dbfs_matches_twenty_log_ten(amplitude: float, expected: float) -> None:
    assert to_dbfs(amplitude) == pytest.approx(expected, abs=0.01)


def test_a_zero_amplitude_gets_a_floor_and_never_an_infinity() -> None:
    # An infinity would serialise as -Infinity, which is not valid JSON, so the
    # MCP client would receive something it is not obliged to parse.
    assert to_dbfs(0.0) == DIGITAL_ZERO_DB
    assert to_dbfs(-0.5) == DIGITAL_ZERO_DB
    assert math.isfinite(to_dbfs(0.0))


def test_two_channels_add_power_not_amplitude() -> None:
    # Both channels at the same level carry that level, not twice it.
    assert combined_rms(0.5, 0.5) == pytest.approx(0.5)
    # One channel silent is 3 dB down from both, not 6.
    assert to_dbfs(combined_rms(0.5, 0.0)) == pytest.approx(to_dbfs(0.5) - 3.01, abs=0.02)


def test_correlation_spans_minus_one_to_plus_one() -> None:
    assert correlation(0.25, 0.5, 0.5) == 1.0
    assert correlation(-0.25, 0.5, 0.5) == -1.0
    assert correlation(0.0, 0.5, 0.5) == 0.0
    assert correlation(0.125, 0.5, 0.5) == pytest.approx(0.5)


def test_correlation_is_clamped_because_three_averages_round_separately() -> None:
    # mean_lr, rms_l and rms_r come from three average~ objects reading the same
    # window. Rounding can push the quotient a hair past one, and a coefficient
    # of 1.0001 is not a value any caller should have to defend against.
    assert correlation(0.2501, 0.5, 0.5) == 1.0
    assert correlation(-0.2501, 0.5, 0.5) == -1.0


def test_a_silent_channel_has_no_correlation_rather_than_zero() -> None:
    # Zero would read as "the two channels are unrelated", which is a statement
    # about a stereo image that is not there at all.
    assert correlation(0.0, 0.0, 0.5) is None
    assert correlation(0.0, 0.5, 0.0) is None
    assert correlation(0.0, 0.0, 0.0) is None


def test_crest_factor_is_the_distance_from_peak_to_average() -> None:
    assert crest_factor_db(-0.8, -16.2) == pytest.approx(15.4)
    assert crest_factor_db(0.0, -3.0) == pytest.approx(3.0)


def test_silence_has_no_crest_factor() -> None:
    # Two floors subtract to zero, which would read as a signal limited flat.
    assert crest_factor_db(DIGITAL_ZERO_DB, DIGITAL_ZERO_DB) is None
    assert crest_factor_db(-95.0, -95.0) is None


def test_the_centroid_sits_at_the_geometric_centre_of_a_single_band() -> None:
    for name, low, high in BANDS:
        bands = {field: 0.0 for field, _, _ in BANDS}
        bands[name] = 0.3
        expected = math.sqrt(low * (high if high is not None else 20000.0))
        # abs covers the reported value being rounded to one decimal, which for
        # the sub band is a larger share of the number than rel allows.
        assert centroid_hz(bands) == pytest.approx(expected, rel=1e-3, abs=0.06)


def test_the_centroid_moves_towards_the_louder_band() -> None:
    quiet_top = {field: 0.0 for field, _, _ in BANDS}
    quiet_top["band_bass"] = 0.4
    quiet_top["band_presence"] = 0.01
    loud_top = dict(quiet_top, band_presence=0.4)
    assert centroid_hz(quiet_top) is not None
    assert centroid_hz(loud_top) is not None
    assert centroid_hz(loud_top) > centroid_hz(quiet_top)  # type: ignore[operator]


def test_the_centroid_weights_energy_and_not_amplitude() -> None:
    # Doubling an amplitude quadruples its energy, so the centre of mass has to
    # move further than a mean of amplitudes would take it.
    bands = {field: 0.0 for field, _, _ in BANDS}
    bands["band_bass"] = 0.2
    bands["band_presence"] = 0.2
    even = centroid_hz(bands)
    bands["band_presence"] = 0.4
    louder = centroid_hz(bands)
    assert even is not None and louder is not None
    low = math.sqrt(60.0 * 250.0)
    high = math.sqrt(6000.0 * 10000.0)
    assert even == pytest.approx((low + high) / 2, rel=1e-3)
    assert louder == pytest.approx((low * 1 + high * 4) / 5, rel=1e-3)


def test_silence_has_no_centroid() -> None:
    assert centroid_hz({field: 0.0 for field, _, _ in BANDS}) is None
    assert centroid_hz({}) is None
