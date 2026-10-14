# Arrangement timing and sample workflows

These tools manage Arrangement timing, MIDI notes and local samples. Native
compatibility covers Live 12.4.6 on Windows and macOS. They work independently of
UVR Maestro and exchange ordinary audio, MIDI and JSON files with other producers.

## Tempo automation in a copy

`read_tempo_map(path)` reads the persisted MainTrack/MasterTrack tempo envelope by
its actual AutomationTarget ID. `write_tempo_map` accepts ordered quarter-beat/BPM
events starting at beat zero. `shape` describes the segment toward the next event:
`step` holds its BPM, `ramp` interpolates BPM linearly in beats. The final shape is
normalized to `step`. Existing destinations and the source itself are refused.
`confirm=False` previews the copy. Other XML nodes and source bytes are preserved.

```json
[
  {"beat": 0, "bpm": 120, "shape": "ramp"},
  {"beat": 8, "bpm": 180, "shape": "step"},
  {"beat": 16, "bpm": 90, "shape": "step"}
]
```

Open the new copy in Live, save it, then call `verify_tempo_map`
with a full, unwarped, non-looping reference clip. It compares the clip's musical
extent with the integral of persisted BPM using a 1 ms tolerance. This verifies
Live's timing arithmetic,
not a sample-exact audio render. `song.tempo` can evaluate ahead of a stopped
playhead on ramps and is not used as an exact point sample.

The reader refuses curved automation instead of interpreting it as a linear ramp.
`get_set_state` reports the open file path and transport. `dirty` is unknown: the
measured Python Song API provides no dirty flag or verified Save/Save As operation.
Save through Live's GUI before relying on persisted tempo. Creating whole tracks
in an offline set, CRC reconstruction and rendering remain outside these tools.

## Warp and audio placement

`set_warp_markers(path, markers, replace=True, confirm=True)` accepts source seconds
and clip-local quarter beats on Session or Arrangement audio clips. Points must
start at `(0, 0)`, increase on both axes and fit the source. The Remote Script
handler constructs actual `Live.Clip.WarpMarker` values before enabling Warp or
removing markers. It reads back requested points, unexpected real markers and
Live's retained final shadow marker. The generic `lom_call` dictionary route still
cannot construct these values. Install the matching Remote Script and restart Live
after changing it.

`import_arrangement_audio` accepts file, track, song beat and Warp state in one call,
using native `Track.create_audio_clip`. It currently requires an empty Arrangement
audio track, preventing overlap replacement. Disabling Warp resets loop/start/end
markers in seconds, avoiding Live's retained beat-valued end-marker trap.
File bytes, position, Warp and full unwarped source boundaries are verified separately
from persisted-tempo duration. Cached start/loop markers are reset to preserve the
beginning. A rejected marker write reports a partial import instead of success.

`convert_clip_time` converts source seconds to song beats and back using the actual
Warp map, clip origin and start marker. Unwarped clips require the saved set currently
open in Live and integrate its step/ramp tempo. Looped clips are refused because one
source time can occur repeatedly. End segments extrapolate linearly. Excerpt times
are file-local: apply a separator's source offset when translating events back to
the original recording's clock.

## Arrangement notes and pads

`write_arrangement_notes` validates the complete list, writes chunks of 128 notes
directly into an existing Arrangement MIDI clip and reports each acknowledged chunk.
A disconnect reports acknowledged chunks and the ambiguous in-flight range; it
never automatically resends a write. Writes are not transactions or rollbacks.
Replacement verifies all notes; append reports the read-back count without claiming
comparison with the previous content.

`load_sample` replaces an existing track or Drum Rack pad-chain Simpler by file path
through `SimplerDevice.replace_sample`. It verifies SHA-256, source-frame attack
marker and Warp state. A nonzero attack offset requires a PCM WAV and is checked
before replacement. `load_drum_pad` also creates an empty pad's Simpler using an
actual indexed browser item. Empty pads require a saved Live project. External
samples are copied under `Samples/Imported/Maestro` with a hash-derived name and
no overwrite. Live indexes new files asynchronously; an unindexed or ambiguous
sample is refused, and the call can be repeated after indexing completes.
The browser is queried after pad targeting because targeting changes
its filters and invalidates old item paths. The hotswap target is cleared afterward.

Pad MIDI note, nonwarped sample, attack marker and one-shot playback mode are read
back. Complex existing layers are refused. Loading leaves the selected track/pad at
the destination. These checks prove stored state; audition determines the sound.

## Compatibility and boundaries

Native compatibility covers direct Arrangement audio import, chunked MIDI writes,
typed WarpMarker replacement, reopened/resaved step and ramp tempo maps, and
Simpler/Drum Rack sample loading on Live 12.4.6 for Windows and macOS.

File-job and MCP package checks are independent of native DAW checks. Stored
clip geometry and sample state do not establish sample-exact audio rendering,
a detected musical grid, auditory quality or mixed-kit transcription. Beat maps
must be reviewed before they are applied to a song.
