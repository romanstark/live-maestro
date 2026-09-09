"""Everything derived from a reply, kept away from the socket so it can be tested.

The device reports amplitudes and the six numbers ``loudness~`` produces. Every
dB value, every correlation, the crest factor, the centroid and every verdict is
computed here, from those numbers alone. That split is the point: a wrong
threshold or a wrong logarithm is a unit test away from being caught, and none of
it needs Ableton Live to be running.

No verdict is taken from the device. A boolean the device asserts cannot be
checked against the numbers it sends alongside it, so every judgement is computed
here from the measurements and can be contradicted by them.

Silence is a real answer here, not a zero. An empty track has no correlation and
no centroid, and reporting 0.0 for either would read as "perfectly decorrelated"
and "no brightness" instead of "nothing was playing".
"""

from __future__ import annotations

import math

from live_maestro.analyzer.protocol import BANDS

__all__ = [
    "SILENCE_FLOOR_AMPLITUDE",
    "SILENCE_FLOOR_DB",
    "centroid_hz",
    "combined_rms",
    "correlation",
    "crest_factor_db",
    "to_dbfs",
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

    This is not the spectral centroid of an FFT. It is the centre of mass of
    seven band energies, each placed at the geometric centre of its own band, and
    it is reported as such. A true FFT centroid needs ``pfft~``, which needs a
    subpatcher file on disk, which would stop the device from being one file.

    The band with no upper edge is placed at the geometric centre of 10 kHz and
    20 kHz, because that is the range it actually passes at every sample rate
    this runs at.

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
