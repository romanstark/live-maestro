# Drum reconstruction from files

UVR Maestro owns model management, source separation, restoration and verified
audio export. Live Maestro owns musical analysis, source-time to beat conversion,
MIDI and DAW placement. Neither server needs the other installed. The shipped
`live-rebuild-drums` skill joins their public tools when both are available.

## Optional analysis runtime

Install `live-maestro[audio]` for NumPy, SciPy and SoundFile, or point
`LIVE_MAESTRO_AUDIO_PYTHON` at an already provisioned interpreter. Only the audio
worker imports these packages. The base control server remains usable without
them. `LIVE_MAESTRO_AUDIO_DATA_DIR` overrides the independent job-record directory.
There are no automatic model downloads or audio uploads.

`analyze_drums` accepts identified isolated kick/snare files, not a kit mixture.
It detects positive energy rises, refines the first crossing of ten percent of
the local peak, and subtracts caller-supplied detector lateness in milliseconds.
The reported sample resolution is not a universal physical-attack accuracy
guarantee. Velocity is a relative square-root mapping of hit peak to MIDI 1-127,
not a calibrated measurement of how hard the drummer struck.

Pass each stem's `source_offset_seconds` from UVR's succeeded result. When UVR
resampled, also pass its original `source_sample_rate`. Events include file-local
samples, absolute source seconds and original-rate source sample positions.
These values share one time axis with the beat anchors and one-shot offsets.

The job returns immediately. Poll `get_audio_job`; `cancel_audio_job` stops only
its worker/process tree. Jobs are serialized and persist terminal evidence.
After restart, unfinished records read as interrupted. Succeeded jobs include
checked JSON/MIDI/audio paths, SHA-256 fingerprints and a manifest.

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
