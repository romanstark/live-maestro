"""What a piece of audio sounds like, as a handful of numbers that can be compared.

Samples in, a small fixed vector out, so two pieces of audio can be ranked against each
other by something other than the name a sound designer gave them.

The vector is a shape and not a level: band energies are normalised to sum to one, which
makes a quiet bass recording comparable to a loud one and the descriptor useless for any
question about loudness.

Pure module: no I/O, no Live, no Max, no third party package. The FFT is here rather than
a dependency, and a 2048 point transform over a three second file costs a fraction of a
second, which is minutes for a whole library rather than hours.

:func:`distance` compares two descriptors and returns a number that is only meaningful
against other numbers from the same function.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import asdict, dataclass, field

__all__ = [
    "BANDS",
    "BAND_EDGES_HZ",
    "DEFAULT_WEIGHTS",
    "FRAME",
    "HOP",
    "SAMPLE_RATE",
    "Descriptor",
    "Weights",
    "band_edges",
    "busiest_window",
    "describe",
    "distance",
    "fft",
    "loudest_window",
]

#: Rate the audio has to arrive at. 32 kHz puts the Nyquist limit at 16 kHz, which keeps
#: the cymbal and hi-hat range inside the analysis. Anything above that separates two
#: crash cymbals and nothing this is used for.
SAMPLE_RATE = 32000

#: Transform size. At 32 kHz this is a 64 ms window with a 15.6 Hz bin, and the bin is
#: what matters: the lowest band edge sits at 30 Hz, and a coarser transform cannot tell
#: a 40 Hz sub from a 60 Hz one at all.
FRAME = 2048

#: Frames overlap by half, so a short percussive event cannot fall between two windows.
HOP = 1024

#: How many logarithmically spaced bands the spectrum is reduced to. Enough to separate
#: a sub from a pad from a hi-hat, few enough that the vector stays comparable when the
#: two recordings are of different lengths.
BANDS = 32

#: Lowest and highest band edge. 30 Hz is below the lowest fundamental in normal use and
#: 16 kHz is the Nyquist limit at :data:`SAMPLE_RATE`.
_LOW_HZ = 30.0
_HIGH_HZ = 16000.0

#: Share of the spectrum's bins counted as its peaks. A harmonic sound puts most of its
#: energy into a few narrow lines and a noisy one spreads it, so the energy inside this
#: slice is a measure of how tonal something is. Two percent of 1024 bins is 20 lines,
#: which is roughly the harmonics a voice or a string has inside the audible range.
_PEAK_BINS_SHARE = 0.02

#: A frame counts as sounding when its RMS is within this many dB of the loudest frame.
#: Everything quieter is treated as the gap between notes rather than as content.
_SOUNDING_DB = 20.0

#: How much louder than the running average a frame has to be to count as an onset. The
#: figure separates a struck note from the ripple inside a sustained one.
_ONSET_RATIO = 1.8

#: Frames that must pass before a second onset may be counted, so one attack with a
#: ragged edge is one onset. At the hop above this is 64 ms, which is faster than any
#: playable repetition and slower than the raggedness.
_ONSET_REFRACTORY = 2


def band_edges(sample_rate: int = SAMPLE_RATE, bands: int = BANDS) -> tuple[int, ...]:
    """Bin index of every band edge, low to high, for a transform of :data:`FRAME`.

    Edges are spaced logarithmically because pitch is. A linear split puts two thirds of
    the bands above 5 kHz, where almost nothing distinguishes one instrument from
    another, and leaves the octaves that carry a bass in a single band.

    >>> edges = band_edges()
    >>> len(edges) == BANDS + 1
    True
    >>> edges[0] < edges[-1]
    True
    """
    top = min(_HIGH_HZ, sample_rate / 2.0)
    ratio = top / _LOW_HZ
    hz_per_bin = sample_rate / FRAME
    out: list[int] = []
    for index in range(bands + 1):
        hz = _LOW_HZ * (ratio ** (index / bands))
        bin_index = round(hz / hz_per_bin)
        out.append(max(bin_index, out[-1] + 1) if out else bin_index)
    return tuple(out)


BAND_EDGES_HZ: tuple[int, ...] = band_edges()


def fft(values: list[complex]) -> list[complex]:
    """Radix 2 fast Fourier transform, iterative, in place on a copy.

    Iterative rather than recursive: this is the hot path of a whole library scan, and
    the recursive form allocates two lists per stage per call.

    Args:
        values: A list whose length is a power of two.

    Returns:
        The transform, same length.

    Raises:
        ValueError: The length is not a power of two.

    >>> [round(abs(v), 6) for v in fft([1+0j, 0j, 0j, 0j])]
    [1.0, 1.0, 1.0, 1.0]
    >>> [round(v.real, 6) for v in fft([1+0j, 1+0j, 1+0j, 1+0j])]
    [4.0, 0.0, 0.0, 0.0]
    """
    size = len(values)
    if size & (size - 1) or size == 0:
        raise ValueError(f"length {size} is not a power of two")

    out = list(values)
    half = size >> 1
    target = 0
    for source in range(1, size):
        bit = half
        while target & bit:
            target ^= bit
            bit >>= 1
        target |= bit
        if source < target:
            out[source], out[target] = out[target], out[source]

    width = 2
    while width <= size:
        step = -2j * math.pi / width
        span = width >> 1
        for start in range(0, size, width):
            for offset in range(span):
                twiddle = cmath.exp(step * offset) * out[start + offset + span]
                upper = out[start + offset]
                out[start + offset] = upper + twiddle
                out[start + offset + span] = upper - twiddle
        width <<= 1
    return out


def _as_float(value: object, default: float = 0.0) -> float:
    """A JSON field as a float, or the default where it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


@dataclass(frozen=True)
class Descriptor:
    """What one piece of audio sounds like, reduced to what can be compared.

    Attributes:
        bands: :data:`BANDS` band energies, normalised to sum to one. The shape of the
            spectrum with the level taken out.
        centroid_hz: The spectrum's centre of mass. A sub bass sits near 100 Hz and a
            hi-hat above 1 kHz.
        rolloff_hz: The frequency below which 85 percent of the energy sits.
        flatness: Geometric mean of the bands over their arithmetic mean, 0 to 1. A
            coarse reading, kept because it describes the spectrum's tilt, and weak at
            telling a tone from noise: 32 bands smear a harmonic series into the floor
            between its lines. Use ``peakiness`` for that question.
        peakiness: Share of the spectrum's energy sitting in its loudest two percent of
            bins, 0 to 1. This is the harmonic against inharmonic reading, taken on the
            fine spectrum before the bands smear it.
        onset_rate_hz: Struck events per second. Separates percussion from a pad without
            knowing anything about either.
        sustain_fraction: Share of frames within 20 dB of the loudest. A pad approaches
            1 and a one shot approaches 0.
        crest_db: Peak over RMS. High is transient, low is compressed or sustained.
        duration_s: Length of the audio the descriptor was taken from.
        frames: How many windows were analysed. Under about ten the rest of these
            numbers rest on too little audio to mean much.
    """

    bands: tuple[float, ...] = field(default_factory=tuple)
    centroid_hz: float = 0.0
    rolloff_hz: float = 0.0
    flatness: float = 0.0
    peakiness: float = 0.0
    onset_rate_hz: float = 0.0
    sustain_fraction: float = 0.0
    crest_db: float = 0.0
    duration_s: float = 0.0
    frames: int = 0

    def as_dict(self) -> dict[str, object]:
        """The descriptor as plain JSON types, for the index file."""
        data = asdict(self)
        data["bands"] = [round(v, 6) for v in self.bands]
        for key in ("centroid_hz", "rolloff_hz", "flatness", "peakiness",
                    "onset_rate_hz", "sustain_fraction", "crest_db", "duration_s"):
            data[key] = round(float(data[key]), 4)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Descriptor:
        """Read one back out of the index file.

        A field that is missing or of the wrong type reads as zero rather than raising.
        The index is generated, so a malformed entry is a bug upstream, and one bad
        entry ranking badly is a better failure than a whole index refusing to load.
        """
        raw = data.get("bands")
        bands = (
            tuple(_as_float(v) for v in raw) if isinstance(raw, list) else ()
        )
        return cls(
            bands=bands,
            centroid_hz=_as_float(data.get("centroid_hz")),
            rolloff_hz=_as_float(data.get("rolloff_hz")),
            flatness=_as_float(data.get("flatness")),
            peakiness=_as_float(data.get("peakiness")),
            onset_rate_hz=_as_float(data.get("onset_rate_hz")),
            sustain_fraction=_as_float(data.get("sustain_fraction")),
            crest_db=_as_float(data.get("crest_db")),
            duration_s=_as_float(data.get("duration_s")),
            frames=int(_as_float(data.get("frames"))),
        )


_WINDOW: tuple[float, ...] = tuple(
    0.5 - 0.5 * math.cos(2.0 * math.pi * i / (FRAME - 1)) for i in range(FRAME)
)


def describe(samples: list[float], sample_rate: int = SAMPLE_RATE) -> Descriptor:
    """Reduce mono samples to a :class:`Descriptor`.

    Args:
        samples: Mono audio, nominally between -1.0 and 1.0.
        sample_rate: Rate the samples are at, for the frequencies in the result.

    Returns:
        A Descriptor. Audio shorter than one frame comes back with ``frames`` 0 and
        everything else at its default, which is the honest answer rather than a vector
        built from one window of silence.

    >>> import math, random
    >>> tone = [math.sin(2 * math.pi * 220 * n / SAMPLE_RATE) for n in range(FRAME * 4)]
    >>> rng = random.Random(0)
    >>> hiss = [rng.uniform(-1.0, 1.0) for _ in range(FRAME * 4)]
    >>> describe(tone).centroid_hz < describe(hiss).centroid_hz
    True
    >>> describe(tone).flatness < describe(hiss).flatness
    True
    """
    if len(samples) < FRAME:
        return Descriptor(duration_s=len(samples) / sample_rate)

    edges = band_edges(sample_rate)
    hz_per_bin = sample_rate / FRAME
    bins = FRAME // 2

    totals = [0.0] * BANDS
    centroid_sum = 0.0
    rolloff_sum = 0.0
    peak_share_sum = 0.0
    peak_bins = max(1, int(bins * _PEAK_BINS_SHARE))
    rms_curve: list[float] = []
    peak = 0.0
    square_sum = 0.0

    for start in range(0, len(samples) - FRAME + 1, HOP):
        block = samples[start : start + FRAME]
        spectrum = fft([complex(block[i] * _WINDOW[i], 0.0) for i in range(FRAME)])
        magnitudes = [abs(spectrum[i]) for i in range(bins)]
        energy = sum(magnitudes) or 1e-12

        centroid_sum += sum(i * m for i, m in enumerate(magnitudes)) / energy
        running = 0.0
        cutoff = bins - 1
        for index, value in enumerate(magnitudes):
            running += value
            if running >= 0.85 * energy:
                cutoff = index
                break
        rolloff_sum += cutoff
        loudest_bins = sorted(magnitudes, reverse=True)[:peak_bins]
        peak_share_sum += sum(loudest_bins) / energy

        for band in range(BANDS):
            low = min(edges[band], bins)
            high = min(max(edges[band + 1], low + 1), bins)
            totals[band] += sum(magnitudes[low:high])

        frame_square = 0.0
        for value in block:
            frame_square += value * value
            peak = max(peak, abs(value))
        square_sum += frame_square
        rms_curve.append(math.sqrt(frame_square / FRAME))

    used = len(rms_curve)
    total = sum(totals) or 1e-12
    bands = tuple(v / total for v in totals)

    positive = [v for v in bands if v > 0.0]
    if positive:
        log_mean = math.exp(sum(math.log(v) for v in positive) / len(positive))
        flatness = log_mean / (sum(bands) / BANDS)
    else:
        flatness = 0.0

    loudest = max(rms_curve)
    floor = loudest * (10.0 ** (-_SOUNDING_DB / 20.0))
    sustain = sum(1 for v in rms_curve if v >= floor) / used

    onsets = 0
    blocked = 0
    average = rms_curve[0] or 1e-12
    for value in rms_curve[1:]:
        if blocked > 0:
            blocked -= 1
        elif value > _ONSET_RATIO * average and value >= floor:
            onsets += 1
            blocked = _ONSET_REFRACTORY
        average = 0.7 * average + 0.3 * value or 1e-12

    duration = len(samples) / sample_rate
    overall_rms = math.sqrt(square_sum / (used * FRAME))
    crest = 20.0 * math.log10(peak / overall_rms) if overall_rms > 0.0 and peak > 0.0 else 0.0

    return Descriptor(
        bands=bands,
        centroid_hz=centroid_sum / used * hz_per_bin,
        rolloff_hz=rolloff_sum / used * hz_per_bin,
        flatness=min(1.0, max(0.0, flatness)),
        peakiness=peak_share_sum / used,
        onset_rate_hz=onsets / duration if duration > 0.0 else 0.0,
        sustain_fraction=sustain,
        crest_db=crest,
        duration_s=duration,
        frames=used,
    )


#: Block size for :func:`loudest_window`, in seconds. Coarse on purpose: the question
#: is which part of a track is playing, not where a note starts.
_SCAN_BLOCK_S = 0.25


def loudest_window(
    samples: list[float],
    window_s: float,
    sample_rate: int = SAMPLE_RATE,
    avoid: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """The loudest stretch of ``window_s`` seconds, as sample indices.

    A stem rests, so a fixed offset into one lands on silence often. A descriptor taken
    from that silence describes nothing and still ranks against the index as confidently
    as a real one.

    Loudest is not busiest. Over five separated stems of one song, 20 s windows every 4 s:
    on drums and bass the loudest window is also among the most eventful, a struck
    instrument being loud when it is struck, but on the acoustic guitar the loudest window
    held 0.25 onsets per second where another window of the same file held 1.50, and on
    the pad and the keys it held none at all. This answers "where is this file playing",
    not "where is it busy".

    Args:
        samples: Mono audio.
        window_s: How long a window to find. A window longer than the audio returns the
            whole of it.
        sample_rate: Rate the samples are at.
        avoid: A ``(start, end)`` range to keep out of, which is how a second window is
            taken from the same file without overlapping the first. A range that leaves
            no room returns the loudest window regardless.

    Returns:
        ``(start, end)`` as indices into ``samples``. Audio that is silent throughout
        returns the first window rather than failing, because where to look is a
        different question from whether anything is there.

    >>> quiet_then_loud = [0.0] * 32000 + [0.5] * 32000
    >>> start, end = loudest_window(quiet_then_loud, 1.0)
    >>> start >= 32000 - 8000
    True
    >>> elsewhere, _ = loudest_window(quiet_then_loud, 1.0, avoid=(start, end))
    >>> elsewhere < start
    True
    """
    width = int(window_s * sample_rate)
    if width <= 0 or width >= len(samples):
        return 0, len(samples)

    block = max(1, int(_SCAN_BLOCK_S * sample_rate))
    energies = [
        sum(v * v for v in samples[start : start + block])
        for start in range(0, len(samples), block)
    ]
    span = max(1, width // block)
    if span >= len(energies):
        return 0, len(samples)

    def clear(first_block: int) -> bool:
        """Whether a window starting at this block stays out of ``avoid``."""
        if avoid is None:
            return True
        begin = first_block * block
        return begin + width <= avoid[0] or begin >= avoid[1]

    running = sum(energies[:span])
    best_total, best_block = (running, 0) if clear(0) else (None, None)
    for index in range(span, len(energies)):
        running += energies[index] - energies[index - span]
        first_block = index - span + 1
        if not clear(first_block):
            continue
        if best_total is None or running > best_total:
            best_total, best_block = running, first_block

    if best_block is None:
        return loudest_window(samples, window_s, sample_rate)

    start = min(best_block * block, len(samples) - width)
    return max(0, start), max(0, start) + width


def busiest_window(
    samples: list[float], window_s: float, sample_rate: int = SAMPLE_RATE
) -> tuple[int, int]:
    """The stretch of ``window_s`` seconds that starts the most events.

    :func:`loudest_window` answers where a file is playing; this answers where it is being
    played. Over five separated stems of one song the loudest window of an acoustic guitar
    held 0.25 onsets per second and the busiest held 1.30, so a question about rhythm put
    to the first one is answered by a chord ringing out.

    The scan is over windows that do not overlap, one pass over the audio, about the cost
    of describing it once. Ties go to the louder window, because material with no onsets
    anywhere, such as a pad, has nothing else to choose by.

    Args:
        samples: Mono audio.
        window_s: How long a window to find. A window longer than the audio returns the
            whole of it.
        sample_rate: Rate the samples are at.

    Returns:
        ``(start, end)`` as indices into ``samples``.

    >>> steady = [0.4, -0.4] * 16000
    >>> struck = ([0.9] * 400 + [0.0] * 3600) * 8
    >>> start, end = busiest_window(steady + struck, 1.0)
    >>> start >= 32000
    True
    """
    width = int(window_s * sample_rate)
    if width <= 0 or width >= len(samples):
        return 0, len(samples)

    best: tuple[float, float, int] | None = None
    for start in range(0, len(samples) - width + 1, width):
        chunk = samples[start : start + width]
        rate = describe(chunk, sample_rate).onset_rate_hz
        energy = sum(v * v for v in chunk)
        if best is None or (rate, energy) > (best[0], best[1]):
            best = (rate, energy, start)
    if best is None:
        return loudest_window(samples, window_s, sample_rate)
    return best[2], best[2] + width


#: How much each part of the comparison counts. The spectrum decides most of it because
#: it is what "sounds like" mostly means; the rest separates two sounds that occupy the
#: same range.
#:
#: ``peakiness`` carries the weight that ``flatness`` looks as though it should. For a sung
#: stem against a crash cymbal, band flatness reads 0.16 and 0.15, which is the wrong way
#: round, while peakiness reads 0.44 against 0.14: thirty two bands cannot see a harmonic
#: series. Flatness stays in the descriptor as a reading of the spectrum's tilt and takes
#: no part in the distance.
@dataclass(frozen=True)
class Weights:
    """How much each part of :func:`distance` counts.

    Separate from the function so that a change can be scored:
    ``scripts/eval_sound_index.py`` measures a set of weights against the category every
    indexed item already carries, over thousands of probes.
    """

    spectrum: float = 1.0
    centroid: float = 0.35
    peakiness: float = 0.60
    sustain: float = 0.25
    onset: float = 0.20
    crest: float = 0.15


DEFAULT_WEIGHTS = Weights()

#: Crest factors above this are treated as equally transient. A one shot reaches 30 dB
#: and a sustained sound sits near 12, so the scale is what separates them, not the tail.
_CREST_CEILING_DB = 24.0

#: Octaves of centroid difference treated as completely unalike.
_CENTROID_SPAN = 6.0

#: Onsets per second treated as completely unalike.
_ONSET_SPAN = 8.0


def distance(
    left: Descriptor, right: Descriptor, weights: Weights = DEFAULT_WEIGHTS
) -> float:
    """How far apart two descriptors are. Smaller is more alike, 0.0 is identical.

    The number has no unit and no absolute meaning. It orders candidates against one
    query and says nothing across two different queries.

    The spectral term is the cosine distance of the band vectors taken in the log
    domain, because loudness is logarithmic and a linear cosine lets one loud band
    decide the whole comparison.

    >>> a = Descriptor(bands=(0.8, 0.1, 0.1), centroid_hz=100.0, frames=1)
    >>> b = Descriptor(bands=(0.79, 0.11, 0.1), centroid_hz=105.0, frames=1)
    >>> c = Descriptor(bands=(0.1, 0.1, 0.8), centroid_hz=4000.0, frames=1)
    >>> distance(a, b) < distance(a, c)
    True
    >>> round(distance(a, a), 10)
    0.0
    """
    if not left.bands or not right.bands or len(left.bands) != len(right.bands):
        return math.inf

    floor = 1e-6
    lhs = [math.log(max(v, floor)) - math.log(floor) for v in left.bands]
    rhs = [math.log(max(v, floor)) - math.log(floor) for v in right.bands]
    dot = sum(a * b for a, b in zip(lhs, rhs, strict=True))
    norm = math.sqrt(sum(a * a for a in lhs)) * math.sqrt(sum(b * b for b in rhs))
    spectral = 1.0 - (dot / norm) if norm > 0.0 else 1.0

    # Octaves, not hertz: 200 Hz against 400 Hz is the same step as 2 kHz against 4 kHz.
    octaves = abs(
        math.log2(max(left.centroid_hz, 1.0)) - math.log2(max(right.centroid_hz, 1.0))
    )
    crest = abs(
        min(left.crest_db, _CREST_CEILING_DB) - min(right.crest_db, _CREST_CEILING_DB)
    )
    return (
        weights.spectrum * spectral
        + weights.centroid * min(octaves / _CENTROID_SPAN, 1.0)
        + weights.peakiness * abs(left.peakiness - right.peakiness)
        + weights.sustain * abs(left.sustain_fraction - right.sustain_fraction)
        + weights.onset
        * min(abs(left.onset_rate_hz - right.onset_rate_hz) / _ONSET_SPAN, 1.0)
        + weights.crest * (crest / _CREST_CEILING_DB)
    )
