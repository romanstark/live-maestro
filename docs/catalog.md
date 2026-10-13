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
    "-2": "no clip is playing on this track."
```

When a property value matches an entry in `means`, the server attaches the explanation to the result. It reaches raw paths too: `lom_get`, `lom_set` and `lom_batch` look the row up with `registry.rows_for_path()`.

---

## Status and verification discipline

Catalog provenance is recorded in `measurements/catalog-live-12.4.5.json` and
`measurements/native-workflows-live-12.4.6.json`. Read each row's operation and
Live version rather than treating one report as coverage of every version.
The reports identify the Remote Script build and the evidence for each verified
operation.

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

Session clips can be duplicated to Arrangement with `arrange`. Direct file import
and MIDI editing use `import_arrangement_audio` and `write_arrangement_notes`.
These specialized workflows have their own validation and native read-back.

Generic `lom_call` cannot construct Live note or WarpMarker value objects.
A method can therefore be inaccessible through that generic route while a typed
workflow supports it. Clip-envelope writing remains a Session workflow.

`lom_set` and `lom_batch` refuse arbitrary Arrangement `end_marker` writes.
Read-back of a stored marker does not prove that Arrangement geometry changed.
The specialized audio-import workflow resets unwarped markers in seconds and
checks duration. See [Arrangement timing](timing-workflows.md).

## Operational constraints

- `song.back_to_arranger=False` is applied asynchronously. Re-read after a
  `not_observed` response; writing true is not a verified way to restore Session control.
- `song.current_song_time` writes are bounded by `song_length`.
- Use note IDs from the target clip; cross-clip ID behavior is unverified.
- `write_automation` interpolates into discrete envelope steps and explicitly
  extends the final value to the clip end.

See [operational limits](limits.md) for state, transport and GUI boundaries.

### Sparse automation layers

`write_automation` builds interpolation from `Envelope.insert_step` calls and
explicitly extends the final value to the clip end. Those are writer behaviors.
Interpolation inside gaps between arbitrary raw steps remains unmeasured.

## Evidence for capability claims

A catalog row describes the operation actually verified. A stored value, a native
method call and an audible or structural effect are different claims.

Record the operation, date, Live version and discriminating read-back in the row's
measurement provenance. Use a suitable object and state: an absent clip or a wrong
device class does not prove that a member is missing. Attribute documentation-only
claims to their source and leave unmeasured behavior untested.

The catalog checks enforce measurement provenance and prevent unsupported
impossibility claims from acquiring a verified status. Detailed raw readings belong
in the measurement reports; user documentation describes current behavior.

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
