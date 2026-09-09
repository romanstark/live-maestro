"""What ``analyze_audio`` returns.

Every metric is optional, and that is the point. A silent track has no
correlation, a reply that just reset the integration has no integrated loudness,
and a device that was never loaded has nothing at all. A metric that was not
measured is absent here and never zero, because zero reads as "perfectly
decorrelated at digital silence" where the truth is "nothing played".

``status`` is a Literal, so pydantic refuses any value this documentation does
not list. A status described in prose but not in the type is a status that can be
returned without being documented.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "AudioMeasurementResult",
    "Calibration",
    "DynamicsMetrics",
    "LoudnessMetrics",
    "MeasurementStatus",
    "SpectralBalance",
    "StereoMetrics",
    "TrackAnswer",
]

#: Every outcome this tool can report.
#:
#: ``ok``                the measurement stands
#: ``clipping``          the same, and the true peak is above 0 dBFS
#: ``silent``            the device answered and there was no signal to measure
#: ``device_not_found``  nothing answered on the analyzer port
#: ``track_not_found``   analyzers answered, none of them on the track asked for
#: ``schema_mismatch``   an analyzer answered with a reply layout this build cannot read
MeasurementStatus = Literal[
    "ok", "clipping", "silent", "device_not_found", "track_not_found", "schema_mismatch"
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

    The device generates a sine of fixed amplitude, feeds it to a second meter of
    the same kind, and reports what came back. Because the reference peak is known
    from the amplitude alone, the two error figures say what this build of Max
    does to a peak reading, on this machine, at this sample rate. A peak figure
    beside a calibration is a measurement. Without one, a meter error cannot be
    told from the signal.
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


class AudioMeasurementResult(BaseModel):
    """The whole answer to one ``analyze_audio`` call."""

    status: MeasurementStatus = Field(description="What kind of answer this is.")
    track: str = Field(description="The track that was asked for.")
    measured_track: str | None = Field(
        default=None, description="The track the reported numbers came from."
    )
    window_ms: float | None = Field(
        default=None,
        description=(
            "What the averaged measurements actually cover, which is the "
            "requested window clamped to what the device's buffers hold at the "
            "current sample rate. Loudness figures keep their own EBU windows."
        ),
    )
    samplerate: float | None = Field(
        default=None, description="Sample rate Live was running at, in Hz."
    )
    loudness: LoudnessMetrics | None = None
    dynamics: DynamicsMetrics | None = None
    stereo: StereoMetrics | None = None
    spectrum: SpectralBalance | None = None
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
    summary: str = Field(description="One line stating what was measured.")
    recommendations: list[str] = Field(
        default_factory=list,
        description="What the numbers suggest doing, each naming the number it rests on.",
    )
