---
name: live-rebuild-drums
description: Rebuild isolated kick/snare recordings as editable MIDI in Live. Use UVR separation when available, inspect timing, analyze drums and optionally extract one-shots.
---

# Rebuild drums from a recording

Check the available servers. UVR Maestro supplies separated files and source
timeline information; Independent Audio Maestro analyzes known kick/snare files;
Live Maestro places reviewed notes and samples. Each server also works independently. Read the actual tool schema.

For an uncertain model choice, separate a short excerpt first. Preserve its
source_offset_seconds from the succeeded UVR result. Use the same offset in each
stem record passed to an independent analysis producer. Also transfer
source_sample_rate when UVR
resampled the output, so source sample positions use the original clock.
A mixed drum stem is not an isolated kick or
snare, and the analyzer does not identify instruments in a kit mixture.

Choose known tempo_bpm or reviewed beat_anchors when available. Automatically
inferred maps require review, including half/double-time ambiguity and grid origin.
Calibration subtracts known detector lateness; it does not establish the physical
attack of every recording. Inspect uncertainty and a few detections before a full
run. Pitched transcription is not mixed-kit drum classification.

Optional Audio Maestro owns analyze_drums and returns a queued job.
Poll that server's get_audio_job and use its cancel_audio_job
when requested. Only succeeded provides verified JSON, MIDI and optional one-shot
files. Without the optional audio runtime, explain the reported dependency error.

Use write_drum_clip with confirm=False to inspect target and notes. Create a long
enough MIDI clip, choose the intended kit's MIDI pitches, and execute within the
owner's authorized scope. This replaces clip notes; existing content matters.
Confirm that Live's actual tempo map matches the analysis map. apply_tempo_map can
set a constant tempo. Changing maps can use tempo-map.mid imported in Arrangement,
with Live's tempo-data prompt accepted, or write_tempo_map with a reviewed list in
a new saved-set copy. Open/save that copy and use verify_tempo_map. Do not claim that
writing MIDI notes applied song tempo automation. write_arrangement_notes writes
existing Arrangement MIDI clips directly in checked chunks; inspect receipts after
a disconnect before deciding whether to resend anything. convert_clip_time uses
actual Warp markers or the saved current set's tempo to place source events.

One-shots report source position, attack offset, overlapping hits and optional
reference residual. Reference-free contamination remains unknown. Audition the
sample, then use load_drum_pad with its file path, pad MIDI note and attack_sample.
Empty pads require a saved project; existing layered pads are refused. load_sample
can replace a known existing Simpler directly. import_arrangement_audio adds a file
to an empty Arrangement audio track with explicit beat and Warp state. Prefer
set_warp_markers for checked source-seconds/beat lists over generic lom_call values.
Do not claim audibility from stored parameters or automatic grid accuracy from an
unreviewed tempo estimate.
