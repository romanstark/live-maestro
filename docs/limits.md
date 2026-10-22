# API Limits and Constraints: Live Maestro

This reference describes API boundaries, required Live state and transport behavior.
Catalog rows retain their own measurement provenance. Native Arrangement and sample
workflows are documented in [Arrangement timing](timing-workflows.md).

## 1. Hard limits: no workaround exists

### Audio rendering and exporting (LOM)
The supported Object Model surface provides no audio render or export operation.
Use Live's File -> Export Audio/Video dialog.

### Saving the project (LOM)
Save and Save As use Live's GUI. Offline `.als` tools read the last saved state.
`get_set_state` reports the file path and transport; its `dirty` value is unknown.

### Auditory judgment (Physical)
Stored parameters and measured audio levels do not establish musical quality,
balance, tuning or a suitable sound. Audition the result.

## 2. LOM limits: Live could lift these, we cannot

### Device reordering
`Song.move_device(device, target_chain, position)` moves devices through object
references such as `{"__path__": "..."}`. Track reordering is a separate operation.

### Track reordering (LOM)
There is no supported track-move method. Use Live's GUI. Rebuilding a track at
another index preserves only the content explicitly recreated.

### Track grouping (LOM)
`group_track` and `is_grouped` are read-only. Group tracks in Live's GUI.

### Sidechain routing configuration
Set `device.input_routing_type` and `device.input_routing_channel` with references
from their available collections. Enable the device's S/C parameter separately.
Use `describe(with_parameters=True)` to find parameter indices.

### The Session / Arrangement boundary (LOM)
`arrange` duplicates Session clips onto the timeline. Direct alternatives are
`import_arrangement_audio` for an empty Arrangement audio track and
`write_arrangement_notes` for an existing Arrangement MIDI clip.

Generic JSON calls do not construct Live note or WarpMarker value objects.
The specialized tools use typed Remote Script handlers. `lom_set` and `lom_batch`
still refuse arbitrary Arrangement `end_marker` writes; the audio-import
workflow manages markers as part of its checked file-placement operation.

### Track automation vs. clip envelopes (LOM)
The live clip-envelope writer targets Session clips. Duplicate those clips to
Arrangement with `arrange`; general live Arrangement track-automation writing is
not provided. Saved-set tempo automation has a dedicated copy workflow:
`write_tempo_map`, followed by opening, saving and `verify_tempo_map`.

Clip envelopes can affect the value played over track automation. Removing an
envelope can expose the underlying track curve rather than restore a static value.

### A clip envelope leaves its last value behind (LOM)
Stopping an automated clip does not restore the parameter's earlier static value.
Capture mixer values before playback if they must be restored afterward.
`read_automation` samples the curve, not the static value underneath it.

### Sparse automation layers and interpolation behavior (LOM)
`write_automation` converts the requested interpolation into `Envelope.insert_step`
calls at the selected resolution. `hold` preserves discrete values; other modes
densify the curve. The writer explicitly extends the final value to the clip end.
Behavior inside gaps between arbitrary raw steps remains unmeasured.

### Note ids across two clips: an untested rule (LOM)
Use note IDs from the target clip when calling ID-based note methods. Whether
IDs are globally unique or how Live handles foreign IDs is unverified.

### song.current_song_time write boundary vs. Session playback (LOM)
Writes are bounded by `song.song_length`. Session playback can advance past that
boundary, so an observed playback position may not be a valid navigation target.

### song.back_to_arranger is writable asynchronously (LOM)
Writing `False` hands control back to Arrangement. It may require another read
after Live's main thread applies it. Writing `True` is not a verified way to
restore Session control; Live raises the flag when Session clips take over.

### A time signature denominator that is not a power of two is accepted and does nothing (Live)
Use 1, 2, 4, 8 or 16. A different denominator can receive an `ok` response without
changing the signature. Read back the actual value.

### Writes that open a modal dialog (Live)
A modal dialog can block the Remote Script until it is dismissed in Live.
Legacy `Clip.set_notes` and `Clip.remove_notes` calls are refused. Generic Warp
off/on restoration can open a timing dialog; the probe script treats those rows
as read-only. Prefer the typed Warp workflow and verify the returned marker state.

### A name that reads back is not a name you can write (Live)
Live adds return-track and rack-return prefixes to displayed names. Writing the
complete displayed name can duplicate the prefix. Strip it before renaming and
read the result back.

### A Max for Live device cannot read its own canonical path (Max)
The analyzer resolves its parent track's name and object ID, not a positional
track path. Channel A maps an index or `master` to a name. Duplicate track names
can produce ambiguous analyzer matches.

### The main track is called two different things in the file (Live)
Live 11 uses `MasterTrack`; Live 12 uses `MainTrack`. Offline readers support
both. Use `als_read` location results rather than a version-specific XPath guessed
by hand; preview XPath writes before committing them.

### A clip's envelopes can be listed, its breakpoints cannot (LOM)
Envelope collections identify automated parameters. `read_automation` samples
their values at a chosen resolution, so it can miss narrow extrema. Exact saved
breakpoints require the offline file reader.

### The peak outlets of Max's `loudness~` (Max)
The analyzer calibrates sample peak with its same-call generated reference.
True peak is reported separately without subtracting the reference's peak hold.
A reset clears accumulated loudness and peaks; allow audio to accumulate before
expecting a useful measurement.

### Loading a device inside a rack (LOM)
Selecting an arbitrary rack chain does not reliably target `Browser.load_item`.
Use a populated rack preset or Live's GUI for general nested-device loading.
`load_drum_pad` provides a specialized route for an empty pad or an existing
single-Simpler pad. It does not provide arbitrary rack-chain editing.

### Encrypted factory samples and `abli` multi-sample containers (Live)
Ableton Factory Pack samples with `COMM` compression `able` (`Ableton Content`)
stay closed: Live Maestro reads their header but never their audio, and
`convert_to_pcm_wav` refuses them. Their sound is reached through the browser
preview audio. Files that carry an `abli` index chunk hold many zones of a
multi-sampled instrument in one container. `SampleHeader.is_multisample_container`
identifies these files; extracting individual zones is not implemented. Sound
matching uses browser preview audio rather than decoding the sample payload.

## 3. Live's GUI gates: the user can lift these, we cannot

### Third-party VST parameter exposure (Live)
Parameter exposure varies by plugin and instance. Inspect `parameter_count` and
`get_parameter_names`; a single exposed parameter can indicate an unconfigured
strip. Use Configure in Live to add controls.

Offline plugin configuration can fill existing parameter-map slots in a closed
saved set through the supported `als_write` operation. Backups and reopen/read-back
checks remain necessary.

### VST2 display units (Live)
VST2 display strings may not provide physical units. Treat internal values as
normalized unless the device returns a usable display mapping.

### Asking for a display string at an endpoint (Live)
Third-party native formatting code can crash Live at exact parameter endpoints.
`set_parameter_display` samples inside the range. Generic endpoint formatting
calls do not have that protection.

### Normalized value vs. physical display value (Live)
`min`, `max` and `value` describe the internal scale, which may differ from Hz,
dB or other displayed units. Read `display` or use `set_parameter_display` rather
than treating every stored number as a physical value.

## 4. Protocol limits: ours, and therefore fixable

### Non-transactional batch operations (Protocol)
`lom_batch` executes sequentially. `atomic: true` stops at the first error;
earlier successful operations remain in effect. It provides no rollback.

### Batch size limit (Protocol)
A call accepts at most 1,000 operations (`max_ops=1000`). Larger requests return
`batch_too_large` before dispatch. Split larger jobs and inspect receipts; read-batch performance does not
establish the same performance for writes.

### Write timeouts and retry policy
Timed-out writes are not automatically repeated. They may already have succeeded.
Read state before deciding what to send next. Arrangement note writes identify
acknowledged chunks and the ambiguous in-flight range.

### Round-trip cost
Each request has scheduling and socket overhead. Use bounded batches for
independent operations; latency depends on the host, set and operation.

### Strictly serial transport
Each connection processes one request at a time without multiplexing.

### A browser uri survives a restart, and needs its root to resolve (Protocol)
Cache a browser URI with its root and verify it after library changes. Positional
item paths can change when content or browser filters change.
An incomplete search does not prove an item is absent; inspect `status`,
truncation and unreached roots.

## 5. Open questions: unverified, and how to settle them

### Warp markers
Typed `set_warp_markers` constructs and verifies new marker lists. Generic
`lom_call` cannot construct those objects. Live retains a final shadow marker,
and removing the last real marker can leave it unchanged; inspect marker read-back
rather than treating an accepted call as proof of removal.

### `end_marker` on an Arrangement clip: ineffective, or ineffective only while the clip does not loop?
A stored marker value does not establish changed Arrangement geometry. Generic
writes remain refused. The specialized unwarped import resets seconds-valued
markers and checks duration. Arbitrary marker editing across loop states remains
unverified.

### Concurrent Control Surfaces
Use different ports for different socket-based scripts. Compatibility and load
with multiple simultaneous socket Control Surfaces are not measured.

### Cost of introspection and of listeners
Many listeners and deep inspection consume work inside Live's process. The event
buffer is bounded, but high listener counts are not a real-time performance
guarantee. Prefer bounded inspection and observe CPU/dropouts in Live.

### Did Live 12.4.6 move the object model?
A point-version comparison on different sets is not complete catalog
re-verification. Use the specimen-set procedure in [upgrading Live](upgrading_live.md)
when extending compatibility to a new version.

### Does a long run of load and delete cycles wear Live down?
The recorded Utility stress check covers a limited empty-set workload. It does
not establish performance for long instrument/plugin sweeps or arbitrary sets.
Benchmark the actual workload before depending on sustained throughput.

## 6. What a success result actually proves

A result can confirm dispatch, stored state or a particular read-back condition.
It does not establish audible effect, persistence to disk, atomicity or protection
against a later user edit. Inspect `verified`, `read_back`, partial results and
the relevant workflow's evidence separately.

## 7. Properties that do not exist, and are therefore not in the catalog

| Property | Supported alternative or boundary |
| --- | --- |
| `master.arm`, `return.arm` | Main and Return tracks do not expose arming/monitoring modes. |
| `clip.follow_action_*` | Configure follow actions through Live's GUI. |
| `device.presets` | Load presets through the Browser. |
| `sampler.sample`, `sampler.playback_mode` | These are Simpler properties, not MultiSampler properties. |
| `song.average_process_usage`, `song.peak_process_usage` | Read CPU meters in Live's GUI. |
| `RackDevice.rename_macro`, `RackDevice.macro_map` | Rename and map macros in Live's GUI. |

`device.view.is_showing_chain_devices` is available and has a catalog row.

## 8. Preconditions are part of the question

Use the concrete class and state: `choke_group` belongs to DrumChain, Warp
properties to audio clips, and drum pads to DrumGroupDevice.

### `clip.length` reports the loop, not the material
On a looping clip, length follows the loop brace. Cropping can discard notes
without changing that length. Verify note content/count when checking a crop.

### An optional argument omitted is not an optional argument passed as null
The C++ binding distinguishes omission from explicit null. The executor omits
trailing optional arguments without a catalog default instead of padding them
with null.

### Track index renumbering
Inserting, deleting or grouping tracks shifts positional indices. Refresh
`get_session` and resolve destinations again after structural changes.

## 9. Where the corpus figures come from

LOM measurement records describe a specific Live version and object state.
Historical corpus statistics describe a private project collection and cannot
be reproduced from this repository. They are context, not capability evidence.
Prefer current-set inspection when choosing musical parameters or deciding
whether an API operation is supported.
