# The Path Catalog

The catalog defines Live's fixed Object Model surface. It is maintained as YAML files under `src/live_maestro/catalog/*.yaml` and loaded at runtime by `registry.py`.

Adding support for a Live property is done by adding a row to this catalog, without modifying the Remote Script inside Live. A method row is not the same case: `access: [call]` says what the server will offer, and `METHOD_ALLOWLIST` inside the Remote Script says what the script will do. A method that is not on that list answers `method_not_allowed` however the row is written, and widening the list means editing the script and restarting Live (see [protocol.md §6](protocol.md#6-the-resolver-and-why-calls-need-an-allowlist)).

---

## Where the rows come from

Because Ableton does not publish a machine-readable specification of the Live Object Model (LOM), catalog entries were initially created from the official LOM documentation and subsequently verified against active Live instances using introspection and probing scripts.

- `scripts/sync_catalog.py` compares Live's runtime objects (via `lom_describe`) against catalog rows to discover uncatalogued properties and flag non-existent paths.
- `scripts/probe_paths.py` executes read and write operations against an active Live set to confirm behavior.

### Handling Non-Existent Properties

If a property does not exist in the Object Model (`no_such_path`), its row is removed from the catalog rather than marked as `broken`. The catalog represents an accurate inventory of real paths. Any non-existent properties that developers might expect to find are documented in [limits.md §7](limits.md#7-properties-that-do-not-exist-and-are-therefore-not-in-the-catalog).

The `broken` status is reserved for paths that exist in Live's API and are still not reachable through this system: Live raises on access (`song.master_track.mute`), the Remote Script's allowlist refuses the call by design (`song.begin_undo_step`), or `lom_call` cannot build the Live value object the method takes (`clip.add_new_notes`).

---

## Why a directory and not one file

The catalog is split into multiple files by domain:

| File | Domain |
|---|---|
| `10-song.yaml` | Song, Application, transport, scenes, and cue points |
| `20-track.yaml` | Track, MixerDevice, returns, and master |
| `30-clip.yaml` | ClipSlot, Session and Arrangement clips, MIDI notes, and clip envelopes |
| `40-device.yaml` | Device, DeviceParameter, Racks, chains, and drum pads |
| `50-browser.yaml` | Browser navigation, items, and Groove Pool |

IDs must be unique across all files. The loader raises an error if duplicate IDs are detected.

---

## Row schema

```yaml
- id: track.volume
  path: song.tracks[{track}].mixer_device.volume
  access: [get, observe, automate]
  kind: float
  range: [0.0, 1.0]
  unit: normalized
  display: db
  quantized: false
  verify: read_back
  destructive: false
  status: verified
  proven: [get]
  doc: >
    Track volume parameter. Values are normalized between 0.0 and 1.0 (0.85 = 0 dB). Use display for human-readable decibel output.
  params:
    - {name: track, kind: int, required: true}
```

### Field Definitions

| Field | Required | Description |
|---|:---:|---|
| `id` | Yes | Unique identifier in the format `domain.name`. |
| `path` | Yes | LOM path template with placeholders (e.g. `song.tracks[{track}]`). |
| `access` | Yes | List of supported operations: `get`, `set`, `call`, `observe`, `automate`. |
| `kind` | No | Data type: `bool`, `int`, `float`, `str`, `enum`, `list`, `object`. Defaults to `object`. |
| `range` | No | List `[min, max]` for numeric validation before sending across socket. |
| `enum` | No | Allowed values for enum types. |
| `unit` | No | Unit type: `normalized`, `db`, `hz`, `semitones`, `beats`, `seconds`, `percent`, `none`. |
| `display` | No | Expected display unit format from Live (via `str_for_value()`). |
| `quantized` | No | Boolean indicating whether parameter uses discrete steps rather than a continuous range. |
| `method` | No | Method name to execute (used only with `access: [call]`). |
| `verify` | No | Verification hook name (defaults to `read_back`). Must resolve to a verifier in `executor.py`, or be `none` to state that there is no post-condition. `tests/test_catalog.py` fails otherwise. See [architecture.md](architecture.md). |
| `destructive` | No | Boolean. If true, executor requires `confirm=True` to run. |
| `status` | Yes | Validation state: `verified`, `broken`, or `untested`. |
| `proven` | No | Access kinds a probe run exercised against a running Live. Always a subset of `access`, never `observe` or `automate`, written by `scripts/probe_paths.py` and checked against the one report in `measurements/`. Empty means no run has exercised the row. |
| `doc` | Yes | Documentation string explaining parameter behavior, scale, and constraints. |
| `means` | No | Mapping of specific values to their semantic meaning. A `means` entry is a claim about Live, so `tests/test_catalog.py` pins the exact set of rows carrying one (`ROWS_WITH_MEANS`) and requires a meaning taken from the LOM reference rather than from a measurement to say so. |
| `params` | No | Parameter definitions for path placeholders. |
| `args` | No | Argument definitions for method calls (`access: [call]` only). |

---

### `params` and `args`

`params` and `args` serve distinct roles in the schema:

| Feature | `params` | `args` |
|---|---|---|
| **Role** | Substitutes `{placeholder}` in `path` | Passed as arguments to a method |
| **Appears in** | Path template string | Wire payload for method execution |
| **Allowed on** | Any row with `{placeholder}` | Rows with `access: [call]` only |

Example method row:

```yaml
- id: track.delete_device
  path: song.tracks[{track}]
  access: [call]
  method: delete_device
  destructive: true
  status: verified
  proven: [call]
  doc: >
    Removes a device from a track by chain index. Deleting shifts the index of every device after it.
  params:
    - {name: track, kind: int, required: true}
  args:
    - {name: index, kind: int, required: true, doc: "Zero-based position in the device chain."}
```

### `args` and method call signatures

A row with `access: [call]` and `args: []` declares that the method takes no arguments. Where a Live method requires parameters (such as an index, string, or value), those parameters must be explicitly declared in `args`:

- `name`: Argument identifier.
- `kind`: Data type (`int`, `float`, `str`, `bool`, etc.).
- `required`: Whether the argument is required by Live's method signature.
- `doc`: Description of the argument and valid values.

When `scripts/sync_catalog.py` discovers new methods via runtime inspection, it cannot infer signatures automatically because Live does not publish method signatures over the LOM. Unmeasured methods default to empty argument lists until verified and measured against an active session. Calling a method without required arguments results in a `type_error`. Probing these rows with `scripts/probe_paths.py` reports them as inconclusive rather than broken when candidate arguments are needed.

---

### `doc` Guidelines

Documentation in `doc` reaches AI clients through the `find_path` tool and the `live://catalog/{selector}` resource. The `live://catalog` index carries no rows and therefore no `doc` text. Clear documentation helps avoid incorrect assumptions about parameters.

Effective documentation should note:
1. Physical unit interpretations (e.g., whether `0.5` represents normalized gain or frequency).
2. Known constraints, asynchronous behavior, or snapping.
3. Read-only limitations that are not obvious from the property name.
4. Version differences between Live 11 and Live 12.

---

### `means`: Value decoding

Some Live properties return numeric enums or sentinel values without descriptive labels. The `means` map provides human-readable explanations for specific return values:

```yaml
- id: track.playing_slot_index
  means:
    "-1": "nothing is playing here. Correct and not a failure: with clip_trigger_quantization set to a bar, a read straight after firing still says -1"
    "-2": "no clip is playing on this track. Measured 2026-09-16 against Live 12.4.5: the tracks reading -2 were exactly those holding no clip in the fired scene, and the reading was unchanged as the scene settled and again after a stop"
```

When a property value matches an entry in `means`, the server attaches the explanation to the result. It reaches raw paths too: `lom_get`, `lom_set` and `lom_batch` look the row up with `registry.rows_for_path()`.

---

## Status and verification discipline

Every reading recorded in this catalog was taken against **Ableton Live 12.4.5** on Windows 11. The Remote Script build it ran under is in `measurements/catalog-live-12.4.5.json`, where it is a recorded field rather than a sentence that has to be kept up to date. That sentence is here, once, instead of in 1166 rows. A measurement against another build belongs in the row that was measured, because then the build is the thing worth saying.

Every new row starts with `status: untested`.

### `proven`: what a run actually exercised

While `status` indicates the general support state (`verified`, `broken`, `untested`), the `proven` field records the specific access operations that were successfully exercised and read back by a live probe run:

```yaml
- id: song.tempo
  access: [get, set, observe]
  proven: [get, set]
  status: verified
```

`scripts/probe_paths.py` automatically updates `proven` during probe runs. Key rules:

- `proven` is always a subset of `access`, checked at load time in `spec.py`.
- `observe` and `automate` never appear in `proven` because probes do not subscribe to listeners or generate envelopes; read them in `access` as available capabilities.
- An empty `proven` list indicates that no probe run has exercised this row yet (e.g. if the target was absent in the probed test set, which `docs/limits.md` documents).

Lifecycle status definitions:

- `status: verified`: Confirmed working against an active Live instance.
- `status: broken`: The row names something real that this system cannot reach. Three causes produce it, and the `doc` says which: Live raises or refuses on access (`master.mute`), the Remote Script's `METHOD_ALLOWLIST` refuses the call by design (`song.begin_undo_step`, `device.store_chosen_bank`), or the method is allowlisted and takes a Live value object that `lom_call` cannot build out of JSON (`clip.add_new_notes`).
- `status: untested`: Documented in the catalog, but not yet verified with a live probe.

### Four outcomes, not two

When probing a path with `scripts/probe_paths.py`, the result falls into one of several categories based on Live's response:

| Response | Meaning | Action |
|---|---|---|
| Valid value returned | Property functions as expected | Set `status: verified` and record what was exercised in `proven`. |
| `no_such_path` | Property does not exist on object | Remove the row from the catalog and document in `limits.md §7`. |
| `live_error` naming the target | The probe aimed at the wrong kind of object | Aim at a suitable object and probe again. Change nothing. |
| `live_error` on a suitable target | Property exists, but Live raises an error | Set `status: broken` with the error message in `doc`. |
| `index_out_of_range` | Target object was missing from test set | Keep `status: untested` until tested with an appropriate set. |
| `not_settable` | Property is read-only | Remove `set` from `access` list. |

Distinguish between errors caused by an invalid target object versus properties that fail on a valid target. Live indicates the distinction directly in the error message:

- `Warping is only available for Audio Clips` is about the clip that was passed,
  not about `clip.warping`.
- `MaxDevice has no attribute 'chains'` is about the device that was passed,
  not about `rack.chains`.
- `Main track has no 'mute' property!` is about the row, because
  `master.mute` names the main track and no other target exists for it.

The test is whether another target could answer. Where one could, the probe
learned nothing about the row and must not write a status.

---

## The Session / Arrangement boundary

The Arrangement timeline is not writable, it can only be populated. All content originates in a Session clip and is duplicated. The Session slot is not an optional intermediate step, but the sole write target.

While Arrangement clips expose properties for inspection (`start_time`, `end_time`, `length`), content and envelopes cannot be written directly to them over the LOM protocol.

`lom_set` and `lom_batch` refuse a write to `end_marker` on an Arrangement clip. Behind that refusal is a single producer-session observation, 2026-09-10 against Live 12.4.5: the write was accepted, `end_time` did not move, and that same clip was one Live would not let loop. Whether the write is ineffective in general or only while the clip does not loop is unproven, and [limits.md](limits.md#5-open-questions-unverified-and-how-to-settle-them) section 5 carries the probe that would settle it. The refusal is kept because a write that reports success and changes nothing is worse than an error, and the row `arrangement_clip.end_marker` must not be read as a promise that the write lands: its `verified` status rests on a read-back that proved only that the value stores.

---

## Operational constraints

- **`song.back_to_arranger` is writable asynchronously**: Writing `false` hands playback control back to the Arrangement from Session view. Live applies it on its own main thread: Measured 2026-09-10 to 2026-09-13 against Live 12.4.5, the write read back as applied after 2 read-back attempts, so a read taken straight after it reports `not_observed` with the write still pending. Writing `true` afterwards answers `not_observed` and the value stays `false` (measured 2026-09-06 against Live 12.4.5): the write travels one way, and Live raises the flag itself when a Session clip is launched.
- **`song.current_song_time` write bounds**: Writing `current_song_time` is bounded by `song_length` (`"Cannot set the Songtime behind the Songlength"`), whereas Session playback advances freely past `song_length`.
- **Note ids across clips, untested**: pass `get_notes_by_id`, `remove_notes_by_id` and `duplicate_notes_by_id` only ids read from the clip being called. The one-clip rule was observed on the ACE Studio note API, a different product, and is not measured against Live, which has no `INVALID_ARG` code. See [limits.md](limits.md) for the probe that settles it.
- **Sparse automation layers**: Clip envelopes in the LOM are written through `Envelope.insert_step(time, length, value)`. There is no native continuous curve API; interpolation across sparse breakpoints is handled before writing, by `write_automation`. Raw sparse steps hold flat between points, and the final value reaches the end of the clip because `write_automation` puts it there: `build_automation_steps` in the Remote Script appends a closing step of length `clip_length` minus the last breakpoint time. What Live holds in the gap between two steps that do not touch is not measured; [limits.md](limits.md) carries the probe.

---

## Impossibility claims: the audit of 2026-09-16

A row that says something cannot be done is read as a statement about Live, and a caller who
believes it stops looking for a route. This section is the inherited list, so the next reader does
not repeat the grep.

**Method.** All five catalog files were swept for the vocabulary an impossibility is written in:
`cannot`, `not settable`, `not writable`, `has no effect`, `is ignored`, `travels one way`,
`not possible`. 140 of the 1166 rows carry one. 121 of those carry a date; 19 did not. The sweep
itself re-measured nothing against Live and changed no status. It is enforced from now on by
`test_every_impossibility_claim_says_where_it_came_from` and
`test_the_undated_impossibility_roster_only_shrinks` in `tests/test_catalog.py`, which hold the
roster. That roster opened at 19 and stands at 14: five rows were probed later the same day and
left it, as recorded below.

**Ten rows claimed an impossibility on nobody's authority and were corrected.** None was corrected
by inventing a measurement:

| Row | What it claimed | What it says now |
|---|---|---|
| `song.signature_denominator` | the validator cannot express "power of two" | a limit of this catalog's `range` field, not of Live, and what Live does with 3 is not measured |
| `cue.time` | a locator cannot be moved through the LOM | the access list comes from Ableton's published LOM documentation, no write is on record, and `lom_set` here would settle it |
| `track.has_midi_input` | writing notes into an audio track's clip is not possible | the note methods are MIDI-only in Ableton's documentation; what Live answers to such a write is not measured |
| `track.view_selected_device` | the write side "needs an object the wire cannot currently carry" | **the claim was wrong**: `view.select_device` takes the device by path and its call is verified on that row (2026-08-30 against Live 12.4.5) |
| `master.available_output_routing_types` | the list cannot be cached across sessions | machine-specific, and that the list survives an interface change is not measured, which is the reason to re-read it |
| `clip.color` | older Live versions snap, "which this installation cannot show" | adds that the snap is therefore not measured here |
| `clip.looping` | an unwarped audio clip cannot loop meaningfully | neither half is measured here, and names the write and read-back that settles the second |
| `master_device.parameter` | `lom_set` answers `not_settable` | cites the companion `_value` row, where it was measured 2026-08-29 against Live 12.4.5 |
| `chain.device_parameter` | `lom_set` answers `not_settable` | same citation |
| `simpler.sample_file_path` | a sample cannot be swapped by writing a path | taken from Ableton's published documentation rather than measured; the browser is the route |

**Five rows left the roster on 2026-09-16, by being probed.** They took the only legitimate exit:
each was offered the write through the raw `lom_set` tool, which does not consult a row's access
list, against Ableton's "Patience" demo song in a running Live 12.4.5. Every write that stored was
restored and the set was left as it was found. Three came back confirming what the row already
said; two came back contradicting it, and those two rows now say what Live answered instead.

| Row | What was written | What Live answered | What it changed |
|---|---|---|---|
| `cue.time` | `song.cue_points[0].time` | `not_settable` | nothing: claim confirmed, and it is now dated rather than attributed to the access list. Nothing stored, nothing to restore |
| `device.is_active` | `false` to `song.tracks[0].devices[1].is_active` | `not_settable` | nothing: claim confirmed. Nothing stored, nothing to restore |
| `clip_slot.color` | the colour of an empty slot, track 0 slot 3, whose read answers `None` | `not_settable` | nothing for an empty slot: claim confirmed there. An occupied slot was not probed and the row now says so. Nothing stored, nothing to restore |
| `clip.color_index` | `70`, then `-1` | `live_error` "Color index out of range." both times, value stayed 32 | the claim. The row expected a clamp past the palette size; Live raises at both ends, so an out-of-range index is an error to handle and not a value to read back. Nothing stored, nothing to restore |
| `song.signature_denominator` | `3`, re-read after 1.5 s; then `8` | 3: `ok` with `read_back: not_observed`, still 4 after 1.5 s. 8: `read_back: applied`, reads 8 | the claim. A denominator that is not a power of two is not rejected and not rounded: it is accepted and silently ineffective, which is the shape this whole specification exists to catch. `docs/limits.md` section 2 now carries it beside the other writes that report success and do nothing. Restored to 4 |

**The roster after 2026-09-19: three rows.** Ten of the fourteen were probed against Live 12.4.6
on an empty set, with the specimens built for the purpose: an Operator and an EQ Eight on a MIDI
track, a Reverb on a return and the same Reverb on an ordinary track, an Oxi Bass Rack for a
nested chain, a Simpler made by dropping a sample on a MIDI track, and a one-shot dropped on an
audio track. Nine claims held and now carry the date. One did not.

| Row | Probe | Live answered | Outcome |
|---|---|---|---|
| `param.value`, `eq8.frequency_hint`, `return_device.on`, `return_device.parameter`, `chain.device_on` | `lom_set` on the parameter object, then on its `.value` | object: `not_settable`. `.value`: `applied`, and `clamped` for the on/off one, which holds 0 or 1 | claim held. The object is the handle and the number is one level in |
| `track.is_grouped`, `track.group_track`, `track.has_midi_input` | `lom_set` on each | `not_settable`, with Live naming the attribute | claim held |
| `simpler.sample_file_path` | read, then `lom_set` with another path | read the file on disk; write `not_settable` | claim held, and it is now this project's reading rather than only Ableton's documentation |
| `clip.looping` | `warping` to false on an audio one-shot, then `looping` to true | `applied`, `changed`, reads back true | Live allows it, where the claim only expected that it would. The flag is not a guard, so a clip carrying it is no evidence that anything loops |
| `return.devices` | first four parameters of a Reverb on return A and of a Reverb on a track | the same `display_value` on both, `Predelay` reading 2.5 either way | **did not reproduce.** The claim carried no date and no version, so what it was true of is not known; it is not true of this one |

**The three that remain, and why.** None is a write that can be attempted.

- `song.tempo` is about writing tempo automation, not about writing tempo.
- `clip.color` is about Live 10 and earlier snapping to a 70-entry palette, which an installation
  running 12.4.6 cannot show.
- `chain.send` is advice about planning send levels in dB rather than in 0 to 1. It is not an
  impossibility claim at all, and the check matches it on wording.

**Known weakness of the check.** It reads a whole `doc`, so a row passes when the word "measured"
belongs to a different sentence than the impossibility. Tightening it to the sentence is the next
pass, not this one.

The probes are the ordinary ones: `lom_set` on the path with its read-back, through
`scripts/probe_paths.py`, against a throwaway set.

One probe that day settled nothing and is written up nowhere as a result. The claim that
Arrangement track automation is unreachable rests on `clip.automation_envelope(parameter)`
answering `None` on an Arrangement clip, and on 2026-09-16 a Session clip in the same set answered
`None` for the same parameter as well. `None` therefore reads equally as "no envelope exists for
that parameter yet" and as "envelopes are unreachable on this clip", so that row stays attributed
to the Live Object Model rather than measured. What would discriminate is a Session clip that
already carries an envelope for the parameter being asked about, compared against an Arrangement
copy of it; `arrangement_clip.automation_envelope` now names that probe instead of the one that
does not separate the two readings.

---

## What the catalog deliberately does not cover

The catalog covers static properties in Live's core Object Model.

Dynamic properties (such as third-party VST/AU plugin parameters) vary by user session and are only populated when configured in Live's device strip. These dynamic parameters are discovered at runtime via `lom_describe` and `lom/introspect.py`.

---

## Adding a new row

1. Identify the target LOM property (use `python scripts/sync_catalog.py --report` to check for uncatalogued properties in an open set).
2. Add the entry to the relevant YAML file under `src/live_maestro/catalog/` with `status: untested` and an informative `doc` string.
3. Run `pytest` to validate catalog syntax and schema integrity.
4. Run `python scripts/probe_paths.py --id <new.row.id> --go` against an open test set to verify behavior.
5. Update `status` to `verified` once confirmed, and append the measurement that earned it: what was done, the date, and the Live version. `tests/test_catalog.py` fails a `verified` row whose `doc` records no measurement, and fails an `untested` row whose `doc` records one.
