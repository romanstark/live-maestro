# Rebuild from a reference

Live Maestro places reviewed notes, chooses instruments, records actual Live audio and compares a candidate with the reference. Independent Audio Maestro owns source measurements and transcription; UVR Maestro optionally provides separated files. Supplied stems and analysis from any other producer are equally usable. None of these packages requires another Maestro installation.

## Bring reviewed analysis

Analyze a short section in Audio Maestro, using Basic Pitch for pitched parts and optional SheetSage for a second lead-sheet estimate. Preserve original-source offsets and sample rates. Review notes and establish source-second/quarter-beat anchors before placement. Chroma, onsets and tempo candidates do not establish notes, meter or downbeats. Timing review does not certify pitch estimates.

Use `convert_clip_time` for the actual Warp/tempo relationship of an existing Live clip. Import a reviewed MIDI file or pass reviewed beat notes to `write_arrangement_notes` / `write_clip_notes`. Live does not install models or invoke transcription. Use the producing server to poll/cancel its jobs.

## Record a candidate from Live

Install/reload the updated LiveMaestro Analyzer at the desired signal point.
`capture_audio` records that signal; Main is before its fader. Use a 0 dB Main
fader when treating the recording as the main output. No track is soloed and no
routing, instrument or effect is changed by capture.

The Analyzer is a device in the track's chain, so it measures before that track's
fader: a fader move does not show in the capture. Load it by hand on every track
you want to record; each capture records one track. A group track with the
Analyzer records the whole group.

Before capturing, check that Back to Arrangement is not lit. A track that follows
the Session does not play its Arrangement clips, and the capture then looks like a
silent instrument; the report's `warnings` names it. Instruments that remember what
they last played (keyswitches, CC, round robins) reach a mid-song window in
whatever state they were left in. Pass `play_from_beat` (0 for the song start) to
play the lead-in unrecorded and record only from `from_beat`.

Without `from_beat`, capture records the current signal. With it, the transport
must be stopped with recording disabled. Capture temporarily sets the start marker and playhead, starts
the Arrangement, then restores both and stops playback. It includes pre-roll,
so its file length is longer than the requested listening interval. Cancellation
is cooperative: wait for `cleanup_pending=false` before starting another capture.
If Live disconnects, check its transport and recorder state before continuing.

The internal six-channel float WAV carries L/R, DSP sample count, host beat,
transport state and validity flags. A continuous recorded counter is required
before exporting stereo `capture.wav`. Initial counter warmup of at most 64 frames
is disclosed; internal discontinuities fail verification. The float32 counter
must remain below 2^24 for single-sample precision. High-rate long captures can
therefore fail verification; choose shorter sections.

Take capture time from the anchors, never from the tempo map:
`get_audio_report(job_id, section="timeline", beats=[...])` returns the frame and
seconds of each song beat. A start estimated from the tempo map has been three
beats off, and every finding built on it was wrong.

Timeline anchors share the audio's frame positions, but host fields are scheduler
sampled. Observed update spacing is not an accuracy bound and does not prove a
sample-accurate song origin. Use that witness and explicit excerpt offsets for
comparison. A capture is not an offline export of stems or an automatic inverse
of the original production chain.

The report's `audio_paths[0]` is the stereo audio. `raw_path` is the six-channel
recorder file in the order of `raw_channels`; only channels 0 and 1 are audio, and
averaging all six measures a counter ramp. Anchor frames index both files alike.

## Calibrate the capture chain

The host beat in the timing witness can lead the recorded audio by tens of
milliseconds. The lag depends on the set: Live's delay compensation, plugins
that report no latency and Track Delay all move it. The report therefore leaves
`beat_witness_lag_ms` empty. Measure it once per set: place a known audio file
in the Arrangement, capture it, then run `compare_audio` with the file as
reference and the capture as candidate. `candidate_delay_ms` is the chain lag;
pass it to `compare_spectrum` as `chain_offset_ms`. An instrument whose plugin
reports no latency adds its own lag on top; `compare_spectrum` estimates the
remaining offset from high-band envelopes and reports it. Measured 2026-11-17
against Live 12.4.16 in one set with several sample-library instruments: +64.6 ms
at Main and -7 ms on a single track, so calibrate at the point you record.

## Compare and improve

`compare_audio` requires explicit reference and candidate start seconds and equal
channel counts. It reports candidate resampling, bounded constant delay alignment,
level, band-power differences, chroma, envelope and stereo correlation. It does
not time-warp a candidate to hide tempo errors. Silent/constant audio cannot supply
a delay estimate. Gain compensation affects comparison only; an optional residual
WAV preserves the actual difference.

A group rebuilt from several parts is compared with the sum of the stems it
replaces: pass the first stem as `reference_path` and the others as
`extra_reference_paths`. The mean of n stems sits about 20*log10(n) dB below their
sum, and a level comparison against one stem or the mean misreads the group.
`compare_spectrum` takes the same argument.

Gain-aligned waveform error is phase sensitive, not a perceptual quality or
leakage score. Use the reported differences to choose a concrete adjustment, then
record again. `find_sound`, `match_sound`, reviewed MIDI and Live's actual devices
help construct candidates; these tools do not automatically recover a full
orchestration, exact synth patch or original reverb/sidechain settings.

## Beat-aligned difference

`compare_spectrum` places reference and candidate on one beat grid, each through
its own tempo map: reference anchors from any source (a MIDI tempo map, beat
tracking or reviewed hand anchors), candidate anchors from a capture job's host
beats or supplied with a file. Rows are 1/12-octave bands or semitones. Semitone
rows come from a harmonic sieve, so a missing or extra note appears under its
own name; an octave doubling folds into the lower note.

Level is not the question: a fader or automation fixes it later, the sound has to
match. Each section (a bar by default, or chosen beats such as one note, or
reference-file seconds) is therefore first matched on K-weighted loudness
(BS.1770 filters), counted only over 100 ms blocks where both files sound, so a
pause or a half-silent candidate does not skew it. Per section the report gives
`sound_difference_db`, the mean absolute difference at equal loudness and the
convergence measure, and `level_difference_db`, the fader or automation move.
True peak is reported beside them but not matched: distorted basses scatter it.

The report ranks regions of missing (positive dB) and excess (negative dB)
energy by area, and lists attack positions and half-height widths from the 1.5-8 kHz
envelope. It also estimates the remaining offset from those attacks. A plugin
that reports no latency shows up there; Track Delay is not in the Object Model,
so the correction is a request to the user, or `apply_estimated_offset` for the
comparison alone. The three-panel image needs the `plot` extra and is meant for
an agent that reads images; the lists carry the same findings.

Repeat render, capture and compare until no region exceeds the chosen threshold
and the sound difference per section stops falling.

Read `live://skill/live-rebuild-reference` for the complete working procedure and
`live://skill/live-rebuild-drums` for known isolated kick/snare parts.
