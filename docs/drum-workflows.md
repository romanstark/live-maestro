# Drum reconstruction from files

UVR Maestro separates sources. Independent Audio Maestro detects attacks in identified kick/snare files and exports `maestro.drum-events` JSON, MIDI and optional one-shots. Live Maestro owns note writing, device/sample loading, tempo application and placement. Any compatible producer can supply these files; no Maestro package imports another.

Preserve each file's original-source offset and rate. Use the analysis server's job tools for analysis; Live's job tools manage capture/comparison only. Attack calibration and relative hit velocity are estimates, not a universal physical-attack guarantee. Pitched transcription does not identify a mixed kit.

## Tempo, MIDI and Live

Supply known constant `tempo_bpm` or ordered `beat_anchors` mapping source seconds
to quarter-note positions. Piecewise linear interpolation handles changing tempo.
Without either, autocorrelation proposes tempos in 60-180 BPM, including explicit
periodicity candidates and `review_required`. This estimate does not establish
meter, downbeat phase, or resolve half/double-time ambiguity.

`analysis.json` has schema `maestro.drum-events`, version 1. `drums.mid` is SMF
type 0, channel 10, 960 PPQ with piecewise tempo events. `tempo-map.mid` carries
the same tempo map and a spanning carrier note for Live's Arrangement import.

`write_drum_clip(confirm=False)` previews notes and destination without contacting
Live. With confirmation it replaces an existing Session MIDI clip using the
existing verified note-write/read-back path. Create a sufficiently long clip
first. `clip_origin_beat` selects which source beat corresponds to clip zero.
Kit-specific note numbers are supplied in each stem's `midi_pitch`.

Writing notes does not apply song tempo automation. `apply_tempo_map` previews or
sets a constant tempo through `set_tempo`. For changing maps, import
`tempo-map.mid` into a muted Arrangement MIDI track and accept Live's tempo-data
prompt. This is the [documented Ableton import route](https://help.ableton.com/hc/en-us/articles/360003387979-Importing-a-tempo-map).
The Remote Script cannot accept that GUI prompt. For a reviewed beat/BPM/step-or-ramp
list, write_tempo_map also creates a verified new .als copy; open and save that copy,
then use verify_tempo_map. See [Arrangement timing](timing-workflows.md).

## One-shots and contamination

`cut_one_shots=True` writes PCM_24 WAV hits with explicit pre-roll and tail.
Results identify source position, local attack position and overlapping hits.
Tails are not shortened silently when another hit falls within the selected
window. Prefer non-overlapping hits when building a clean kit.

An optional aligned reference must have identical rate, channels and frames.
Its residual RMS measures a difference, which can also include gain/timing/model
differences. Without a reference, contamination remains unavailable. Simultaneous
energy in another supplied stem is reported separately and never labelled measured
leakage. Audition before assigning samples with load_drum_pad; pass the one-shot's
attack_sample to trim pre-roll from playback. load_sample addresses an existing
Simpler directly. Both verify file bytes, attack marker and Warp state.

## Scope and compatibility

The file worker runs independently of Live and of a separator. Its output contract
covers source offsets, reviewed beat anchors, MIDI tempo events, detector
calibration, one-shot bounds, reference residuals and cancellation.

Audio decoding uses macOS afconvert, FFmpeg, or the optional SoundFile/SciPy
runtime. Encrypted factory samples remain unreadable; their ordinary browser
previews can be indexed. Native DAW placement is described in
[Arrangement timing](timing-workflows.md). Mixed-kit transcription, inferred
downbeats and automatic auditory quality scores are outside this workflow.
