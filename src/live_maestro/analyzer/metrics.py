"""Derive every reported figure from an analyzer reply, as pure functions.

The device reports amplitudes and the six numbers ``loudness~`` produces. Every dB
value, every correlation, the crest factor, the centroid and every verdict is computed
here from those numbers alone, with no Ableton Live in the loop. No verdict comes from
the device: a boolean it asserts cannot be checked against the numbers beside it.

Silence is a real answer here, not a zero. An empty track has no correlation and no
centroid, and reporting 0.0 for either would read as "perfectly decorrelated" and "no
brightness" instead of "nothing was playing".
"""

from __future__ import annotations

import math

from live_maestro.analyzer.protocol import BANDS

__all__ = [
    "SILENCE_FLOOR_AMPLITUDE",
    "SILENCE_FLOOR_DB",
    "band_amplitude_means",
    "band_occupancy_percentages",
    "band_occupancy_window",
    "centroid_hz",
    "combined_rms",
    "correlation",
    "crest_factor_db",
    "to_dbfs",
    "window_coverage_pct",
]

#: Below this an amplitude is treated as silence rather than as a level. Live's
#: own meters bottom out well above it, and 16 bit dither sits near -96 dBFS, so
#: anything under this floor carries no mix information.
SILENCE_FLOOR_DB = -90.0

#: What ``to_dbfs`` returns for a zero amplitude. A float log of zero raises, and
#: -inf serialises to JSON as ``-Infinity``, which is not valid JSON and which no
#: MCP client should have to parse.
DIGITAL_ZERO_DB = -144.0

#: The same floor as an amplitude, which is what the device reports. A band with
#: no content does not average to exactly zero: it averages to a number small
#: enough to be rounding, and dividing two of those produces a correlation that
#: looks like a measurement of a stereo image that is not there.
SILENCE_FLOOR_AMPLITUDE = 10.0 ** (SILENCE_FLOOR_DB / 20.0)


def to_dbfs(amplitude: float) -> float:
    """Convert a linear amplitude to dBFS, with a floor instead of an infinity.

    >>> to_dbfs(1.0)
    0.0
    >>> to_dbfs(0.5)
    -6.02
    >>> to_dbfs(0.0)
    -144.0
    """
    if amplitude <= 0.0:
        return DIGITAL_ZERO_DB
    return max(DIGITAL_ZERO_DB, round(20.0 * math.log10(amplitude), 2))


def combined_rms(rms_l: float, rms_r: float) -> float:
    """Compute the combined RMS of both channels.

    Power adds and amplitude does not, so this is the root of the mean of the two
    squares rather than the mean of the two amplitudes. Two channels each at 0.5
    carry the same power as one channel at 0.5, not twice it.

    >>> combined_rms(0.5, 0.5)
    0.5
    >>> round(combined_rms(0.5, 0.0), 4)
    0.3536
    """
    return math.sqrt((rms_l * rms_l + rms_r * rms_r) / 2.0)


def correlation(mean_lr: float, rms_l: float, rms_r: float) -> float | None:
    """Compute the normalised correlation coefficient of two channels.

    ``None`` where either channel is silent, because a correlation needs both
    channels to have energy. Silent means below :data:`SILENCE_FLOOR_AMPLITUDE`
    and not merely zero: a band carrying nothing still averages to a very small
    number, and a quotient of two of those is arithmetic rather than a phase
    relationship.

    Identical channels correlate at +1, an inverted channel at -1, and the result
    is clamped because a rolling average of the product and two rolling averages
    of the squares are taken over the same window but not over the same rounding.

    >>> correlation(0.25, 0.5, 0.5)
    1.0
    >>> correlation(-0.25, 0.5, 0.5)
    -1.0
    >>> correlation(0.0, 0.5, 0.5)
    0.0
    >>> correlation(0.25, 0.0, 0.5) is None
    True
    >>> correlation(1e-12, 1e-6, 1e-6) is None
    True
    """
    if min(rms_l, rms_r) < SILENCE_FLOOR_AMPLITUDE:
        return None
    denominator = rms_l * rms_r
    if denominator <= 0.0:
        return None
    return round(max(-1.0, min(1.0, mean_lr / denominator)), 3)


def crest_factor_db(peak_db: float, rms_db: float) -> float | None:
    """Calculate the crest factor in dB between peak and average level.

    ``None`` for silence, where the difference of two floors is zero and would
    read as a completely squashed signal.

    >>> crest_factor_db(-0.8, -16.2)
    15.4
    >>> crest_factor_db(-144.0, -144.0) is None
    True
    """
    if rms_db <= SILENCE_FLOOR_DB:
        return None
    return round(peak_db - rms_db, 2)


def centroid_hz(bands: dict[str, float]) -> float | None:
    """Calculate the energy weighted centre of the seven measured bands in Hz.

    This is not the spectral centroid of an FFT: it is the centre of mass of seven
    band energies, each placed at the geometric centre of its own band. A true FFT
    centroid would need ``pfft~``, and so a subpatcher file on disk, which would stop
    the device from being one file.

    The band with no upper edge is placed at the geometric centre of 10 kHz and
    20 kHz, the range it actually passes at every sample rate this runs at.

    >>> centroid_hz({name: 0.0 for name, _, _ in BANDS}) is None
    True
    >>> only_mid = {name: 0.0 for name, _, _ in BANDS}
    >>> only_mid["band_mid"] = 0.5
    >>> centroid_hz(only_mid)
    1000.0
    """
    weighted = 0.0
    total = 0.0
    for name, low, high in BANDS:
        amplitude = bands.get(name, 0.0)
        if amplitude <= 0.0:
            continue
        # Energy, not amplitude: a centre of mass weights by power.
        energy = amplitude * amplitude
        centre = math.sqrt(low * (high if high is not None else 20000.0))
        weighted += energy * centre
        total += energy
    if total <= 0.0:
        return None
    return round(weighted / total, 1)


def band_occupancy_window(
    band_db_values: dict[str, float | None],
    threshold_db: float = 12.0,
) -> dict[str, bool]:
    """Determine for a single window which bands are within threshold_db of the loudest band.

    A band is occupied if it carries energy at all and its level is within
    threshold_db of the loudest band in the window. If all bands are silent (at or
    below SILENCE_FLOOR_DB), no band is occupied.

    The silence floor is checked on the band itself and not only when picking the
    loudest. A silent band clears a cutoff that sits below the floor, so a quiet
    window with a wide threshold would otherwise count bands that carry nothing:
    with a loudest band at -80 dBFS the cutoff is -92 dBFS, and the floor at -90
    dBFS is above it.

    >>> band_occupancy_window({"band_mid": -20.0, "band_high_mid": -30.0, "band_air": -35.0}, 12.0)
    {'band_mid': True, 'band_high_mid': True, 'band_air': False}
    >>> band_occupancy_window({"band_mid": -80.0, "band_sub": -90.0}, 12.0)
    {'band_mid': True, 'band_sub': False}
    >>> band_occupancy_window({"band_mid": -80.0, "band_sub": None}, 70.0)
    {'band_mid': True, 'band_sub': False}
    """
    valid = {k: v for k, v in band_db_values.items() if v is not None and v > SILENCE_FLOOR_DB}
    if not valid:
        return {k: False for k in band_db_values}
    loudest = max(valid.values())
    cutoff = loudest - threshold_db
    return {
        k: (v is not None and v > SILENCE_FLOOR_DB and v >= cutoff)
        for k, v in band_db_values.items()
    }


def band_occupancy_percentages(
    occupied_counts: dict[str, int],
    total_windows: int,
) -> dict[str, float | None]:
    """Calculate the occupancy percentage per band over multiple analysis windows.

    The figure is a share of the windows that were counted, and those windows are
    a sample of the span they were taken from rather than the whole of it.

    >>> band_occupancy_percentages({"band_mid": 8, "band_air": 1}, 8)
    {'band_mid': 100.0, 'band_air': 12.5}
    >>> band_occupancy_percentages({"band_mid": 0}, 0)
    {'band_mid': None}
    """
    if total_windows <= 0:
        return {k: None for k in occupied_counts}
    return {
        k: round((count / total_windows) * 100.0, 1)
        for k, count in occupied_counts.items()
    }


def band_amplitude_means(
    amplitude_sums: dict[str, float],
    total_windows: int,
) -> dict[str, float] | None:
    """Average each band's summed amplitude over the windows they were summed from.

    ``None`` when nothing was counted. A mean over no window is not a quiet
    measurement, it is no measurement, and a dictionary of zeroes would reach the
    spectrum as seven bands sitting at digital silence.

    >>> band_amplitude_means({"band_sub": 1.0, "band_mid": 2.0}, 4)
    {'band_sub': 0.25, 'band_mid': 0.5}
    >>> band_amplitude_means({"band_sub": 0.0}, 0) is None
    True
    """
    if total_windows <= 0:
        return None
    return {k: total / total_windows for k, total in amplitude_sums.items()}


def window_coverage_pct(covered_ms: float, elapsed_s: float) -> float | None:
    """Return what share of an elapsed span the measured windows add up to, in %.

    ``None`` where either figure is zero or negative, because neither a span of no
    time nor a set of windows covering none of it supports a share.

    The result is not clamped. Under 100 the windows sampled the span rather than
    covering it. Over 100 they add up to more time than has elapsed, which the first
    window does on its own: it reaches back to the audio before the span began, so a
    short accumulation reads well above 100 with every window abutting the next.

    >>> window_coverage_pct(4000.0, 10.0)
    40.0
    >>> window_coverage_pct(12000.0, 10.0)
    120.0
    >>> window_coverage_pct(400.0, 0.0) is None
    True
    """
    if covered_ms <= 0.0 or elapsed_s <= 0.0:
        return None
    return round(covered_ms / (elapsed_s * 1000.0) * 100.0, 1)

