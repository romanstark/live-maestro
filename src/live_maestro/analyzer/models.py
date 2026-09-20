"""What ``analyze_audio`` returns.

Every metric is optional: a silent track has no correlation, a reply that just reset
the integration has no integrated loudness, and a device that was never loaded has
nothing at all. A metric that was not measured is absent and never zero, because zero
reads as "perfectly decorrelated at digital silence" where the truth is "nothing
played".

``status`` is a Literal, so pydantic refuses any value ``MeasurementStatus`` omits.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "AccumulationSpan",
    "AudioMeasurementResult",
    "BandOccupancy",
    "Calibration",
    "DynamicsMetrics",
    "LoudnessMetrics",
    "MeasurementStatus",
    "SpectralBalance",
    "StereoMetrics",
    "TrackAnswer",
    "TransportState",
    "TransportUnavailable",
]

#: Every outcome this tool can report.
#:
#: ``ok``                the measurement stands
#: ``clipping``          the same, and the true peak is above 0 dBFS
#: ``silent``            the device answered and there was no signal to measure,
#:                       and on accumulate='read' none in any counted window
#: ``device_not_found``  nothing answered on the analyzer port
#: ``track_not_found``   analyzers answered, none of them on the track asked for
#: ``schema_mismatch``   an analyzer answered with a reply layout this build cannot read
#: ``accumulating``      background accumulation started
#: ``reset``             accumulation reset
#: ``not_accumulating``  read requested but no accumulation was active
MeasurementStatus = Literal[
    "ok",
    "clipping",
    "silent",
    "device_not_found",
    "track_not_found",
    "schema_mismatch",
    "accumulating",
    "reset",
    "not_accumulating",
]


class LoudnessMetrics(BaseModel):
    """Loudness as ITU-R BS.1770-4 and EBU R128 define it, from Max's ``loudness~``."""

    momentary_lufs: float | None = Field(
        default=None, description="Loudness of the last 400 ms in LUFS."
    )
    short_term_lufs: float | None = Field(
        default=None, description="Loudness of the last 3 seconds in LUFS."
    )
    integrated_lufs: float | None = Field(
        default=None,
        description=(
            "Gated loudness in LUFS since audio started or since the last reset. "
            "Absent on a call that performed a reset: a reset clears everything "
            "the meter holds, momentary and short term and both peaks included, "
            "so a reset call reports nothing and the call after it reports again."
        ),
    )
    lra_lu: float | None = Field(
        default=None,
        description=(
            "Loudness range in LU over the integration. Absent whenever the "
            "integrated figure is, because a range over no measurement is 0 and "
            "0 LU reads as a programme of perfectly even loudness."
        ),
    )


class DynamicsMetrics(BaseModel):
    """Peak and average level, and the distance between them."""

    true_peak_dbfs: float | None = Field(
        default=None,
        description=(
            "Highest true peak in dBFS, oversampled, so it includes peaks that "
            "fall between two samples and that a sample peak meter misses. This "
            "is the highest since the meter was last reset and not a figure over "
            "'window_ms': it only rises. To measure a peak, call once with "
            "reset=true, let at least one pass of the material through, then call "
            "again. "
            "Reported as the meter gives it. The reference signal's own true peak "
            "error is in 'calibration' and is deliberately not subtracted here: "
            "it is a latched transient rather than an offset, and correcting by it "
            "was measured to move a correct reading by 1.09 dB."
        ),
    )
    sample_peak_dbfs: float | None = Field(
        default=None,
        description=(
            "Highest single sample value in dBFS, a maximum since the last reset "
            "in the same way as the true peak, corrected by the error the "
            "device's own reference measured on this call. The correction is "
            "needed: against a reference sine of amplitude 0.5, whose peak is "
            "-6.02 dBFS exactly, this outlet reports -12.04 dBFS, which is half "
            "the amplitude. Measured against Live 12.4.5 the offset is exactly a "
            "factor of two in every condition tried, with the transport stopped "
            "and running, at two levels 12 dB apart, and with one channel silent. "
            "See 'calibration' for the figure this call used."
        ),
    )
    rms_dbfs: float | None = Field(
        default=None,
        description="Both channels' RMS over the measurement window, in dBFS.",
    )
    crest_factor_db: float | None = Field(
        default=None,
        description=(
            "True peak minus RMS in dB. Large means the signal has transients "
            "left, small means it is compressed or limited flat. The two figures "
            "cover different spans: the peak is a maximum since the last reset "
            "and the RMS is an average over 'window_ms', so this is only a crest "
            "factor of one passage when the reset and the window bracket the same "
            "material."
        ),
    )


class StereoMetrics(BaseModel):
    """Phase relationship between the two channels."""

    correlation: float | None = Field(
        default=None,
        description=(
            "Correlation coefficient over the whole band, from -1.0 for an "
            "inverted channel through 0.0 for uncorrelated to +1.0 for mono. "
            "Absent when either channel is silent."
        ),
    )
    low_end_correlation: float | None = Field(
        default=None,
        description=(
            "The same coefficient measured below 120 Hz only. It is an average "
            "over the measurement window, so a figure sitting near a threshold "
            "moves between calls and is worth reading twice."
        ),
    )
    mono_compatible: bool | None = Field(
        default=None,
        description=(
            "Derived here, not reported by the device: true when the low end "
            "correlates positively enough to survive being summed to mono and the "
            "whole band does not correlate negatively. A figure near -1 in either "
            "correlation is one inverted channel rather than a wide image, and "
            "summing that to mono removes what is there instead of narrowing it."
        ),
    )


class SpectralBalance(BaseModel):
    """Energy per musical zone, measured on the mono sum of the two channels."""

    sub_dbfs: float | None = Field(default=None, description="20 Hz to 60 Hz, in dBFS.")
    bass_dbfs: float | None = Field(default=None, description="60 Hz to 250 Hz, in dBFS.")
    low_mid_dbfs: float | None = Field(default=None, description="250 Hz to 500 Hz, in dBFS.")
    mid_dbfs: float | None = Field(default=None, description="500 Hz to 2 kHz, in dBFS.")
    high_mid_dbfs: float | None = Field(default=None, description="2 kHz to 6 kHz, in dBFS.")
    presence_dbfs: float | None = Field(default=None, description="6 kHz to 10 kHz, in dBFS.")
    air_dbfs: float | None = Field(
        default=None, description="Above 10 kHz, in dBFS. A high pass with no upper edge."
    )
    centroid_hz: float | None = Field(
        default=None,
        description=(
            "Energy weighted centre of the seven bands above, in Hz. This is a "
            "centre of mass over seven band levels, not the spectral centroid of "
            "an FFT, and it moves in coarser steps than one."
        ),
    )


class Calibration(BaseModel):
    """What the meter reports for a signal whose peak is known exactly.

    The device generates a sine of fixed amplitude, feeds it to a second meter of the
    same kind, and reports what came back. The reference peak follows from the
    amplitude alone, so the two error figures say what this build of Max does to a peak
    reading, on this machine, at this sample rate. Read a peak figure against them: on
    its own it cannot be told apart from a meter error.
    """

    reference_peak_dbfs: float = Field(
        description="The reference sine's peak, computed from its amplitude alone."
    )
    reference_hz: float = Field(description="The reference sine's frequency in Hz.")
    sample_peak_error_db: float | None = Field(
        default=None,
        description=(
            "Reported sample peak minus the known reference peak. Zero means the "
            "sample peak outlet can be read as dBFS directly."
        ),
    )
    true_peak_error_db: float | None = Field(
        default=None,
        description=(
            "Reported true peak minus the known reference peak, for information "
            "only. Nothing is corrected by it. Measured against Live 12.4.5: this "
            "error is 0.00 dB with the transport stopped and +1.09 dB with it "
            "running, and it does not change when the audio on the track changes "
            "level by 12 dB, so it is a transient latched by a peak hold in the "
            "reference chain rather than an offset of the outlet. Subtracting it "
            "would move a true peak that does not carry it."
        ),
    )
    momentary_lufs: float | None = Field(
        default=None,
        description="Momentary loudness of the reference, for checking the loudness path.",
    )
    sample_peak_corrected: bool = Field(
        default=False,
        description=(
            "True when the sample peak in 'dynamics' had "
            "'sample_peak_error_db' subtracted. False means the reference had "
            "nothing to report, which is what a call that reset the meters gets, "
            "and the sample peak is then 6 dB low and says so here."
        ),
    )


class TrackAnswer(BaseModel):
    """One analyzer instance that answered, whether or not it was the one asked for."""

    track_name: str = Field(description="Name of the track the instance sits on.")
    track_id: int | None = Field(
        default=None,
        description=(
            "The track's Live Object Model id. It distinguishes two tracks that "
            "share a name from each other, and nothing maps it back to a track "
            "index over this channel."
        ),
    )


class BandOccupancy(BaseModel):
    """Share of the accumulated windows in which each band carried energy, in %.

    Two bands can average to the same level while one plays through the section and the
    other answers twice a bar; this is what tells them apart.

    It is an estimator over sampled windows and not a measurement of the whole span:
    each figure is a share of the windows that were recorded, and
    'accumulation.coverage_pct' says how much of the elapsed span those windows reach.

    A band counts for a window when it is above the silence floor and within
    occupancy_threshold_db of that window's own loudest band, so the comparison is
    relative to the material rather than to an absolute level.
    """

    sub_pct: float | None = Field(
        default=None,
        description="20 Hz to 60 Hz occupancy percentage.",
    )
    bass_pct: float | None = Field(
        default=None,
        description="60 Hz to 250 Hz occupancy percentage.",
    )
    low_mid_pct: float | None = Field(
        default=None,
        description="250 Hz to 500 Hz occupancy percentage.",
    )
    mid_pct: float | None = Field(
        default=None,
        description="500 Hz to 2 kHz occupancy percentage.",
    )
    high_mid_pct: float | None = Field(
        default=None,
        description="2 kHz to 6 kHz occupancy percentage.",
    )
    presence_pct: float | None = Field(
        default=None,
        description="6 kHz to 10 kHz occupancy percentage.",
    )
    air_pct: float | None = Field(
        default=None,
        description="Above 10 kHz occupancy percentage.",
    )


class AccumulationSpan(BaseModel):
    """What an accumulated read counted, and what stretch of time it reached.

    Occupancy and the averaged spectrum are taken over these windows, so this says
    whether they describe a bar or a whole section, and how much of that stretch was
    sampled.
    """

    windows: int = Field(
        description="How many analysis windows were recorded and counted."
    )
    elapsed_s: float = Field(
        description=(
            "Seconds between the start of the accumulation and this read. It is "
            "wall clock time and includes whatever the transport did in it, "
            "stopping included."
        )
    )
    window_ms: float | None = Field(
        default=None,
        description=(
            "How far back a single recorded window reached, in milliseconds. Null "
            "where no window was recorded to report it."
        ),
    )
    coverage_pct: float | None = Field(
        default=None,
        description=(
            "What share of 'elapsed_s' the recorded windows add up to, in %. "
            "Under 100 the windows sampled the span rather than covering it. "
            "Over 100 they add up to more time than has elapsed, which the first "
            "window does on its own: it reaches back to the audio before the "
            "accumulation started. That is up to one window more than the span "
            "holds, so a short accumulation reads well above 100 without any two "
            "windows counting the same audio. Null while nothing has been "
            "recorded."
        ),
    )


class TransportUnavailable(BaseModel):
    """Why no transport state was read, standing in the place of one.

    A null transport with no reason cannot be told apart from a transport that was read
    and found empty, so a state that could not be taken says so here.
    """

    code: str = Field(description="Machine readable reason the transport was not read.")
    reason: str = Field(description="One sentence saying what stopped the read.")


class TransportState(BaseModel):
    """Transport and playback state during measurement.

    Every field is derived from what Live answered in one batch, and each description
    says what its number counts and what it leaves out.
    """

    is_playing: bool = Field(description="Whether Live's transport is currently running.")
    tracks_sounding: int = Field(
        description=(
            "How many tracks hold a Session clip playing: the count of tracks whose "
            "playing_slot_index is 0 or more while is_playing is true, and 0 whenever the "
            "transport is stopped. It is not a count of what can be heard. A muted track, "
            "a track under an active solo elsewhere, a track whose clip is silent and a "
            "track routed away from the master are each counted the same as any other. It "
            "is read from Session slots only, so Arrangement playback is outside what it "
            "counts. It is forced to 0 on a stopped transport on purpose: "
            "playing_slot_index keeps the slot a track last played after the transport "
            "stops, so counting it there would report tracks that had already fallen "
            "silent."
        )
    )
    tracks_total: int = Field(description="Total number of tracks in the song.")
    in_transition: bool = Field(
        description=(
            "True while at least one track reports a fired_slot_index of 0 or more, which "
            "is a clip launch accepted and still waiting for the next quantization "
            "boundary. It falls back to false as the boundary passes and the fired slot "
            "becomes the playing slot, so a measurement taken while it is true spans a "
            "scene change rather than one settled arrangement of clips. A launch fired "
            "with the transport stopped applies immediately, has no boundary to wait for, "
            "and reports no transition. It is not derived from playing_slot_index: the "
            "value -2 there is the standing state \"no clip is playing on this track\", "
            "which survives a stop and is not a moment in a transition."
        )
    )


class AudioMeasurementResult(BaseModel):
    """The whole answer to one ``analyze_audio`` call."""

    status: MeasurementStatus = Field(
        description=(
            "What kind of answer this is. On accumulate='read' it is taken over "
            "the whole accumulation, the same span as 'spectrum' and "
            "'band_occupancy', and not over the last recorded window: a section "
            "whose last window fell in a gap is not a silent section. Everywhere "
            "else it covers 'window_ms'."
        )
    )
    track: str = Field(description="The track that was asked for.")
    measured_track: str | None = Field(
        default=None, description="The track the reported numbers came from."
    )
    window_ms: float | None = Field(
        default=None,
        description=(
            "What the averaged measurements actually cover, which is the "
            "requested window clamped to what the device's buffers hold at the "
            "current sample rate. Loudness figures keep their own EBU windows. "
            "On accumulate='read' this is the last recorded window and it governs "
            "the loudness, dynamics and stereo figures alone: 'spectrum' and "
            "'band_occupancy' cover 'accumulation' instead."
        ),
    )
    samplerate: float | None = Field(
        default=None, description="Sample rate Live was running at, in Hz."
    )
    loudness: LoudnessMetrics | None = None
    dynamics: DynamicsMetrics | None = None
    stereo: StereoMetrics | None = None
    spectrum: SpectralBalance | None = Field(
        default=None,
        description=(
            "Energy per band. On a single measurement it covers 'window_ms', the "
            "same span as the other averaged figures. On accumulate='read' it is "
            "the mean of the recorded windows' band amplitudes, so it describes "
            "the same windows the occupancy was counted over."
        ),
    )
    band_occupancy: BandOccupancy | None = Field(
        default=None,
        description=(
            "Share of the accumulated windows in which each band carried energy "
            "within occupancy_threshold_db of that window's loudest band, in %. "
            "Present on accumulate='read' alone. On any other measured result it "
            "is null and 'band_occupancy_note' says why. A result that measured "
            "nothing (device_not_found, schema_mismatch, track_not_found) carries "
            "neither: its 'summary' and 'recommendations' say what went wrong."
        ),
    )
    band_occupancy_note: str | None = Field(
        default=None,
        description=(
            "Why 'band_occupancy' is null, and how to get one. Null whenever the "
            "occupancy is present, and on a result that measured nothing, where "
            "no accumulation would produce one either."
        ),
    )
    occupancy_threshold_db: float | None = Field(
        default=None,
        description=(
            "Threshold in dB below each window's loudest band inside which a band "
            "counted as occupied. Null wherever 'band_occupancy' is null, because "
            "a threshold nothing was compared against describes no measurement."
        ),
    )
    accumulated_windows: int | None = Field(
        default=None,
        description=(
            "How many windows the accumulation counted. The same number as "
            "'accumulation.windows', kept for callers that read it, and null on a "
            "single measurement."
        ),
    )
    accumulation: AccumulationSpan | None = Field(
        default=None,
        description=(
            "What the accumulated windows counted and what stretch of time they "
            "reach. Present on accumulate='read' alone and null on a single "
            "measurement."
        ),
    )
    transport: TransportState | None = Field(
        default=None,
        description="Transport state (playback, sounding tracks, transition).",
    )
    transport_unavailable: TransportUnavailable | None = Field(
        default=None,
        description=(
            "Why 'transport' is null, as a machine code and a sentence. Null "
            "whenever the transport state is present."
        ),
    )
    calibration: Calibration | None = Field(
        default=None,
        description=(
            "What the same kind of meter reported for the device's own reference "
            "sine. Read the peak figures above against this."
        ),
    )
    answered: list[TrackAnswer] = Field(
        default_factory=list,
        description=(
            "Every analyzer instance that answered. More than one entry means "
            "the device is loaded on several tracks and any of them can be "
            "measured by name."
        ),
    )
    summary: str = Field(
        description=(
            "One line stating what was measured. On accumulate='read' it says "
            "which span each half of the answer covers: the level figures come "
            "from the last recorded window and the spectrum and the occupancy "
            "from every counted window."
        )
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="What the numbers suggest doing, each naming the number it rests on.",
    )
