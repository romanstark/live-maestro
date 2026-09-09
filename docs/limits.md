# API Limits and Constraints: Live Maestro

This document details known limitations across Live's Object Model, the graphical user interface, and the Maestro socket protocol.

Categories of limitations:
- **LOM:** Not exposed or permitted by Live's Python API.
- **Live GUI:** Available in Live, but gated behind GUI interactions (e.g., plugin parameter configuration).
- **Protocol:** Limitations inherent to our socket transport or serialization format.
- **Physical / Contextual:** Fundamental differences between data inspection and human auditory judgment.

---

## 1. Hard limits: no workaround exists

### Audio rendering and exporting (LOM)
Ableton Live's Object Model does not expose functions for rendering, bouncing, or exporting audio. Audio export must be performed directly in Live's interface via **File -> Export Audio/Video**.

### Saving the project (LOM)
The LOM does not provide an API method to save the open set. Project files on disk (`.als`) represent the state from the last manual save (Ctrl+S / Cmd+S). When reading from disk via Channel B (`als/read.py`), data reflects the saved state rather than unsaved in-memory changes.

### Auditory judgment (Physical)
The MCP server can read parameter values and verify stored numbers (e.g. confirming a cutoff is set to 800 Hz), but cannot evaluate musical aesthetic quality, vocal tuning, or balance.

---

## 2. LOM limits: Live could lift these, we cannot

### Device reordering
This reads as a missing call because it sits on the `Song` rather than on the `Device`. Device positions in chains are adjusted with `Song.move_device(device, target_chain, position)`, which takes the device and the target chain as object references (`{"__path__": ...}`) rather than indices. Call verified 2026-08-30 against Live 12.4.5: a five-device chain went from `['Eq8', 'Compressor2', ...]` to `['Compressor2', 'Eq8', ...]`.

### Track reordering (LOM)
There is no equivalent for tracks. Neither `Track` nor `Song` offers a method for moving one, so a track's position is fixed once it is created. Reordering means creating a track at the target position, loading the instrument again, rebuilding the clips and notes, restoring the mixer settings and deleting the original. Anything not explicitly recreated is lost, and every cached track index shifts.

### Track grouping (LOM)
`group_track` and `is_grouped` are read-only properties in the LOM. Tracks must be grouped manually in Live (Ctrl+G / Cmd+G). Grouping inserts a new container track, which shifts the numerical index of subsequent tracks.

### Sidechain routing configuration
Sidechain routing is configured directly on the receiving device rather than at the track level:
- `device.input_routing_type`: Selects the source track from available routing types.
- `device.input_routing_channel`: Selects the routing tap point (`Pre FX`, `Post FX`, `Post Mixer`).
- The `S/C On` parameter arms it. Parameters are addressed by integer index, so the path is `device.parameters[N]`. A quoted name is not a legal path. Use `describe` with `with_parameters=True` to find N.

Routing types must be assigned by object reference (using `{"__path__": "<path>"}`) rather than by string name.

### Track automation vs. clip envelopes (LOM)
Arrangement track automation envelopes are not directly writable via the live LOM API (`clip.automation_envelope()` returns `None` for arrangement clips). Automation must be written into Session clips first and then duplicated to the Arrangement timeline via `Track.duplicate_clip_to_arrangement()`.

When both layers target the same parameter over the same stretch, the **clip envelope is what plays**. Measured 2026-09-01 against Live 12.4.5 on a 51-track arrangement: sampling a filter cutoff during playback returned the clip envelope's value wherever one existed (573 Hz, then 402, 335 and 199 Hz across a later section) and the track automation's value only where none did (10.0 kHz). Read this before deleting either layer: the only one reachable from here is the clip envelope, which is the audible one. Removing it does not tidy a redundant curve. It uncovers the track automation underneath, which in that measured case would have opened a drone from around 200 Hz to 10 kHz.

### A clip envelope leaves its last value behind (LOM)
A Session clip envelope drives its parameter while the clip plays and lets go wherever it stopped. Live keeps no static value underneath to restore, nothing in the Object Model reports the value the envelope covered, and stopping the transport puts nothing back. A mixer that reads wrong after a playthrough is usually this.

Measured 2026-09-08 against Live 12.4.5: firing one scene and stopping left three track volumes at -5.00, -1.72 and +1.50 dB rather than at the 0.0 dB they were set to, and a second pass that stopped at a different point in the same envelope left one of them at -0.72 dB. The parked value is wherever the curve happened to be, so it differs from one playthrough to the next and no two runs leave the same mixer.

The defence is to record the statics before the first playthrough and write them back afterwards. `read_automation` samples the curve, never the value beneath it, so a comparison against what you set is the only way to see the damage.

---

### Writes that open a modal dialog (Live)
A modal dialog owns Live's main thread. While one is on screen the Remote Script answers nothing, every request times out after 20 seconds, the connection ends in `CloseWait`, and only a person clicking the dialog away restores service. No timeout, retry or reconnect helps, because the process is not wedged: it is waiting for a human.

Two families of write do this, and only the first was known.

The legacy note API (`Clip.set_notes`, `Clip.remove_notes`) is refused inside the Remote Script for exactly this reason, so it cannot be reached from here.

Warping is the second, and nothing guarded it. Measured 2026-09-06 against Live 12.4.5 on an Arrangement audio clip carrying two warp markers: writing `warping` false went through and answered normally. Writing `warping` true afterwards, to put the clip back, opened *Keep the current clip timing?*, which offers to hold the new timing or return to the previous warp markers. Live stopped answering until the dialog was dismissed. The clip came back unharmed on *No*.

The direction is what matters. One write of `warping` is safe. Turning warping off and on again is what makes Live ask a question, so any caller that reads a value, writes another and puts the first one back is the pattern at risk. `scripts/probe_paths.py` does precisely that, which is how this was found, and it now lists both `clip.warping` and `arrangement_clip.warping` in `MODAL_RISK_ROWS` and reads them without writing.

A caller that needs to restore warping should expect a person to be at the machine.

---

### A name that reads back is not a name you can write (Live)
Live prefixes the name of a return track and of a rack's return chain with the letter it shows in the mixer, and the prefix is part of what a read returns. Writing that same string back adds the letter again.

Measured 2026-09-06 and 2026-09-07 against Live 12.4.5. `song.return_tracks[0].name` read `A-Plate Vocal`, writing it back stored `A-A-Plate Vocal`, and writing `Plate Vocal` stored `A-Plate Vocal` with `read_back` `clamped`. A return chain inside a Drum Rack behaves the same way one level down: writing `X` stores `a X`. A probe that restores by writing what it read grew one name to `a a a Channel EQ` across two runs.

Read the name, strip the letter, write the rest, and read the result back. `scripts/probe_paths.py` lists both rows in `READ_IS_NOT_A_WRITE` and reads them without writing.

---

### A Max for Live device cannot read its own canonical path (Max)
`live.path` resolves a path expression to an object and reports an id. It does not report the canonical path of what it resolved.

Measured 2026-09-07 against Live 12.4.5 with a device on the Main track. `path this_device canonical_parent` answers `id 2` and nothing else. `path this_device` answers the same way. `route path` matched on none of the three outlets, and the outlet that carries what route did not match stayed silent, so no outlet emits a path as a list either. Feeding the id to `live.object` and asking `get name` returns the track's name, which is the identity that is available.

A device therefore knows the name and the id of the track it sits on, and not its index. `analyze_audio` turns a caller's index or `master` into a name over Channel A, where the Remote Script can read the set, and matches on that. Two tracks may share a name, so the reply carries the id as well and the result says when more than one instance answered to the same name.

Order matters when asking. `live.thisdevice` fires one bang, and two patch cords from one outlet fire in an order Max does not define, so setting the path and asking for the name have to be sequenced through `trigger`, which fires right to left.

---

### The main track is called two different things in the file (Live)
Live 11 writes the main track into the `.als` as `MasterTrack`. Live 12 writes it as `MainTrack`. Both files otherwise place the tempo identically, under `DeviceChain/Mixer/Tempo/Manual`.

Measured 2026-09-07 on two saved projects: a Live 11 project (`11.0_11300`) and a Live 12 project (`12.0_12402`), both at 124 BPM. `.//MasterTrack/DeviceChain/Mixer/Tempo/Manual` selects one element in the first and **nothing** in the second, and `.//MainTrack/...` does the reverse.

An expression written down once is therefore right for one Live version and silently wrong for the other. It does not fail loudly: `als_write` refuses a write that matches nothing, which is correct and says nothing about the cause. `als_read` with `locate` derives the expression from the file in hand for that reason, and `als_write` with `confirm=False` resolves an expression against the file and reports how many elements it selects before anything is written.

The reader and the writer already carry both names (`als/read.py`, `als/write.py`), so parsing a project was never affected. What was affected is any expression built by hand.

---

### A clip's envelopes can be listed, its breakpoints cannot (LOM)
`Clip.has_envelopes` is one boolean for the whole clip and `Clip.automation_envelope(parameter)` answers for one parameter at a time, but `Clip.automation_envelopes` returns the lot, and each `Envelope` carries a `parameter` child that names itself. So "what is automated in this clip?" does have a direct answer.

Measured 2026-09-08 against Live 12.4.5. On an audio clip carrying two envelopes the collection returned two, naming `Track Volume` and `A-Reverb`; on a MIDI clip it returned one, naming the device parameter `Frequency`. A mixer control and a device knob are reported the same way. `Envelope` itself publishes no properties and its methods (`create_event`, `delete_events_in_range`, `events_in_range`, `insert_step`, `value_at_time`) are not on the Remote Script allowlist, so the collection answers which parameters are automated and nothing about the curves.

What stays out of reach is the breakpoints. `read_automation` samples a curve at a chosen resolution and reports the range, where the low and the high point sit, and how densely it looked. Those are samples: `als_read` is the only route to the breakpoints themselves, and it reads the saved file rather than the open set.

The sampling density is part of the answer for a reason. At 16 samples over a 32 beat clip, one every two beats, a V shaped dip written to 0.6 at beat 20 was reported as bottoming out at 0.655, understating the loss by about 2 dB, because the floor fell between two samples. The dry run now takes 128.

---

### The peak outlets of Max's `loudness~` (Max)
`loudness~` is the object Channel C measures loudness with. On a bang it reports six values: momentary, short term and integrated loudness, loudness range, sample peak and true peak. The two peak outlets do not agree, and one of them is wrong by a fixed factor.

Measured 2026-09-07 against Live 12.4.5 at 96 kHz, against a reference the device generates itself: a 1 kHz sine of amplitude 0.5, whose peak is therefore -6.0206 dBFS by construction. The sample peak outlet reports -12.0412 dBFS, which is `20*log10(0.25)`, so it reports half the amplitude. The true peak outlet reports -6.0206 dBFS, exact to four decimals.

The factor of two on the sample peak outlet held in every condition tried: transport stopped and running, two programme levels 12 dB apart, one channel silent, and two separate instances of the device. `analyze_audio` subtracts it, taking the figure the reference measures on the same call rather than a constant, and reports what it used under `calibration`.

The true peak outlet needs no correction and is given none. Its reference error reads 0.00 dB with the transport stopped and has been observed at 1.09 dB with it running, and that reading does not follow the programme: moving the measured track's fader by 12 dB moved the programme's true peak by 12.00 dB and left the reference error where it was. It is a transient a peak hold has latched, not an offset the outlet applies, and subtracting it would move a reading that does not carry it.

A reset clears everything the object holds. Straight after one, all six outlets read negative infinity, both peak holds included, so a call that resets reports no loudness and no peak at all and the call after it reports again.

There is no `ebur128~` in Max. The object list under `Resources/Max/resources/docs/refpages` in Live 12 Suite carries `loudness~` and `mc.loudness~` and nothing else matching loudness, LUFS or R128. Max draws an object name it does not have as a broken box, reports no error, and measures nothing.

---

### Loading a device inside a rack (LOM)
`Browser.load_item` loads onto `song.view.selected_track` and cannot be aimed at a chain from here. Writing `rack.view.selected_chain` appears to work: the value reads back as the chain that was asked for. The load still goes to the track.

Measured 2026-09-07 against Live 12.4.5 on an Instrument Rack with one chain. `selected_chain` read back as the chain named *Operator*. Loading a Hybrid Reverb afterwards left the chain at six devices and took the track from one device to two. Live's browser follows the selection in its own interface, and no write over this channel drives that.

A device goes into a rack chain by hand, or by loading a preset that already contains one. `.adg` files in the browser are device groups and arrive with their chains populated, which is how a rack with chains can be built without touching the interface.

The rack's own structure is a different question and it is reachable. `RackDevice.insert_chain` appends an empty chain: measured 2026-09-08 against Live 12.4.5 on a Drum Rack, it belongs to no drum pad, so no note triggers it and it stays silent whatever goes into it. `RackDevice.copy_pad` duplicates a populated pad onto another note and takes the chain with it, which is the way to a second playable pad. That row is destructive, and `probe_paths.py` refuses a destructive row without a person typing the row id at an interactive terminal, so it stands `untested` however often it answers.

---

## 3. Live's GUI gates: the user can lift these, we cannot

### Third-party VST parameter exposure (Live)
How much of a plugin the Object Model sees is decided by the plugin. Measured 2026-09-01 against Live 12.4.5, each loaded from the browser with no GUI interaction:

| Plugin | Format | `type` | Parameters on load |
|---|---|---|---|
| Vendor A, synth | VST3 | instrument | 1 |
| Vendor A, electric piano | VST3 | instrument | 1 |
| Vendor A, tape effect | VST3 | audio effect | 16 |
| Vendor B, synth | VST3 | instrument | 29 |
| Vendor C, lo-fi effect | VST2 | audio effect | 17 |

It is not the format, not the vendor (Vendor A appears at both 1 and 16) and not instrument versus effect. No rule has been established, so do not predict it: read `parameter_count`, which `describe` reports and also diagnoses. One parameter means this instance exposes nothing yet, not that plugins never do.

What a plugin *could* expose is readable: `lom_call(device, 'get_parameter_names')` returns the plugin's own list whatever the strip holds, up to the 512-entry list cap, past which the reply comes back with `truncated: true`. Expect noise in it (program-change, MPE and MIDI-CC helper entries can run into the hundreds) with the real controls at the head.

**Configure** mode adds a parameter to the strip: press Configure, click the control in the plugin's own GUI, leave the mode. Live allocates 128 slots per instance and stores the mapping in the set rather than in the plugin, so a second instance of the same plugin starts empty.

That mapping is **writable in the file**, which makes this the rare GUI gate that can be lifted. Measured 2026-09-01 against Live 12.4.5, on an instance whose strip held nothing: writing `ParameterName`, `ParameterId` (the index into the declared list) and `VisualIndex` into two empty slots of the saved set produced two working parameters on reopen, read back with their real values, writable through `lom_set` with `read_back: applied`, one of them reporting a unit. Nothing is inserted: Live leaves all 128 slots in the file, so this fills three fields of an element that is already there. The slot's placeholder value does not need writing. Live asks the plugin. See `als/write.py`, `configure_plugin_parameters`, which needs the set closed in Live and takes a backup.

### VST2 display units (Live)
`str_for_value()` does not return formatted physical units for VST2 plugins. Parameter values remain normalized floats between 0.0 and 1.0.

### Asking for a display string at an endpoint (Live)
`str_for_value()` is the only route to a physical reading, and it moves nothing, which makes it the safe way to ask a device what a value would look like. It is safe **except at the ends of the range**. Passing `param.min` or `param.max` has taken Live down: the formatting runs inside the plugin's own native code, so the crash is not a Python exception, no `try`/`except` in this project or in the Remote Script reaches it, and the process goes down with the unsaved set. Measured with a third-party sampler plugin; the Remote Script records it at the `METHOD_ALLOWLIST` entry for `DeviceParameter.str_for_value`.

The endpoints are also not worth the risk, because a value just inside them answers the same question. `min + (max - min) * 0.01` and `* 0.99` establish whether the device reports units at all, and neither has crashed anything here. `set_parameter_display` samples strictly inside the range for this reason. A caller that deliberately wants the exact endpoint reading is calibrating a device it already trusts, and is taking that risk knowingly.

### Normalized value vs. physical display value (Live)
In Live, internal parameter values (`value`) often use a normalized scale (0.0 to 1.0) or custom integer ranges, while the user interface displays physical units (`display`, such as Hz or dB) via non-linear mapping curves:

| Device | Parameter | Internal `value` | Range `min`..`max` | Display `display` |
|---|---|---|---|---|
| Utility | Bass Freq | 0.3802 | 0.0 .. 1.0 | 120 Hz |
| Utility | Output | -0.171875 | -1.0 .. 1.0 | -6.02 dB |
| Max device | LPF | 8794.30 | 20 .. 21000 | 288 Hz |

All three measured 2026-08-31 against Live 12.4.5. Note that `min` and `max` bound `value`, not `display`. On Live's own devices the value is usually normalized, and on a Max device it is whatever range the device defines, so the LPF above is out by a factor of thirty if read as hertz.

Set values in the internal range and read `display` back to confirm what was stored.

---

## 4. Protocol limits: ours, and therefore fixable

### Non-transactional batch operations
`lom_batch` executes operations sequentially. The LOM does not support atomic database-style rollbacks. When `atomic: true` is passed, execution stops at the first error, but previously executed steps remain in effect.

### Write timeouts and retry policy
Write operations that encounter socket timeouts are not retried automatically, because a timed-out write may have succeeded in Live. Repeating the write could result in duplicate actions (such as duplicate notes). Clients should verify state using a read operation following a timeout.

### Round-trip cost
One round trip costs about 450 ms (measured 2026-08-29 against Live 12.4.5). That is the reason `lom_batch` exists: a twenty-track mixer pass is twenty round trips without it and one with it.

### Strictly serial transport
The socket protocol processes one request at a time per connection without multiplexing. Multi-operation tasks should use `lom_batch` to execute within a single round trip.

---

## 5. Open questions: unverified, and how to settle them

### Warp markers
Warp markers can be inspected, moved, and removed via the LOM, but cannot be dynamically created from the client. Only the last of those is still an open question. The rest of this entry is settled and stays here because the two belong together.

Measured 2026-09-05 against Live 12.4.5 on a scratch audio track holding a copy of a warped 8-beat sample. That clip is the one every figure below refers to.

- `clip.warp_markers` reads, and answers a list of **opaque handles**:
  `{"__lom__": "WarpMarker", "path": "...warp_markers[0]"}`, carrying no times at all. The
  times sit on the marker itself. `lom_describe` on one reports exactly two properties,
  `beat_time` and `sample_time`, both read-only. The clip above answered beats 0.0, 8.0 and
  8.03125 against sample times 0.0, 3.8709583 and 3.8860793 seconds. Rows
  `clip.warp_marker_beat_time` and `clip.warp_marker_sample_time` exist for this, with
  arrangement twins, because a method that identifies a marker by beat time cannot be
  driven from a list that does not carry one.
- `Clip.remove_warp_marker(beat_time)` works, and has two traps. The highest marker is
  Live's **shadow marker** and refuses deletion outright ("The shadow marker can't be
  deleted."). Worse, aimed at the last real marker it answers **success and removes
  nothing**: the count went 3 to 2 on the first removal and 2 to 2 on the second, both
  reported as success. Do not read a success here as a removal. The `warp_state` verify
  hook compares the count for exactly this reason.
- `Clip.move_warp_marker(marker_beat_time, beat_time_distance)` works and leaves the count
  unchanged. Since the marker list carries no times, confirming that a marker *moved*
  means reading `clip.warp_marker_beat_time` per index before and after.
- `Clip.add_warp_marker` remains unreachable, and this is the part still without a route.
  It takes a `WarpMarker` object that has to be constructed inside Live. Live answers
  `type_error`, "No registered converter was able to produce a C++ rvalue". The
  `{"__path__": ...}` form resolves an existing object and cannot build a new one. So warp
  markers can be read, moved and (mostly) removed from here, and not created.

### Concurrent Control Surfaces
Live holds several Control Surfaces at once, and Maestro listens on port 9878 by default so it does not collide with another socket-based script. Whether Live is content with two Control Surfaces each running a socket server of its own is unverified.

To settle it: install a second socket-based Remote Script on a different port, start Live, and check that both answer a liveness request. Until that is done, the fallback if it does not hold is to switch between the two scripts, at the cost of one Live restart per switch.

### Cost of introspection and of listeners
Listeners registered through `events_observe` run inside Live's own process, which carries a real-time audio thread. The cost of many simultaneous registrations has not been measured, and neither has the cost of a deep `lom_describe` over a large set.

Properties that fire on every buffer are the ones to watch: `clip.playing_position` and `clip.is_playing` are observable, and a listener on either across many clips means a callback storm rather than an occasional event. Because the cost is unmeasured rather than known to be safe, `events_observe` reports the registration count back to the caller and bounds its event buffer instead of assuming either is free.

To settle it: register listeners in increasing numbers on `clip.playing_position` across a set during playback and watch Live's CPU meter and audio dropout counter. Until that is done, treat a high listener count as an unknown risk and prefer polling a property when a poll will do.

---

## 6. What a success result actually proves

A success response confirms that the command was executed by the Remote Script. For `lom_set`, it confirms that the value is stored in memory.

It does not guarantee:
- That the audio output is audibly altered (e.g. if the device is disabled or the track is muted).
- That the project has been saved to disk.
- That the change will not be overwritten by subsequent user actions or undo commands.

---

## 7. Properties that do not exist, and are therefore not in the catalog

The following properties do not exist in the Live Object Model and return `no_such_path`:

| Property | Note |
|---|---|
| `master.arm`, `return.arm` | Main and Return tracks do not support arming or monitoring modes. |
| `clip.follow_action_*` | Follow actions are managed via GUI controls and are not exposed in the LOM. |
| `device.presets` | Device presets are loaded through the Browser rather than directly on the Device object. |
| `sampler.sample`, `sampler.playback_mode` | These properties belong to `Simpler` devices. `MultiSampler` (Sampler) exposes only the common `Device` interface. |
| `song.average_process_usage`, `song.peak_process_usage` | Measured 2026-09-07 against Live 12.4.5: both answer `no_such_path` on `Song`. Live's CPU meters are not in the object model, so the cost of a device cannot be read from here and has to be judged in Live's own display. |
| `RackDevice.rename_macro`, `RackDevice.macro_map` | Measured 2026-09-07 against Live 12.4.5 with `lom_describe` on a Drum Rack and on an Instrument Rack, both of which answer class `RackDevice`. Neither method is there. What the class does offer is `add_macro`, `remove_macro`, `randomize_macros`, `store_variation`, `recall_selected_variation`, `recall_last_used_variation` and `delete_selected_variation`. A macro is renamed through the GUI, and a mapping is made there too. |

One property is often expected on this list and does not belong on it. `device.view.is_showing_chain_devices` exists: read verified 2026-08-30 against Live 12.4.5 at `song.tracks[5].devices[0].view.is_showing_chain_devices` through `lom_describe`, which returned `True`. The catalog carries the row `device.is_showing_chain_devices`, and a later run exercised both directions, so it reads `proven: [get, set]`.

---

## 8. Preconditions are part of the question

Object Model attributes depend on the concrete object class and state:
- `choke_group` is specific to `DrumChain` (not standard `Chain`).
- Warping properties apply only to `AudioClip` instances (not `MidiClip`). Measured
  2026-09-05 against Live 12.4.5: reading `clip.warp_markers` on a MIDI clip answers
  `live_error` "Warp markers are only available for Audio Clips", and calling
  `remove_warp_marker` on one answers "Warping is only available for audio clips".
- Drum pads are present only on `DrumGroupDevice` instances.

### `clip.length` reports the loop, not the material
On a looping clip, `length` is the length of the loop brace, not of the material behind
it. Measured 2026-09-05 against Live 12.4.5 on a scratch MIDI track: setting `loop_end`
from 8.0 to 4.0 on an 8-beat clip moved `length` to 4.0 with every note still in place,
and cropping that clip left `length` at 4.0 on both sides while discarding half the notes
(8 to 4). `duplicate_loop` does move it, 4.0 to 8.0.

So `length` is the wrong evidence for a crop and the right evidence for a duplicate. What
a crop discarded shows up in the note count instead. The `clip_length` verify hook expects
only "did not grow" from a crop for this reason (docs/architecture.md, verify hooks).

### An optional argument omitted is not an optional argument passed as null
The LOM is a C++ binding and each method parameter carries its own default, which applies
only to an argument that was genuinely not passed. Sending an explicit null in its place
is a different call and can be refused.

Measured 2026-09-05 against Live 12.4.5: `clip.duplicate_notes_by_id` declares
`note_ids` plus two optional arguments, and calling it with `note_ids` alone while padding
the other two with null was refused with "Python argument types in
`Clip.duplicate_notes_by_id(Clip, list, NoneType, NoneType)` did not match C++ signature:
... `destination_time=None`, `int transposition_amount=0`". `destination_time` accepts null
and `transposition_amount` does not, so no single padding value is right for both. The
executor therefore stops at the first trailing optional argument the caller omitted and the
row gives no default for, and hands Live the shorter call.

### Track index renumbering
Live references tracks by zero-based positional index. Inserting or deleting a track shifts the indices of subsequent tracks. Always re-survey session track lists (`get_session`) after adding or removing tracks.

---

## 9. Where the corpus figures come from

Two different kinds of evidence appear in this repository under the same word.

A **LOM measurement** is a claim about Live, dated against a Live version, and anyone with that version can re-run it: open a set, read the path, watch what happens. Most of what is written here is that kind.

A **corpus figure** is a claim about how people's projects tend to be built: that 52 of 174 projects carry clip envelopes while 159 carry track automation, that 472 of the 648 tracks with a constant velocity sit at 100. These come from reading a private collection of Live project files off disk. That material is commercially licensed to the author, is not redistributable, and is not part of this repository. No file, name or path from it appears here, and none should.

The three collections behind the figures in this repository:

| Corpus | Shape | What it is quoted for |
|---|---|---|
| Project corpus | 174 projects | Clip envelopes against track automation, sidechain wiring, gain factors, meter usage |
| Note corpus | 103 projects, 833 tracks, 73,539 notes | Velocity distributions, note density, grid usage |
| Query corpus | producer-language queries, labelled for the correct mechanism, split in half for tuning and scoring | `find_path` route accuracy and first-hit accuracy |

**Method.** Raw counts are per occurrence. A distribution is not: an arrangement repeat and a layered twin track are the same phrase counted twice, and one clip copied 24 times would otherwise weigh 24 times. Distributions are therefore de-duplicated by note-content fingerprint and taken as a median over clips. `als/read.py` prints the same caution on the notes report, in `format_notes_report`.

**What a corpus figure is worth.** It is context, not a check. It cannot be re-run from this repository, so it is the one class of number here that a reader has to take on trust, and it is recorded as such rather than left to look like a LOM measurement. Where a decision turns on one, prefer a reading from the open set.
