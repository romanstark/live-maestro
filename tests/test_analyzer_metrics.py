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
    SILENCE_FLOOR_DB,
    band_amplitude_means,
    band_occupancy_window,
    centroid_hz,
    combined_rms,
    correlation,
    crest_factor_db,
    to_dbfs,
    window_coverage_pct,
)
from live_maestro.analyzer.models import AccumulationSpan
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


def test_a_band_at_or_below_the_silence_floor_is_never_occupied() -> None:
    # The floor has to be checked on the band itself and not only when picking
    # the loudest one. With the loudest band at -80 dBFS the 12 dB cutoff is
    # -92 dBFS, which sits below the -90 dBFS floor, so a band carrying nothing
    # clears the cutoff and would be reported as occupied.
    assert SILENCE_FLOOR_DB == -90.0
    assert band_occupancy_window({"band_mid": -80.0, "band_sub": -90.0}, 12.0) == {
        "band_mid": True,
        "band_sub": False,
    }
    assert band_occupancy_window({"band_mid": -80.0, "band_sub": -95.0}, 12.0) == {
        "band_mid": True,
        "band_sub": False,
    }
    # A threshold wide enough to reach digital silence still does not reach it.
    wide = band_occupancy_window({"band_mid": -80.0, "band_sub": DIGITAL_ZERO_DB}, 70.0)
    assert wide["band_sub"] is False
    assert wide["band_mid"] is True
    # Nothing measured is nothing occupied, rather than an empty dictionary the
    # caller has to tell apart from seven False values.
    assert band_occupancy_window({"band_mid": None, "band_sub": None}, 12.0) == {
        "band_mid": False,
        "band_sub": False,
    }


def test_band_amplitude_means_averages_over_the_windows_it_had() -> None:
    assert band_amplitude_means({"band_mid": 2.0, "band_air": 0.5}, 4) == {
        "band_mid": 0.5,
        "band_air": 0.125,
    }
    # A mean over no window is no measurement, not a quiet band. Seven zeroes
    # would reach the spectrum as seven bands sitting at digital silence.
    assert band_amplitude_means({"band_mid": 0.0}, 0) is None
    assert band_amplitude_means({}, 0) is None
    # The means are amplitudes and the spectrum is dBFS, so this is the figure
    # to_dbfs is handed on an accumulated read.
    means = band_amplitude_means({"band_mid": 2.0}, 4)
    assert means is not None
    assert to_dbfs(means["band_mid"]) == -6.02


def test_window_coverage_pct_reports_a_share_or_reports_nothing() -> None:
    assert window_coverage_pct(4000.0, 10.0) == 40.0
    # Neither a span of no time nor windows covering none of it supports a
    # share, and 0.0 or 100.0 would both read as a figure that was taken.
    assert window_coverage_pct(0.0, 10.0) is None
    assert window_coverage_pct(4000.0, 0.0) is None
    # Windows adding up to more time than has elapsed. That is a fact about the
    # sampling worth reading, so it is reported rather than clamped to 100.
    assert window_coverage_pct(12000.0, 10.0) == 120.0


def test_coverage_over_100_is_the_first_window_and_not_an_overlap() -> None:
    """Over 100% is what one window reaching back before the start looks like.

    The sampler takes a window every ``max(0.05, window_ms / 1000)`` seconds and
    each window is at most that long, so two of its windows can never overlap. N
    windows of 400 ms taken 400 ms apart and read just after the Nth add up to
    400*N ms over an elapsed 400*(N-1) ms, which is over 100% with every window
    abutting the next. The excess is at most one window, which is exactly the
    audio window one covered before the accumulation began.

    Measured with the production ``AudioAccumulator`` driven by a stub analyzer:
    reads taken right after a sample landed gave 196.7, 147.6 and 131.1% at two,
    three and four windows.
    """
    for n in (2, 3, 4, 10):
        covered_ms = 400.0 * n
        elapsed_s = 0.4 * (n - 1)
        pct = window_coverage_pct(covered_ms, elapsed_s)
        assert pct is not None
        assert pct > 100.0
        assert pct == pytest.approx(100.0 * n / (n - 1), abs=0.1)
        # Never more than one window's worth over the span, which is the ceiling
        # the reach-back explanation implies and an overlap would not respect.
        assert covered_ms <= elapsed_s * 1000.0 + 400.0

    # The published explanation has to name that cause. A caller reading
    # "consecutive windows overlapped" concludes the sampler counted some audio
    # twice and distrusts the spectrum, when nothing was counted twice.
    described = AccumulationSpan.model_fields["coverage_pct"].description
    assert described is not None
    assert "reaches back" in described
    assert "overlapped" not in described
    assert window_coverage_pct.__doc__ is not None
    assert "reaches back" in window_coverage_pct.__doc__
    assert "overlapped" not in window_coverage_pct.__doc__
