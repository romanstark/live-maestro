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

Timeline anchors share the audio's frame positions, but host fields are scheduler
sampled. Observed update spacing is not an accuracy bound and does not prove a
sample-accurate song origin. Use that witness and explicit excerpt offsets for
comparison. A capture is not an offline export of stems or an automatic inverse
of the original production chain.

## Compare and improve

`compare_audio` requires explicit reference and candidate start seconds and equal
channel counts. It reports candidate resampling, bounded constant delay alignment,
level, band-power differences, chroma, envelope and stereo correlation. It does
not time-warp a candidate to hide tempo errors. Silent/constant audio cannot supply
a delay estimate. Gain compensation affects comparison only; an optional residual
WAV preserves the actual difference.

Gain-aligned waveform error is phase sensitive, not a perceptual quality or
leakage score. Use the reported differences to choose a concrete adjustment, then
record again. `find_sound`, `match_sound`, reviewed MIDI and Live's actual devices
help construct candidates; these tools do not automatically recover a full
orchestration, exact synth patch or original reverb/sidechain settings.

Read `live://skill/live-rebuild-reference` for the complete working procedure and
`live://skill/live-rebuild-drums` for known isolated kick/snare parts.
