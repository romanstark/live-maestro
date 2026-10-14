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
Check one concrete mismatch, change the relevant sound/notes/effect within the
owner's scope, then capture again. Retain the measured evidence and uncertainty
needed to review the result. Save or publish only when requested.
