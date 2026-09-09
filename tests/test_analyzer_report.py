"""Tests for what a caller is actually told.

This is the half of Channel C that decides a verdict, picks which analyzer to
report, and words every recommendation. It runs on a list of replies and nothing
else, so all of it is checkable here.
"""

from __future__ import annotations

import json

import pytest

from live_maestro.analyzer import report
from live_maestro.analyzer.client import Reply
from live_maestro.analyzer.protocol import NUMERIC_REPLY_FIELDS, REPLY_SCHEMA

#: A reply from a healthy master: comfortable level, peak under the ceiling,
#: correlated low end, nothing to warn about.
CLEAN: dict[str, float] = {
    "schema": float(REPLY_SCHEMA),
    "request_id": 1.0,
    "samplerate": 48000.0,
    "window_samples": 19200.0,
    "momentary_lufs": -12.5,
    "short_term_lufs": -13.0,
    "integrated_lufs": -14.2,
    "lra_lu": 4.5,
    "sample_peak_db": -1.8,
    "true_peak_db": -1.4,
    "rms_l": 0.15,
    "rms_r": 0.15,
    "mean_lr": 0.018,
    "rms_low_l": 0.10,
    "rms_low_r": 0.10,
    "mean_low_lr": 0.0095,
    "band_sub": 0.02,
    "band_bass": 0.09,
    "band_low_mid": 0.03,
    "band_mid": 0.04,
    "band_high_mid": 0.02,
    "band_presence": 0.01,
    "band_air": 0.005,
    # The device's own reference sine: amplitude 0.5, so its peak is -6.02 dBFS.
    # Both outlets report it back exactly here, so this fixture is a meter with no
    # error and every other test in this file reads undisturbed numbers. The
    # errors a real Max shows are exercised below.
    "cal_momentary_lufs": -6.03,
    "cal_sample_peak_db": -6.02,
    "cal_true_peak_db": -6.02,
    "track_id": 12.0,
}


def make(
    name: str = "Main",
    extra: dict[str, float] | None = None,
    **overrides: float,
) -> Reply:
    """A reply built on CLEAN. ``extra`` takes a whole dict of overrides at once."""
    return Reply(
        numbers=dict(CLEAN, **(extra or {}), **overrides),
        strings={"track_name": name},
    )


def test_the_reply_fixture_covers_every_declared_field() -> None:
    # A field added to the protocol without a value here would otherwise be
    # silently defaulted, and every test below would be measuring a default.
    assert set(CLEAN) == set(NUMERIC_REPLY_FIELDS)


def test_nothing_answering_is_reported_as_the_device_missing() -> None:
    result = report.build([], "master", reset=False)
    assert result.status == "device_not_found"
    assert result.loudness is None
    # The first thing to do is run the installer, which puts the device where
    # Live's browser looks. Naming a path to copy by hand sends the reader to do
    # the installer's job.
    assert any("install_script.py" in line for line in result.recommendations)
    assert any("last in the chain" in line for line in result.recommendations)
    # The other 36 tools do not need this device, and a caller reading this
    # should not conclude the server is broken.
    assert "unaffected" in result.summary


def test_a_clean_master_is_ok_and_gets_no_warnings() -> None:
    result = report.build([make()], "master", reset=False)
    assert result.status == "ok"
    assert result.recommendations == []
    assert result.measured_track == "Main"
    assert result.loudness is not None and result.loudness.integrated_lufs == -14.2
    assert result.dynamics is not None and result.dynamics.true_peak_dbfs == -1.4
    assert result.stereo is not None and result.stereo.mono_compatible is True


def test_the_window_is_reported_in_milliseconds_from_samples_and_rate() -> None:
    result = report.build([make()], "master", reset=False)
    assert result.window_ms == pytest.approx(400.0)
    assert result.samplerate == 48000.0


def test_a_true_peak_over_full_scale_is_clipping() -> None:
    result = report.build([make(true_peak_db=0.6)], "master", reset=False)
    assert result.status == "clipping"
    assert any("+0.60 dBFS" in line for line in result.recommendations)


def test_a_peak_over_the_delivery_ceiling_warns_without_calling_it_clipping() -> None:
    result = report.build([make(true_peak_db=-0.4)], "master", reset=False)
    assert result.status == "ok"
    assert any("-1.0" in line for line in result.recommendations)


def test_a_peak_over_the_delivery_ceiling_is_not_described_as_compliant() -> None:
    """A warning that reads as reassurance is worse than no warning.

    -0.4 dBFS is above a -1.0 dBFS ceiling, so the line that fires here has to
    say so. A reader who takes "inside the ceiling" at face value reports the
    requirement as met while it is broken.
    """
    result = report.build([make(true_peak_db=-0.4)], "master", reset=False)
    line = next(line for line in result.recommendations if "ceiling" in line)
    assert "above" in line
    assert "inside" not in line
    assert "-0.40 dBFS" in line


def test_a_decorrelated_low_end_is_named_with_its_number() -> None:
    result = report.build([make(mean_low_lr=-0.005)], "master", reset=False)
    assert result.stereo is not None
    assert result.stereo.low_end_correlation == pytest.approx(-0.5)
    assert result.stereo.mono_compatible is False
    assert any("120 Hz" in line and "-0.50" in line for line in result.recommendations)


def test_an_inverted_channel_is_not_treated_as_a_wide_image() -> None:
    """Bass Mono cures a decorrelated low end and destroys an opposed one.

    Measured 2026-09-07 against Live 12.4.5 with Utility's Right Inv on a mono
    bass: both correlations read -0.999, and the mono sum of that bass was 30 dB
    below the same bass with the inversion cleared. Summing L and -L below a
    crossover leaves nothing, so naming Bass Mono here would silence the part of
    the mix the caller is asking about.
    """
    # rms_l = rms_r = 0.15 and rms_low = 0.10, so these products give -0.999.
    result = report.build(
        [make(mean_lr=-0.0225 * 0.999, mean_low_lr=-0.01 * 0.999)], "master", reset=False
    )
    assert result.stereo is not None
    assert result.stereo.correlation == pytest.approx(-0.999, abs=0.002)
    assert result.stereo.mono_compatible is False

    low_line = next(line for line in result.recommendations if "120 Hz" in line)
    assert "Do not reach for Bass Mono" in low_line
    assert "opposition" in low_line
    broad_line = next(line for line in result.recommendations if "whole band" in line)
    assert "inverted channel" in broad_line
    assert "Right Inv" in broad_line


def test_a_merely_decorrelated_low_end_still_gets_the_bass_mono_advice() -> None:
    result = report.build([make(mean_low_lr=0.01 * 0.1)], "master", reset=False)
    assert result.stereo is not None
    assert result.stereo.low_end_correlation == pytest.approx(0.1, abs=0.01)
    line = next(line for line in result.recommendations if "120 Hz" in line)
    assert "Bass Mono on the bass sources is the usual fix" in line
    assert "Do not reach for" not in line


def test_a_negative_whole_band_correlation_is_not_called_mono_compatible() -> None:
    """The state a planted polarity flip produced through a mono kick.

    Measured 2026-09-07 against Live 12.4.5: the inverted bass sat under a mono
    kick, so the band below 120 Hz read +0.23 while the whole band read -0.22.
    A low band figure above the floor is not enough to call that mix summable,
    and reading only the low band reported it as compatible.
    """
    result = report.build(
        [make(mean_lr=-0.0225 * 0.222, mean_low_lr=0.01 * 0.231)], "master", reset=False
    )
    assert result.stereo is not None
    assert result.stereo.correlation == pytest.approx(-0.222, abs=0.002)
    assert result.stereo.low_end_correlation == pytest.approx(0.231, abs=0.002)
    assert result.stereo.low_end_correlation is not None
    assert result.stereo.low_end_correlation > report.MONO_CORRELATION_FLOOR
    assert result.stereo.mono_compatible is False
    assert any("whole band" in line for line in result.recommendations)


def test_silence_is_its_own_outcome_and_carries_no_verdicts() -> None:
    quiet = {
        field: 0.0
        for field in NUMERIC_REPLY_FIELDS
        if field.startswith(("rms", "band", "mean"))
    }
    result = report.build(
        [
            make(
                extra=quiet,
                true_peak_db=-144.0,
                momentary_lufs=-70.0,
                short_term_lufs=-70.0,
                integrated_lufs=-70.0,
            )
        ],
        "master",
        reset=False,
    )
    assert result.status == "silent"
    assert result.recommendations == []
    assert result.dynamics is not None and result.dynamics.rms_dbfs is None
    assert result.dynamics.crest_factor_db is None
    assert result.stereo is not None and result.stereo.correlation is None
    assert result.spectrum is not None and result.spectrum.centroid_hz is None
    # Loudness at the absolute gate is absent, not "very quiet".
    assert result.loudness is not None and result.loudness.momentary_lufs is None


def test_a_reset_call_reports_no_loudness_and_no_peak_at_all() -> None:
    """Measured against Live 12.4.5: a reset clears every window loudness~ holds.

    All six of its outlets read negative infinity straight after one, both peak
    holds included, so a reset call has nothing to report from the meter. Naming
    only the integration here would leave three stale figures looking measured.
    """
    result = report.build([make()], "master", reset=True)
    assert result.loudness is not None
    assert result.loudness.integrated_lufs is None
    assert result.loudness.lra_lu is None
    assert result.loudness.momentary_lufs is None
    assert result.loudness.short_term_lufs is None
    assert result.dynamics is not None
    assert result.dynamics.true_peak_dbfs is None
    assert result.dynamics.sample_peak_dbfs is None
    assert result.dynamics.crest_factor_db is None
    # average~ is not part of the meter, so the windowed figures survive a reset.
    assert result.dynamics.rms_dbfs is not None
    assert result.stereo is not None and result.stereo.correlation is not None


def test_a_range_is_absent_wherever_the_integration_is() -> None:
    # 0 LU reads as a programme of perfectly even loudness, which is the opposite
    # of "there was no integration to take a range over".
    result = report.build([make(integrated_lufs=-90.0, lra_lu=0.0)], "master", reset=False)
    assert result.loudness is not None
    assert result.loudness.integrated_lufs is None
    assert result.loudness.lra_lu is None


def test_the_calibration_says_what_the_meter_does_to_a_known_peak() -> None:
    """The reference peak follows from its amplitude, so the error is measurable."""
    result = report.build([make()], "master", reset=False)
    assert result.calibration is not None
    assert result.calibration.reference_peak_dbfs == pytest.approx(-6.02, abs=0.01)
    assert result.calibration.reference_hz == 1000.0
    assert result.calibration.sample_peak_error_db == pytest.approx(0.0, abs=0.01)
    assert result.calibration.true_peak_error_db == pytest.approx(0.0, abs=0.01)
    assert result.calibration.sample_peak_corrected is True


def test_the_sample_peak_outlet_of_live_12_4_5_is_corrected_by_its_own_error() -> None:
    """Measured against Live 12.4.5 at 96 kHz, with the device's reference sine.

    The reference is a sine of amplitude 0.5, so its peak is -6.0206 dBFS by
    definition. ``loudness~`` reported -6.0206 on its true peak outlet and
    -12.0412 on its sample peak outlet, and -12.0412 is 20*log10(0.25): that
    outlet reports half the amplitude. The offset does not depend on channel
    balance, because the same 6.02 dB was measured with one channel silent.
    """
    result = report.build(
        [make(cal_sample_peak_db=-12.0412, cal_true_peak_db=-6.0206, sample_peak_db=-12.0)],
        "master",
        reset=False,
    )
    assert result.calibration is not None
    assert result.calibration.sample_peak_error_db == pytest.approx(-6.02, abs=0.01)
    assert result.calibration.sample_peak_corrected is True
    assert result.dynamics is not None
    # Reported -12.0, corrected by the measured -6.02, so -5.98 dBFS.
    assert result.dynamics.sample_peak_dbfs == pytest.approx(-5.98, abs=0.01)


def test_the_true_peak_is_never_moved_by_the_reference_error() -> None:
    """Measured against Live 12.4.5: that error is a latched transient, not an offset.

    It reads 0.00 dB with the transport stopped and 1.09 dB with it running, and
    it did not change when the measured audio moved 12 dB on the track fader. A
    true peak that had followed that fader move exactly would be pushed 1.09 dB
    past it by subtracting this, so it is reported and not applied.
    """
    result = report.build([make(cal_true_peak_db=-4.9335)], "master", reset=False)
    assert result.calibration is not None
    assert result.calibration.true_peak_error_db == pytest.approx(1.09, abs=0.01)
    assert result.dynamics is not None
    assert result.dynamics.true_peak_dbfs == -1.4


def test_a_sample_peak_is_not_corrected_when_the_reference_had_nothing_to_report() -> None:
    # A reset clears the reference meter too, so there is no error to subtract.
    # Leaving the peak uncorrected without saying so would be worse than saying
    # the correction did not happen: the figure is 6 dB low and nothing marks it.
    result = report.build(
        [make(cal_sample_peak_db=float("-inf"), cal_true_peak_db=float("-inf"))],
        "master",
        reset=False,
    )
    assert result.calibration is not None
    assert result.calibration.sample_peak_corrected is False
    assert result.dynamics is not None
    assert result.dynamics.sample_peak_dbfs == -1.8


def test_an_infinite_loudness_never_reaches_the_caller() -> None:
    result = report.build([make(true_peak_db=float("-inf"))], "master", reset=False)
    assert result.dynamics is not None and result.dynamics.true_peak_dbfs is None
    # The whole result must survive being serialised as JSON, which -Infinity
    # would not: json.dumps writes it but no client is obliged to read it.
    text = json.dumps(result.model_dump(), allow_nan=False)
    assert "Infinity" not in text


def test_every_instance_is_listed_and_the_named_one_is_measured() -> None:
    replies = [make("Main"), make("Drums", true_peak_db=0.9, track_id=34.0)]
    by_name = report.build(replies, "Drums", reset=False)
    assert by_name.measured_track == "Drums"
    assert by_name.status == "clipping"
    assert {answer.track_name for answer in by_name.answered} == {"Main", "Drums"}
    assert {answer.track_id for answer in by_name.answered} == {12, 34}

    by_master = report.build(replies, "master", reset=False)
    assert by_master.measured_track == "Main"


def test_an_index_is_matched_through_the_name_it_resolved_to() -> None:
    """An index reaches a device that reports only a name by being resolved first.

    ``server.analyze_audio`` reads the name at that index over Channel A and
    passes it as ``match_name``, because the reply carries a name and an id and
    no index.
    """
    replies = [make("Main"), make("Drums", track_id=34.0)]
    result = report.build(replies, "2", reset=False, match_name="Drums")
    assert result.status == "ok"
    assert result.measured_track == "Drums"
    # The caller is told what they asked for, not what it resolved to.
    assert result.track == "2"


def test_an_index_that_resolved_to_a_name_nobody_answered_on_says_both() -> None:
    result = report.build([make("Main")], "9", reset=False, match_name="Bass")
    assert result.status == "track_not_found"
    assert "'9'" in result.summary
    assert "Bass" in result.summary
    assert "Main" in result.summary


def test_an_unresolvable_index_matches_nothing_rather_than_guessing() -> None:
    # No match_name means Live refused the path, which is what an index past the
    # end of the set does. A digit never matches a name on its own.
    result = report.build([make("Main")], "99", reset=False)
    assert result.status == "track_not_found"


def test_several_instances_answering_is_not_itself_ambiguous() -> None:
    # "*" matches every instance on purpose. Two answers with two names identify
    # two tracks perfectly well, and warning about them would be a warning about
    # the feature working.
    replies = [make("Main"), make("Drums", track_id=34.0)]
    result = report.build(replies, "*", reset=False)
    assert result.status == "ok"
    assert result.measured_track == "Main"
    assert not [line for line in result.recommendations if "named" in line]


def test_two_tracks_of_the_same_name_are_reported_as_ambiguous() -> None:
    # Live allows duplicate track names and the reply carries no index, so
    # measuring the first of them silently would describe one track with a
    # description that fits two.
    replies = [make("Drums", track_id=34.0), make("Drums", track_id=56.0)]
    result = report.build(replies, "Drums", reset=False)
    assert result.status == "ok"
    assert result.measured_track == "Drums"
    warning = [line for line in result.recommendations if "same" in line or "named" in line]
    assert warning, result.recommendations
    assert "34" in warning[0] and "56" in warning[0]


def test_asking_for_a_track_that_did_not_answer_says_which_ones_did() -> None:
    result = report.build([make("Drums")], "Bass", reset=False)
    assert result.status == "track_not_found"
    assert "Drums" in result.summary
    assert result.loudness is None
    assert [answer.track_name for answer in result.answered] == ["Drums"]


def test_a_star_takes_whichever_instance_answered() -> None:
    result = report.build([make("Drums")], "*", reset=False)
    assert result.status == "ok"
    assert result.measured_track == "Drums"


def test_a_reply_from_another_schema_is_refused_rather_than_misread() -> None:
    result = report.build([make(schema=99.0)], "master", reset=False)
    assert result.status == "schema_mismatch"
    assert result.loudness is None
    assert "99" in result.summary
    assert any("build_analyzer_device" in line for line in result.recommendations)


def test_a_flat_crest_factor_is_reported() -> None:
    loud = make(rms_l=0.7, rms_r=0.7, mean_lr=0.49, true_peak_db=-1.5)
    result = report.build([loud], "master", reset=False)
    assert result.dynamics is not None
    assert result.dynamics.crest_factor_db is not None
    assert result.dynamics.crest_factor_db < 6.0
    assert any("Crest factor" in line for line in result.recommendations)


def test_a_sub_heavy_balance_is_reported_with_the_difference() -> None:
    result = report.build([make(band_sub=0.2, band_bass=0.09)], "master", reset=False)
    assert any("20 to 60 Hz" in line for line in result.recommendations)


def test_the_boxy_range_is_reported_with_the_difference() -> None:
    result = report.build([make(band_low_mid=0.2, band_mid=0.04)], "master", reset=False)
    assert any("250 to 500 Hz" in line for line in result.recommendations)


def test_a_master_pushed_loud_is_compared_against_platform_targets() -> None:
    result = report.build([make(integrated_lufs=-6.0)], "master", reset=False)
    assert any("-14 LUFS" in line for line in result.recommendations)


def test_every_recommendation_quotes_the_number_it_rests_on() -> None:
    result = report.build(
        [make(true_peak_db=0.6, mean_low_lr=-0.005, rms_l=0.7, rms_r=0.7,
              mean_lr=0.49, integrated_lufs=-5.0, band_sub=0.3, band_bass=0.05,
              band_low_mid=0.3, band_mid=0.02)],
        "master",
        reset=False,
    )
    assert len(result.recommendations) >= 5
    for line in result.recommendations:
        assert any(char.isdigit() for char in line), line


def test_track_matching_rules() -> None:
    track = Reply(strings={"track_name": "Bass"})
    main = Reply(strings={"track_name": "Main"})
    ret = Reply(strings={"track_name": "A Reverb"})

    assert report.matches(track, "bass") and report.matches(track, "BASS")
    assert report.matches(track, " Bass ")
    assert report.matches(main, "master") and report.matches(main, "main")
    assert report.matches(ret, "A Reverb")
    assert not report.matches(track, "master")
    assert not report.matches(ret, "Reverb")
    assert all(report.matches(reply, "*") for reply in (track, main, ret))


def test_a_digit_never_matches_on_its_own() -> None:
    # A reply carries a name and an id, and neither is a position in the set. An
    # index is resolved to a name before it reaches here.
    for reply in (Reply(strings={"track_name": "Bass"}), Reply(strings={"track_name": "0"})):
        assert not report.matches(reply, "0")
        assert not report.matches(reply, "7")
