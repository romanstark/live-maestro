---
name: live-rebuild-reference
description: Rebuild a reference recording or supplied stems as editable parts in Ableton Live, using source analysis, reviewed timing, transcription, sound candidates and recorded comparisons.
---

# Rebuild a reference in Live

Start with a short representative section and the owner's intended parts. Prefer
supplied stems when available. Any separator or DAW export is usable; UVR Maestro
is optional. Preserve each file's source offset and rate instead of assuming all
stems start at zero. Keep the original recording as a comparison reference.

Use externally produced source analysis and notes, optionally from independent Audio Maestro. That server owns analyze_reference_audio and transcribe_audio (Basic Pitch or optional SheetSage). Live does not provision or invoke models. Review its estimates and retain source offsets; use the producing server's job tools for analysis, Live's for capture/comparison.

Establish a reviewed source-seconds/quarter-beat map before placing notes.
Periodicity alone does not establish tempo, meter, downbeats or structural labels.
For an existing clip, convert_clip_time accounts for its real Warp markers or the
saved set's tempo map. Tempo in the source file and tempo automation in Live are
different clocks. If using a saved set, ensure it reflects the open set.

Review externally estimated pitch, attacks and durations before importing MIDI or passing beat-mapped notes to write_arrangement_notes. Reverberant parts can produce false notes. Known kick/snare analysis can come from Audio Maestro or any compatible producer; Live accepts its JSON with write_drum_clip. Neither pitched transcription nor isolated-source onset analysis classifies every instrument in a mixed kit. Live does not recover exact orchestration, patches or original effects chains.

Use find_sound or match_sound to choose candidates, explaining whether a ranking
uses library names or audio features. Audition/render candidates through Live.
capture_audio records the real signal at the Analyzer, including MIDI instruments
and effects. Put the Analyzer at the intended point; Main is before its fader.
from_beat requires a stopped transport and temporarily changes its start marker
and playhead. Capture includes pre-roll. Read the recorded timing witness rather
than treating the first file sample as the requested song beat. Host timing is
scheduler sampled, not a sample-accurate song-origin guarantee. Wait for
cleanup_pending=false after cancelling a capture before starting another.

Compare explicit source and capture ranges with compare_audio. It reports
bounded delay, level, spectral, chroma and envelope differences; gain-aligned
waveform error is phase sensitive and is not a perceptual or leakage score.

Refine in a loop until the reference is reproduced:

1. Fix the reference tempo map (source seconds against beats, from any source).
   Calibrate the capture chain once per set: capture a known file and read
   compare_audio's candidate_delay_ms.
2. Get notes: a lead sheet first (Audio Maestro's transcribe_audio, with
   align_to_audio against track_voice), a kit pattern with analyze_drum_kit.
3. Before each capture, check every track's solo state and restore it after.
4. Render with capture_audio, then compare_spectrum with chain_offset_ms. Use
   semitones for notes and bands for timbre. Sections are loudness matched:
   judge sound_difference_db, and take level_difference_db as a later fader
   or automation move. Read regions and attack pairs; the image is optional.
5. If estimated_offset_ms is large, the plugin hides latency: ask the user to
   set the opposite Track Delay, since it is not in the Object Model.
6. Change one cause: notes, sound or effect. For a synthesizer, measure with
   describe_sound. Live's Wavetable needs no reload: set its knobs with
   set_parameter and route LFOs and envelopes with wavetable_matrix. A plugin
   gets a preset from instrument_preset (Vital is one adapter); the user loads
   it, read_als confirms what the plugin holds.
7. Capture again. Stop when no region exceeds the threshold and the sound
   difference per section stops falling. Retain the evidence; save or publish only when
   requested.
