"""Turn the replies that arrived into the answer ``analyze_audio`` gives back.

Every function here is pure: no socket, no Ableton Live, no clock. The thresholds, the
track matching and the wording of every recommendation are decided here.
"""

from __future__ import annotations

import math

from live_maestro.analyzer.client import Reply
from live_maestro.analyzer.metrics import (
    SILENCE_FLOOR_DB,
    centroid_hz,
    combined_rms,
    correlation,
    crest_factor_db,
    to_dbfs,
)
from live_maestro.analyzer.models import (
    AccumulationSpan,
    AudioMeasurementResult,
    BandOccupancy,
    Calibration,
    DynamicsMetrics,
    LoudnessMetrics,
    SpectralBalance,
    StereoMetrics,
    TrackAnswer,
    TransportState,
    TransportUnavailable,
)
from live_maestro.analyzer.protocol import (
    BANDS,
    CALIBRATION_AMPLITUDE,
    CALIBRATION_HZ,
    LOW_BAND_HZ,
    REPLY_SCHEMA,
)

__all__ = [
    "MONO_CORRELATION_FLOOR",
    "POLARITY_SUSPECT",
    "SINGLE_WINDOW_OCCUPANCY_NOTE",
    "build",
    "matches",
]

#: Below this, summing the low end to mono changes the level of what is there.
#: A positive correlation this small already means the two channels are pulling
#: against each other where a club system has only one driver to reproduce them.
MONO_CORRELATION_FLOOR = 0.2

#: Above this, a true peak leaves nothing for a lossy encoder, which can add a
#: dB of its own. -1.0 dBFS is the ceiling most delivery specifications name.
DELIVERY_CEILING_DBFS = -1.0

#: A crest factor under this has no transients left to speak of.
FLAT_CREST_DB = 6.0

#: At or below this the two channels are not decorrelated, they oppose each other,
#: which is what one inverted channel produces. The distinction reverses the
#: advice: a decorrelated low end is cured by summing it to mono, and an opposed
#: one is destroyed by the same move, because summing L and -L leaves nothing.
#: ``tests/test_analyzer_report.py`` carries the measurement this rests on.
POLARITY_SUSPECT = -0.5

#: What stands in the place of an occupancy that was never counted. A single
#: window can say which bands carry energy in it, and that is not a share of
#: time: turning one sample into 100% or 0% per band is a percentage with one
#: observation behind it.
SINGLE_WINDOW_OCCUPANCY_NOTE = (
    "Occupancy is a share of time and one window is a sample of time rather than "
    "a share of it: call analyze_audio(accumulate='start'), let the section play, "
    "then analyze_audio(accumulate='read')."
)


def matches(reply: Reply, target: str) -> bool:
    """Determine whether a reply came from the requested track by name.

    ``"*"`` takes everything. ``"master"`` and ``"main"`` match the name Live
    gives its main track. Anything else is matched against the track's name,
    ignoring case and surrounding space.

    A number never matches here. The reply carries a name and an id and no index, so
    ``server.analyze_audio`` turns an index into a name over Channel A before matching.

    >>> reply = Reply(strings={"track_name": "01 SC Trigger"})
    >>> [matches(reply, t) for t in ("*", "01 sc trigger", " 01 SC Trigger ")]
    [True, True, True]
    >>> [matches(reply, t) for t in ("0", "master", "Drums")]
    [False, False, False]
    >>> [matches(Reply(strings={"track_name": "Main"}), t) for t in ("master", "main")]
    [True, True]
    """
    wanted = target.strip()
    if wanted == "*":
        return True
    if wanted.casefold() in ("master", "main"):
        return reply.track_name.strip().casefold() in ("main", "master")
    if wanted.isdigit():
        return False
    return reply.track_name.strip().casefold() == wanted.casefold()


def _finite(value: float | None) -> float | None:
    """Drop a value that is not a real number.

    ``loudness~`` reports negative infinity for digital silence, and an infinity
    serialises to JSON as ``-Infinity``, which is not JSON any client has to
    accept. A missing measurement is the honest shape for it.

    >>> [_finite(-14.0), _finite(float("-inf")), _finite(float("nan"))]
    [-14.0, None, None]
    """
    if value is None or not math.isfinite(value):
        return None
    return value


def _lufs(value: float | None) -> float | None:
    """Return a loudness reading, or ``None`` at or below the -70 LUFS gate.

    BS.1770 gates everything below -70 LUFS out of the measurement, so a reading
    at or under it says the object had nothing to measure rather than that the
    programme is very quiet.

    >>> [_lufs(-14.2), _lufs(-70.0), _lufs(-120.0)]
    [-14.2, None, None]
    """
    finite = _finite(value)
    if finite is None or finite <= -70.0:
        return None
    return round(finite, 2)


def _no_reply(
    target: str,
    transport: TransportState | None = None,
    transport_unavailable: TransportUnavailable | None = None,
) -> AudioMeasurementResult:
    return AudioMeasurementResult(
        status="device_not_found",
        track=target,
        transport=transport,
        transport_unavailable=transport_unavailable,
        summary=(
            "No LiveMaestro Analyzer answered. Every other Maestro tool is "
            "unaffected: this is the only one that needs the device."
        ),
        recommendations=[
            (
                "Install the device with 'live-maestro-install' (or "
                "'python scripts/install_script.py' from a clone), which copies it "
                "into the User Library where Live's browser finds it, under "
                "Presets > Audio Effects > Max Audio Effect."
            ),
            (
                "Drop it on the track to measure, last in the chain: it reads the "
                "signal at its own position, so anything after it is not in what "
                "it reports."
            ),
            (
                "Check that Live's audio engine is on: the device measures the "
                "signal passing through it, and nothing passes while it is off."
            ),
        ],
    )


def _wrong_schema(
    replies: list[Reply],
    target: str,
    transport: TransportState | None = None,
    transport_unavailable: TransportUnavailable | None = None,
) -> AudioMeasurementResult:
    seen = sorted({int(reply.numbers.get("schema", 0)) for reply in replies})
    return AudioMeasurementResult(
        status="schema_mismatch",
        track=target,
        transport=transport,
        transport_unavailable=transport_unavailable,
        answered=[_answer(reply) for reply in replies],
        summary=(
            f"An analyzer answered with reply schema {seen} where this build reads "
            f"schema {REPLY_SCHEMA}. The numbers were not read, because reading "
            f"them against the wrong layout would report the wrong metric."
        ),
        recommendations=[
            (
                "Reinstall the shipped device with 'live-maestro-install' and "
                "reload it in Live, or regenerate it with "
                "'python scripts/build_analyzer_device.py' from a clone."
            )
        ],
    )


def _answer(reply: Reply) -> TrackAnswer:
    return TrackAnswer(
        track_name=reply.track_name or "unknown", track_id=reply.track_id
    )


def _recommend(
    loudness: LoudnessMetrics,
    dynamics: DynamicsMetrics,
    stereo: StereoMetrics,
    spectrum: SpectralBalance,
) -> list[str]:
    """Return the observations worth acting on, each naming the figure it rests on."""
    out: list[str] = []
    peak = dynamics.true_peak_dbfs
    if peak is not None and peak > 0.0:
        out.append(
            f"True peak is {peak:+.2f} dBFS, above full scale. Lower the output or "
            f"set a limiter ceiling: a converter or a lossy encoder clips here even "
            f"though the sample peak may not."
        )
    elif peak is not None and peak > DELIVERY_CEILING_DBFS:
        out.append(
            f"True peak is {peak:+.2f} dBFS, above the {DELIVERY_CEILING_DBFS:+.1f} "
            f"dBFS ceiling most delivery specifications ask for. Lossy encoding "
            f"can add to it, so leave the headroom before delivering."
        )

    broad = stereo.correlation
    if broad is not None and broad <= POLARITY_SUSPECT:
        out.append(
            f"Correlation over the whole band is {broad:+.2f}. The two channels are "
            f"opposing rather than merely different, which is what one inverted "
            f"channel does: look for Left Inv or Right Inv switched on in a Utility, "
            f"a sample whose channels are inverted, or a swapped cable."
        )
    elif broad is not None and broad < 0.0:
        out.append(
            f"Correlation over the whole band is {broad:+.2f}. Part of the signal "
            f"cancels rather than adds when it is summed to mono."
        )

    low = stereo.low_end_correlation
    if low is not None and low <= POLARITY_SUSPECT:
        out.append(
            f"Correlation below {LOW_BAND_HZ:.0f} Hz is {low:+.2f}, which is "
            f"opposition and not width: summing to mono removes the low end instead "
            f"of narrowing it. Find the inverted channel and clear it. Do not reach "
            f"for Bass Mono here, because it sums the two channels below its "
            f"crossover and would take the low end away completely."
        )
    elif low is not None and low < MONO_CORRELATION_FLOOR:
        out.append(
            f"Correlation below {LOW_BAND_HZ:.0f} Hz is {low:+.2f}. The low end will "
            f"change level when it is summed to mono. Utility with Bass Mono on the "
            f"bass sources is the usual fix, and it is the wrong move if this figure "
            f"ever reads near -1, which is an inverted channel rather than width."
        )

    crest = dynamics.crest_factor_db
    if crest is not None and crest < FLAT_CREST_DB:
        out.append(
            f"Crest factor is {crest:.1f} dB, so peak and average sit close together "
            f"and little transient is left."
        )

    integrated = loudness.integrated_lufs
    if integrated is not None and integrated > -9.0:
        out.append(
            f"Integrated loudness is {integrated:.1f} LUFS. Streaming platforms "
            f"normalise to roughly -14 LUFS, so this will be turned down there, and "
            f"club masters usually sit nearer -9 to -8."
        )

    sub, bass = spectrum.sub_dbfs, spectrum.bass_dbfs
    if sub is not None and bass is not None and sub > bass + 3.0:
        out.append(
            f"The 20 to 60 Hz band is {sub - bass:.1f} dB above the 60 to 250 Hz "
            f"band. That much sub relative to bass tends to eat headroom without "
            f"being heard on small speakers."
        )
    low_mid, mid = spectrum.low_mid_dbfs, spectrum.mid_dbfs
    if low_mid is not None and mid is not None and low_mid > mid + 6.0:
        out.append(
            f"The 250 to 500 Hz band is {low_mid - mid:.1f} dB above the 500 Hz to "
            f"2 kHz band, which is where a mix reads as boxy."
        )
    return out


def _calibration(numbers: dict[str, float]) -> Calibration:
    """Compare what the reference meter reported against what its signal is."""
    reference = to_dbfs(CALIBRATION_AMPLITUDE)
    reported_sample = _finite(numbers.get("cal_sample_peak_db"))
    reported_true = _finite(numbers.get("cal_true_peak_db"))
    return Calibration(
        reference_peak_dbfs=reference,
        reference_hz=CALIBRATION_HZ,
        sample_peak_error_db=(
            None if reported_sample is None else round(reported_sample - reference, 2)
        ),
        true_peak_error_db=(
            None if reported_true is None else round(reported_true - reference, 2)
        ),
        momentary_lufs=_lufs(numbers.get("cal_momentary_lufs")),
    )


def build(
    replies: list[Reply],
    target: str,
    *,
    reset: bool,
    match_name: str | None = None,
    occupancy_threshold_db: float | None = None,
    transport: TransportState | None = None,
    transport_unavailable: TransportUnavailable | None = None,
    band_occupancy: BandOccupancy | None = None,
    accumulated_bands: dict[str, float] | None = None,
    accumulation: AccumulationSpan | None = None,
) -> AudioMeasurementResult:
    """Assemble one result from every reply that arrived.

    ``match_name`` is the track name the target resolved to over Channel A, which is
    how an index or "master" reaches a device that reports only a name. Where it is
    given it decides the match on its own, and ``target`` is only what the caller is
    told they asked for.

    ``band_occupancy``, ``accumulated_bands`` and ``accumulation`` travel together from
    an accumulation the caller has been running. Given them, the spectrum is the mean of
    the recorded windows' band amplitudes rather than the last window's, so spectrum,
    occupancy and the ``status`` verdict all cover the one span ``accumulation`` names.
    Given none of them, a result that measured something carries no occupancy at all and
    ``band_occupancy_note`` says why and how to get one. A result that measured nothing,
    such as the ``device_not_found`` below, carries neither.

    >>> build([], "master", reset=False).status
    'device_not_found'
    """
    if not replies:
        return _no_reply(
            target, transport=transport, transport_unavailable=transport_unavailable
        )

    usable = [r for r in replies if int(r.numbers.get("schema", 0)) == REPLY_SCHEMA]
    if not usable:
        return _wrong_schema(
            replies,
            target,
            transport=transport,
            transport_unavailable=transport_unavailable,
        )

    answered = [_answer(reply) for reply in usable]
    if match_name is not None:
        wanted = match_name.strip().casefold()
        candidates = [r for r in usable if r.track_name.strip().casefold() == wanted]
    else:
        candidates = [r for r in usable if matches(r, target)]
    chosen = candidates[0] if candidates else None
    if chosen is None:
        names = ", ".join(sorted(answer.track_name for answer in answered)) or "none"
        asked = target if match_name is None else f"{target!r} ({match_name})"
        return AudioMeasurementResult(
            status="track_not_found",
            track=target,
            transport=transport,
            transport_unavailable=transport_unavailable,
            answered=answered,
            summary=(
                f"{len(usable)} analyzer instance(s) answered, none of them on "
                f"{asked}. Answering tracks: {names}."
            ),
            recommendations=[
                (
                    "Ask for one of the tracks listed in 'answered', or pass '*' "
                    "to measure whichever instance answers first."
                )
            ],
        )

    numbers = chosen.numbers
    samplerate = _finite(numbers.get("samplerate")) or 0.0
    window_samples = _finite(numbers.get("window_samples")) or 0.0
    window_ms = round(window_samples / samplerate * 1000.0, 1) if samplerate > 0 else None

    rms = combined_rms(numbers.get("rms_l", 0.0), numbers.get("rms_r", 0.0))
    rms_db = to_dbfs(rms)
    true_peak = _finite(numbers.get("true_peak_db"))
    sample_peak = _finite(numbers.get("sample_peak_db"))

    # The device measures its own error on every call, so the sample peak is corrected
    # by the figure just measured rather than by a constant. Measured against Live
    # 12.4.5: that outlet reports half the amplitude, a fixed 6.02 dB, in every
    # condition tried.
    #
    # The true peak is left alone. Its reference error is 0.00 dB with the transport
    # stopped and 1.09 dB with it running, and it did not move when the audio changed
    # level by 12 dB, so it is a transient the reference chain's peak hold latched and
    # not an offset the outlet applies. Correcting by it pushed a reading that had
    # tracked a 12 dB fader move exactly 1.09 dB past where the fader put it.
    calibration = _calibration(numbers)
    if calibration.sample_peak_error_db is not None and sample_peak is not None:
        sample_peak -= calibration.sample_peak_error_db
        calibration.sample_peak_corrected = True

    # A reset clears every window the meter holds, not only the integration, so a reset
    # call reports no loudness and no peak at all. Measured against Live 12.4.5: after a
    # reset all six of loudness~'s outlets read negative infinity, both peak holds
    # included. The averaged figures below come from average~ and are untouched by it.
    integrated = None if reset else _lufs(numbers.get("integrated_lufs"))
    loudness = LoudnessMetrics(
        momentary_lufs=None if reset else _lufs(numbers.get("momentary_lufs")),
        short_term_lufs=None if reset else _lufs(numbers.get("short_term_lufs")),
        integrated_lufs=integrated,
        # A range over no integration is 0 LU, which reads as a programme of
        # perfectly even loudness rather than as an absent measurement.
        lra_lu=None if integrated is None else _finite(numbers.get("lra_lu")),
    )
    if reset:
        true_peak = sample_peak = None
    dynamics = DynamicsMetrics(
        true_peak_dbfs=None if true_peak is None else round(true_peak, 2),
        sample_peak_dbfs=None if sample_peak is None else round(sample_peak, 2),
        rms_dbfs=rms_db if rms_db > SILENCE_FLOOR_DB else None,
        crest_factor_db=(
            crest_factor_db(true_peak, rms_db) if true_peak is not None else None
        ),
    )
    broad = correlation(
        numbers.get("mean_lr", 0.0), numbers.get("rms_l", 0.0), numbers.get("rms_r", 0.0)
    )
    low_band = correlation(
        numbers.get("mean_low_lr", 0.0),
        numbers.get("rms_low_l", 0.0),
        numbers.get("rms_low_r", 0.0),
    )
    # The name promises more than the low band on its own can carry. A mix whose
    # whole band correlates negatively loses level when it is summed, whatever the
    # low band does, so both have to hold for this to be true.
    if low_band is None:
        compatible = None
    else:
        compatible = low_band >= MONO_CORRELATION_FLOOR and (broad is None or broad >= 0.0)
    stereo = StereoMetrics(
        correlation=broad,
        low_end_correlation=low_band,
        mono_compatible=compatible,
    )
    # An accumulated read averages the band amplitudes over the windows it
    # counted, so the spectrum and the occupancy describe the same span. Without
    # one, the bands come from the window this reply covers.
    if accumulated_bands is not None:
        amplitudes = {name: accumulated_bands.get(name, 0.0) for name, _, _ in BANDS}
    else:
        amplitudes = {name: numbers.get(name, 0.0) for name, _, _ in BANDS}
    spectrum = SpectralBalance(
        sub_dbfs=_band_db(amplitudes["band_sub"]),
        bass_dbfs=_band_db(amplitudes["band_bass"]),
        low_mid_dbfs=_band_db(amplitudes["band_low_mid"]),
        mid_dbfs=_band_db(amplitudes["band_mid"]),
        high_mid_dbfs=_band_db(amplitudes["band_high_mid"]),
        presence_dbfs=_band_db(amplitudes["band_presence"]),
        air_dbfs=_band_db(amplitudes["band_air"]),
        centroid_hz=centroid_hz(amplitudes),
    )

    # A verdict has to cover the same span as the figures printed beside it. The reply
    # is one window, while on an accumulated read the spectrum and the occupancy are the
    # whole accumulation, so silence there means both: a section whose last window fell
    # in a gap between two phrases is not a silent section.
    last_window_silent = dynamics.rms_dbfs is None
    if accumulation is None:
        silent = last_window_silent
    else:
        silent = last_window_silent and all(
            level is None for level in _band_levels(spectrum)
        )
    status = "silent" if silent else ("clipping" if (true_peak or -99.0) > 0.0 else "ok")
    where = chosen.track_name or target

    # Nothing is synthesised where no accumulation was run. One window says which bands
    # carry energy in it and not what share of time they do: 100.0 or 0.0 per band off a
    # single sample is a percentage with one observation behind it.
    note = None if band_occupancy is not None else SINGLE_WINDOW_OCCUPANCY_NOTE

    if silent and accumulation is not None:
        # Silence established over the counted windows, not over the last of them.
        summary = f"{where} was silent over the accumulation."
    elif silent:
        summary = (
            f"{where} was silent over the last {window_ms:.0f} ms."
            if window_ms
            else f"{where} was silent."
        )
    else:
        parts: list[str] = []
        if dynamics.rms_dbfs is not None:
            parts.append(f"{dynamics.rms_dbfs:.1f} dBFS RMS")
        if true_peak is not None:
            parts.append(f"{true_peak:+.2f} dBFS true peak")
        if loudness.short_term_lufs is not None:
            parts.append(f"{loudness.short_term_lufs:.1f} LUFS short term")
        if broad is not None:
            parts.append(f"correlation {broad:+.2f}")
        summary = (
            f"{where}: " + ", ".join(parts) + "."
            if parts
            else f"{where} carried signal in the windows that were recorded."
        )
        if last_window_silent:
            # Whatever is missing above is missing for one stated reason, rather
            # than reading as a measurement that failed.
            summary += (
                " Loudness, dynamics and stereo come from the last recorded "
                "window, and that window was silent."
            )

    if accumulation is not None:
        noun = "window" if accumulation.windows == 1 else "windows"
        span = (
            f" Occupancy and spectrum cover {accumulation.windows} {noun} over "
            f"{accumulation.elapsed_s:.1f} s"
        )
        if accumulation.coverage_pct is not None:
            span += f", covering {accumulation.coverage_pct:.1f}% of it"
        summary += f"{span}."

    return AudioMeasurementResult(
        status=status,
        track=target,
        measured_track=chosen.track_name or None,
        window_ms=window_ms,
        samplerate=samplerate or None,
        loudness=loudness,
        dynamics=dynamics,
        stereo=stereo,
        spectrum=spectrum,
        band_occupancy=band_occupancy,
        band_occupancy_note=note,
        occupancy_threshold_db=(
            occupancy_threshold_db if band_occupancy is not None else None
        ),
        accumulated_windows=(None if accumulation is None else accumulation.windows),
        accumulation=accumulation,
        transport=transport,
        transport_unavailable=transport_unavailable,
        calibration=calibration,
        answered=answered,
        summary=summary,
        recommendations=(
            [] if silent else _recommend(loudness, dynamics, stereo, spectrum)
        )
        + _ambiguity(chosen, candidates),
    )


def _ambiguity(chosen: Reply, candidates: list[Reply]) -> list[str]:
    """Warn when the reported track's name is shared by another answering instance.

    Live allows two tracks to carry the same name and the reply carries no index, so
    measuring the first of them and saying nothing would report one track under a
    description that fits two.

    Several candidates is not itself ambiguous, because ``"*"`` matches every instance.
    What decides is whether the reported track's name identifies it on its own.
    """
    name = chosen.track_name.strip().casefold()
    same = [r for r in candidates if r.track_name.strip().casefold() == name]
    if len(same) < 2:
        return []
    ids = ", ".join(str(reply.track_id) for reply in same)
    return [
        (
            f"{len(same)} tracks named {chosen.track_name!r} answered, with ids "
            f"{ids}. The first is reported. Rename one of them to measure them "
            f"apart."
        )
    ]


def _band_levels(spectrum: SpectralBalance) -> list[float | None]:
    """Every band level of a spectrum, so the seven can be judged together.

    A band reads ``None`` where its amplitude sits at or under the silence floor,
    so a spectrum whose seven levels are all ``None`` measured no energy at all.

    >>> _band_levels(SpectralBalance(mid_dbfs=-20.0)).count(None)
    6
    """
    return [
        spectrum.sub_dbfs,
        spectrum.bass_dbfs,
        spectrum.low_mid_dbfs,
        spectrum.mid_dbfs,
        spectrum.high_mid_dbfs,
        spectrum.presence_dbfs,
        spectrum.air_dbfs,
    ]


def _band_db(amplitude: float) -> float | None:
    value = to_dbfs(amplitude)
    return value if value > SILENCE_FLOOR_DB else None
